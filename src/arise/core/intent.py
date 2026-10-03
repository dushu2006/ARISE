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
class IntentEntity:
    """A lexical span extracted for routing/planning context, never an authority grant."""

    kind: str
    value: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if not self.kind or len(self.kind) > 32:
            raise ValueError("intent entity kind must be a short token")
        if not self.value or len(self.value) > 2048:
            raise ValueError("intent entity value must be non-empty and bounded")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("intent entity source span is invalid")


@dataclass(frozen=True, slots=True)
class IntentActionStep:
    """One advisory action phrase from the user's text; this is not an executable plan step."""

    operation: str
    target_text: str | None
    start: int
    end: int

    def __post_init__(self) -> None:
        if not self.operation or len(self.operation) > 64:
            raise ValueError("intent operation must be non-empty and bounded")
        if self.target_text is not None and len(self.target_text) > 2048:
            raise ValueError("intent target hint exceeds the configured limit")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("intent action source span is invalid")


@dataclass(frozen=True, slots=True)
class StructuredCommand:
    """Lossy, deterministic command outline; TaskEngine and PolicyEngine remain authoritative."""

    steps: tuple[IntentActionStep, ...]
    entities: tuple[IntentEntity, ...]

    def __post_init__(self) -> None:
        if not self.steps or len(self.steps) > 32:
            raise ValueError("structured command must contain between one and 32 action hints")
        if len(self.entities) > 96:
            raise ValueError("structured command contains too many extracted entities")


@dataclass(frozen=True, slots=True)
class IntentClassification:
    kind: IntentKind
    confidence: float
    reason_code: str
    entities: tuple[IntentEntity, ...] = ()
    structured_command: StructuredCommand | None = None

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
        r"summari[sz]e|research|organize|schedule|set up|turn on|turn off)\b",
        re.IGNORECASE,
    )
    _step_joiner = re.compile(r"\b(?:and then|then|after that|plus)\b")

    def classify(self, text: str) -> IntentClassification:
        result = self._classify_surface(text)
        if not isinstance(text, str) or not result.may_require_runtime_task:
            return result
        structured = self._structured_command(text)
        if structured is None:
            return result
        return IntentClassification(
            kind=result.kind,
            confidence=result.confidence,
            reason_code=result.reason_code,
            entities=structured.entities,
            structured_command=structured,
        )

    def _classify_surface(self, text: str) -> IntentClassification:
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

    _quoted_value = re.compile(
        r'"(?P<double>[^"\n]{1,512})"|'
        r"'(?P<single>[^'\n]{1,512})'|"
        r"“(?P<curly_double>[^”\n]{1,512})”|"
        r"‘(?P<curly_single>[^’\n]{1,512})’"
    )
    _url_value = re.compile(r"""https?://[^\s<>"']{1,2048}""", re.IGNORECASE)
    _trailing_joiner = re.compile(
        r"(?:\s*,?\s+)(?:and\s+then|after\s+that|then|plus|and)\s*$",
        re.IGNORECASE,
    )

    def _structured_command(self, text: str) -> StructuredCommand | None:
        bounded = text[:16_384]
        matches = list(self._action.finditer(bounded))[:32]
        if not matches:
            return None
        steps: list[IntentActionStep] = []
        entities: list[IntentEntity] = []
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(bounded)
            segment = bounded[match.end() : segment_end]
            target_limit = len(segment)
            if index + 1 < len(matches):
                joiner = self._trailing_joiner.search(segment)
                if joiner is not None:
                    target_limit = joiner.start()
            target_source = segment[:target_limit]
            leading = len(target_source) - len(target_source.lstrip())
            trailing = len(target_source.rstrip())
            while leading < trailing and target_source[leading] in ",;:-":
                leading += 1
            while trailing > leading and target_source[trailing - 1] in ",.;:!?":
                trailing -= 1
            target = target_source[leading:trailing].strip()[:2048]
            target_start = match.end() + leading
            operation = " ".join(match.group(0).casefold().split())
            steps.append(
                IntentActionStep(
                    operation=operation,
                    target_text=target or None,
                    start=match.start(),
                    end=match.end(),
                )
            )
            if target:
                entities.append(
                    IntentEntity("target", target, target_start, target_start + len(target))
                )

        for match in self._quoted_value.finditer(bounded):
            value = next((item for item in match.groupdict().values() if item is not None), None)
            if value:
                entities.append(
                    IntentEntity("quoted_text", value, match.start() + 1, match.end() - 1)
                )
        for match in self._url_value.finditer(bounded):
            value = match.group(0).rstrip(".,;:!?)]")
            if value:
                entities.append(
                    IntentEntity("url", value, match.start(), match.start() + len(value))
                )
        entities.sort(key=lambda item: (item.start, item.end, item.kind))
        return StructuredCommand(tuple(steps), tuple(entities[:96]))


__all__ = [
    "IntentActionStep",
    "IntentClassification",
    "IntentClassifier",
    "IntentEntity",
    "IntentKind",
    "StructuredCommand",
]
