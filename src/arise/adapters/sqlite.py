"""Initial local SQLite adapters for durable task and event state.

Domain code depends only on repository/store protocols. The database is local,
uses short WAL transactions, and stores no raw action parameters or secrets.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from arise.adapters.process_lock import DatabaseInstanceLock
from arise.core.contracts import utc_now
from arise.core.errors import DatabaseError
from arise.core.events import DuplicateEventError, EventEnvelope, EventSeverity
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

    CURRENT_SCHEMA_VERSION = 5

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
            connection.execute(
                "ALTER TABLE task_requests ADD COLUMN request_fingerprint TEXT"
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
            raise DuplicateTaskRequestError(
                "request ID was reused with different task content"
            )
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
        with self.database.locked() as connection:
            row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()
        return int(row[0])
