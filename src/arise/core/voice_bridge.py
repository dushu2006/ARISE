"""Narrow bridge from provider function calls into ARISE's existing task engine.

Gemini receives no shell, browser, desktop, or arbitrary tool authority. It may submit a user's
text as a typed ``UserRequest``; TaskEngine remains responsible for planning, policy, execution,
cancellation, and verification.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

from arise.core.contracts import thaw_json
from arise.core.engine import TaskEngine
from arise.core.intent import IntentClassifier, IntentKind
from arise.core.models import RequestSource, UserRequest
from arise.core.tasks import TaskRecord, TaskStatus
from arise.core.voice import LiveToolCall

_TERMINAL_STATES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.UNKNOWN,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.INTERRUPTED,
    }
)
_TERMINAL_WORDS = frozenset(state.value for state in _TERMINAL_STATES)
_MAX_TRACKED_TASKS = 128
_MAX_TRACKED_SESSIONS = 32
_REQUEST_TEXT_WORDS = re.compile(r"\w+")


class VoiceTaskPort(Protocol):
    async def submit(self, request: UserRequest, *, principal_id: str) -> TaskRecord: ...

    async def get(self, task_id: str, *, principal_id: str) -> TaskRecord | None: ...

    async def cancel(self, task_id: str, *, principal_id: str) -> TaskRecord: ...

    def watch(
        self, task_id: str, *, principal_id: str, poll_interval_seconds: float
    ) -> AsyncIterator[TaskRecord]: ...


class TaskEngineVoiceAdapter:
    """Voice-facing facade over TaskEngine; no alternate execution path is introduced."""

    def __init__(self, engine: TaskEngine) -> None:
        self.engine = engine

    async def submit(self, request: UserRequest, *, principal_id: str) -> TaskRecord:
        return await self.engine.submit(
            request, principal_id=principal_id, session_id=request.session_id
        )

    async def get(self, task_id: str, *, principal_id: str) -> TaskRecord | None:
        task = self.engine.tasks.get(task_id)
        if task is None:
            return None
        owner = task.authorization.principal_id if task.authorization is not None else None
        if owner != principal_id:
            return None
        return task

    async def cancel(self, task_id: str, *, principal_id: str) -> TaskRecord:
        return await self.engine.cancel(task_id, principal_id=principal_id)

    async def watch(
        self,
        task_id: str,
        *,
        principal_id: str,
        poll_interval_seconds: float,
    ) -> AsyncIterator[TaskRecord]:
        previous_signature: tuple[object, ...] | None = None
        while True:
            task = await self.get(task_id, principal_id=principal_id)
            if task is None:
                return
            signature = (
                task.status,
                tuple((step.action_id, step.status) for step in task.steps),
            )
            if signature != previous_signature:
                yield task
                previous_signature = signature
            if task.status in _TERMINAL_STATES:
                return
            await asyncio.sleep(poll_interval_seconds)


class VoiceConversationBridge:
    """Validates a tiny provider tool vocabulary and returns runtime-derived task status."""

    def __init__(
        self,
        tasks: VoiceTaskPort,
        *,
        informational_responder: Callable[[str, str, str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.tasks = tasks
        self.informational_responder = informational_responder
        self._intent_classifier = IntentClassifier()
        self._session_tasks: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
        self._task_session: OrderedDict[str, str] = OrderedDict()
        self._task_owner: dict[str, str] = {}

    def immediate_acknowledgement(self, user_text: str) -> str:
        """Return a deterministic local acknowledgement without requiring a cloud round-trip."""

        cleaned = (user_text or "").strip()
        if not cleaned:
            return "ARISE is listening."
        classification = self._intent_classifier.classify(cleaned)
        if classification.kind is IntentKind.CANCELLATION:
            return "ARISE received your cancellation request."
        if classification.kind is IntentKind.CLARIFICATION:
            return "Could you clarify what you would like ARISE to do?"
        if classification.may_require_runtime_task and classification.confidence >= 0.75:
            return "ARISE accepted the task and is working on it."
        return "ARISE heard your request."

    async def process_utterance(
        self,
        user_text: str,
        *,
        principal_id: str,
        session_id: str,
        locale: str = "en",
    ) -> dict[str, Any]:
        """Route a transcribed spoken utterance through IntentClassifier without bypassing policy.

        - Commands and multi-step tasks submit through ``VoiceTaskPort`` (``TaskEngine``).
        - Questions and casual conversation return an informational answer without task admission.
        - Ambiguous utterances request clarification without task admission.
        """

        cleaned = (user_text or "").strip()
        if not cleaned or len(cleaned) > 16_384:
            return self._ask_user({"question": "Could you repeat what you would like ARISE to do?"})
        classification = self._intent_classifier.classify(cleaned)
        if classification.kind is IntentKind.CANCELLATION:
            latest_id = self._latest_task_id(session_id, principal_id=principal_id)
            if latest_id is None:
                return {
                    "status": "not_found",
                    "intent": classification.kind.value,
                    "verified": False,
                    "summary": "There is no active voice task in this session to cancel.",
                    "spoken_response": "There is no active voice task in this session to cancel.",
                }
            return await self._cancel_task(
                {"task_id": latest_id},
                principal_id=principal_id,
                session_id=session_id,
                user_text=cleaned,
            )
        if classification.kind is IntentKind.STATUS_REQUEST:
            return await self._get_status({}, principal_id=principal_id, session_id=session_id)
        if classification.kind is IntentKind.CLARIFICATION:
            return self._ask_user(
                {"question": "What would you like ARISE to know or do? No task was created."}
            )
        if classification.may_require_runtime_task:
            if classification.confidence < 0.75:
                return self._ask_user(
                    {
                        "question": (
                            "I am not sure whether you want an action. "
                            "Please rephrase as a direct command or question; no task was created."
                        )
                    }
                )
            call = LiveToolCall(
                call_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"arise-voice-utterance:{session_id}:{cleaned}",
                    )
                ),
                name="execute_task",
                arguments={"text": cleaned},
            )
            result = await self._execute_task(
                call,
                {"text": cleaned},
                principal_id=principal_id,
                session_id=session_id,
                locale=locale,
                user_text=cleaned,
            )
            result["intent"] = classification.kind.value
            return result
        answer: str | None = None
        if self.informational_responder is not None:
            try:
                answer = await self.informational_responder(cleaned, session_id, locale)
            except Exception:
                answer = None
        spoken = (
            answer.strip()
            if isinstance(answer, str) and answer.strip()
            else "No informational model is configured; no task was created."
        )
        return {
            "status": "answered",
            "intent": classification.kind.value,
            "verified": False,
            "task_id": None,
            "summary": spoken,
            "spoken_response": spoken,
        }

    async def handle_tool_call(
        self,
        call: LiveToolCall,
        *,
        principal_id: str,
        session_id: str,
        locale: str = "en",
        user_text: str | None = None,
    ) -> dict[str, Any]:
        arguments = thaw_json(call.arguments)
        if not isinstance(arguments, dict):
            return self._invalid_arguments()
        if call.name == "execute_task":
            return await self._execute_task(
                call,
                arguments,
                principal_id=principal_id,
                session_id=session_id,
                locale=locale,
                user_text=user_text,
            )
        if call.name in {"ask_user", "request_clarification"}:
            return self._ask_user(arguments)
        if call.name in {"get_task_status", "report_status"}:
            return await self._get_status(
                arguments, principal_id=principal_id, session_id=session_id
            )
        if call.name == "cancel_task":
            return await self._cancel_task(
                arguments,
                principal_id=principal_id,
                session_id=session_id,
                user_text=user_text,
            )
        return self._not_authorized("ARISE cannot authorize that operation.")

    async def watch_task(
        self,
        task_id: str,
        *,
        principal_id: str,
        session_id: str,
        poll_interval_seconds: float = 0.5,
    ) -> AsyncIterator[dict[str, Any]]:
        # Waking again creates a new voice-session ID. The task adapter rechecks principal
        # ownership on every poll, so the current session need not match the admission session.
        if (
            task_id not in self._task_session
            or not session_id.strip()
            or len(session_id) > 128
            or not 0.1 <= poll_interval_seconds <= 10
        ):
            return
        async for task in self.tasks.watch(
            task_id,
            principal_id=principal_id,
            poll_interval_seconds=poll_interval_seconds,
        ):
            yield self._status_result(task)

    async def _execute_task(
        self,
        call: LiveToolCall,
        arguments: Mapping[str, Any],
        *,
        principal_id: str,
        session_id: str,
        locale: str,
        user_text: str | None,
    ) -> dict[str, Any]:
        if set(arguments) != {"text"}:
            return self._invalid_arguments()
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 16_384:
            return self._invalid_arguments()
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 16_384:
            return self._not_authorized("ARISE could not verify a spoken task request.")
        classification = self._intent_classifier.classify(user_text)
        if not classification.may_require_runtime_task or classification.confidence < 0.75:
            return self._not_authorized(
                "ARISE did not submit a task because the spoken intent was unclear."
            )
        if _normalize_request_text(text) != _normalize_request_text(user_text):
            return self._not_authorized(
                "ARISE did not submit a task that differs from what you said."
            )
        canonical_text = user_text.strip()
        try:
            request_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"arise-voice:{session_id}:{call.call_id}")
            )
            request = UserRequest(
                request_id=request_id,
                session_id=session_id,
                text=canonical_text,
                source=RequestSource.VOICE,
                locale=locale,
            )
            task = await self.tasks.submit(request, principal_id=principal_id)
        except Exception:
            return {
                "status": "unavailable",
                "verified": False,
                "summary": "ARISE could not accept that task.",
            }
        self._remember(session_id, task.task_id, principal_id=principal_id)
        return {
            "status": "accepted",
            "task_id": task.task_id,
            "state": task.status.value,
            "verified": False,
            "acknowledgement": self.immediate_acknowledgement(canonical_text),
            "local_acknowledgement": True,
        }

    def _ask_user(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"question"}:
            return self._invalid_arguments()
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 2048:
            return self._invalid_arguments()
        return {
            "status": "waiting_for_user",
            "question": question.strip(),
            "spoken_response": question.strip(),
            "verified": False,
        }

    async def _get_status(
        self,
        arguments: Mapping[str, Any],
        *,
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if set(arguments) - {"task_id"}:
            return self._invalid_arguments()
        task_id = arguments.get("task_id")
        if task_id is None:
            task_id = self._latest_task_id(session_id, principal_id=principal_id)
        if not isinstance(task_id, str) or not self._was_submitted(task_id):
            return self._not_found()
        task = await self.tasks.get(task_id, principal_id=principal_id)
        if task is None:
            return self._not_found()
        return self._status_result(task)

    async def _cancel_task(
        self,
        arguments: Mapping[str, Any],
        *,
        principal_id: str,
        session_id: str,
        user_text: str | None,
    ) -> dict[str, Any]:
        if set(arguments) != {"task_id"}:
            return self._invalid_arguments()
        if (
            not isinstance(user_text, str)
            or not user_text.strip()
            or len(user_text) > 16_384
            or self._intent_classifier.classify(user_text).kind is not IntentKind.CANCELLATION
        ):
            return self._not_authorized(
                "ARISE did not cancel the task without an explicit request."
            )
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not self._was_submitted(task_id):
            return self._not_found()
        try:
            task = await self.tasks.cancel(task_id, principal_id=principal_id)
        except Exception:
            return self._not_authorized("ARISE could not confirm the cancellation result.")
        self._remember(session_id, task.task_id, principal_id=principal_id)
        return self._status_result(task, cancellation_requested=True)

    @staticmethod
    def _status_result(task: TaskRecord, *, cancellation_requested: bool = False) -> dict[str, Any]:
        state = task.status
        verified = state is TaskStatus.COMPLETED
        if verified:
            summary = "ARISE reports completion after its task verifier passed."
        elif state is TaskStatus.UNKNOWN:
            summary = "The outcome is unknown; ARISE has not confirmed success or rollback."
        elif state is TaskStatus.CANCELLED:
            summary = "The task was cancelled before verified completion."
        elif state is TaskStatus.FAILED:
            summary = "The task failed and was not verified as complete."
        elif state is TaskStatus.BLOCKED:
            summary = "The task is blocked and was not completed."
        elif state is TaskStatus.PARTIALLY_COMPLETED:
            summary = "Some task steps completed, but the whole task is not verified complete."
        elif state is TaskStatus.INTERRUPTED:
            summary = "The task was interrupted and is not verified complete."
        elif state in {TaskStatus.WAITING_USER, TaskStatus.WAITING_AUTH}:
            summary = "The task is waiting for user input or authorization."
        elif state is TaskStatus.REQUIRES_USER_INPUT:
            summary = "The task needs clarification before it can continue."
        elif state is TaskStatus.QUEUED:
            summary = "ARISE queued your task."
        elif state in {TaskStatus.UNDERSTANDING, TaskStatus.PLANNING, TaskStatus.READY}:
            summary = "ARISE is planning your task."
        elif state is TaskStatus.VERIFYING:
            summary = "ARISE is verifying the task result."
        else:
            summary = "ARISE is still working on the task."
        result: dict[str, Any] = {
            "status": state.value,
            "task_id": task.task_id,
            "state": state.value,
            "verified": verified,
            "summary": summary,
            "completed_steps": sum(step.status.value == "succeeded" for step in task.steps),
            "total_steps": len(task.steps),
        }
        if cancellation_requested:
            result["cancellation_requested"] = state not in _TERMINAL_STATES
        return result

    def _remember(self, session_id: str, task_id: str, *, principal_id: str) -> None:
        session_tasks = self._session_tasks.setdefault(session_id, OrderedDict())
        session_tasks.pop(task_id, None)
        session_tasks[task_id] = None
        self._session_tasks.move_to_end(session_id)
        self._task_session.pop(task_id, None)
        self._task_session[task_id] = session_id
        self._task_owner[task_id] = principal_id
        while len(session_tasks) > _MAX_TRACKED_TASKS:
            evicted, _ = session_tasks.popitem(last=False)
            self._task_session.pop(evicted, None)
            self._task_owner.pop(evicted, None)
        while len(self._session_tasks) > _MAX_TRACKED_SESSIONS:
            old_session, old_tasks = self._session_tasks.popitem(last=False)
            for old_task in old_tasks:
                if self._task_session.get(old_task) == old_session:
                    self._task_session.pop(old_task, None)
                    self._task_owner.pop(old_task, None)

    def _latest_task_id(self, session_id: str, *, principal_id: str) -> str | None:
        tasks = self._session_tasks.get(session_id)
        if tasks:
            for task_id in reversed(tasks):
                if self._task_owner.get(task_id) == principal_id:
                    return task_id
        return next(
            (
                task_id
                for task_id in reversed(self._task_session)
                if self._task_owner.get(task_id) == principal_id
            ),
            None,
        )

    def _was_submitted(self, task_id: str) -> bool:
        return task_id in self._task_session

    @staticmethod
    def _invalid_arguments() -> dict[str, Any]:
        return {
            "status": "invalid_arguments",
            "verified": False,
            "summary": "ARISE could not process that voice request.",
        }

    @staticmethod
    def _not_found() -> dict[str, Any]:
        return {
            "status": "not_found",
            "verified": False,
            "summary": "ARISE could not find that task for this account.",
        }

    @staticmethod
    def _not_authorized(summary: str) -> dict[str, Any]:
        return {"status": "not_authorized", "verified": False, "summary": summary}


def _normalize_request_text(text: str) -> str:
    return " ".join(_REQUEST_TEXT_WORDS.findall(text.casefold()))


__all__ = ["TaskEngineVoiceAdapter", "VoiceConversationBridge", "VoiceTaskPort"]
