"""FastAPI composition root for the authenticated local control API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette import status
from starlette.types import ASGIApp, Receive, Scope, Send

from arise.adapters.diagnostics import EnvironmentDiscovery
from arise.adapters.openai_compatible import OpenAICompatibleProvider
from arise.adapters.secrets import CompositeSecretProvider, EnvironmentSecretProvider
from arise.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteSessionRepository,
    SQLiteTaskRepository,
)
from arise.adapters.unavailable import UnavailableEnvironment
from arise.config.settings import AppSettings, get_settings, validate_api_token
from arise.core.capabilities import CapabilityService
from arise.core.contracts import utc_now
from arise.core.engine import (
    TaskEngine,
    TaskEngineConfig,
    TaskInputNotAccepted,
    TaskQueueFull,
    UnavailablePlanner,
)
from arise.core.errors import AriseError, classify_exception
from arise.core.event_bus import EventBroker, EventSubscription, PublishingEventStore
from arise.core.health import HealthService
from arise.core.model_gateway import ModelRouter
from arise.core.models import (
    Capability,
    ConversationTurn,
    DiagnosticsSnapshot,
    HealthSnapshot,
    Session,
    TaskDetail,
    TaskSnapshot,
    UserRequest,
)
from arise.core.planner import GatewayTaskPlanner
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.protocol import (
    PROTOCOL_VERSION,
    ClientFrame,
    ClientGoodbyeFrame,
    ClientHello,
    EventSubscribeFrame,
    PingFrame,
    ServerFrame,
    TaskApproveFrame,
    TaskCancelFrame,
    TaskRespondFrame,
    TaskSubmitFrame,
    parse_client_frame,
)
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.storage import SessionRepository
from arise.core.tasks import DuplicateTaskRequestError, TaskNotFoundError, TaskRepository

_LOG = logging.getLogger("arise.api")
_PREVIEW_ORIGIN = re.compile(r"^https://[0-9]+-[A-Za-z0-9-]+\.e2b\.app$")


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: str = Field(default="en", min_length=2, max_length=32)


class TaskApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_id: str = Field(min_length=1, max_length=128)


class TaskUserInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=16_384)


@dataclass(slots=True)
class ServerServices:
    settings: AppSettings
    database: SQLiteDatabase
    tasks: TaskRepository
    sessions: SessionRepository
    event_store: PublishingEventStore
    broker: EventBroker
    tools: ToolRegistry
    policy: PolicyEngine
    router: ModelRouter
    engine: TaskEngine
    health: HealthService
    api_token: str | None
    api_token_file: Path | None
    principal_id: str = "local-user"


def _load_or_create_token(settings: AppSettings) -> tuple[str | None, Path | None]:
    if not settings.security.require_api_auth:
        return None, None
    if settings.api.auth_token is not None:
        return settings.api.auth_token.get_secret_value(), None

    directory = settings.data_dir.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    token_path = directory / "api.token"
    if token_path.is_symlink():
        raise RuntimeError("The local API credential file is invalid.")
    if token_path.exists():
        if token_path.is_symlink():
            raise RuntimeError("The local API credential file is invalid.")
        if os.name != "nt":
            token_path.chmod(0o600)
        raw_token = token_path.read_text(encoding="utf-8")
        token = raw_token[:-1] if raw_token.endswith("\n") else raw_token
        try:
            validate_api_token(token)
        except ValueError as exc:
            raise RuntimeError("The local API credential file is invalid.") from exc
        return token, token_path

    token = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_or_create_token(settings)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        token_path.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        token_path.chmod(0o600)
    return token, token_path


def _is_loopback_endpoint(base_url: str) -> bool:
    from ipaddress import ip_address
    from urllib.parse import urlparse

    hostname = urlparse(base_url).hostname or ""
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _build_services(settings: AppSettings) -> ServerServices:
    settings.data_dir.expanduser().mkdir(parents=True, exist_ok=True)
    token, token_file = _load_or_create_token(settings)
    database = SQLiteDatabase(
        settings.database_path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
        acquire_instance_lock=True,
    )
    task_repository = SQLiteTaskRepository(database)
    session_repository = SQLiteSessionRepository(database)
    broker = EventBroker()
    event_store = PublishingEventStore(SQLiteEventStore(database), broker)
    tools = ToolRegistry()
    policy = PolicyEngine()
    router = ModelRouter(
        allow_cloud=settings.model.allow_cloud and settings.security.allow_cloud_models,
        max_concurrent_requests=settings.model.max_concurrent_requests,
    )
    planner = UnavailablePlanner()
    provider: OpenAICompatibleProvider | None = None
    if settings.model.base_url is not None and settings.model.model_id:
        base_url = str(settings.model.base_url)
        local = _is_loopback_endpoint(base_url)
        cloud_allowed = settings.model.allow_cloud and settings.security.allow_cloud_models
        if local or cloud_allowed:
            secret_provider = CompositeSecretProvider(
                environment_provider=EnvironmentSecretProvider(
                    enabled=(
                        settings.security.allow_environment_secrets
                        and settings.security.environment == "development"
                    )
                )
            )
            provider = OpenAICompatibleProvider(
                provider_id=settings.model.provider_id or "openai-compatible",
                base_url=base_url,
                model_id=settings.model.model_id,
                api_key_secret_name=settings.model.api_key_secret_name,
                secret_provider=secret_provider,
                is_cloud=not local,
                max_concurrent_requests=settings.model.max_concurrent_requests,
                timeout_seconds=settings.model.request_timeout_seconds,
                connect_timeout_seconds=settings.model.connect_timeout_seconds,
            )
            router.register(provider)
            planner = GatewayTaskPlanner(
                router,
                tools,
                model_id=settings.model.model_id,
                privacy="cloud_allowed" if not local else "local_only",
                timeout_seconds=settings.model.request_timeout_seconds,
            )
        else:
            _LOG.warning(
                "Configured cloud model ignored because cloud use is not explicitly enabled"
            )

    environment = UnavailableEnvironment()
    runtime = AgentRuntime(
        tasks=task_repository,
        events=event_store,
        tools=tools,
        policy=policy,
        environment=environment,
        resources=ResourceManager(),
        verifier=FactVerifier(environment),
    )
    engine = TaskEngine(
        tasks=task_repository,
        events=event_store,
        runtime=runtime,
        tools=tools,
        policy=policy,
        planner=planner,
        config=TaskEngineConfig(
            max_concurrent_tasks=settings.runtime.max_concurrent_tasks,
            max_queued_tasks=settings.runtime.max_queued_tasks,
            task_timeout_seconds=settings.runtime.task_timeout_seconds,
            resource_wait_timeout_seconds=settings.runtime.resource_wait_timeout_seconds,
            confirmation_ttl_seconds=settings.security.confirmation_timeout_seconds,
        ),
    )
    capability_service = CapabilityService(
        router=router,
        tools=tools,
        database_available=database.health_check,
    )
    health = HealthService(
        app_name=settings.app_name,
        app_version=settings.app_version,
        capability_service=capability_service,
        task_engine=engine,
        environment=EnvironmentDiscovery(),
        database_schema_version=database.CURRENT_SCHEMA_VERSION,
        database_path_kind="configured" if settings.database.path is not None else "default",
        database_probe=database.health_check,
    )
    return ServerServices(
        settings=settings,
        database=database,
        tasks=task_repository,
        sessions=session_repository,
        event_store=event_store,
        broker=broker,
        tools=tools,
        policy=policy,
        router=router,
        engine=engine,
        health=health,
        api_token=token,
        api_token_file=token_file,
    )


class RequestSizeLimitMiddleware:
    """Enforce declared and actually received HTTP request body sizes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        body_timeout_seconds: float = 30.0,
    ) -> None:
        if max_request_bytes < 1 or body_timeout_seconds <= 0:
            raise ValueError("request body limits must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.body_timeout_seconds = body_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(lengths) > 1 and len(set(lengths)) > 1:
            await self._reject(scope, receive, send, 400, "INVALID_LENGTH")
            return
        if lengths:
            try:
                if not lengths[0].isdigit():
                    raise ValueError
                declared_length = int(lengths[0])
            except ValueError:
                await self._reject(scope, receive, send, 400, "INVALID_LENGTH")
                return
            if declared_length > self.max_request_bytes:
                await self._reject(scope, receive, send, 413, "REQUEST_TOO_LARGE")
                return

        body = bytearray()
        try:
            async with asyncio.timeout(self.body_timeout_seconds):
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        return
                    if message.get("type") != "http.request":
                        continue
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_request_bytes:
                        await self._reject(scope, receive, send, 413, "REQUEST_TOO_LARGE")
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self._reject(scope, receive, send, 408, "REQUEST_BODY_TIMEOUT")
            return

        body_bytes = bytes(body)
        body_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status_code: int, error_code: str
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"error": {"code": error_code}},
        )
        await response(scope, receive, send)


