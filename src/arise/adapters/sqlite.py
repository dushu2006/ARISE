"""Initial local SQLite adapters for durable task and event state.

Domain code depends only on repository/store protocols. The database is local,
uses short WAL transactions, and stores no raw action parameters or secrets.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arise.adapters.process_lock import DatabaseInstanceLock
from arise.core.contracts import utc_now
from arise.core.errors import DatabaseError
from arise.core.events import DuplicateEventError, EventEnvelope, EventSeverity
from arise.core.extensions import (
    ContextQuery,
    ContextSource,
    EmbeddingPort,
    EmbeddingResult,
    MemoryConsentError,
    MemoryConsentPort,
    MemoryEntry,
    MemoryKind,
    MemoryPort,
    MemoryRecord,
    RetrievedContext,
    memory_entry_fingerprint,
    require_memory_write_consent,
)
from arise.core.models import ConversationTurn, Session
from arise.core.redaction import DEFAULT_REDACTOR, SecretRedactor
from arise.core.storage import SessionRepository
from arise.core.tasks import (
    ConcurrentTaskUpdateError,
    DuplicateTaskRequestError,
    TaskRecord,
    TaskRepository,
    TaskStatus,
)


class UnsupportedDatabaseVersion(RuntimeError):
    pass


class SQLiteDatabase:
    """Shared SQLite connection with optional process ownership and atomic migrations."""

    CURRENT_SCHEMA_VERSION = 9

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        acquire_instance_lock: bool = False,
    ) -> None:
        if not 100 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 100 and 60000")
        raw_path = str(path)
        if raw_path != ":memory:":
            resolved = Path(path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            raw_path = str(resolved)
        self.path = raw_path
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._closed = False
        self._instance_lock: DatabaseInstanceLock | None = None
        if acquire_instance_lock and raw_path != ":memory:":
            instance_lock = DatabaseInstanceLock(raw_path)
            instance_lock.acquire()
            self._instance_lock = instance_lock
        try:
            self._connection = sqlite3.connect(
                raw_path,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            if raw_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")
            self._migrate()
        except sqlite3.Error as exc:
            try:
                self._close_failed_connection()
            finally:
                self._release_instance_lock()
            raise self._database_error("initialize", exc) from exc
        except BaseException:
            try:
                self._close_failed_connection()
            finally:
                self._release_instance_lock()
            raise

    def _close_failed_connection(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def _release_instance_lock(self) -> None:
        instance_lock, self._instance_lock = self._instance_lock, None
        if instance_lock is not None:
            instance_lock.release()

    @staticmethod
    def _database_error(operation: str, exc: sqlite3.Error) -> DatabaseError:
        message = str(exc).lower()
        retryable = isinstance(exc, sqlite3.OperationalError) and (
            "locked" in message or "busy" in message
        )
        return DatabaseError(
            "The local database operation failed.",
            component="sqlite-adapter",
            operation=operation,
            retryable=retryable,
            metadata={"driver_error": type(exc).__name__},
        )

    def _migrate(self) -> None:
        with self._lock:
            try:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                rows = self._connection.execute("SELECT version FROM schema_migrations").fetchall()
                applied = {int(row[0]) for row in rows}
                if applied and max(applied) > self.CURRENT_SCHEMA_VERSION:
                    raise UnsupportedDatabaseVersion(
                        f"database schema {max(applied)} is newer than this runtime"
                    )
            except sqlite3.Error as exc:
                raise self._database_error("inspect-schema", exc) from exc

            migrations = {
                1: self._migration_v1,
                2: self._migration_v2,
                3: self._migration_v3,
                4: self._migration_v4,
                5: self._migration_v5,
                6: self._migration_v6,
                7: self._migration_v7,
                8: self._migration_v8,
                9: self._migration_v9,
            }
            for version, migrate in migrations.items():
                try:
                    with self.transaction() as connection:
                        exists = connection.execute(
                            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                        ).fetchone()
                        if exists is not None:
                            continue
                        migrate(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, utc_now().isoformat()),
                        )
                except DatabaseError:
                    raise
                except sqlite3.Error as exc:
                    raise self._database_error(f"migrate-v{version}", exc) from exc

    @staticmethod
    def _migration_v1(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, schema_version INTEGER NOT NULL, "
            "version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, "
            "event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
            "monotonic_timestamp_ns INTEGER NOT NULL, "
            "task_id TEXT, step_id TEXT, parent_event_id TEXT, session_id TEXT, "
            "source TEXT NOT NULL, "
            "severity TEXT NOT NULL, payload_json TEXT NOT NULL, runtime_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_task_sequence ON events(task_id, sequence)"
        )

    @staticmethod
    def _migration_v2(connection: sqlite3.Connection) -> None:
        event_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "correlation_id" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN correlation_id TEXT")
        if "causation_id" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN causation_id TEXT")

    @staticmethod
    def _migration_v3(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, locale TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS conversation_turns ("
            "turn_id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE, "
            "speaker TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, "
            "task_id TEXT, metadata_json TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_session_created "
            "ON conversation_turns(session_id, created_at)"
        )

    @staticmethod
    def _migration_v4(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS task_requests ("
            "principal_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE, "
            "PRIMARY KEY(principal_id, session_id, request_id))"
        )
        rows = connection.execute("SELECT task_id, payload_json FROM tasks").fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            authorization = payload.get("authorization")
            principal_id = (
                authorization.get("principal_id") if isinstance(authorization, dict) else None
            )
            session_id = payload.get("session_id")
            request_id = payload.get("request_id")
            if all(
                isinstance(value, str) and value for value in (principal_id, session_id, request_id)
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO task_requests "
                    "(principal_id, session_id, request_id, task_id) "
                    "VALUES (?, ?, ?, ?)",
                    (principal_id, session_id, request_id, row["task_id"]),
                )

    @staticmethod
    def _migration_v5(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(task_requests)").fetchall()
        }
        if "request_fingerprint" not in columns:
            connection.execute("ALTER TABLE task_requests ADD COLUMN request_fingerprint TEXT")

    @staticmethod
    def _migration_v6(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_records ("
            "record_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "text TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, source_task_id TEXT)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_principal_expiry_created "
            "ON memory_records(principal_id, expires_at, created_at DESC)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_consents ("
            "consent_hash TEXT PRIMARY KEY, principal_id TEXT NOT NULL, "
            "entry_fingerprint TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_consent_principal_expiry "
            "ON memory_consents(principal_id, expires_at)"
        )

    @staticmethod
    def _migration_v7(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(memory_records)").fetchall()
        }
        if "embedding_json" not in columns:
            connection.execute("ALTER TABLE memory_records ADD COLUMN embedding_json TEXT")
        if "embedding_model_id" not in columns:
            connection.execute("ALTER TABLE memory_records ADD COLUMN embedding_model_id TEXT")

    @staticmethod
    def _migration_v8(connection: sqlite3.Connection) -> None:
        """Preserve request-idempotency tombstones when a user clears task history."""

        connection.execute(
            "CREATE TABLE IF NOT EXISTS task_request_tombstones ("
            "principal_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "request_fingerprint TEXT, deleted_at TEXT NOT NULL, "
            "PRIMARY KEY(principal_id, session_id, request_id))"
        )

    @staticmethod
    def _migration_v9(connection: sqlite3.Connection) -> None:
        """Persist the event sequence floor when user-controlled history is deleted."""

        connection.execute(
            "CREATE TABLE IF NOT EXISTS event_replay_state ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "replay_floor INTEGER NOT NULL CHECK (replay_floor >= 0))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO event_replay_state(singleton, replay_floor) VALUES (1, 0)"
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise self._database_error("begin-transaction", exc) from exc
            try:
                yield self._connection
            except BaseException as exc:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise self._database_error("transaction", exc) from exc
                raise
            else:
                try:
                    self._connection.commit()
                except sqlite3.Error as exc:
                    try:
                        self._connection.rollback()
                    except sqlite3.Error:
                        pass
                    raise self._database_error("commit-transaction", exc) from exc

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Serialize a read or a small set of statements on the shared connection."""

        with self._lock:
            try:
                yield self._connection
            except sqlite3.Error as exc:
                raise self._database_error("query", exc) from exc

    def backup_to(self, destination: str | Path) -> Path:
        """Create an atomic, no-overwrite SQLite snapshot with private POSIX permissions."""

        destination_path = Path(destination).expanduser()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path = destination_path.resolve()
        source_path = Path(self.path).resolve() if self.path != ":memory:" else None
        if source_path is not None and destination_path == source_path:
            raise ValueError("backup destination cannot be the live database")
        if destination_path.exists():
            raise FileExistsError("backup destination already exists")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".partial",
            dir=destination_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            if os.name != "nt":
                temporary_path.chmod(0o600)
            target = sqlite3.connect(temporary_path)
            try:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("cannot back up a closed database")
                    self._connection.backup(target, pages=256, sleep=0.01)
                integrity = target.execute("PRAGMA quick_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("database backup failed its integrity check")
                target.commit()
            finally:
                target.close()
            with temporary_path.open("rb+") as stream:
                os.fsync(stream.fileno())
            # A same-directory hard link publishes the completed snapshot atomically and
            # fails rather than replacing a path the user already owns.
            os.link(temporary_path, destination_path)
            temporary_path.unlink()
            if os.name != "nt":
                directory_fd = os.open(destination_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return destination_path
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def health_check(self) -> bool:
        with self.locked() as connection:
            return connection.execute("SELECT 1").fetchone() is not None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise self._database_error("close", exc) from exc
            self._closed = True
            self._release_instance_lock()


class SQLiteTaskRepository(TaskRepository):
    _HISTORY_PURGEABLE = (
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
    )
    _RECOVERY_IGNORED = (
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.FAILED.value,
        TaskStatus.UNKNOWN.value,
        TaskStatus.INTERRUPTED.value,
        TaskStatus.BLOCKED.value,
    )

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @staticmethod
    def _request_key(task: TaskRecord) -> tuple[str, str, str] | None:
        principal_id = task.authorization.principal_id if task.authorization is not None else None
        if principal_id is None:
            return None
        return principal_id, task.session_id, task.request_id

    @staticmethod
    def _decode_task(payload_json: str) -> TaskRecord:
        try:
            payload = json.loads(payload_json)
            return TaskRecord.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(
                "A persisted task record is invalid.",
                component="sqlite-task-repository",
                operation="decode-task",
                metadata={"record_kind": "task"},
            ) from exc

    @staticmethod
    def _write_new(
        connection: sqlite3.Connection,
        task: TaskRecord,
        request_fingerprint: str | None = None,
    ) -> None:
        if task.version != 0:
            raise ConcurrentTaskUpdateError("task does not exist at the supplied version")
        key = SQLiteTaskRepository._request_key(task)
        if key is not None:
            tombstone = connection.execute(
                "SELECT 1 FROM task_request_tombstones "
                "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
                key,
            ).fetchone()
            if tombstone is not None:
                raise DuplicateTaskRequestError(
                    "request ID belongs to cleared history; submit with a new request ID"
                )
        task.version = 1
        task.updated_at = utc_now()
        payload = json.dumps(task.to_dict(), sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO tasks(task_id, status, schema_version, version, created_at, updated_at, "
            "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.status.value,
                task.schema_version,
                task.version,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                payload,
            ),
        )
        key = SQLiteTaskRepository._request_key(task)
        if key is not None:
            connection.execute(
                "INSERT INTO task_requests("
                "principal_id, session_id, request_id, task_id, request_fingerprint) "
                "VALUES (?, ?, ?, ?, ?)",
                (*key, task.task_id, request_fingerprint),
            )

    def create_or_get(
        self, task: TaskRecord, *, request_fingerprint: str | None = None
    ) -> tuple[TaskRecord, bool]:
        if task.version != 0:
            raise ConcurrentTaskUpdateError("new task must start at version zero")
        previous_version = task.version
        previous_updated_at = task.updated_at
        key = self._request_key(task)
        try:
            with self.database.transaction() as connection:
                if key is not None:
                    row = connection.execute(
                        "SELECT tasks.payload_json, task_requests.request_fingerprint "
                        "FROM task_requests JOIN tasks USING (task_id) "
                        "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
                        key,
                    ).fetchone()
                    if row is not None:
                        existing_fingerprint = row["request_fingerprint"]
                        if (
                            existing_fingerprint is not None
                            and request_fingerprint is not None
                            and existing_fingerprint != request_fingerprint
                        ):
                            raise DuplicateTaskRequestError(
                                "request ID was reused with different task content"
                            )
                        return self._decode_task(row["payload_json"]), False
                if (
                    connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?", (task.task_id,)
                    ).fetchone()
                    is not None
                ):
                    raise ConcurrentTaskUpdateError("task already exists")
                self._write_new(connection, task, request_fingerprint)
        except BaseException:
            task.version = previous_version
            task.updated_at = previous_updated_at
            raise
        return TaskRecord.from_dict(task.to_dict()), True

    def save(self, task: TaskRecord) -> TaskRecord:
        previous_version = task.version
        previous_updated_at = task.updated_at
        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT version FROM tasks WHERE task_id = ?", (task.task_id,)
                ).fetchone()
                if row is None:
                    key = self._request_key(task)
                    if key is not None:
                        duplicate = connection.execute(
                            "SELECT task_id FROM task_requests "
                            "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
                            key,
                        ).fetchone()
                        if duplicate is not None:
                            raise DuplicateTaskRequestError(
                                "request ID is already associated with a task in this session"
                            )
                    self._write_new(connection, task)
                else:
                    current_version = int(row["version"])
                    if task.version != current_version:
                        raise ConcurrentTaskUpdateError(
                            f"stale task version {task.version}; "
                            f"current version is {current_version}"
                        )
                    task.version = current_version + 1
                    task.updated_at = utc_now()
                    payload = json.dumps(task.to_dict(), sort_keys=True, separators=(",", ":"))
                    cursor = connection.execute(
                        "UPDATE tasks SET status = ?, schema_version = ?, version = ?, "
                        "updated_at = ?, payload_json = ? WHERE task_id = ? AND version = ?",
                        (
                            task.status.value,
                            task.schema_version,
                            task.version,
                            task.updated_at.isoformat(),
                            payload,
                            task.task_id,
                            previous_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConcurrentTaskUpdateError("task changed during save")
        except BaseException:
            task.version = previous_version
            task.updated_at = previous_updated_at
            raise
        return TaskRecord.from_dict(task.to_dict())

    def get(self, task_id: str) -> TaskRecord | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._decode_task(row["payload_json"]) if row else None

    def get_by_request_id(
        self,
        *,
        principal_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str | None = None,
    ) -> TaskRecord | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT tasks.payload_json, task_requests.request_fingerprint "
                "FROM task_requests JOIN tasks USING (task_id) "
                "WHERE principal_id = ? AND session_id = ? AND request_id = ?",
                (principal_id, session_id, request_id),
            ).fetchone()
        if row is None:
            return None
        existing_fingerprint = row["request_fingerprint"]
        if (
            existing_fingerprint is not None
            and request_fingerprint is not None
            and existing_fingerprint != request_fingerprint
        ):
            raise DuplicateTaskRequestError("request ID was reused with different task content")
        return self._decode_task(row["payload_json"])

    def list_incomplete(
        self, *, limit: int = 100, after_task_id: str | None = None
    ) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        placeholders = ", ".join("?" for _ in self._RECOVERY_IGNORED)
        after = " AND task_id > ?" if after_task_id is not None else ""
        parameters: tuple[object, ...] = self._RECOVERY_IGNORED
        if after_task_id is not None:
            parameters += (after_task_id,)
        parameters += (limit,)
        with self.database.locked() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM tasks WHERE status NOT IN ({placeholders}){after} "
                "ORDER BY task_id LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode_task(row["payload_json"]) for row in rows]

    def list_recent(self, *, limit: int = 100) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_task(row["payload_json"]) for row in rows]

    def list_for_principal(self, *, principal_id: str, limit: int = 5000) -> list[TaskRecord]:
        if not principal_id.strip() or limit < 1:
            raise ValueError("principal_id and positive task-history limit are required")
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT tasks.payload_json FROM task_requests "
                "JOIN tasks USING (task_id) WHERE task_requests.principal_id = ? "
                "ORDER BY tasks.updated_at DESC LIMIT ?",
                (principal_id, limit),
            ).fetchall()
        records = [self._decode_task(row["payload_json"]) for row in rows]
        return [
            record
            for record in records
            if record.authorization is not None
            and record.authorization.principal_id == principal_id
        ]

    def prune_terminal_history(self, *, before: datetime) -> dict[str, int]:
        """Apply configured retention to settled tasks older than an aware UTC cutoff."""

        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("history retention cutoff must be timezone-aware")
        cutoff = before.astimezone(UTC)
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT DISTINCT task_requests.principal_id FROM task_requests "
                "JOIN tasks USING (task_id) WHERE tasks.updated_at < ?",
                (cutoff.isoformat(),),
            ).fetchall()
        totals = {
            "deleted_tasks": 0,
            "retained_recoverable_tasks": 0,
            "deleted_events": 0,
            "deleted_sessions": 0,
        }
        for row in rows:
            result = self.clear_terminal_history(
                principal_id=str(row["principal_id"]), updated_before=cutoff
            )
            for key in totals:
                totals[key] += result[key]
        return totals

    def clear_terminal_history(
        self, *, principal_id: str, updated_before: datetime | None = None
    ) -> dict[str, int]:
        """Delete settled outcomes while preserving active/ambiguous tasks and tombstones."""

        if not principal_id.strip():
            raise ValueError("principal_id is required")
        if updated_before is not None and (
            updated_before.tzinfo is None or updated_before.utcoffset() is None
        ):
            raise ValueError("history retention cutoff must be timezone-aware")
        query = (
            "SELECT tasks.task_id, tasks.status, tasks.payload_json, "
            "task_requests.session_id, task_requests.request_id, "
            "task_requests.request_fingerprint FROM task_requests "
            "JOIN tasks USING (task_id) WHERE task_requests.principal_id = ?"
        )
        query_parameters: tuple[object, ...] = (principal_id,)
        if updated_before is not None:
            query += " AND tasks.updated_at < ?"
            query_parameters += (updated_before.astimezone(UTC).isoformat(),)
        deleted_tasks = 0
        retained_recoverable_tasks = 0
        deleted_events = 0
        deleted_sessions = 0
        retained_session_ids: set[str] = set()
        protected_session_ids: set[str] = set()
        terminal_session_ids: set[str] = set()
        event_replay_floor = 0
        with self.database.transaction() as connection:
            rows = connection.execute(query, query_parameters).fetchall()
            for row in rows:
                task = self._decode_task(row["payload_json"])
                owner = task.authorization.principal_id if task.authorization is not None else None
                session_id = str(row["session_id"])
                if owner != principal_id:
                    protected_session_ids.add(session_id)
                    continue
                if task.status.value not in self._HISTORY_PURGEABLE:
                    retained_recoverable_tasks += 1
                    retained_session_ids.add(session_id)
                    continue
                terminal_session_ids.add(session_id)
                connection.execute(
                    "INSERT OR REPLACE INTO task_request_tombstones("
                    "principal_id, session_id, request_id, request_fingerprint, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        row["session_id"],
                        row["request_id"],
                        row["request_fingerprint"],
                        utc_now().isoformat(),
                    ),
                )
                event_sequence_row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_sequence "
                    "FROM events WHERE task_id = ?",
                    (row["task_id"],),
                ).fetchone()
                event_replay_floor = max(
                    event_replay_floor, int(event_sequence_row["max_sequence"])
                )
                cursor = connection.execute(
                    "DELETE FROM events WHERE task_id = ?", (row["task_id"],)
                )
                deleted_events += cursor.rowcount
                connection.execute(
                    "DELETE FROM conversation_turns WHERE task_id = ?", (row["task_id"],)
                )
                connection.execute("DELETE FROM tasks WHERE task_id = ?", (row["task_id"],))
                deleted_tasks += 1

            if event_replay_floor:
                connection.execute(
                    "UPDATE event_replay_state SET replay_floor = MAX(replay_floor, ?) "
                    "WHERE singleton = 1",
                    (event_replay_floor,),
                )

            for session_id in terminal_session_ids - retained_session_ids - protected_session_ids:
                remaining_requests = connection.execute(
                    "SELECT 1 FROM task_requests WHERE session_id = ? LIMIT 1", (session_id,)
                ).fetchone()
                remaining_turns = connection.execute(
                    "SELECT 1 FROM conversation_turns WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
                remaining_events = connection.execute(
                    "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)
                ).fetchone()
                if (
                    remaining_requests is not None
                    or remaining_turns is not None
                    or remaining_events is not None
                ):
                    continue
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND principal_id = ?",
                    (session_id, principal_id),
                )
                deleted_sessions += cursor.rowcount
        return {
            "deleted_tasks": deleted_tasks,
            "retained_recoverable_tasks": retained_recoverable_tasks,
            "deleted_events": deleted_events,
            "deleted_sessions": deleted_sessions,
        }


