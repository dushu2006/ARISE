from __future__ import annotations

import asyncio
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from arise.adapters.process_lock import DatabaseInstanceLock, InstanceLockError
from arise.config.settings import AppSettings, DatabaseSettings, SecuritySettings
from arise.core.errors import DatabaseError, PolicyDeniedError
from arise.server import RequestSizeLimitMiddleware, _load_or_create_token, create_app


class ApiTokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_dir = Path(self.temporary_directory.name)
        self.settings = AppSettings(
            data_dir=self.data_dir,
            security=SecuritySettings(environment="test"),
        )

    def test_existing_token_file_matches_the_configured_token_contract(self) -> None:
        token_path = self.data_dir / "api.token"
        for token in ("x" * 31, "x" * 513, "x" * 16 + " " + "x" * 16, "é" * 32):
            token_path.write_text(token + "\n", encoding="utf-8")
            with self.subTest(token_length=len(token)), self.assertRaises(RuntimeError):
                _load_or_create_token(self.settings)

    def test_valid_existing_token_is_loaded_and_made_private_on_posix(self) -> None:
        token = "A" * 32
        token_path = self.data_dir / "api.token"
        token_path.write_text(token + "\n", encoding="utf-8")

        actual, actual_path = _load_or_create_token(self.settings)

        self.assertEqual(actual, token)
        self.assertEqual(actual_path, token_path)
        if os.name != "nt":
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.app = create_app(
            AppSettings(
                data_dir=root,
                database=DatabaseSettings(path=root / "server.sqlite3"),
                security=SecuritySettings(environment="test", require_api_auth=True),
            )
        )
        self.token = self.app.state.services.api_token
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_second_app_cannot_start_workers_for_the_same_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AppSettings(
                data_dir=root,
                database=DatabaseSettings(path=root / "shared.sqlite3"),
                security=SecuritySettings(environment="test"),
            )
            first_app = create_app(settings)
            with TestClient(first_app):
                with self.assertRaises(InstanceLockError):
                    create_app(settings)
                self.assertTrue(first_app.state.services.database.health_check())

    def test_backend_startup_signal_is_emitted_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                AppSettings(
                    data_dir=root,
                    database=DatabaseSettings(path=root / "ready.sqlite3"),
                    security=SecuritySettings(environment="test", require_api_auth=True),
                )
            )
            output = io.StringIO()
            with patch.dict(os.environ, {"ARISE_BACKEND_READY_SIGNAL": "1"}):
                with redirect_stdout(output), TestClient(app):
                    pass
            self.assertEqual(output.getvalue().splitlines(), ["ARISE_BACKEND_READY"])

    def test_shutdown_failure_still_closes_model_provider_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                AppSettings(
                    data_dir=root,
                    database=DatabaseSettings(path=root / "shutdown.sqlite3"),
                    security=SecuritySettings(environment="test", require_api_auth=True),
                )
            )
            services = app.state.services
            original_close = services.engine.close

            async def close_then_fail() -> None:
                await original_close()
                raise RuntimeError("injected engine shutdown failure")

            with (
                patch.object(services.engine, "close", new=close_then_fail),
                patch.object(
                    services.router,
                    "close",
                    new=AsyncMock(wraps=services.router.close),
                ) as close_router,
                self.assertRaisesRegex(RuntimeError, "injected engine shutdown failure"),
            ):
                with TestClient(app):
                    pass
            close_router.assert_awaited_once()
            self.assertTrue(services.database._closed)
            with DatabaseInstanceLock(services.database.path):
                pass

    def test_health_is_truthful_and_control_routes_require_auth(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "degraded")
        self.assertEqual(self.client.get("/api/v1/diagnostics").status_code, 401)

        diagnostics = self.client.get("/api/v1/diagnostics", headers=self.headers)
        self.assertEqual(diagnostics.status_code, 200)
        capabilities = {item["name"]: item for item in diagnostics.json()["capabilities"]}
        self.assertEqual(capabilities["model.planning"]["status"], "requires_configuration")
        self.assertEqual(capabilities["desktop.ui_automation"]["status"], "unavailable")
        self.assertEqual(capabilities["voice.asr"]["status"], "disabled")

    def test_database_failure_is_reported_as_server_unavailable_not_bad_request(self) -> None:
        with patch.object(
            self.app.state.services.event_store,
            "read_after",
            side_effect=DatabaseError(
                "The local database operation failed.",
                component="sqlite-event-store",
                operation="read-events",
            ),
        ):
            response = self.client.get("/api/v1/events", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["error"]["message"], "The local database operation failed."
        )
        with patch.object(
            self.app.state.services.event_store,
            "read_after",
            side_effect=DatabaseError(
                "The local database is busy.",
                component="sqlite-event-store",
                operation="read-events",
                retryable=True,
            ),
        ):
            retryable_response = self.client.get("/api/v1/events", headers=self.headers)
        self.assertEqual(retryable_response.status_code, 503)
        with patch.object(
            self.app.state.services.event_store,
            "read_after",
            side_effect=PolicyDeniedError("The policy denied this operation."),
        ):
            denied_response = self.client.get("/api/v1/events", headers=self.headers)
        self.assertEqual(denied_response.status_code, 403)

    def test_task_submission_is_durable_and_does_not_fake_planning(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            json={"text": "Open the mail app"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        initial = response.json()
        self.assertEqual(initial["state"], "queued")
        task_id = initial["task_id"]
        for _ in range(100):
            detail = self.client.get(f"/api/v1/tasks/{task_id}", headers=self.headers)
            self.assertEqual(detail.status_code, 200)
            if detail.json()["task"]["state"] == "requires_user_input":
                break
            time.sleep(0.005)
        self.assertEqual(detail.json()["task"]["state"], "requires_user_input")
        self.assertFalse(detail.json()["task"]["steps"])
        self.assertIn("No planning model", detail.json()["task"]["status_reason"])
        history = self.client.get("/api/v1/events", headers=self.headers)
        self.assertEqual(history.status_code, 200)
        events = [event for event in history.json()["events"] if event["task_id"] == task_id]
        self.assertTrue(events)
        self.assertTrue(all(event["correlation_id"] == initial["request_id"] for event in events))
        session = self.client.get(f"/api/v1/sessions/{initial['session_id']}", headers=self.headers)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["turns"][0]["text"], "Open the mail app")

    def test_duplicate_http_request_id_reuses_task_and_conversation_turn(self) -> None:
        body = {
            "request_id": "http-retry-key",
            "session_id": "http-retry-session",
            "text": "Open the mail app",
        }
        first = self.client.post("/api/v1/tasks", json=body, headers=self.headers)
        replay = self.client.post("/api/v1/tasks", json=body, headers=self.headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(first.json()["task_id"], replay.json()["task_id"])

        tasks = self.client.get("/api/v1/tasks", headers=self.headers).json()
        matches = [task for task in tasks if task["request_id"] == "http-retry-key"]
        self.assertEqual(len(matches), 1)
        events = self.client.get("/api/v1/events", headers=self.headers).json()["events"]
        accepted = [
            event
            for event in events
            if event["task_id"] == first.json()["task_id"]
            and event["event_type"] == "TASK_ACCEPTED"
        ]
        self.assertEqual(len(accepted), 1)
        session = self.client.get(
            "/api/v1/sessions/http-retry-session", headers=self.headers
        ).json()
        self.assertEqual(len(session["turns"]), 1)

        conflicting = self.client.post(
            "/api/v1/tasks",
            json={**body, "text": "Send a different request"},
            headers=self.headers,
        )
        self.assertEqual(conflicting.status_code, 409)
        self.assertEqual(conflicting.json()["error"]["code"], "REQUEST_ID_CONFLICT")

    def test_websocket_replays_durable_events_from_the_requested_cursor(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            json={"request_id": "replay-request", "text": "Replay this task"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        for _ in range(100):
            detail_response = self.client.get(
                f"/api/v1/tasks/{task_id}", headers=self.headers
            )
            detail = detail_response.json()["task"]
            if detail["state"] == "requires_user_input":
                break
            time.sleep(0.005)
        expected = self.client.get("/api/v1/events", headers=self.headers).json()["events"]
        self.assertTrue(expected)

        replayed = []
        with self.client.websocket_connect("/ws/v1") as websocket:
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "type": "client.hello",
                    "auth_token": self.token,
                    "last_event_sequence": 0,
                }
            )
            self.assertEqual(websocket.receive_json()["type"], "server.hello")
            for _ in expected:
                frame = websocket.receive_json()
                self.assertEqual(frame["type"], "event")
                replayed.append(frame["payload"]["event"])

        self.assertEqual(
            [event["sequence"] for event in replayed],
            [event["sequence"] for event in expected],
        )
        self.assertEqual(replayed[-1]["sequence"], expected[-1]["sequence"])

    def test_websocket_client_can_reset_a_future_cursor_and_replay_from_zero(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            json={"request_id": "cursor-reset-request", "text": "Create replay events"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        for _ in range(100):
            detail = self.client.get(
                f"/api/v1/tasks/{task_id}", headers=self.headers
            ).json()["task"]
            if detail["state"] == "requires_user_input":
                break
            time.sleep(0.005)
        expected = self.client.get("/api/v1/events", headers=self.headers).json()["events"]
        future_cursor = expected[-1]["sequence"] + 100
        replayed = []

        with self.client.websocket_connect("/ws/v1") as websocket:
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "type": "client.hello",
                    "auth_token": self.token,
                    "last_event_sequence": future_cursor,
                }
            )
            hello = websocket.receive_json()
            self.assertEqual(hello["type"], "server.hello")
            self.assertLess(hello["payload"]["current_event_sequence"], future_cursor)
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "message_id": "reset-replay-cursor",
                    "type": "events.subscribe",
                    "after_sequence": 0,
                }
            )
            for _ in expected:
                frame = websocket.receive_json()
                self.assertEqual(frame["type"], "event")
                replayed.append(frame["payload"]["event"])
            self.assertEqual(websocket.receive_json()["type"], "events.subscribed")

        self.assertEqual(
            [event["sequence"] for event in replayed],
            [event["sequence"] for event in expected],
        )

    def test_websocket_requires_versioned_hello_and_streams_acknowledgements(self) -> None:
        with self.client.websocket_connect("/ws/v1") as websocket:
            websocket.send_json(
                {
                    "protocol_version": 1,
                    "type": "client.hello",
                    "auth_token": self.token,
                    "last_event_sequence": 0,
                }
            )
            hello = websocket.receive_json()
            self.assertEqual(hello["type"], "server.hello")
            self.assertEqual(hello["payload"]["protocol_version"], 1)
            websocket.send_json({"protocol_version": 1, "type": "ping"})
            pong = websocket.receive_json()
            self.assertEqual(pong["type"], "pong")


class RequestSizeLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_body_limit_applies_without_or_with_a_false_content_length(self) -> None:
        cases = (
            ([], 413),
            ([(b"content-length", b"1")], 413),
            ([(b"content-length", b"invalid")], 400),
        )
        for headers, expected_status in cases:
            messages = [
                {"type": "http.request", "body": b"x" * 1025, "more_body": False}
            ]
            responses = []

            async def receive(messages=messages):
                if messages:
                    return messages.pop(0)
                return {"type": "http.disconnect"}

            async def send(message, responses=responses):
                responses.append(message)

            async def endpoint(scope, receive, send):
                del scope
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if not message.get("more_body"):
                        break
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = RequestSizeLimitMiddleware(endpoint, max_request_bytes=1024)
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/upload",
                "raw_path": b"/upload",
                "query_string": b"",
                "headers": headers,
                "server": ("testserver", 80),
                "client": ("testclient", 1234),
            }
            await middleware(scope, receive, send)
            start = next(item for item in responses if item["type"] == "http.response.start")
            self.assertEqual(start["status"], expected_status)

    async def test_oversized_body_is_rejected_even_if_the_endpoint_would_not_read_it(self) -> None:
        messages = [{"type": "http.request", "body": b"x" * 1025, "more_body": False}]
        responses = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            responses.append(message)

        async def endpoint(scope, receive, send):
            del scope, receive
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        }
        middleware = RequestSizeLimitMiddleware(endpoint, max_request_bytes=1024)
        await middleware(scope, receive, send)
        start = next(item for item in responses if item["type"] == "http.response.start")
        self.assertEqual(start["status"], 413)

    async def test_slow_request_body_times_out(self) -> None:
        responses = []

        async def receive():
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            responses.append(message)

        async def endpoint(scope, receive, send):
            del scope, receive
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/tasks",
            "raw_path": b"/api/v1/tasks",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        }
        middleware = RequestSizeLimitMiddleware(
            endpoint, max_request_bytes=1024, body_timeout_seconds=0.01
        )
        await middleware(scope, receive, send)
        start = next(item for item in responses if item["type"] == "http.response.start")
        self.assertEqual(start["status"], 408)

if __name__ == "__main__":
    unittest.main()
