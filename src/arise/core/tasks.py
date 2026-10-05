"""Task state machine and repository contract."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from arise.core.contracts import AuthorizationContext, RiskLevel, utc_now, validate_safe_token


class InvalidTaskTransition(RuntimeError):
    pass


class DuplicateActionError(RuntimeError):
    pass


class TaskStatus(StrEnum):
    CREATED = "created"
    RECEIVED = "received"
    QUEUED = "queued"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    EXECUTING = "running"  # semantic alias retained for new task-engine code
    WAITING = "waiting"
    WAITING_MODEL = "waiting_model"
    WAITING_RESOURCE = "waiting_resource"
    WAITING_USER = "waiting_user"
    WAITING_AUTH = "waiting_auth"
    # Long-running waits name what the task is blocked on, so the engine always
    # knows the completion source it is waiting for instead of merely "waiting".
    WAITING_FOR_APPLICATION = "waiting_for_application"
    WAITING_FOR_BROWSER = "waiting_for_browser"
    WAITING_FOR_EXTERNAL_RESULT = "waiting_for_external_result"
    WAITING_FOR_VERIFICATION = "waiting_for_verification"
    RESUMING = "resuming"
    REQUIRES_USER_INPUT = "requires_user_input"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    INTERRUPTED = "interrupted"
    PARTIALLY_COMPLETED = "partially_completed"
    UNKNOWN = "unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    COMPLETED = "completed"


WAITING_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.WAITING,
        TaskStatus.WAITING_MODEL,
        TaskStatus.WAITING_RESOURCE,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_AUTH,
        TaskStatus.WAITING_FOR_APPLICATION,
        TaskStatus.WAITING_FOR_BROWSER,
        TaskStatus.WAITING_FOR_EXTERNAL_RESULT,
        TaskStatus.WAITING_FOR_VERIFICATION,
    }
)


class StepStatus(StrEnum):
    PLANNED = "planned"
    WAITING_USER = "waiting_user"
    WAITING_RESOURCE = "waiting_resource"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PLANNED: frozenset(
        {
            StepStatus.WAITING_USER,
            StepStatus.WAITING_RESOURCE,
            StepStatus.RUNNING,
            StepStatus.BLOCKED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.WAITING_USER: frozenset(
        {StepStatus.WAITING_RESOURCE, StepStatus.BLOCKED, StepStatus.CANCELLED}
    ),
    StepStatus.WAITING_RESOURCE: frozenset(
        {
            StepStatus.WAITING_USER,
            StepStatus.RUNNING,
            StepStatus.BLOCKED,
            StepStatus.UNKNOWN,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.VERIFYING,
            StepStatus.FAILED,
            StepStatus.UNKNOWN,
            StepStatus.BLOCKED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.VERIFYING: frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.UNKNOWN}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.UNKNOWN: frozenset(),
    StepStatus.BLOCKED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


class InvalidStepTransition(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {
            TaskStatus.RECEIVED,
            TaskStatus.QUEUED,
            TaskStatus.UNDERSTANDING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.RECEIVED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.UNDERSTANDING,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.UNDERSTANDING,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.UNDERSTANDING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.WAITING_USER,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }
    ),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.WAITING_USER,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_AUTH,
            TaskStatus.WAITING_RESOURCE,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_MODEL,
            TaskStatus.WAITING_RESOURCE,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_AUTH,
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.UNKNOWN,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            # A running action may hand control to a named wait, so the task keeps
            # reporting *what* it is waiting for instead of a bare "running".
            TaskStatus.WAITING_FOR_APPLICATION,
            TaskStatus.WAITING_FOR_BROWSER,
            TaskStatus.WAITING_FOR_EXTERNAL_RESULT,
            TaskStatus.WAITING_FOR_VERIFICATION,
            TaskStatus.RESUMING,
        }
    ),
    TaskStatus.WAITING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.WAITING_MODEL: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_FOR_APPLICATION: frozenset(
        {
            TaskStatus.RESUMING,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_FOR_BROWSER: frozenset(
        {
            TaskStatus.RESUMING,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_FOR_EXTERNAL_RESULT: frozenset(
        {
            TaskStatus.RESUMING,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_FOR_VERIFICATION: frozenset(
        {
            TaskStatus.RESUMING,
            TaskStatus.VERIFYING,
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.RESUMING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.WAITING_USER,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.RECOVERING,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_RESOURCE: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.WAITING_USER: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.WAITING_AUTH: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.UNKNOWN,
            TaskStatus.FAILED,
            TaskStatus.WAITING_USER,
            # Verification of a slow external result waits on that result.
            TaskStatus.WAITING_FOR_EXTERNAL_RESULT,
            TaskStatus.WAITING_FOR_VERIFICATION,
            TaskStatus.RESUMING,
            TaskStatus.COMPLETED,
        }
    ),
    TaskStatus.RECOVERING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_AUTH,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.UNKNOWN,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }
    ),
    TaskStatus.PARTIALLY_COMPLETED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.UNKNOWN,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }
    ),
    TaskStatus.UNKNOWN: frozenset(
        {
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_USER,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.INTERRUPTED: frozenset(
        {
            TaskStatus.RECOVERING,
            TaskStatus.QUEUED,
            TaskStatus.REQUIRES_USER_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.REQUIRES_USER_INPUT: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.QUEUED,
            TaskStatus.WAITING_USER,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.READY, TaskStatus.WAITING_USER, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RECOVERING}),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.COMPLETED: frozenset(),
}

# Cancellation and process interruption are explicit control transitions from every
# non-terminal state. Keep them in the transition table so persistence/recovery cannot
# bypass the same rules used by ordinary task progress.
for _status in TaskStatus:
    if _status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
        _ALLOWED_TRANSITIONS[_status] = _ALLOWED_TRANSITIONS[_status] | frozenset(
            {TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
        )


@dataclass(slots=True)
class ActionStep:
    action_id: str
    contract_fingerprint: str
    tool_name: str
    risk: RiskLevel
    status: StepStatus = StepStatus.PLANNED
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    verification_status: VerificationStatus | None = None
    status_reason: str | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.action_id, "action_id")
        validate_safe_token(self.tool_name, "tool_name")
        if not self.contract_fingerprint.strip():
            raise ValueError("contract fingerprint cannot be blank")
        if not isinstance(self.risk, RiskLevel) or not isinstance(self.status, StepStatus):
            raise ValueError("action step risk/status types are invalid")
        if self.created_at.tzinfo is None:
            raise ValueError("action step created_at must be timezone-aware")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("action step started_at must be timezone-aware")
        if self.finished_at is not None and self.finished_at.tzinfo is None:
            raise ValueError("action step finished_at must be timezone-aware")

    def set_status(
        self,
        status: StepStatus,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or utc_now()
        if status is not self.status and status not in _STEP_TRANSITIONS[self.status]:
            raise InvalidStepTransition(
                f"cannot transition action {self.action_id} from "
                f"{self.status.value} to {status.value}"
            )
        self.status = status
        self.status_reason = reason
        if status is StepStatus.RUNNING and self.started_at is None:
            self.started_at = now
        if status in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.UNKNOWN,
            StepStatus.BLOCKED,
            StepStatus.CANCELLED,
        }:
            self.finished_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "contract_fingerprint": self.contract_fingerprint,
            "tool_name": self.tool_name,
            "risk": int(self.risk),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "verification_status": (
                self.verification_status.value if self.verification_status is not None else None
            ),
            "status_reason": self.status_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionStep:
        return cls(
            action_id=str(data["action_id"]),
            contract_fingerprint=str(data["contract_fingerprint"]),
            tool_name=str(data["tool_name"]),
            risk=RiskLevel(int(data["risk"])),
            status=StepStatus(data.get("status", StepStatus.PLANNED.value)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            started_at=(
                datetime.fromisoformat(str(data["started_at"])) if data.get("started_at") else None
            ),
            finished_at=(
                datetime.fromisoformat(str(data["finished_at"]))
                if data.get("finished_at")
                else None
            ),
            verification_status=(
                VerificationStatus(data["verification_status"])
                if data.get("verification_status")
                else None
            ),
            status_reason=data.get("status_reason"),
        )


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    goal: str
    status: TaskStatus = TaskStatus.CREATED
    authorization: AuthorizationContext | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_task_id: str | None = None
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    steps: list[ActionStep] = field(default_factory=list)
    status_reason: str | None = None
    version: int = 0
    schema_version: int = 2
    # What the engine is currently waiting for, and how often a waiting task has
    # been resumed. Persisted so a restart or a reconnect can report and, where
    # safe, continue an interrupted long-running wait.
    waiting_reason: str | None = None
    resume_count: int = 0

    def __post_init__(self) -> None:
        validate_safe_token(self.task_id, "task_id")
        validate_safe_token(self.request_id, "request_id")
        validate_safe_token(self.session_id, "session_id")
        if self.parent_task_id is not None:
            validate_safe_token(self.parent_task_id, "parent_task_id")
            if self.parent_task_id == self.task_id:
                raise ValueError("a task cannot be its own parent")
        validate_safe_token(self.correlation_id, "correlation_id")
        if not self.goal.strip():
            raise ValueError("task goal cannot be blank")
        if len(self.goal) > 16_384:
            raise ValueError("task goal exceeds the storage limit")
        if not isinstance(self.status, TaskStatus):
            raise ValueError("status must be a TaskStatus")
        if self.authorization is not None and not isinstance(
            self.authorization, AuthorizationContext
        ):
            raise ValueError("authorization must be an AuthorizationContext")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("task timestamps must be timezone-aware")
        if self.version < 0 or self.schema_version < 1:
            raise ValueError("invalid task version")
        action_ids = [step.action_id for step in self.steps]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("a task cannot contain duplicate action IDs")

    @classmethod
    def new(
        cls,
        goal: str,
        *,
        task_id: str | None = None,
        authorization: AuthorizationContext | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TaskRecord:
        return cls(
            task_id=task_id or str(uuid.uuid4()),
            goal=goal,
            authorization=authorization,
            request_id=request_id or str(uuid.uuid4()),
            session_id=session_id or str(uuid.uuid4()),
            parent_task_id=parent_task_id,
            correlation_id=correlation_id or str(uuid.uuid4()),
        )

    @classmethod
    def planned(
        cls,
        goal: str,
        *,
        task_id: str | None = None,
        authorization: AuthorizationContext | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TaskRecord:
        """Create a task at the boundary after intent understanding and planning."""

        now = utc_now()
        return cls(
            task_id=task_id or str(uuid.uuid4()),
            goal=goal,
            status=TaskStatus.READY,
            authorization=authorization,
            request_id=request_id or str(uuid.uuid4()),
            session_id=session_id or str(uuid.uuid4()),
            parent_task_id=parent_task_id,
            correlation_id=correlation_id or str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
        )

    @property
    def waiting_target(self) -> str | None:
        """The bounded identifier of what this task is waiting for, if any."""

        return self.waiting_reason if self.status in WAITING_STATUSES else None

    def begin_wait(
        self,
        status: TaskStatus,
        *,
        waiting_reason: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Enter a waiting state and record the completion source being awaited."""

        if status not in WAITING_STATUSES:
            raise InvalidTaskTransition(f"{status.value} is not a waiting state")
        safe_reason = " ".join(str(waiting_reason).split())[:256] or None
        if status is not self.status:
            self.transition_to(status, reason=reason or safe_reason, now=now)
        elif reason is not None or safe_reason is not None:
            self.status_reason = reason or safe_reason
            self.updated_at = now or utc_now()
        self.waiting_reason = safe_reason

    def end_wait(self, *, resume_count: int | None = None) -> None:
        """Leave a waiting state; the resume counter records the transition out."""

        if self.status in WAITING_STATUSES:
            self.resume_count = self.resume_count + 1 if resume_count is None else int(resume_count)
        self.waiting_reason = None

    def transition_to(
        self,
        status: TaskStatus,
        *,
        reason: str | None = None,
        now: datetime | None = None,
        verification_passed: bool = False,
    ) -> None:
        if status is TaskStatus.COMPLETED and (
            not verification_passed or self.status is not TaskStatus.VERIFYING
        ):
            raise InvalidTaskTransition(
                "task completion requires a passing verification while in VERIFYING state"
            )
        if status is self.status:
            if reason is not None:
                self.status_reason = reason
                self.updated_at = now or utc_now()
            return
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTaskTransition(
                f"cannot transition task {self.task_id} from {self.status.value} to {status.value}"
            )
        self.status = status
        self.status_reason = reason
        self.updated_at = now or utc_now()

    def add_step(self, step: ActionStep) -> ActionStep:
        for existing in self.steps:
            if existing.action_id == step.action_id:
                if existing.contract_fingerprint != step.contract_fingerprint:
                    raise DuplicateActionError(
                        "action_id was reused with a different contract fingerprint"
                    )
                return existing
        self.steps.append(step)
        self.updated_at = utc_now()
        return step

    def find_step(self, action_id: str) -> ActionStep | None:
        return next((step for step in self.steps if step.action_id == action_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "authorization": self.authorization.to_dict()
            if self.authorization is not None
            else None,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "parent_task_id": self.parent_task_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "steps": [step.to_dict() for step in self.steps],
            "status_reason": self.status_reason,
            "version": self.version,
            "waiting_reason": self.waiting_reason,
            "resume_count": self.resume_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskRecord:
        """Load known fields while tolerating future additive fields."""

        return cls(
            task_id=str(data["task_id"]),
            goal=str(data["goal"]),
            status=TaskStatus(data["status"]),
            authorization=(
                AuthorizationContext.from_dict(data["authorization"])
                if data.get("authorization") is not None
                else None
            ),
            request_id=str(data.get("request_id", uuid.uuid4())),
            session_id=str(data.get("session_id", uuid.uuid4())),
            parent_task_id=(
                str(data["parent_task_id"]) if data.get("parent_task_id") is not None else None
            ),
            correlation_id=str(data.get("correlation_id", uuid.uuid4())),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            steps=[ActionStep.from_dict(step) for step in data.get("steps", [])],
            status_reason=data.get("status_reason"),
            version=int(data.get("version", 0)),
            schema_version=int(data.get("schema_version", 1)),
            waiting_reason=data.get("waiting_reason"),
            resume_count=int(data.get("resume_count", 0) or 0),
        )


class TaskNotFoundError(LookupError):
    pass


class ConcurrentTaskUpdateError(RuntimeError):
    """Raised when another runtime has persisted a newer task version."""


class DuplicateTaskRequestError(RuntimeError):
    """Raised if an idempotency key is reused outside the create-or-get path."""


class TaskRepository(Protocol):
    def save(self, task: TaskRecord) -> TaskRecord: ...

    def create_or_get(
        self, task: TaskRecord, *, request_fingerprint: str | None = None
    ) -> tuple[TaskRecord, bool]: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def get_by_request_id(
        self,
        *,
        principal_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str | None = None,
    ) -> TaskRecord | None: ...

    def list_incomplete(
        self, *, limit: int = 100, after_task_id: str | None = None
    ) -> list[TaskRecord]: ...

    def list_children(
        self,
        *,
        principal_id: str,
        parent_task_id: str,
        after_task_id: str = "",
        limit: int = 100,
    ) -> list[TaskRecord]: ...

    def list_recent(self, *, limit: int = 100) -> list[TaskRecord]: ...

    def list_for_principal(self, *, principal_id: str, limit: int = 5000) -> list[TaskRecord]: ...


class InMemoryTaskRepository:
    """Copy-on-read repository useful for tests and short-lived local sessions."""

    _RECOVERY_IGNORED = frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
            TaskStatus.INTERRUPTED,
            TaskStatus.BLOCKED,
        }
    )

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._requests: dict[tuple[str, str, str], tuple[str, str | None]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _request_key(task: TaskRecord) -> tuple[str, str, str] | None:
        principal_id = task.authorization.principal_id if task.authorization is not None else None
        if principal_id is None:
            return None
        return principal_id, task.session_id, task.request_id

    def create_or_get(
        self, task: TaskRecord, *, request_fingerprint: str | None = None
    ) -> tuple[TaskRecord, bool]:
        if task.version != 0:
            raise ConcurrentTaskUpdateError("new task must start at version zero")
        key = self._request_key(task)
        with self._lock:
            existing_request = self._requests.get(key) if key is not None else None
            if existing_request is not None:
                existing_id, existing_fingerprint = existing_request
                if (
                    existing_fingerprint is not None
                    and request_fingerprint is not None
                    and existing_fingerprint != request_fingerprint
                ):
                    raise DuplicateTaskRequestError(
                        "request ID was reused with different task content"
                    )
                existing = self._tasks.get(existing_id)
                if existing is not None:
                    return TaskRecord.from_dict(existing), False
            if task.task_id in self._tasks:
                raise ConcurrentTaskUpdateError("task already exists")
            stored = self.save(task)
            if key is not None:
                self._requests[key] = (stored.task_id, request_fingerprint)
            return stored, True

    def save(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            existing = self._tasks.get(task.task_id)
            key = self._request_key(task)
            if existing is None:
                if task.version != 0:
                    raise ConcurrentTaskUpdateError("task does not exist at the supplied version")
                if key is not None:
                    existing_request = self._requests.get(key)
                    if existing_request is not None and existing_request[0] != task.task_id:
                        raise DuplicateTaskRequestError(
                            "request ID is already associated with a task in this session"
                        )
                task.version = 1
            else:
                stored_version = int(existing["version"])
                if task.version != stored_version:
                    raise ConcurrentTaskUpdateError(
                        f"stale task version {task.version}; current version is {stored_version}"
                    )
                task.version = stored_version + 1
            task.updated_at = utc_now()
            stored = task.to_dict()
            self._tasks[task.task_id] = stored
            if existing is None and key is not None:
                self._requests[key] = (task.task_id, None)
            return TaskRecord.from_dict(stored)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            stored = self._tasks.get(task_id)
            return TaskRecord.from_dict(stored) if stored is not None else None

    def get_by_request_id(
        self,
        *,
        principal_id: str,
        session_id: str,
        request_id: str,
        request_fingerprint: str | None = None,
    ) -> TaskRecord | None:
        with self._lock:
            mapping = self._requests.get((principal_id, session_id, request_id))
            if mapping is None:
                return None
            task_id, existing_fingerprint = mapping
            if (
                existing_fingerprint is not None
                and request_fingerprint is not None
                and existing_fingerprint != request_fingerprint
            ):
                raise DuplicateTaskRequestError("request ID was reused with different task content")
            stored = self._tasks.get(task_id)
            return TaskRecord.from_dict(stored) if stored is not None else None

    def list_incomplete(
        self, *, limit: int = 100, after_task_id: str | None = None
    ) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            tasks = []
            for value in self._tasks.values():
                task_id = str(value["task_id"])
                if TaskStatus(value["status"]) not in self._RECOVERY_IGNORED and (
                    after_task_id is None or task_id > after_task_id
                ):
                    tasks.append(TaskRecord.from_dict(value))
        tasks.sort(key=lambda task: task.task_id)
        return tasks[:limit]

    def list_recent(self, *, limit: int = 100) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            tasks = [TaskRecord.from_dict(value) for value in self._tasks.values()]
        tasks.sort(key=lambda task: task.updated_at, reverse=True)
        return tasks[:limit]

    def list_for_principal(self, *, principal_id: str, limit: int = 5000) -> list[TaskRecord]:
        if not principal_id.strip() or limit < 1:
            raise ValueError("principal_id and positive task-history limit are required")
        with self._lock:
            tasks = [
                TaskRecord.from_dict(value)
                for value in self._tasks.values()
                if value.get("authorization") is not None
                and value["authorization"].get("principal_id") == principal_id
            ]
        tasks.sort(key=lambda task: task.updated_at, reverse=True)
        return tasks[:limit]

    def list_children(
        self,
        *,
        principal_id: str,
        parent_task_id: str,
        after_task_id: str = "",
        limit: int = 100,
    ) -> list[TaskRecord]:
        if not principal_id or limit < 1:
            raise ValueError("principal and positive limit required")
        with self._lock:
            rows = [
                TaskRecord.from_dict(value)
                for key, value in self._tasks.items()
                if key > after_task_id
                and value.get("parent_task_id") == parent_task_id
                and value.get("authorization") is not None
                and value["authorization"].get("principal_id") == principal_id
            ]
        return sorted(rows, key=lambda row: row.task_id)[:limit]