def _conversation_turn_id(session_id: str, request_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"arise:{session_id}:{request_id}"))


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build an isolated app; useful for packaging and API tests."""

    chosen_settings = settings or get_settings()
    services = _build_services(chosen_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await services.engine.start()
            if os.environ.get("ARISE_BACKEND_READY_SIGNAL") == "1":
                print("ARISE_BACKEND_READY", flush=True)
            yield
        finally:
            try:
                await services.engine.close()
            finally:
                try:
                    await services.router.close()
                finally:
                    services.database.close()

    app = FastAPI(
        title=chosen_settings.app_name,
        version=chosen_settings.app_version,
        docs_url="/docs" if chosen_settings.security.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=chosen_settings.api.trusted_origins,
        allow_origin_regex=(
            _PREVIEW_ORIGIN.pattern
            if chosen_settings.security.environment == "development"
            else None
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=300,
    )
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_request_bytes=chosen_settings.api.max_request_bytes,
        body_timeout_seconds=chosen_settings.api.request_body_timeout_seconds,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_REQUEST", "message": "Request validation failed."}},
        )

    @app.exception_handler(DuplicateTaskRequestError)
    async def duplicate_request_error_handler(
        _: Request, exc: DuplicateTaskRequestError
    ) -> JSONResponse:
        del exc
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "REQUEST_ID_CONFLICT",
                    "message": "Request ID was already used for different task content.",
                }
            },
        )

    @app.exception_handler(AriseError)
    async def arise_error_handler(_: Request, exc: AriseError) -> JSONResponse:
        info = classify_exception(exc, component="api")
        code = 400
        if info.category.value == "authentication":
            code = 401
        elif info.category.value in {"permission", "policy"}:
            code = 403
        elif info.category.value == "validation":
            code = 422
        elif info.category.value == "protocol":
            code = 400
        elif info.category.value == "database":
            code = 503 if info.retryable else 500
        elif info.retryable or info.category.value in {
            "capability",
            "environment",
            "model",
            "provider",
            "resource",
            "timeout",
        }:
            code = 503
        elif info.category.value in {"configuration", "execution", "internal", "verification"}:
            code = 500
        return JSONResponse(status_code=code, content={"error": info.to_dict()})

    async def require_principal(
        authorization: str | None = Header(default=None),
    ) -> str:
        if not chosen_settings.security.require_api_auth:
            return services.principal_id
        if services.api_token is None:
            raise HTTPException(status_code=503, detail="Local API authentication is unavailable")
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, services.api_token):
            raise HTTPException(status_code=401, detail="Authentication required")
        return services.principal_id

    @app.get("/healthz", response_model=HealthSnapshot, tags=["health"])
    async def healthz() -> HealthSnapshot:
        return services.health.health()

    @app.get("/api/v1/health", response_model=HealthSnapshot, tags=["health"])
    async def api_health(_: str = Depends(require_principal)) -> HealthSnapshot:
        return services.health.health()

    @app.get("/api/v1/capabilities", response_model=tuple[Capability, ...], tags=["diagnostics"])
    async def capabilities(_: str = Depends(require_principal)) -> tuple[Capability, ...]:
        return services.health.capability_service.list_capabilities()

    @app.get("/api/v1/diagnostics", response_model=DiagnosticsSnapshot, tags=["diagnostics"])
    async def diagnostics(_: str = Depends(require_principal)) -> DiagnosticsSnapshot:
        return services.health.diagnostics()

    @app.post("/api/v1/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
    async def create_session(
        body: SessionCreateRequest, principal: str = Depends(require_principal)
    ) -> Session:
        return services.sessions.create(Session(principal_id=principal, locale=body.locale))

    @app.get("/api/v1/sessions", response_model=tuple[Session, ...])
    async def list_sessions(
        principal: str = Depends(require_principal),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> tuple[Session, ...]:
        return tuple(services.sessions.list_recent(principal_id=principal, limit=limit))

    @app.get("/api/v1/sessions/{session_id}", response_model=Session)
    async def get_session(session_id: str, principal: str = Depends(require_principal)) -> Session:
        session = services.sessions.get(session_id)
        if session is None or session.principal_id != principal:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @app.post("/api/v1/tasks", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
    async def submit_task(
        request_body: UserRequest, principal: str = Depends(require_principal)
    ) -> TaskSnapshot:
        session = services.sessions.get(request_body.session_id)
        if session is None:
            session = services.sessions.create(
                Session(
                    session_id=request_body.session_id,
                    principal_id=principal,
                    locale=request_body.locale or "en",
                )
            )
        elif session.principal_id != principal:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            task = await services.engine.submit(
                request_body,
                principal_id=principal,
                session_id=session.session_id,
            )
        except TaskQueueFull as exc:
            raise HTTPException(status_code=429, detail="Task queue is full") from exc
        try:
            services.sessions.append_turn(
                ConversationTurn(
                    turn_id=_conversation_turn_id(session.session_id, request_body.request_id),
                    session_id=session.session_id,
                    speaker="user",
                    text=request_body.text,
                    task_id=task.task_id,
                    metadata={"source": request_body.source.value},
                )
            )
        except Exception:
            _LOG.warning("Conversation turn was not persisted for task %s", task.task_id)
        return TaskSnapshot.from_record(task)

    @app.get("/api/v1/tasks", response_model=tuple[TaskSnapshot, ...])
    async def list_tasks(
        _: str = Depends(require_principal),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> tuple[TaskSnapshot, ...]:
        effective_limit = min(limit, chosen_settings.runtime.task_history_limit)
        return tuple(
            TaskSnapshot.from_record(task)
            for task in services.engine.list_tasks(limit=effective_limit)
        )

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskDetail)
    async def get_task(task_id: str, principal: str = Depends(require_principal)) -> TaskDetail:
        record = services.engine.get_task(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if record.authorization is not None and record.authorization.principal_id != principal:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskDetail(
            task=TaskSnapshot.from_record(record),
            confirmations=services.engine.pending_confirmations(task_id),
            accepts_user_input=services.engine.can_accept_input(task_id),
        )

    @app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskSnapshot)
    async def cancel_task(
        task_id: str, principal: str = Depends(require_principal)
    ) -> TaskSnapshot:
        try:
            task = await services.engine.cancel(task_id, principal_id=principal)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Task ownership check failed") from exc
        return TaskSnapshot.from_record(task)

    @app.post("/api/v1/tasks/{task_id}/respond", response_model=TaskSnapshot)
    async def respond_to_task(
        task_id: str,
        body: TaskUserInputRequest,
        principal: str = Depends(require_principal),
    ) -> TaskSnapshot:
        try:
            task = await services.engine.provide_input(task_id, body.text, principal_id=principal)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Task ownership check failed") from exc
        except TaskInputNotAccepted as exc:
            raise HTTPException(
                status_code=409, detail="Task is not awaiting a clarification"
            ) from exc
        except TaskQueueFull as exc:
            raise HTTPException(status_code=429, detail="Task queue is full") from exc
        session = services.sessions.get(task.session_id)
        if session is not None and session.principal_id == principal:
            try:
                services.sessions.append_turn(
                    ConversationTurn(
                        session_id=task.session_id,
                        speaker="user",
                        text=body.text,
                        task_id=task.task_id,
                        metadata={"kind": "clarification"},
                    )
                )
            except Exception:
                _LOG.warning("Clarification turn was not persisted for task %s", task.task_id)
        return TaskSnapshot.from_record(task)

    @app.post("/api/v1/tasks/{task_id}/approve", response_model=TaskSnapshot)
    async def approve_task(
        task_id: str,
        body: TaskApprovalRequest,
        principal: str = Depends(require_principal),
    ) -> TaskSnapshot:
        try:
            task = await services.engine.approve(
                task_id=task_id,
                confirmation_id=body.confirmation_id,
                approved_by=principal,
            )
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=403, detail="Confirmation is invalid or out of scope"
            ) from exc
        except TaskQueueFull as exc:
            raise HTTPException(status_code=429, detail="Task queue is full") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=409, detail="Task cannot be approved in its current state"
            ) from exc
        return TaskSnapshot.from_record(task)

    @app.get("/api/v1/events", tags=["events"])
    async def read_events(
        _: str = Depends(require_principal),
        after: int = Query(default=0, ge=0),
        task_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        events = services.event_store.read_after(after, task_id=task_id, limit=limit)
        return {
            "events": [event.to_dict() for event in events],
            "next_sequence": events[-1].sequence if events else after,
        }

    @app.websocket("/ws/v1")
    async def websocket_protocol(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin and origin not in chosen_settings.api.trusted_origins:
            if not (
                chosen_settings.security.environment == "development"
                and _PREVIEW_ORIGIN.fullmatch(origin)
            ):
                await websocket.close(code=1008, reason="Untrusted origin")
                return
        await websocket.accept()
        subscription: EventSubscription | None = None
        sender: asyncio.Task[None] | None = None
        send_lock = asyncio.Lock()
        processed_messages: OrderedDict[str, None] = OrderedDict()
        cached_responses: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

        async def send_frame(
            frame_type: str, payload: dict[str, Any], message_id: str | None = None
        ) -> None:
            frame = ServerFrame(
                type=frame_type, payload=payload, message_id=message_id or secrets.token_hex(12)
            )
            if message_id is not None and frame_type not in {"event", "server.hello"}:
                cached_responses[message_id] = (frame_type, payload)
                cached_responses.move_to_end(message_id)
                while len(cached_responses) > 512:
                    cached_responses.popitem(last=False)
            async with send_lock:
                await websocket.send_json(frame.to_wire())

        async def replay_after(sequence: int, *, task_id: str | None = None) -> int:
            cursor = sequence
            limit = chosen_settings.api.websocket_client_queue_size
            while True:
                page = services.event_store.read_after(cursor, task_id=task_id, limit=limit)
                if not page:
                    break
                for event in page:
                    event_sequence = event.sequence or 0
                    if event_sequence <= cursor:
                        continue
                    await send_frame("event", {"event": event.to_dict()})
                    cursor = event_sequence
                if len(page) < limit:
                    break
            return cursor

        try:
            try:
                raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=8.0)
            except TimeoutError:
                await websocket.close(code=1008, reason="Hello timed out")
                return
            if len(raw_hello.encode("utf-8")) > chosen_settings.api.max_request_bytes:
                await websocket.close(code=1009, reason="Frame too large")
                return
            try:
                hello_data = json.loads(raw_hello)
                hello = parse_client_frame(hello_data)
            except Exception:
                await websocket.close(code=1002, reason="Invalid protocol hello")
                return
            if not isinstance(hello, ClientHello):
                await websocket.close(code=1002, reason="First frame must be client.hello")
                return
            if chosen_settings.security.require_api_auth:
                if services.api_token is None or not hmac.compare_digest(
                    hello.auth_token, services.api_token
                ):
                    await websocket.close(code=1008, reason="Authentication required")
                    return

            current_sequence = services.event_store.latest_sequence()
            await send_frame(
                "server.hello",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "connection_id": secrets.token_hex(12),
                    "current_event_sequence": current_sequence,
                    "requested_event_sequence": hello.last_event_sequence,
                    "heartbeat_interval_seconds": chosen_settings.api.websocket_heartbeat_seconds,
                    "health": services.health.health().model_dump(mode="json"),
                },
                hello.message_id,
            )
            subscription = services.broker.subscribe(
                max_queue_size=chosen_settings.api.websocket_client_queue_size
            )
            last_sent_sequence = await replay_after(hello.last_event_sequence)

            async def send_ordered_event(event) -> None:
                nonlocal last_sent_sequence
                sequence = event.sequence or 0
                if sequence <= last_sent_sequence:
                    return
                if subscription is not None and subscription.task_id is None:
                    # A live queue can race the durable replay. Backfill any events
                    # between the last sent cursor and this event before forwarding it.
                    while sequence > last_sent_sequence + 1:
                        before = last_sent_sequence
                        last_sent_sequence = await replay_after(last_sent_sequence)
                        if last_sent_sequence == before:
                            break
                if sequence > last_sent_sequence:
                    await send_frame("event", {"event": event.to_dict()})
                    last_sent_sequence = sequence

            async def send_live_events() -> None:
                assert subscription is not None
                try:
                    while True:
                        if subscription.overflowed:
                            await send_frame(
                                "protocol.error",
                                {
                                    "code": "EVENT_BACKPRESSURE",
                                    "message": "Reconnect and replay from the last sequence.",
                                },
                            )
                            await websocket.close(code=1013, reason="Event queue overflow")
                            return
                        try:
                            event = await asyncio.wait_for(subscription.queue.get(), timeout=0.5)
                        except TimeoutError:
                            continue
                        await send_ordered_event(event)
                except WebSocketDisconnect:
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOG.warning("WebSocket event sender failed; closing the connection")
                    try:
                        await websocket.close(code=1011, reason="Event delivery failed")
                    except Exception:
                        pass

            sender = asyncio.create_task(send_live_events(), name="arise-websocket-event-sender")
            heartbeat_timeout = chosen_settings.api.websocket_heartbeat_seconds * 2.5
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=heartbeat_timeout
                    )
                except TimeoutError:
                    await websocket.close(code=1001, reason="Client heartbeat timed out")
                    return
                if len(raw.encode("utf-8")) > chosen_settings.api.max_request_bytes:
                    await send_frame("protocol.error", {"code": "FRAME_TOO_LARGE"})
                    await websocket.close(code=1009, reason="Frame too large")
                    return
                try:
                    frame: ClientFrame = parse_client_frame(json.loads(raw))
                except Exception:
                    await send_frame("protocol.error", {"code": "INVALID_FRAME"})
                    continue

                cached = cached_responses.get(frame.message_id)
                if cached is not None:
                    await send_frame(cached[0], cached[1], frame.message_id)
                    continue
                if frame.message_id in processed_messages:
                    await send_frame(
                        "protocol.error", {"code": "DUPLICATE_MESSAGE"}, frame.message_id
                    )
                    continue
                processed_messages[frame.message_id] = None
                processed_messages.move_to_end(frame.message_id)
                while len(processed_messages) > 2048:
                    processed_messages.popitem(last=False)

                if isinstance(frame, ClientHello):
                    await send_frame(
                        "protocol.error", {"code": "HELLO_ALREADY_RECEIVED"}, frame.message_id
                    )
                elif isinstance(frame, PingFrame):
                    await send_frame("pong", {"timestamp": utc_now().isoformat()}, frame.message_id)
                elif isinstance(frame, ClientGoodbyeFrame):
                    await websocket.close(code=1000)
                    return
                elif isinstance(frame, TaskSubmitFrame):
                    try:
                        requested_session = frame.session_id or frame.request.session_id
                        session = services.sessions.get(requested_session)
                        if session is None:
                            session = services.sessions.create(
                                Session(
                                    session_id=requested_session,
                                    principal_id=services.principal_id,
                                    locale=frame.request.locale or "en",
                                )
                            )
                        elif session.principal_id != services.principal_id:
                            raise PermissionError("session owner mismatch")
                        task = await services.engine.submit(
                            frame.request,
                            principal_id=services.principal_id,
                            session_id=session.session_id,
                        )
                        try:
                            services.sessions.append_turn(
                                ConversationTurn(
                                    turn_id=_conversation_turn_id(
                                        session.session_id, frame.request.request_id
                                    ),
                                    session_id=session.session_id,
                                    speaker="user",
                                    text=frame.request.text,
                                    task_id=task.task_id,
                                    metadata={"source": frame.request.source.value},
                                )
                            )
                        except Exception:
                            _LOG.warning(
                                "Conversation turn was not persisted for task %s", task.task_id
                            )
                        await send_frame(
                            "task.accepted",
                            {"task": TaskSnapshot.from_record(task).model_dump(mode="json")},
                            frame.message_id,
                        )
                    except DuplicateTaskRequestError:
                        await send_frame(
                            "protocol.error", {"code": "REQUEST_ID_CONFLICT"}, frame.message_id
                        )
                    except TaskQueueFull:
                        await send_frame(
                            "protocol.error", {"code": "TASK_QUEUE_FULL"}, frame.message_id
                        )
                    except PermissionError:
                        await send_frame("protocol.error", {"code": "FORBIDDEN"}, frame.message_id)
                    except Exception:
                        _LOG.warning("WebSocket task submission failed")
                        await send_frame(
                            "protocol.error", {"code": "TASK_SUBMISSION_FAILED"}, frame.message_id
                        )
                elif isinstance(frame, TaskCancelFrame):
                    try:
                        task = await services.engine.cancel(
                            frame.task_id,
                            principal_id=services.principal_id,
                        )
                        await send_frame(
                            "task.updated",
                            {"task": TaskSnapshot.from_record(task).model_dump(mode="json")},
                            frame.message_id,
                        )
                    except TaskNotFoundError:
                        await send_frame(
                            "protocol.error", {"code": "TASK_NOT_FOUND"}, frame.message_id
                        )
                    except PermissionError:
                        await send_frame("protocol.error", {"code": "FORBIDDEN"}, frame.message_id)
                elif isinstance(frame, TaskApproveFrame):
                    try:
                        task = await services.engine.approve(
                            task_id=frame.task_id,
                            confirmation_id=frame.confirmation_id,
                            approved_by=services.principal_id,
                        )
                        await send_frame(
                            "task.updated",
                            {"task": TaskSnapshot.from_record(task).model_dump(mode="json")},
                            frame.message_id,
                        )
                    except TaskNotFoundError:
                        await send_frame(
                            "protocol.error", {"code": "TASK_NOT_FOUND"}, frame.message_id
                        )
                    except PermissionError:
                        await send_frame("protocol.error", {"code": "FORBIDDEN"}, frame.message_id)
                    except TaskQueueFull:
                        await send_frame(
                            "protocol.error", {"code": "TASK_QUEUE_FULL"}, frame.message_id
                        )
                    except (ValueError, RuntimeError):
                        await send_frame(
                            "protocol.error", {"code": "APPROVAL_REJECTED"}, frame.message_id
                        )
                elif isinstance(frame, TaskRespondFrame):
                    try:
                        task = await services.engine.provide_input(
                            frame.task_id,
                            frame.text,
                            principal_id=services.principal_id,
                        )
                        await send_frame(
                            "task.updated",
                            {"task": TaskSnapshot.from_record(task).model_dump(mode="json")},
                            frame.message_id,
                        )
                    except TaskNotFoundError:
                        await send_frame(
                            "protocol.error", {"code": "TASK_NOT_FOUND"}, frame.message_id
                        )
                    except PermissionError:
                        await send_frame("protocol.error", {"code": "FORBIDDEN"}, frame.message_id)
                    except TaskQueueFull:
                        await send_frame(
                            "protocol.error", {"code": "TASK_QUEUE_FULL"}, frame.message_id
                        )
                    except (TaskInputNotAccepted, ValueError):
                        await send_frame(
                            "protocol.error", {"code": "INPUT_NOT_ACCEPTED"}, frame.message_id
                        )
                elif isinstance(frame, EventSubscribeFrame):
                    if subscription is None:
                        subscription = services.broker.subscribe(
                            task_id=frame.task_id,
                            max_queue_size=chosen_settings.api.websocket_client_queue_size,
                        )
                    else:
                        subscription.task_id = frame.task_id
                        while not subscription.queue.empty():
                            try:
                                subscription.queue.get_nowait()
                                subscription.queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                    last_sent_sequence = await replay_after(
                        frame.after_sequence, task_id=frame.task_id
                    )
                    await send_frame(
                        "events.subscribed",
                        {"task_id": frame.task_id, "after_sequence": frame.after_sequence},
                        frame.message_id,
                    )
                else:
                    await send_frame(
                        "protocol.error", {"code": "UNSUPPORTED_FRAME"}, frame.message_id
                    )
        except WebSocketDisconnect:
            return
        except Exception:
            _LOG.warning("WebSocket connection closed after an internal protocol error")
            try:
                await websocket.close(code=1011, reason="Internal protocol error")
            except Exception:
                pass
        finally:
            if subscription is not None:
                services.broker.unsubscribe(subscription)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    app = create_app(settings)
    if app.state.services.api_token_file is not None:
        _LOG.info(
            "Local API token stored in the OS user data directory; the token value is not logged"
        )
    config = uvicorn.Config(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
        access_log=True,
        ws_max_size=settings.api.max_request_bytes,
    )
    server = uvicorn.Server(config)
    if os.environ.get("ARISE_BACKEND_SUPERVISED") == "1":

        async def serve_supervised() -> None:
            loop = asyncio.get_running_loop()

            def wait_for_parent_pipe_close() -> None:
                try:
                    sys.stdin.buffer.read(1)
                except Exception:
                    pass
                loop.call_soon_threadsafe(setattr, server, "should_exit", True)

            threading.Thread(
                target=wait_for_parent_pipe_close,
                name="arise-parent-lifecycle-monitor",
                daemon=True,
            ).start()
            await server.serve()

        asyncio.run(serve_supervised())
    else:
        server.run()


if __name__ == "__main__":
    main()