class SQLiteSessionRepository(SessionRepository):
    """Local session history with credential-shaped text redacted before storage."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
    ) -> None:
        self.database = database
        self.redactor = redactor

    def create(self, session: Session) -> Session:
        if session.turns:
            raise ValueError("create a session first, then append turns individually")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, principal_id, created_at, updated_at, locale) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.principal_id,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.locale,
                ),
            )
        return session

    def get(self, session_id: str) -> Session | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            turn_rows = connection.execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? "
                "ORDER BY created_at, turn_id",
                (session_id,),
            ).fetchall()
        turns = tuple(
            ConversationTurn(
                turn_id=turn["turn_id"],
                session_id=turn["session_id"],
                speaker=turn["speaker"],
                text=turn["text"],
                created_at=datetime.fromisoformat(turn["created_at"]),
                task_id=turn["task_id"],
                metadata=json.loads(turn["metadata_json"]),
            )
            for turn in turn_rows
        )
        return Session(
            session_id=row["session_id"],
            principal_id=row["principal_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            locale=row["locale"],
            turns=turns,
        )

    def list_recent(self, *, principal_id: str, limit: int = 50) -> list[Session]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT session_id FROM sessions WHERE principal_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (principal_id, limit),
            ).fetchall()
        return [session for row in rows if (session := self.get(row["session_id"])) is not None]

    def append_turn(self, turn: ConversationTurn) -> ConversationTurn:
        safe_turn = turn.model_copy(
            update={
                "text": self.redactor.redact(turn.text),
                "metadata": self.redactor.redact_object(turn.metadata),
            }
        )
        metadata_json = json.dumps(safe_turn.metadata, sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            session = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (safe_turn.session_id,)
            ).fetchone()
            if session is None:
                raise LookupError("session does not exist")
            existing = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id = ?", (safe_turn.turn_id,)
            ).fetchone()
            if existing is not None:
                existing_metadata = json.loads(existing["metadata_json"])
                if (
                    existing["session_id"] != safe_turn.session_id
                    or existing["speaker"] != safe_turn.speaker
                    or existing["text"] != safe_turn.text
                    or existing["task_id"] != safe_turn.task_id
                    or existing_metadata != safe_turn.metadata
                ):
                    raise ValueError("turn_id was reused for different conversation content")
                return ConversationTurn(
                    turn_id=existing["turn_id"],
                    session_id=existing["session_id"],
                    speaker=existing["speaker"],
                    text=existing["text"],
                    created_at=datetime.fromisoformat(existing["created_at"]),
                    task_id=existing["task_id"],
                    metadata=existing_metadata,
                )
            connection.execute(
                "INSERT INTO conversation_turns(turn_id, session_id, speaker, text, "
                "created_at, task_id, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    safe_turn.turn_id,
                    safe_turn.session_id,
                    safe_turn.speaker,
                    safe_turn.text,
                    safe_turn.created_at.isoformat(),
                    safe_turn.task_id,
                    metadata_json,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (utc_now().isoformat(), safe_turn.session_id),
            )
        return safe_turn


class SQLiteMemoryRepository(MemoryPort, MemoryConsentPort):
    """Explicit-consent, principal-scoped local memory with lexical retrieval.

    Consent references are stored only as SHA-256 digests and are scoped to a fingerprint of
    the exact text, category, source task, and retention expiry. A grant is consumed immediately
    before the record insert; if persistence then fails, the grant remains consumed and clients
    must obtain new consent rather than retrying an unknown write.
    """

    _TOKEN_PATTERN = re.compile(r"[\w'-]{2,}", re.UNICODE)

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        max_records_per_principal: int = 1000,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
        embedding: EmbeddingPort | None = None,
    ) -> None:
        if not 1 <= max_records_per_principal <= 100_000:
            raise ValueError("max_records_per_principal must be between 1 and 100000")
        self.database = database
        self.max_records_per_principal = max_records_per_principal
        self.redactor = redactor
        self.embedding = embedding

    async def issue_write_consent(
        self,
        entry: MemoryEntry,
        *,
        ttl_seconds: int = 120,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """Create an exact-scope one-time grant; no memory content is stored here."""

        if not 1 <= ttl_seconds <= 600:
            raise ValueError("memory consent lifetime must be between 1 and 600 seconds")
        issued_at = now or utc_now()
        if issued_at.tzinfo is None:
            raise ValueError("memory consent issue time must be timezone-aware")
        if entry.expires_at <= issued_at:
            raise MemoryConsentError("memory proposal has expired")
        reference = f"mem_{secrets.token_urlsafe(32)}"
        digest = hashlib.sha256(reference.encode("ascii")).hexdigest()
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM memory_consents WHERE expires_at <= ?", (issued_at.isoformat(),)
            )
            connection.execute(
                "INSERT INTO memory_consents(consent_hash, principal_id, entry_fingerprint, "
                "expires_at, consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (
                    digest,
                    entry.principal_id,
                    memory_entry_fingerprint(entry),
                    expires_at.isoformat(),
                ),
            )
        return reference, expires_at

    async def consume_write_consent(
        self,
        *,
        principal_id: str,
        consent_reference: str,
        entry_fingerprint: str,
        now: datetime,
    ) -> bool:
        if now.tzinfo is None:
            raise ValueError("memory consent check time must be timezone-aware")
        try:
            digest = hashlib.sha256(consent_reference.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_consents SET consumed_at = ? "
                "WHERE consent_hash = ? AND principal_id = ? AND entry_fingerprint = ? "
                "AND expires_at > ? AND consumed_at IS NULL",
                (
                    now.isoformat(),
                    digest,
                    principal_id,
                    entry_fingerprint,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    async def store(self, entry: MemoryEntry) -> str:
        safe_text = self.redactor.redact(entry.text).strip()
        if not safe_text:
            raise ValueError("memory text was empty after redaction")
        record_id = str(uuid.uuid4())
        embedding: EmbeddingResult | None = None
        if self.embedding is not None:
            try:
                embedding = await self.embedding.embed(safe_text, correlation_id=record_id)
            except Exception:
                # Semantic enrichment is optional; explicit local storage can use lexical search.
                embedding = None
        await require_memory_write_consent(entry, self)
        created_at = utc_now()
        provenance = "explicit user-approved local memory"
        embedding_json = (
            json.dumps(embedding.vector, separators=(",", ":")) if embedding is not None else None
        )
        embedding_model_id = embedding.model_id if embedding is not None else None
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM memory_records WHERE principal_id = ? AND expires_at <= ?",
                (entry.principal_id, created_at.isoformat()),
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_records WHERE principal_id = ?",
                (entry.principal_id,),
            ).fetchone()[0]
            if int(count) >= self.max_records_per_principal:
                raise ValueError("memory limit reached; delete existing records before adding more")
            connection.execute(
                "INSERT INTO memory_records(record_id, principal_id, kind, text, provenance, "
                "created_at, expires_at, source_task_id, embedding_json, embedding_model_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    entry.principal_id,
                    entry.kind.value,
                    safe_text,
                    provenance,
                    created_at.isoformat(),
                    entry.expires_at.isoformat(),
                    entry.source_task_id,
                    embedding_json,
                    embedding_model_id,
                ),
            )
        return record_id

    async def retrieve(self, query: ContextQuery) -> tuple[RetrievedContext, ...]:
        now = utc_now()
        query_embedding: EmbeddingResult | None = None
        if self.embedding is not None:
            try:
                query_embedding = await self.embedding.embed(
                    query.query,
                    correlation_id=query.task_id or query.session_id or str(uuid.uuid4()),
                )
            except Exception:
                query_embedding = None
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM memory_records WHERE principal_id = ? AND expires_at <= ?",
                (query.principal_id, now.isoformat()),
            )
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE principal_id = ? AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1000",
                (query.principal_id, now.isoformat()),
            ).fetchall()
        records = [self._decode_record(row) for row in rows]
        ranked = self._rank(query.query, records, query_embedding=query_embedding)
        maximum = max((score for score, _ in ranked), default=0.0)
        result: list[RetrievedContext] = []
        for score, record in ranked[: query.limit]:
            if score <= 0:
                continue
            result.append(
                RetrievedContext(
                    source=ContextSource.MEMORY,
                    source_id=record.record_id,
                    text=record.text,
                    provenance=f"{record.provenance}; kind={record.kind.value}",
                    retrieved_at=now,
                    relevance=min(1.0, score / maximum) if maximum else None,
                )
            )
        return tuple(result)

    def list_records(
        self,
        *,
        principal_id: str,
        limit: int = 100,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("memory list limit must be between 1 and 1000")
        now = utc_now()
        with self.database.transaction() as connection:
            if not include_expired:
                connection.execute(
                    "DELETE FROM memory_records WHERE principal_id = ? AND expires_at <= ?",
                    (principal_id, now.isoformat()),
                )
            comparator = "" if include_expired else "AND expires_at > ?"
            parameters: tuple[object, ...] = (principal_id,)
            if not include_expired:
                parameters += (now.isoformat(),)
            parameters += (limit,)
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE principal_id = ? {comparator} "
                "ORDER BY created_at DESC, record_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._decode_record(row) for row in rows]

    def get_record(self, *, principal_id: str, record_id: str) -> MemoryRecord | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id = ? AND principal_id = ? "
                "AND expires_at > ?",
                (record_id, principal_id, utc_now().isoformat()),
            ).fetchone()
        return self._decode_record(row) if row is not None else None

    def delete(self, *, principal_id: str, record_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_records WHERE record_id = ? AND principal_id = ?",
                (record_id, principal_id),
            )
            return cursor.rowcount == 1

    def delete_all(self, *, principal_id: str) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_records WHERE principal_id = ?", (principal_id,)
            )
            connection.execute(
                "DELETE FROM memory_consents WHERE principal_id = ?", (principal_id,)
            )
            return cursor.rowcount

    def purge_expired(self, *, now: datetime | None = None) -> int:
        checked_at = now or utc_now()
        if checked_at.tzinfo is None:
            raise ValueError("memory purge time must be timezone-aware")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_records WHERE expires_at <= ?", (checked_at.isoformat(),)
            )
            connection.execute(
                "DELETE FROM memory_consents WHERE expires_at <= ?", (checked_at.isoformat(),)
            )
            return cursor.rowcount

    @staticmethod
    def _decode_record(row: sqlite3.Row) -> MemoryRecord:
        try:
            return MemoryRecord(
                record_id=row["record_id"],
                principal_id=row["principal_id"],
                text=row["text"],
                kind=MemoryKind(row["kind"]),
                provenance=row["provenance"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                source_task_id=row["source_task_id"],
                embedding=(
                    tuple(json.loads(row["embedding_json"]))
                    if row["embedding_json"] is not None
                    else None
                ),
                embedding_model_id=row["embedding_model_id"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(
                "A persisted memory record is invalid.",
                component="sqlite-memory-repository",
                operation="decode-record",
                metadata={"record_kind": "memory"},
            ) from exc

    @classmethod
    def _rank(
        cls,
        query: str,
        records: list[MemoryRecord],
        *,
        query_embedding: EmbeddingResult | None = None,
    ) -> list[tuple[float, MemoryRecord]]:
        if (
            query_embedding is not None
            and records
            and all(
                record.embedding is not None
                and record.embedding_model_id == query_embedding.model_id
                and len(record.embedding) == len(query_embedding.vector)
                for record in records
            )
        ):
            query_norm = math.sqrt(sum(value * value for value in query_embedding.vector))
            semantic_scores: list[tuple[float, MemoryRecord]] = []
            for record in records:
                assert record.embedding is not None
                record_norm = math.sqrt(sum(value * value for value in record.embedding))
                dot_product = sum(
                    left * right
                    for left, right in zip(query_embedding.vector, record.embedding, strict=True)
                )
                score = dot_product / (query_norm * record_norm)
                semantic_scores.append((max(0.0, min(1.0, score)), record))
            return sorted(
                semantic_scores, key=lambda item: (item[0], item[1].created_at), reverse=True
            )

        query_terms = set(cls._TOKEN_PATTERN.findall(query.casefold()))
        if not query_terms:
            return []
        tokenized = [cls._TOKEN_PATTERN.findall(record.text.casefold()) for record in records]
        document_frequency = {
            term: sum(term in set(tokens) for tokens in tokenized) for term in query_terms
        }
        average_length = sum(map(len, tokenized)) / max(1, len(tokenized))
        count = len(records)
        ranked: list[tuple[float, MemoryRecord]] = []
        for record, tokens in zip(records, tokenized, strict=True):
            frequencies: dict[str, int] = {}
            for token in tokens:
                if token in query_terms:
                    frequencies[token] = frequencies.get(token, 0) + 1
            score = 0.0
            for term, frequency in frequencies.items():
                inverse_document_frequency = math.log(
                    1 + (count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
                )
                length_norm = frequency + 1.2 * (
                    0.25 + 0.75 * len(tokens) / max(1.0, average_length)
                )
                score += inverse_document_frequency * frequency * 2.2 / length_norm
            if query.casefold() in record.text.casefold():
                score += 0.25
            if score > 0:
                ranked.append((score, record))
        return sorted(ranked, key=lambda item: (item[0], item[1].created_at), reverse=True)


class SQLiteEventStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> EventEnvelope:
        try:
            return EventEnvelope(
                event_id=row["event_id"],
                event_type=row["event_type"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                monotonic_timestamp_ns=int(row["monotonic_timestamp_ns"]),
                task_id=row["task_id"],
                step_id=row["step_id"],
                parent_event_id=row["parent_event_id"],
                session_id=row["session_id"],
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
                source=row["source"],
                severity=EventSeverity(row["severity"]),
                payload=json.loads(row["payload_json"]),
                runtime_id=row["runtime_id"],
                schema_version=int(row["schema_version"]),
                sequence=int(row["sequence"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(
                "A persisted event record is invalid.",
                component="sqlite-event-store",
                operation="decode-event",
                metadata={"record_kind": "event"},
            ) from exc

    @staticmethod
    def _same_content(first: EventEnvelope, second: EventEnvelope) -> bool:
        first_data = first.to_dict()
        second_data = second.to_dict()
        first_data.pop("sequence", None)
        second_data.pop("sequence", None)
        return first_data == second_data

    def append(self, event: EventEnvelope) -> EventEnvelope:
        payload = json.dumps(event.to_dict()["payload"], sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing is not None:
                stored = self._decode_event(existing)
                if not self._same_content(stored, event):
                    raise DuplicateEventError("event_id was reused with different event content")
                return stored
            cursor = connection.execute(
                "INSERT INTO events(event_id, event_type, timestamp, monotonic_timestamp_ns, "
                "task_id, step_id, parent_event_id, session_id, source, severity, "
                "payload_json, runtime_id, schema_version, correlation_id, causation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.event_type,
                    event.timestamp.isoformat(),
                    event.monotonic_timestamp_ns,
                    event.task_id,
                    event.step_id,
                    event.parent_event_id,
                    event.session_id,
                    event.source,
                    event.severity.value,
                    payload,
                    event.runtime_id,
                    event.schema_version,
                    event.correlation_id,
                    event.causation_id,
                ),
            )
            sequence = int(cursor.lastrowid)
        return event.with_sequence(sequence)

    def read_after(
        self,
        sequence: int = 0,
        *,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[EventEnvelope]:
        if sequence < 0:
            raise ValueError("sequence cannot be negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.database.locked() as connection:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                    (sequence, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM events WHERE sequence > ? AND task_id = ? "
                    "ORDER BY sequence LIMIT ?",
                    (sequence, task_id, limit),
                ).fetchall()
        return [self._decode_event(row) for row in rows]

    def latest_sequence(self) -> int:
        """Return the durable cursor high-water mark, even after event-history deletion."""

        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT max("
                "COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'events'), 0), "
                "COALESCE((SELECT MAX(sequence) FROM events), 0))"
            ).fetchone()
        return int(row[0])

    def replay_floor(self) -> int:
        """Return the greatest pruned sequence before which cursors are no longer complete."""

        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT replay_floor FROM event_replay_state WHERE singleton = 1"
            ).fetchone()
        return int(row[0]) if row is not None else 0
