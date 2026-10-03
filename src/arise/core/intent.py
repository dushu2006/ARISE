"""Conservative, deterministic intent hints for voice routing and safety UX.

These rules do not authorize execution. Task submissions still require an explicit provider
function call into VoiceConversationBridge and then pass through TaskEngine, policy, and runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class IntentKind(StrEnum):
    CASUAL_CONVERSATION = "casual_conversation"
    QUESTION = "question"
    COMMAND = "command"
    TASK = "task"
    MULTI_STEP_TASK = "multi_step_task"
    CLARIFICATION = "clarification"
    CANCELLATION = "cancellation"
    FOLLOW_UP = "follow_up"
    STATUS_REQUEST = "status_request"


@dataclass(frozen=True, slots=True)
class IntentClassification:
    kind: IntentKind
    confidence: float
    reason_code: str

    @property
    def may_require_runtime_task(self) -> bool:
        return self.kind in {
            IntentKind.COMMAND,
            IntentKind.TASK,
            IntentKind.MULTI_STEP_TASK,
            IntentKind.FOLLOW_UP,
        }

    @property
    def is_control_request(self) -> bool:
        return self.may_require_runtime_task or self.kind in {
            IntentKind.CANCELLATION,
            IntentKind.STATUS_REQUEST,
        }


class IntentClassifier:
    """Classify only common surface forms; low confidence means conversational fallback."""

    _cancel = re.compile(r"^(?:please\s+)?(?:stop|cancel|abort|halt|never mind|nevermind)\b")
    _status = re.compile(
        r"^(?:what(?:'s| is)\s+(?:the\s+)?(?:task\s+)?status|status|"
        r"how(?:'s| is)\s+(?:that|it|the task)\s+going|"
        r"are you\s+(?:done|finished)|did you\s+(?:finish|complete))\b"
    )
    _follow_up = re.compile(
        r"^(?:and then|also|next|do the same|that one|the second one|"
        r"what about that|continue with that)\b"
    )
    _question = re.compile(
        r"^(?:who|what|when|where|why|how|which|is|are|was|were|do|does|did|"
        r"can|could|would|should|will|has|have|had)\b"
    )
    _polite_command = re.compile(r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?")
    _instructional_request = re.compile(
        r"^(?:please\s+)?(?:tell me|explain|teach me|show me|help me understand)\s+"
        r"(?:how|why|what|whether|if|the steps? to)\b"
    )
    _action = re.compile(
        r"\b(?:open|launch|start|close|quit|switch|focus|search|find|look up|"
        r"navigate|browse|click|tap|type|enter|fill|select|scroll|save|create|"
        r"delete|remove|rename|move|copy|send|download|upload|install|compare|"
        r"summari[sz]e|research|organize|schedule|set up|turn on|turn off)\b"
    )
    _step_joiner = re.compile(r"\b(?:and then|then|after that|plus)\b")

    def classify(self, text: str) -> IntentClassification:
        if not isinstance(text, str) or not text.strip():
            return IntentClassification(IntentKind.CLARIFICATION, 1.0, "empty_input")
        normalized = " ".join(text.casefold().split())
        if len(normalized) > 16_384:
            normalized = normalized[:16_384]
        if self._cancel.search(normalized):
            return IntentClassification(IntentKind.CANCELLATION, 0.98, "explicit_cancel_phrase")
        if self._status.search(normalized):
            return IntentClassification(IntentKind.STATUS_REQUEST, 0.96, "status_question")
        if self._instructional_request.search(normalized):
            return IntentClassification(
                IntentKind.QUESTION, 0.96, "instructional_information_request"
            )
        action_count = len(self._action.findall(normalized))
        has_step_joiner = bool(self._step_joiner.search(normalized))
        polite_prefix = self._polite_command.match(normalized)
        if polite_prefix:
            remainder = normalized[polite_prefix.end() :]
            if self._action.match(remainder):
                kind = IntentKind.MULTI_STEP_TASK if action_count > 1 else IntentKind.COMMAND
                confidence = 0.9 if kind is IntentKind.COMMAND else 0.94
                return IntentClassification(kind, confidence, "polite_action_request")
        if self._follow_up.search(normalized):
            kind = (
                IntentKind.MULTI_STEP_TASK
                if action_count > 1 or has_step_joiner
                else IntentKind.FOLLOW_UP
            )
            return IntentClassification(kind, 0.78, "follow_up_phrase")
        starts_with_action = self._action.search(
            normalized[:96]
        ) is not None and normalized.startswith(
            (
                "please ",
                "open ",
                "launch ",
                "start ",
                "close ",
                "quit ",
                "switch ",
                "focus ",
                "search ",
                "find ",
                "look up ",
                "navigate ",
                "browse ",
                "click ",
                "tap ",
                "type ",
                "enter ",
                "fill ",
                "select ",
                "scroll ",
                "save ",
                "create ",
                "delete ",
                "remove ",
                "rename ",
                "move ",
                "copy ",
                "send ",
                "download ",
                "upload ",
                "install ",
                "compare ",
                "summarize ",
                "summarise ",
                "research ",
                "organize ",
                "schedule ",
                "set up ",
                "turn on ",
                "turn off ",
            )
        )
        if starts_with_action:
            kind = (
                IntentKind.MULTI_STEP_TASK
                if action_count > 1 or has_step_joiner
                else IntentKind.COMMAND
            )
            return IntentClassification(
                kind,
                0.92 if kind is IntentKind.COMMAND else 0.96,
                "imperative_action_request",
            )
        if normalized.endswith("?") or self._question.match(normalized):
            return IntentClassification(IntentKind.QUESTION, 0.85, "question_surface_form")
        if action_count >= 2 and has_step_joiner:
            return IntentClassification(IntentKind.MULTI_STEP_TASK, 0.78, "multiple_action_verbs")
        if action_count == 1:
            return IntentClassification(IntentKind.TASK, 0.58, "possible_action_request")
        if normalized in {"", "...", "?"}:
            return IntentClassification(IntentKind.CLARIFICATION, 0.95, "no_usable_intent")
        return IntentClassification(IntentKind.CASUAL_CONVERSATION, 0.65, "conversational_fallback")


__all__ = ["IntentClassification", "IntentClassifier", "IntentKind"]
