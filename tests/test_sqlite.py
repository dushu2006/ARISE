from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from arise.adapters.process_lock import DatabaseInstanceLock, InstanceLockError
from arise.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteSessionRepository,
    SQLiteTaskRepository,
    UnsupportedDatabaseVersion,
)
from arise.core.contracts import AuthorizationContext, TrustLevel, utc_now
from arise.core.errors import DatabaseError
from arise.core.events import DuplicateEventError, EventEnvelope
from arise.core.models import ConversationTurn, Session
from arise.core.tasks import (
    ConcurrentTaskUpdateError,
    DuplicateTaskRequestError,
    TaskRecord,
    TaskStatus,
)


class SQLiteAdapterTests(unittest.TestCase):
    def test_file_database_uses_wal_and_normal_synchronous_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "wal.sqlite3")
            with database.locked() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].casefold(), "wal"
                )
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
            database.close()

    def test_online_backup_is_atomic_private_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = SQLiteDatabase(root / "live.sqlite3")
            repository = SQLiteTaskRepository(database)
            task = TaskRecord.new("Backup this task")
            task.transition_to(TaskStatus.QUEUED)
            repository.save(task)
            event_store = SQLiteEventStore(database)
            event_store.append(EventEnvelope(event_type="BACKUP_FIXTURE", task_id=task.task_id))

            destination = database.backup_to(root / "backups" / "snapshot.sqlite3")
            self.assertTrue(destination.is_file())
            if os.name != "nt":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                database.backup_to(destination)
            with self.assertRaises(ValueError):
                database.backup_to(database.path)

            snapshot = SQLiteDatabase(destination)
            self.assertEqual(
                SQLiteTaskRepository(snapshot).get(task.task_id).status, TaskStatus.QUEUED
            )
            self.assertEqual(
                [event.event_type for event in SQLiteEventStore(snapshot).read_after()],
                ["BACKUP_FIXTURE"],
            )
            with snapshot.locked() as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            snapshot.close()
            database.close()

    def test_backup_rejects_a_symlink_alias_of_the_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = SQLiteDatabase(root / "live.sqlite3")
            alias = root / "live-alias.sqlite3"
            try:
                alias.symlink_to(database.path)
            except OSError as exc:
                database.close()
                self.skipTest(f"symlink creation is unavailable: {type(exc).__name__}")

            try:
                with self.assertRaisesRegex(ValueError, "cannot be the live database"):
                    database.backup_to(alias)
            finally:
                database.close()

    def test_backup_race_does_not_overwrite_winner_or_leave_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = SQLiteDatabase(root / "live.sqlite3")
            destination = root / "snapshot.sqlite3"

            def create_winner_then_fail(_source: Path, target: Path) -> None:
                target.write_text("another process won", encoding="utf-8")
                raise FileExistsError(target)

            try:
                with (
                    patch("arise.adapters.sqlite.os.link", side_effect=create_winner_then_fail),
                    self.assertRaises(FileExistsError),
                ):
                    database.backup_to(destination)
                self.assertEqual(destination.read_text(encoding="utf-8"), "another process won")
                self.assertEqual(list(root.glob(f".{destination.name}.*.partial")), [])
            finally:
                database.close()

    def test_backup_failure_cleans_temporary_file_and_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = SQLiteDatabase(root / "live.sqlite3")
            destination = root / "snapshot.sqlite3"
            try:
                with (
                    patch(
                        "arise.adapters.sqlite.sqlite3.connect",
                        side_effect=sqlite3.OperationalError("injected backup failure"),
                    ),
                    self.assertRaisesRegex(sqlite3.OperationalError, "injected backup failure"),
                ):
                    database.backup_to(destination)
                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob(f".{destination.name}.*.partial")), [])
            finally:
                database.close()

    def test_managed_instance_lock_precedes_connection_and_releases_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.db"
            database = SQLiteDatabase(path, acquire_instance_lock=True)
            with patch("arise.adapters.sqlite.sqlite3.connect") as connect:
                with self.assertRaises(InstanceLockError):
                    SQLiteDatabase(path, acquire_instance_lock=True)
                connect.assert_not_called()

            database.close()
            reopened = SQLiteDatabase(path, acquire_instance_lock=True)
            reopened.close()

    def test_managed_instance_lock_is_released_when_database_initialization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'future')"
            )
            connection.commit()
            connection.close()

            with self.assertRaises(UnsupportedDatabaseVersion):
                SQLiteDatabase(path, acquire_instance_lock=True)
            with DatabaseInstanceLock(path):
                pass

    def test_task_roundtrip_and_optimistic_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "arise.db"
            database = SQLiteDatabase(path)
            repository = SQLiteTaskRepository(database)
            authority = AuthorizationContext(
                principal_id="test-user",
                user_intent_id="intent-1",
                trust=TrustLevel.USER_INSTRUCTION,
                capabilities=frozenset({"local.open"}),
            )
            task = TaskRecord.planned("Persist the task", authorization=authority)
            repository.save(task)
            self.assertEqual(task.version, 1)

            first_copy = repository.get(task.task_id)
            stale_copy = repository.get(task.task_id)
            self.assertEqual(first_copy.status, TaskStatus.READY)
            self.assertEqual(first_copy.authorization, authority)
            first_copy.transition_to(TaskStatus.RUNNING)
            repository.save(first_copy)

            stale_copy.transition_to(TaskStatus.RUNNING)
            with self.assertRaises(ConcurrentTaskUpdateError):
                repository.save(stale_copy)

            loaded = repository.get(task.task_id)
            self.assertEqual(loaded.status, TaskStatus.RUNNING)
            self.assertEqual(loaded.version, 2)
            database.close()

            reopened = SQLiteDatabase(path)
            self.assertEqual(
                SQLiteTaskRepository(reopened).get(task.task_id).status, TaskStatus.RUNNING
            )
            reopened.close()

    def test_event_sequence_is_persistent_and_filterable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "events.db")
            store = SQLiteEventStore(database)
            first = store.append(
                EventEnvelope(
                    event_type="TASK_CREATED",
                    task_id="task-a",
                    correlation_id="request-1",
                    causation_id="request-1",
                )
            )
            second = store.append(EventEnvelope(event_type="ACTION_STARTED", task_id="task-a"))
            third = store.append(EventEnvelope(event_type="TASK_CREATED", task_id="task-b"))

            self.assertEqual((first.sequence, second.sequence, third.sequence), (1, 2, 3))
            task_events = store.read_after(1, task_id="task-a")
            self.assertEqual([event.event_type for event in task_events], ["ACTION_STARTED"])
            all_events = store.read_after()
            self.assertEqual([event.sequence for event in all_events], [1, 2, 3])
            self.assertEqual(all_events[0].correlation_id, "request-1")
            self.assertEqual(all_events[0].causation_id, "request-1")
            self.assertEqual(store.append(first).sequence, first.sequence)
            with self.assertRaises(DuplicateEventError):
                store.append(replace(first, payload={"different": True}))
            self.assertEqual(store.latest_sequence(), 3)
            database.close()

    def test_clear_terminal_history_preserves_active_work_events_and_request_tombstones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "history-clear.db")
            tasks = SQLiteTaskRepository(database)
            sessions = SQLiteSessionRepository(database)
            events = SQLiteEventStore(database)
            session = sessions.create(Session(session_id="session-a", principal_id="user-a"))
            terminal_auth = AuthorizationContext(
                principal_id="user-a",
                user_intent_id="request-terminal",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            terminal = TaskRecord.new(
                "Terminal task",
                authorization=terminal_auth,
                request_id="request-terminal",
                session_id=session.session_id,
            )
            terminal.transition_to(TaskStatus.FAILED, reason="fixture failure")
            terminal, created = tasks.create_or_get(
                terminal, request_fingerprint="terminal-fingerprint"
            )
            self.assertTrue(created)

            active_auth = AuthorizationContext(
                principal_id="user-a",
                user_intent_id="request-active",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            active = TaskRecord.new(
                "Still active",
                authorization=active_auth,
                request_id="request-active",
                session_id=session.session_id,
            )
            active.transition_to(TaskStatus.QUEUED)
            active, created = tasks.create_or_get(active, request_fingerprint="active-fingerprint")
            self.assertTrue(created)

            recoverable_tasks = []
            for request_id, final_status in (
                ("request-unknown", TaskStatus.UNKNOWN),
                ("request-interrupted", TaskStatus.INTERRUPTED),
                ("request-partial", TaskStatus.PARTIALLY_COMPLETED),
            ):
                authority = AuthorizationContext(
                    principal_id="user-a",
                    user_intent_id=request_id,
                    trust=TrustLevel.USER_INSTRUCTION,
                )
                recoverable = TaskRecord.new(
                    request_id,
                    authorization=authority,
                    request_id=request_id,
                    session_id=session.session_id,
                )
                for status in (
                    TaskStatus.QUEUED,
                    TaskStatus.UNDERSTANDING,
                    TaskStatus.PLANNING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    final_status,
                ):
                    recoverable.transition_to(status)
                recoverable, created = tasks.create_or_get(
                    recoverable, request_fingerprint=f"{request_id}-fingerprint"
                )
                self.assertTrue(created)
                recoverable_tasks.append(recoverable)

            active_event = events.append(
                EventEnvelope(
                    event_type="ACTIVE_EVENT",
                    task_id=active.task_id,
                    session_id=session.session_id,
                )
            )
            terminal_event = events.append(
                EventEnvelope(
                    event_type="TERMINAL_EVENT",
                    task_id=terminal.task_id,
                    session_id=session.session_id,
                )
            )
            self.assertEqual(events.replay_floor(), 0)
            self.assertEqual(terminal_event.sequence, active_event.sequence + 1)
            sessions.append_turn(
                ConversationTurn(
                    session_id=session.session_id,
                    speaker="user",
                    text="Terminal task request",
                    task_id=terminal.task_id,
                )
            )
            sessions.append_turn(
                ConversationTurn(
                    session_id=session.session_id,
                    speaker="user",
                    text="Active task request",
                    task_id=active.task_id,
                )
            )

            latest_sequence_before_clear = events.latest_sequence()
            result = tasks.clear_terminal_history(principal_id="user-a")
            self.assertEqual(events.latest_sequence(), latest_sequence_before_clear)
            self.assertEqual(events.replay_floor(), terminal_event.sequence)
            next_event = events.append(EventEnvelope(event_type="AFTER_HISTORY_CLEAR"))
            self.assertGreater(next_event.sequence, latest_sequence_before_clear)
            self.assertEqual(
                result,
                {
                    "deleted_tasks": 1,
                    "retained_recoverable_tasks": 4,
                    "deleted_events": 1,
                    "deleted_sessions": 0,
                },
            )
            self.assertIsNone(tasks.get(terminal.task_id))
            self.assertEqual(tasks.get(active.task_id).status, TaskStatus.QUEUED)
            self.assertEqual(
                {item.task_id for item in tasks.list_for_principal(principal_id="user-a")},
                {active.task_id, *(item.task_id for item in recoverable_tasks)},
            )
            self.assertEqual(events.read_after(task_id=terminal.task_id), [])
            self.assertEqual(
                [event.event_type for event in events.read_after(task_id=active.task_id)],
                ["ACTIVE_EVENT"],
            )
            remaining_session = sessions.get(session.session_id)
            self.assertEqual([turn.task_id for turn in remaining_session.turns], [active.task_id])

            replay = TaskRecord.new(
                "Terminal task replay",
                authorization=terminal_auth,
                request_id="request-terminal",
                session_id=session.session_id,
            )
            replay.transition_to(TaskStatus.QUEUED)
            with self.assertRaisesRegex(DuplicateTaskRequestError, "cleared history"):
                tasks.create_or_get(replay, request_fingerprint="terminal-fingerprint")

            other_session = sessions.create(Session(session_id="session-b", principal_id="user-a"))
            other_terminal_auth = AuthorizationContext(
                principal_id="user-a",
                user_intent_id="request-other-terminal",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            other_terminal = TaskRecord.new(
                "Other settled task",
                authorization=other_terminal_auth,
                request_id="request-other-terminal",
                session_id=other_session.session_id,
            )
            other_terminal.transition_to(TaskStatus.FAILED, reason="fixture failure")
            other_terminal, created = tasks.create_or_get(
                other_terminal, request_fingerprint="other-terminal-fingerprint"
            )
            self.assertTrue(created)
            events.append(
                EventEnvelope(
                    event_type="OTHER_TERMINAL_EVENT",
                    task_id=other_terminal.task_id,
                    session_id=other_session.session_id,
                )
            )
            sessions.append_turn(
                ConversationTurn(
                    session_id=other_session.session_id,
                    speaker="user",
                    text="Other terminal task request",
                    task_id=other_terminal.task_id,
                )
            )

            last_active = tasks.get(active.task_id)
            last_active.transition_to(TaskStatus.CANCELLED, reason="test cleanup")
            tasks.save(last_active)
            final_clear = tasks.clear_terminal_history(principal_id="user-a")
            self.assertEqual(final_clear["deleted_tasks"], 2)
            self.assertEqual(final_clear["retained_recoverable_tasks"], 3)
            self.assertEqual(final_clear["deleted_sessions"], 1)
            self.assertIsNotNone(sessions.get(session.session_id))
            self.assertIsNone(sessions.get(other_session.session_id))
            database.close()

    def test_history_retention_prunes_old_settled_tasks_and_keeps_ambiguous_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "history-retention.db")
            tasks = SQLiteTaskRepository(database)
            sessions = SQLiteSessionRepository(database)
            events = SQLiteEventStore(database)

            def create_task(request_id: str, session_id: str, status: TaskStatus) -> TaskRecord:
                sessions.create(Session(session_id=session_id, principal_id="user-a"))
                authority = AuthorizationContext(
                    principal_id="user-a",
                    user_intent_id=request_id,
                    trust=TrustLevel.USER_INSTRUCTION,
                )
                task = TaskRecord.new(
                    request_id,
                    authorization=authority,
                    request_id=request_id,
                    session_id=session_id,
                )
                if status is TaskStatus.UNKNOWN:
                    for next_status in (
                        TaskStatus.QUEUED,
                        TaskStatus.UNDERSTANDING,
                        TaskStatus.PLANNING,
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.UNKNOWN,
                    ):
                        task.transition_to(next_status)
                else:
                    task.transition_to(status)
                saved, created = tasks.create_or_get(task, request_fingerprint=request_id)
                self.assertTrue(created)
                return saved

            old_terminal = create_task("old-terminal", "old-session", TaskStatus.FAILED)
            old_unknown = create_task("old-unknown", "unknown-session", TaskStatus.UNKNOWN)
            recent_terminal = create_task("recent-terminal", "recent-session", TaskStatus.FAILED)
            events.append(
                EventEnvelope(
                    event_type="OLD_TERMINAL_EVENT",
                    task_id=old_terminal.task_id,
                    session_id=old_terminal.session_id,
                )
            )
            events.append(
                EventEnvelope(
                    event_type="UNKNOWN_EVENT",
                    task_id=old_unknown.task_id,
                    session_id=old_unknown.session_id,
                )
            )
            cutoff = utc_now() - timedelta(days=30)
            old_time = (cutoff - timedelta(days=2)).isoformat()
            with database.transaction() as connection:
                for task in (old_terminal, old_unknown):
                    persisted = tasks.get(task.task_id)
                    persisted.updated_at = cutoff - timedelta(days=2)
                    payload = json.dumps(persisted.to_dict(), sort_keys=True, separators=(",", ":"))
                    connection.execute(
                        "UPDATE tasks SET updated_at = ?, payload_json = ? WHERE task_id = ?",
                        (old_time, payload, task.task_id),
                    )

            result = tasks.prune_terminal_history(before=cutoff)
            self.assertEqual(
                result,
                {
                    "deleted_tasks": 1,
                    "retained_recoverable_tasks": 1,
                    "deleted_events": 1,
                    "deleted_sessions": 1,
                },
            )
            self.assertIsNone(tasks.get(old_terminal.task_id))
            self.assertEqual(tasks.get(old_unknown.task_id).status, TaskStatus.UNKNOWN)
            self.assertEqual(tasks.get(recent_terminal.task_id).status, TaskStatus.FAILED)
            self.assertIsNone(sessions.get("old-session"))
            self.assertIsNotNone(sessions.get("unknown-session"))
            replay = TaskRecord.new(
                "Replay old terminal task",
                authorization=old_terminal.authorization,
                request_id=old_terminal.request_id,
                session_id=old_terminal.session_id,
            )
            replay.transition_to(TaskStatus.QUEUED)
            with self.assertRaisesRegex(DuplicateTaskRequestError, "cleared history"):
                tasks.create_or_get(replay, request_fingerprint="old-terminal")
            database.close()

    def test_task_request_ids_are_idempotent_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "idempotency.db")
            repository = SQLiteTaskRepository(database)
            authority = AuthorizationContext(
                principal_id="user-a",
                user_intent_id="request-a",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            first_candidate = TaskRecord.new(
                "Original goal",
                authorization=authority,
                request_id="request-a",
                session_id="session-a",
            )
            first_candidate.transition_to(TaskStatus.QUEUED)
            first, created = repository.create_or_get(
                first_candidate, request_fingerprint="fingerprint-original"
            )
            self.assertTrue(created)

            replay_candidate = TaskRecord.new(
                "Replayed payload",
                authorization=authority,
                request_id="request-a",
                session_id="session-a",
            )
            replay_candidate.transition_to(TaskStatus.QUEUED)
            replay, created = repository.create_or_get(
                replay_candidate, request_fingerprint="fingerprint-original"
            )
            self.assertFalse(created)
            self.assertEqual(replay.task_id, first.task_id)
            self.assertEqual(replay.goal, "Original goal")

            changed_candidate = TaskRecord.new(
                "Changed goal",
                authorization=authority,
                request_id="request-a",
                session_id="session-a",
            )
            with self.assertRaises(DuplicateTaskRequestError):
                repository.create_or_get(
                    changed_candidate, request_fingerprint="fingerprint-changed"
                )
            with self.assertRaises(DuplicateTaskRequestError):
                repository.get_by_request_id(
                    principal_id="user-a",
                    session_id="session-a",
                    request_id="request-a",
                    request_fingerprint="fingerprint-changed",
                )

            changed = repository.get(first.task_id)
            changed.request_id = "clarification-a"
            repository.save(changed)
            self.assertEqual(
                repository.get_by_request_id(
                    principal_id="user-a", session_id="session-a", request_id="request-a"
                ).task_id,
                first.task_id,
            )

            another_session = TaskRecord.new(
                "Other session goal",
                authorization=authority,
                request_id="request-a",
                session_id="session-b",
            )
            other_session, created = repository.create_or_get(another_session)
            self.assertTrue(created)
            self.assertNotEqual(other_session.task_id, first.task_id)

            other_authority = AuthorizationContext(
                principal_id="user-b",
                user_intent_id="request-a",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            other_principal = TaskRecord.new(
                "Other principal goal",
                authorization=other_authority,
                request_id="request-a",
                session_id="session-a",
            )
            other_user, created = repository.create_or_get(other_principal)
            self.assertTrue(created)
            self.assertNotEqual(other_user.task_id, first.task_id)
            database.close()

    def test_concurrent_same_request_creates_only_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-idempotency.db"
            databases = [SQLiteDatabase(path), SQLiteDatabase(path)]
            repositories = [SQLiteTaskRepository(database) for database in databases]
            barrier = threading.Barrier(2)
            authority = AuthorizationContext(
                principal_id="concurrent-user",
                user_intent_id="concurrent-request",
                trust=TrustLevel.USER_INSTRUCTION,
            )

            def create(repository: SQLiteTaskRepository, goal: str):
                candidate = TaskRecord.new(
                    goal,
                    authorization=authority,
                    request_id="concurrent-request",
                    session_id="concurrent-session",
                )
                barrier.wait(timeout=3)
                return repository.create_or_get(candidate, request_fingerprint="same-fingerprint")

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(create, repository, f"Concurrent goal {index}")
                        for index, repository in enumerate(repositories)
                    ]
                    results = [future.result(timeout=5) for future in futures]
                self.assertEqual(sum(created for _, created in results), 1)
                self.assertEqual(results[0][0].task_id, results[1][0].task_id)

                changed = TaskRecord.new(
                    "Different goal",
                    authorization=authority,
                    request_id="concurrent-request",
                    session_id="concurrent-session",
                )
                with self.assertRaises(DuplicateTaskRequestError):
                    repositories[0].create_or_get(
                        changed, request_fingerprint="different-fingerprint"
                    )
            finally:
                for database in databases:
                    database.close()

    def test_sqlite_write_lock_is_retryable_and_preserves_the_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "busy.db"
            database = SQLiteDatabase(path, busy_timeout_ms=100)
            repository = SQLiteTaskRepository(database)
            lock_holder = sqlite3.connect(path, timeout=0.1, isolation_level=None)
            lock_holder.execute("BEGIN IMMEDIATE")
            task = TaskRecord.new("A task blocked by the database lock")
            with self.assertRaises(DatabaseError) as caught:
                repository.save(task)
            self.assertTrue(caught.exception.info.retryable)
            self.assertIsNone(repository.get(task.task_id))
            self.assertEqual(task.version, 0)
            lock_holder.rollback()
            lock_holder.close()
            database.close()

    def test_schema_migration_failure_rolls_back_ddl_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "atomic-migration.db"

            def fail_after_ddl(connection) -> None:
                connection.execute("CREATE TABLE partial_v4(value TEXT)")
                raise RuntimeError("injected migration failure")

            with patch.object(SQLiteDatabase, "_migration_v4", staticmethod(fail_after_ddl)):
                with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                    SQLiteDatabase(path)

            connection = sqlite3.connect(path)
            versions = [
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            ]
            partial_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_v4'"
            ).fetchone()
            connection.close()
            self.assertEqual(versions, [1, 2, 3])
            self.assertIsNone(partial_table)

            recovered = SQLiteDatabase(path)
            with recovered.locked() as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8, 9])
            with recovered.locked() as connection:
                task_request_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(task_requests)")
                }
            self.assertIn("request_fingerprint", task_request_columns)
            recovered.close()

    def test_session_turns_are_persisted_with_credential_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "sessions.db")
            repository = SQLiteSessionRepository(database)
            session = repository.create(Session(principal_id="test-user"))
            stored_turn = repository.append_turn(
                ConversationTurn(
                    session_id=session.session_id,
                    speaker="user",
                    text="Use api_key=sk-123456789012345678901234",
                    metadata={"note": "password=hunter2"},
                )
            )
            self.assertNotIn("sk-123456", stored_turn.text)
            self.assertNotIn("hunter2", str(stored_turn.metadata))
            loaded = repository.get(session.session_id)
            self.assertEqual(loaded.turns[0].text, stored_turn.text)
            self.assertEqual(loaded.turns[0].metadata, stored_turn.metadata)
            database.close()

    def test_session_turns_with_equal_timestamps_keep_insertion_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "ordered-sessions.db")
            repository = SQLiteSessionRepository(database)
            session = repository.create(Session(principal_id="test-user"))
            created_at = utc_now()
            for speaker, text in (("user", "Question"), ("assistant", "Answer")):
                repository.append_turn(
                    ConversationTurn(
                        session_id=session.session_id,
                        speaker=speaker,
                        text=text,
                        created_at=created_at,
                    )
                )

            loaded = repository.get(session.session_id)
            self.assertEqual([turn.speaker for turn in loaded.turns], ["user", "assistant"])
            database.close()

    def test_schema_v1_database_is_migrated_for_causal_events_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (1, '2026-01-01T00:00:00+00:00');
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, status TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
                    monotonic_timestamp_ns INTEGER NOT NULL,
                    task_id TEXT, step_id TEXT, parent_event_id TEXT, session_id TEXT,
                    source TEXT NOT NULL, severity TEXT NOT NULL, payload_json TEXT NOT NULL,
                    runtime_id TEXT NOT NULL, schema_version INTEGER NOT NULL
                );
                CREATE INDEX idx_events_task_sequence ON events(task_id, sequence);
                """
            )
            connection.close()
            database = SQLiteDatabase(path)
            with database.locked() as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
                request_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(task_requests)")
                }
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8, 9])
            self.assertIn("request_fingerprint", request_columns)
            self.assertIn("correlation_id", event_columns)
            self.assertIn("causation_id", event_columns)
            store = SQLiteEventStore(database)
            self.assertEqual(store.replay_floor(), 0)
            event = store.append(
                EventEnvelope(
                    event_type="TASK_CREATED",
                    correlation_id="legacy-request",
                    causation_id="legacy-request",
                )
            )
            self.assertEqual(store.read_after()[0].correlation_id, "legacy-request")
            self.assertEqual(store.latest_sequence(), event.sequence)
            database.close()


if __name__ == "__main__":
    unittest.main()
