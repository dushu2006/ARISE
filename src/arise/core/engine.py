"""Asynchronous task orchestration above the deterministic action runtime.

The engine owns task scheduling and orchestration, not policy. A planner may
only return typed proposals; task-owned authority is injected here and the
runtime still validates every action before an adapter can execute it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    RiskLevel,
    TrustLevel,
    canonical_json,
    thaw_json,
    utc_now,
    validate_safe_token,
)
from arise.core.events import EventEnvelope, EventSeverity, EventStore
from arise.core.models import (
    ConfirmationRequest,
    PlanStep,
    TaskPlan,
    UserRequest,
)
from arise.core.planning import InvalidPlan, PlannerUnavailable, TaskPlanner
from arise.core.policy import ApprovalGrant, PolicyDecisionKind, PolicyEngine
from arise.core.ports import ToolNotFoundError, ToolRegistry
from arise.core.redaction import DEFAULT_REDACTOR, SecretRedactor
from arise.core.runtime import ActionRunResult, AgentRuntime
from arise.core.tasks import (
    ActionStep,
    DuplicateActionError,
    TaskNotFoundError,
    TaskRecord,
    TaskRepository,
    TaskStatus,
)


class TaskQueueFull(RuntimeError):
    pass


class TaskInputNotAccepted(RuntimeError):
    pass


class UnavailablePlanner:
    """Explicit no-provider implementation used when model access is disabled."""

    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan:
        del request, task
        raise PlannerUnavailable("No planning model is configured or available.")


@dataclass(frozen=True, slots=True)
class _ApprovalContinuation:
    task_id: str
    ordered: list[tuple[PlanStep, ActionContract]]
    start_index: int
    grant: ApprovalGrant


@dataclass(frozen=True, slots=True)
class TaskEngineConfig:
    max_concurrent_tasks: int = 2
    max_queued_tasks: int = 64
    task_timeout_seconds: float = 900.0
    resource_wait_timeout_seconds: float = 15.0
    max_plan_steps: int = 64
    confirmation_ttl_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.max_concurrent_tasks < 1 or self.max_queued_tasks < 1:
            raise ValueError("task concurrency and queue limits must be positive")
        if self.task_timeout_seconds <= 0 or self.resource_wait_timeout_seconds < 0:
            raise ValueError("task timeouts must be non-negative and bounded")
        if not 1 <= self.max_plan_steps <= 256:
            raise ValueError("max_plan_steps must be between 1 and 256")
        if self.confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")


class TaskEngine:
    """Bounded worker queue, task lifecycle, plan validation, and approval flow."""

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        events: EventStore,
        runtime: AgentRuntime,
        tools: ToolRegistry,
        policy: PolicyEngine,
        planner: TaskPlanner | None = None,
        config: TaskEngineConfig | None = None,
        capability_grants: Callable[[str], frozenset[str]] | None = None,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
    ) -> None:
        self.tasks = tasks
        self.events = events
        self.runtime = runtime
        self.tools = tools
        self.policy = policy
        self.planner = planner or UnavailablePlanner()
        self.config = config or TaskEngineConfig()
        self.capability_grants = capability_grants or (lambda _principal: frozenset())
        self.redactor = redactor
        self._queue: asyncio.Queue[tuple[str, UserRequest] | _ApprovalContinuation | None] = (
            asyncio.Queue(maxsize=self.config.max_queued_tasks)
        )
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}
        self._requests: dict[str, UserRequest] = {}
        self._deadlines: dict[str, float] = {}
        self._paused_deadlines: dict[str, float] = {}
        self._clarification_tasks: set[str] = set()
        self._plans: dict[str, tuple[TaskPlan, list[tuple[PlanStep, ActionContract]]]] = {}
        self._actions: dict[tuple[str, str], ActionContract] = {}
        self._pending_confirmations: dict[str, tuple[str, str, str]] = {}
        self._confirmation_requests: dict[str, ConfirmationRequest] = {}
        self._principal_confirmations: dict[tuple[str, str], str] = {}
        self._closed = False

    @property
    def queued_count(self) -> int:
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("task engine has been closed")
        if self._workers:
            return
        await self._recover_incomplete()
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"arise-task-worker-{index}")
            for index in range(self.config.max_concurrent_tasks)
        ]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active = list(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        for _ in self._workers:
            await self._queue.put(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(
        self,
        request: UserRequest,
        *,
        principal_id: str,
        session_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> TaskRecord:
        if self._closed:
            raise RuntimeError("task engine is not accepting requests")
        if not self._workers:
            await self.start()
        principal = principal_id.strip()
        if not principal:
            raise ValueError("an authenticated local principal is required")
        chosen_session = session_id or request.session_id
        safe_request = request.model_copy(
            update={
                "text": self.redactor.redact(request.text),
                "session_id": chosen_session,
            }
        )
        fingerprint_payload = safe_request.model_dump(
            mode="json",
            exclude={"request_id", "session_id", "received_at"},
        )
        if parent_task_id is not None:
            fingerprint_payload["parent_task_id"] = parent_task_id
        request_fingerprint = hashlib.sha256(
            canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        existing = self.tasks.get_by_request_id(
            principal_id=principal,
            session_id=chosen_session,
            request_id=request.request_id,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            if existing.status is TaskStatus.QUEUED and existing.task_id not in self._requests:
                self._schedule_accepted(existing, safe_request)
            return existing
        if parent_task_id is not None:
            validate_safe_token(parent_task_id, "parent_task_id")
            parent = self.tasks.get(parent_task_id)
            if parent is None:
                raise TaskNotFoundError(parent_task_id)
            parent_owner = (
                parent.authorization.principal_id if parent.authorization is not None else None
            )
            if parent_owner != principal:
                raise PermissionError("parent task does not belong to the authenticated principal")
            if parent.session_id != chosen_session:
                raise ValueError("child task must remain in the parent conversation session")
            if parent.status in {
                TaskStatus.COMPLETED,
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
                TaskStatus.UNKNOWN,
                TaskStatus.INTERRUPTED,
                TaskStatus.BLOCKED,
                TaskStatus.PARTIALLY_COMPLETED,
            }:
                raise ValueError("a terminal parent task cannot accept new child tasks")
        if self._queue.full():
            raise TaskQueueFull("the task queue is full; retry after current work completes")
        authority = AuthorizationContext(
            principal_id=principal,
            user_intent_id=request.request_id,
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=self.capability_grants(principal),
        )
        task = TaskRecord.new(
            safe_request.text,
            authorization=authority,
            request_id=request.request_id,
            session_id=chosen_session,
            parent_task_id=parent_task_id,
            correlation_id=request.request_id,
        )
        task.transition_to(TaskStatus.QUEUED, reason="Accepted and queued for planning.")
        task, _ = self.tasks.create_or_get(task, request_fingerprint=request_fingerprint)
        if task.status is TaskStatus.QUEUED and task.task_id not in self._requests:
            self._schedule_accepted(task, safe_request)
        return self.tasks.get(task.task_id) or task

    def _schedule_accepted(self, task: TaskRecord, request: UserRequest) -> None:
        if self._queue.full():
            raise TaskQueueFull("the task queue is full; retry after current work completes")
        self._emit(
            "TASK_ACCEPTED",
            task,
            {"source": request.source.value, "queue_depth": self._queue.qsize()},
        )
        self._requests[task.task_id] = request
        self._deadlines[task.task_id] = (
            asyncio.get_running_loop().time() + self.config.task_timeout_seconds
        )
        self._queue.put_nowait((task.task_id, request))

    async def provide_input(
        self,
        task_id: str,
        text: str,
        *,
        principal_id: str,
    ) -> TaskRecord:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        owner = task.authorization.principal_id if task.authorization is not None else None
        if owner != principal_id:
            raise PermissionError("task does not belong to the authenticated principal")
        if (
            task.status is not TaskStatus.REQUIRES_USER_INPUT
            or task_id not in self._clarification_tasks
        ):
            raise TaskInputNotAccepted("task is not awaiting an in-memory clarification")
        if self._queue.full():
            raise TaskQueueFull("the task queue is full; retry after current work completes")
        original = self._requests.get(task_id)
        if original is None:
            raise TaskInputNotAccepted("clarification context is unavailable; resubmit the request")
        answer = text.strip()
        if not answer or len(answer) > 16_384:
            raise ValueError("clarification must contain 1 to 16384 characters")
        safe_answer = self.redactor.redact(answer)
        request_id = str(uuid.uuid4())
        combined = f"{original.text}\n\nUser clarification: {safe_answer}"
        request = UserRequest(
            request_id=request_id,
            session_id=task.session_id,
            text=combined,
            source=original.source,
            locale=original.locale,
        )
        task.request_id = request_id
        if task.authorization is not None:
            task.authorization = replace(task.authorization, user_intent_id=request_id)
        task.goal = self.redactor.redact(combined)
        task.transition_to(
            TaskStatus.QUEUED, reason="User clarification received; queued for replanning."
        )
        task = self.tasks.save(task)
        self._requests[task_id] = request
        self._clarification_tasks.discard(task_id)
        self._paused_deadlines.pop(task_id, None)
        self._deadlines[task_id] = (
            asyncio.get_running_loop().time() + self.config.task_timeout_seconds
        )
        self._emit("USER_CLARIFICATION_RECEIVED", task, {"character_count": len(answer)})
        self._queue.put_nowait((task_id, request))
        return self.tasks.get(task_id) or task

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.tasks.get(task_id)

    def can_accept_input(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        return (
            task is not None
            and task.status is TaskStatus.REQUIRES_USER_INPUT
            and task_id in self._clarification_tasks
        )

    def list_tasks(self, *, principal_id: str, limit: int = 100) -> list[TaskRecord]:
        return self.tasks.list_for_principal(principal_id=principal_id, limit=limit)

    def pending_confirmations(self, task_id: str) -> tuple[ConfirmationRequest, ...]:
        now = utc_now()
        expired = [
            confirmation_id
            for confirmation_id, confirmation in self._confirmation_requests.items()
            if confirmation.expires_at <= now
        ]
        for confirmation_id in expired:
            pending = self._pending_confirmations.pop(confirmation_id, None)
            self._confirmation_requests.pop(confirmation_id, None)
            if pending is not None:
                task_id, action_id, _ = pending
                self._principal_confirmations.pop((task_id, action_id), None)
                task = self.tasks.get(task_id)
                if task is not None and task.status is TaskStatus.WAITING_USER:
                    task.transition_to(
                        TaskStatus.REQUIRES_USER_INPUT,
                        reason="Action approval expired; provide a fresh instruction to replan.",
                    )
                    task = self.tasks.save(task)
                    self._clarification_tasks.add(task_id)
                    self._emit(
                        "APPROVAL_EXPIRED",
                        task,
                        {"action_id": action_id},
                        EventSeverity.WARNING,
                    )
        return tuple(
            confirmation
            for confirmation_id, confirmation in self._confirmation_requests.items()
            if confirmation.task_id == task_id and confirmation_id in self._pending_confirmations
        )

    def _require_pending_confirmation(
        self, *, task_id: str, confirmation_id: str, approved_by: str
    ) -> tuple[tuple[str, str, str], ConfirmationRequest]:
        self.pending_confirmations(task_id)
        pending = self._pending_confirmations.get(confirmation_id)
        confirmation = self._confirmation_requests.get(confirmation_id)
        if (
            pending is None
            or confirmation is None
            or confirmation.expires_at <= utc_now()
            or pending[0] != task_id
            or pending[2] != approved_by
        ):
            raise PermissionError("confirmation is invalid, expired, or out of scope")
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.status is not TaskStatus.WAITING_USER:
            raise PermissionError("task is no longer waiting for approval")
        return pending, confirmation

    async def approve(
        self,
        *,
        task_id: str,
        confirmation_id: str,
        approved_by: str,
    ) -> TaskRecord:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        expected_owner = task.authorization.principal_id if task.authorization is not None else None
        if not expected_owner or expected_owner != approved_by:
            raise PermissionError("approval principal does not own this task")
        if not self._workers:
            await self.start()
        self._require_pending_confirmation(
            task_id=task_id, confirmation_id=confirmation_id, approved_by=approved_by
        )

        # A task can be durably marked WAITING_USER just before its worker has
        # returned from the final bookkeeping for that turn. Let that processor
        # and the worker's active-slot cleanup finish before queuing continuation.
        current = asyncio.current_task()
        while (active := self._active.get(task_id)) is not None:
            if active is current:
                raise RuntimeError("a task cannot approve itself while it is active")
            if active.done():
                await asyncio.sleep(0)
            else:
                await asyncio.gather(active, return_exceptions=True)
                await asyncio.sleep(0)

        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        pending, _ = self._require_pending_confirmation(
            task_id=task_id, confirmation_id=confirmation_id, approved_by=approved_by
        )
        if self._queue.full():
            raise TaskQueueFull("the task queue is full; approval remains pending")
        _, action_id, _ = pending
        action = self._actions.get((task_id, action_id))
        plan_info = self._plans.get(task_id)
        if action is None or plan_info is None:
            raise PlannerUnavailable("the pending plan is not available; replan the task")
        ordered = plan_info[1]
        start_index = next(
            (
                index
                for index, (step, _) in enumerate(ordered)
                if step.action.action_id == action_id
            ),
            None,
        )
        if start_index is None:
            raise InvalidPlan("approved action no longer belongs to the in-memory plan")
        tool = self.tools.get(action.tool_name)
        grant: ApprovalGrant = self.policy.issue_approval(
            action,
            tool.spec,
            approved_by=approved_by,
            ttl_seconds=self.config.confirmation_ttl_seconds,
        )
        continuation = _ApprovalContinuation(task_id, ordered, start_index, grant)
        self._queue.put_nowait(continuation)
        self._pending_confirmations.pop(confirmation_id, None)
        self._confirmation_requests.pop(confirmation_id, None)
        self._principal_confirmations.pop((task_id, action_id), None)
        remaining = self._paused_deadlines.pop(task_id, self.config.task_timeout_seconds)
        self._deadlines[task_id] = asyncio.get_running_loop().time() + max(0.0, remaining)
        return self.tasks.get(task_id) or task

    async def _resume_approved(self, continuation: _ApprovalContinuation) -> None:
        try:
            await self._continue_plan(
                continuation.task_id,
                continuation.ordered,
                start_index=continuation.start_index,
                approval=continuation.grant,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._handle_action_exception(
                continuation.task_id,
                continuation.ordered[continuation.start_index][1],
                f"Approved task continuation failed ({type(exc).__name__}).",
            )

    async def cancel(self, task_id: str, *, principal_id: str) -> TaskRecord:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        owner = task.authorization.principal_id if task.authorization is not None else None
        if owner and owner != principal_id:
            raise PermissionError("task does not belong to the authenticated principal")
        terminal_states = {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
            TaskStatus.INTERRUPTED,
            TaskStatus.BLOCKED,
            TaskStatus.PARTIALLY_COMPLETED,
        }
        for child in self.tasks.list_for_principal(principal_id=principal_id, limit=5000):
            if child.parent_task_id == task_id and child.status not in terminal_states:
                await self.cancel(child.task_id, principal_id=principal_id)
        active = self._active.get(task_id)
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        latest = self.tasks.get(task_id)
        if latest is None:
            raise TaskNotFoundError(task_id)
        if latest.status not in {
            TaskStatus.CANCELLED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }:
            latest.transition_to(
                TaskStatus.CANCELLED, reason="Cancelled by the authenticated user."
            )
            latest = self.tasks.save(latest)
            self._emit("TASK_CANCELLED", latest, {"by_user": True})
        self._drop_confirmations(task_id)
        return latest

    async def _worker(self, index: int) -> None:
        del index
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                task_id = item.task_id if isinstance(item, _ApprovalContinuation) else item[0]
                task = self.tasks.get(task_id)
                if task is None or task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
                    continue
                if isinstance(item, _ApprovalContinuation):
                    processor = asyncio.create_task(
                        self._resume_approved(item), name=f"arise-approved-task-{task_id}"
                    )
                else:
                    _, request = item
                    if task.status is not TaskStatus.QUEUED:
                        continue
                    processor = asyncio.create_task(
                        self._process(task_id, request), name=f"arise-task-{task_id}"
                    )
                self._active[task_id] = processor
                try:
                    await processor
                except asyncio.CancelledError:
                    # The processing coroutine persists CANCELLED before dispatch
                    # or UNKNOWN after dispatch. Never overwrite an unknown effect.
                    current = self.tasks.get(task_id)
                    if current is not None and current.status not in {
                        TaskStatus.CANCELLED,
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.UNKNOWN,
                    }:
                        current.transition_to(
                            TaskStatus.CANCELLED, reason="Task cancelled before action dispatch."
                        )
                        self.tasks.save(current)
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
                finally:
                    self._active.pop(task_id, None)
            finally:
                self._queue.task_done()

    async def _process(self, task_id: str, request: UserRequest) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status is not TaskStatus.QUEUED:
            return
        try:
            task.transition_to(TaskStatus.UNDERSTANDING, reason="Understanding the user request.")
            task = self.tasks.save(task)
            self._emit("TASK_UNDERSTANDING", task, {})
            task.transition_to(TaskStatus.PLANNING, reason="Preparing a bounded typed plan.")
            task = self.tasks.save(task)
            self._emit("PLAN_REQUESTED", task, {"planner": type(self.planner).__name__})
            deadline = self._deadlines.setdefault(
                task_id,
                asyncio.get_running_loop().time() + self.config.task_timeout_seconds,
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            plan = await asyncio.wait_for(
                self.planner.create_plan(request, task), timeout=remaining
            )
            if not isinstance(plan, TaskPlan):
                raise InvalidPlan("planner returned an untyped plan")
            if plan.needs_clarification:
                task = self.tasks.get(task_id) or task
                task.transition_to(
                    TaskStatus.REQUIRES_USER_INPUT,
                    reason=(
                        "The planner needs one clarification before it can create "
                        "an executable plan."
                    ),
                )
                task = self.tasks.save(task)
                self._clarification_tasks.add(task_id)
                self._requests[task_id] = request
                self._deadlines.pop(task_id, None)
                self._emit(
                    "CLARIFICATION_REQUIRED",
                    task,
                    {"question": self.redactor.redact(plan.clarification_question or "")},
                    EventSeverity.WARNING,
                )
                return
            ordered = self._validate_and_bind_plan(plan, task)
            task = self.tasks.get(task_id) or task
            task.transition_to(
                TaskStatus.READY, reason="A typed plan passed structural validation."
            )
            for _step, action in ordered:
                tool = self._get_tool(action.tool_name)
                if tool is None:
                    fingerprint = action.approval_fingerprint(action.risk, "unavailable")
                    risk = action.risk
                else:
                    fingerprint = self.policy.contract_fingerprint(action, tool.spec)
                    risk = RiskLevel(max(int(action.risk), int(tool.spec.minimum_risk)))
                task.add_step(
                    ActionStep(
                        action_id=action.action_id,
                        contract_fingerprint=fingerprint,
                        tool_name=action.tool_name,
                        risk=risk,
                    )
                )
                self._actions[(task_id, action.action_id)] = action
            task = self.tasks.save(task)
            self._plans[task_id] = (plan, ordered)
            self._emit(
                "PLAN_READY",
                task,
                {"plan_id": plan.plan_id, "step_count": len(ordered), "planner": plan.planner_id},
            )
            await self._continue_plan(task_id, ordered)
        except PlannerUnavailable as exc:
            self._deadlines.pop(task_id, None)
            current = self.tasks.get(task_id)
            if current is not None and current.status not in {
                TaskStatus.CANCELLED,
                TaskStatus.INTERRUPTED,
            }:
                reason = self.redactor.redact(str(exc))[:512] or "Planner is unavailable."
                current.transition_to(TaskStatus.REQUIRES_USER_INPUT, reason=reason)
                current = self.tasks.save(current)
                self._emit(
                    "PLANNER_UNAVAILABLE",
                    current,
                    {"capability": "model.planning", "reason": reason},
                    EventSeverity.WARNING,
                )
        except asyncio.CancelledError:
            current = self.tasks.get(task_id)
            if current is not None and current.status not in {
                TaskStatus.CANCELLED,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.UNKNOWN,
            }:
                current.transition_to(
                    TaskStatus.CANCELLED, reason="Cancelled before action dispatch."
                )
                current = self.tasks.save(current)
                self._emit("TASK_CANCELLED", current, {"by_user": True})
            raise
        except TimeoutError:
            self._fail_before_dispatch(task_id, "Planning exceeded its task deadline.")
        except (InvalidPlan, DuplicateActionError, ValueError) as exc:
            self._fail_before_dispatch(
                task_id, f"The generated plan was rejected ({type(exc).__name__})."
            )
        except Exception as exc:
            # Do not leak provider exception bodies, URLs, prompts, or credentials.
            self._fail_before_dispatch(task_id, f"Task planning failed ({type(exc).__name__}).")

    def _validate_and_bind_plan(
        self, plan: TaskPlan, task: TaskRecord
    ) -> list[tuple[PlanStep, ActionContract]]:
        if plan.task_id != task.task_id:
            raise InvalidPlan("planner returned a plan for a different task")
        if plan.goal != task.goal:
            raise InvalidPlan("planner returned a plan for a different goal")
        if len(plan.steps) > self.config.max_plan_steps:
            raise InvalidPlan("plan exceeds the configured step limit")
        if task.authorization is None:
            raise InvalidPlan("task has no trusted user authority")
        ordered_steps = self._topological_order(plan.steps)
        bound: list[tuple[PlanStep, ActionContract]] = []
        seen_actions: set[str] = set()
        for step in ordered_steps:
            if step.action.action_id in seen_actions:
                raise InvalidPlan("plan reuses an action identifier")
            seen_actions.add(step.action.action_id)
            action = step.action.to_domain(task_id=task.task_id, authority=task.authorization)
            tool = self._get_tool(action.tool_name)
            if tool is not None:
                # Trusted adapter metadata, not model output, supplies these
                # operational constraints and conservative idempotency.
                action = replace(
                    action,
                    idempotency=tool.spec.idempotency,
                    required_resources=tuple(
                        sorted(set(action.required_resources) | set(tool.spec.required_resources))
                    ),
                )
            bound.append((step, action))
        return bound

    @staticmethod
    def _topological_order(steps: tuple[PlanStep, ...]) -> list[PlanStep]:
        by_id = {step.step_id: step for step in steps}
        if len(by_id) != len(steps):
            raise InvalidPlan("plan contains duplicate step IDs")
        for step in steps:
            if step.step_id in step.depends_on:
                raise InvalidPlan("a plan step cannot depend on itself")
            if set(step.depends_on) - set(by_id):
                raise InvalidPlan("plan references a missing dependency")
        remaining = {step.step_id: set(step.depends_on) for step in steps}
        ordered: list[PlanStep] = []
        while remaining:
            ready = [step_id for step_id, dependencies in remaining.items() if not dependencies]
            if not ready:
                raise InvalidPlan("plan dependency graph contains a cycle")
            for step_id in ready:
                ordered.append(by_id[step_id])
                remaining.pop(step_id)
                for dependencies in remaining.values():
                    dependencies.discard(step_id)
        return ordered

    async def _continue_plan(
        self,
        task_id: str,
        ordered: list[tuple[PlanStep, ActionContract]],
        *,
        start_index: int = 0,
        approval: ApprovalGrant | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        for index in range(start_index, len(ordered)):
            step, action = ordered[index]
            final_action = index == len(ordered) - 1
            deadline = self._deadlines.setdefault(
                task_id, loop.time() + self.config.task_timeout_seconds
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._fail_before_dispatch(task_id, "Task execution exceeded its deadline.")
                return
            try:
                async with asyncio.timeout(remaining):
                    result: ActionRunResult = await self.runtime.execute_action(
                        action,
                        approval=approval if index == start_index else None,
                        final_action=final_action,
                        resource_wait_timeout=self.config.resource_wait_timeout_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._handle_action_exception(
                    task_id,
                    action,
                    f"Action orchestration failed ({type(exc).__name__}).",
                )
                return
            if result.policy_decision.kind is PolicyDecisionKind.CONFIRM:
                self._register_confirmation(task_id, action, result)
                return
            if (
                result.task_status
                in {
                    TaskStatus.COMPLETED,
                    TaskStatus.PARTIALLY_COMPLETED,
                }
                and result.step_status.value == "succeeded"
            ):
                continue
            self._deadlines.pop(task_id, None)
            return
        self._deadlines.pop(task_id, None)

    def _handle_action_exception(
        self,
        task_id: str,
        action: ActionContract,
        reason: str,
    ) -> None:
        task = self.tasks.get(task_id)
        self._deadlines.pop(task_id, None)
        if task is None or task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.UNKNOWN,
        }:
            return
        step = task.find_step(action.action_id)
        dispatched_or_uncertain = step is not None and (
            step.started_at is not None or step.status.value in {"running", "verifying", "unknown"}
        )
        if dispatched_or_uncertain:
            try:
                task.transition_to(
                    TaskStatus.UNKNOWN,
                    reason=(
                        "Runtime failed after action dispatch may have begun; "
                        "reconcile before retrying."
                    ),
                )
                task = self.tasks.save(task)
                self._emit(
                    "ACTION_ORCHESTRATION_UNKNOWN",
                    task,
                    {"action_id": action.action_id, "reason": self.redactor.redact(reason)},
                    EventSeverity.WARNING,
                )
                return
            except Exception:
                # Persistence failures cannot justify claiming success. If the
                # state write fails, the last durable step remains non-success.
                return
        self._fail_before_dispatch(task_id, self.redactor.redact(reason)[:512])

    def _register_confirmation(
        self, task_id: str, action: ActionContract, result: ActionRunResult
    ) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.authorization is None or task.authorization.principal_id is None:
            return
        action_id = action.action_id
        deadline = self._deadlines.pop(task_id, None)
        if deadline is not None:
            self._paused_deadlines[task_id] = max(0.0, deadline - asyncio.get_running_loop().time())
        key = (task_id, action_id)
        old_id = self._principal_confirmations.get(key)
        if old_id is not None:
            self._pending_confirmations.pop(old_id, None)
            self._confirmation_requests.pop(old_id, None)
            self._principal_confirmations.pop(key, None)
        tool = self.tools.get(action.tool_name)
        safe_parameters = canonical_json(self.redactor.redact_object(thaw_json(action.parameters)))
        summarizer = getattr(tool, "summarize_action", None)
        try:
            proposed_summary = summarizer(action) if callable(summarizer) else ""
        except Exception:
            proposed_summary = ""
        summary = (
            self.redactor.redact(proposed_summary).strip()
            if isinstance(proposed_summary, str)
            else ""
        )
        if not summary:
            summary = f"{action.tool_name} with parameters {safe_parameters}"
        if len(summary) > 512:
            self._paused_deadlines.pop(task_id, None)
            task.transition_to(
                TaskStatus.REQUIRES_USER_INPUT,
                reason=(
                    "Action details exceed the safe approval-display limit; "
                    "provide a narrower instruction."
                ),
            )
            task = self.tasks.save(task)
            self._clarification_tasks.add(task_id)
            self._emit(
                "APPROVAL_SUMMARY_TOO_LARGE",
                task,
                {"action_id": action_id},
                EventSeverity.WARNING,
            )
            return
        target = action.target
        target_summary = self.redactor.redact(
            (target.semantic_name or target.application or "Unspecified target")
            if target is not None
            else "Unspecified target"
        )[:256]
        confirmation_id = str(uuid.uuid4())
        principal = task.authorization.principal_id
        self._principal_confirmations[key] = confirmation_id
        self._pending_confirmations[confirmation_id] = (task_id, action_id, principal)
        expires_at = utc_now() + timedelta(seconds=self.config.confirmation_ttl_seconds)
        confirmation = ConfirmationRequest(
            confirmation_id=confirmation_id,
            task_id=task_id,
            action_id=action_id,
            risk=result.policy_decision.effective_risk,
            target_summary=target_summary,
            action_summary=summary,
            expires_at=expires_at,
            contract_fingerprint=self.policy.contract_fingerprint(action, tool.spec),
        )
        self._confirmation_requests[confirmation_id] = confirmation
        self._emit(
            "APPROVAL_PROMPT_CREATED",
            task,
            {
                "confirmation_id": confirmation_id,
                "action_id": action_id,
                "risk": int(result.policy_decision.effective_risk),
                "target_summary": target_summary,
                "action_summary": summary,
            },
            EventSeverity.WARNING,
        )

    def _get_tool(self, tool_name: str):
        try:
            return self.tools.get(tool_name)
        except ToolNotFoundError:
            return None

    def _fail_before_dispatch(self, task_id: str, reason: str) -> None:
        self._deadlines.pop(task_id, None)
        self._paused_deadlines.pop(task_id, None)
        task = self.tasks.get(task_id)
        if task is None or task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.UNKNOWN,
            TaskStatus.FAILED,
        }:
            return
        task.transition_to(TaskStatus.FAILED, reason=reason)
        task = self.tasks.save(task)
        self._emit("TASK_FAILED", task, {"reason": reason}, EventSeverity.ERROR)

    async def _recover_incomplete(self) -> None:
        page_size = 1000
        after_task_id: str | None = None
        while True:
            batch = self.tasks.list_incomplete(limit=page_size, after_task_id=after_task_id)
            if not batch:
                return
            after_task_id = batch[-1].task_id
            for task in batch:
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.CANCELLED,
                    TaskStatus.FAILED,
                    TaskStatus.UNKNOWN,
                    TaskStatus.INTERRUPTED,
                    TaskStatus.BLOCKED,
                }:
                    continue
                if task.status is TaskStatus.QUEUED:
                    request = UserRequest(
                        request_id=task.request_id,
                        session_id=task.session_id,
                        text=task.goal,
                    )
                    try:
                        self._requests[task.task_id] = request
                        self._queue.put_nowait((task.task_id, request))
                    except asyncio.QueueFull:
                        task.transition_to(
                            TaskStatus.REQUIRES_USER_INPUT,
                            reason="The recovery queue is full; resubmit this request when ready.",
                        )
                        self.tasks.save(task)
                    continue
                old_status = task.status
                if old_status in {
                    TaskStatus.WAITING_USER,
                    TaskStatus.WAITING_AUTH,
                    TaskStatus.REQUIRES_USER_INPUT,
                }:
                    target = TaskStatus.REQUIRES_USER_INPUT
                    reason = (
                        "Approval/clarification state was not restored after restart; "
                        "provide a fresh instruction to replan."
                    )
                else:
                    target = TaskStatus.INTERRUPTED
                    reason = (
                        "Runtime restarted mid-task; reconcile current state "
                        "before any further action."
                    )
                task.transition_to(target, reason=reason)
                task = self.tasks.save(task)
                if target is TaskStatus.REQUIRES_USER_INPUT:
                    self._requests[task.task_id] = UserRequest(
                        request_id=task.request_id,
                        session_id=task.session_id,
                        text=task.goal,
                    )
                    self._clarification_tasks.add(task.task_id)
                self._emit(
                    "TASK_RECOVERED_AS_NONRUNNABLE",
                    task,
                    {"previous_status": old_status.value},
                    EventSeverity.WARNING,
                )

    def _drop_confirmations(self, task_id: str) -> None:
        for confirmation_id, pending in list(self._pending_confirmations.items()):
            if pending[0] == task_id:
                self._pending_confirmations.pop(confirmation_id, None)
                self._confirmation_requests.pop(confirmation_id, None)
                self._principal_confirmations.pop((task_id, pending[1]), None)

    def _emit(
        self,
        event_type: str,
        task: TaskRecord,
        payload: Mapping[str, object],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        self.events.append(
            EventEnvelope(
                event_type=event_type,
                task_id=task.task_id,
                session_id=task.session_id,
                correlation_id=task.correlation_id,
                causation_id=task.request_id,
                source="task-engine",
                severity=severity,
                payload=payload,
            )
        )
