"""Deterministic answers for system/environment questions.

The model must never invent environment state. When ARISE can inspect the machine
it inspects it; when it cannot, it says so. This module deliberately owns only a
small, closed set of question shapes that can be answered from adapters, and
returns ``None`` for anything else so the caller can fall back to a normal
informational answer.

Nothing here can authorize, execute or verify an action: it is read-only
observation plus bounded text formatting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from arise.core.computer import InstalledApplication, RunningApplication, WindowRecord
from arise.core.redaction import DEFAULT_REDACTOR, SecretRedactor

_MAX_LISTED_ITEMS = 12
_MAX_NAME_LENGTH = 96

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _match_key(value: str) -> str:
    """Bounded, case-insensitive comparison key for application/window names."""

    return _NON_ALNUM.sub("", str(value or "").casefold())


class EnvironmentQuestionKind(StrEnum):
    """The closed set of environment questions ARISE answers deterministically."""

    NONE = "none"
    APPLICATION_RUNNING = "application_running"
    APPLICATION_INSTALLED = "application_installed"
    RUNNING_APPLICATIONS = "running_applications"
    FOREGROUND_WINDOW = "foreground_window"
    SYSTEM_RESOURCES = "system_resources"


@dataclass(frozen=True, slots=True)
class EnvironmentAnswer:
    """A natural-language answer built only from observed environment facts."""

    kind: EnvironmentQuestionKind
    answer: str
    available: bool
    unavailable_reason: str | None = None
    subject: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    def as_unavailable(self, reason: str) -> EnvironmentAnswer:
        return EnvironmentAnswer(
            kind=self.kind,
            answer=(
                "I cannot inspect that on this machine right now, so I will not guess. "
                f"Reason: {reason}."
            ),
            available=False,
            unavailable_reason=reason,
            subject=self.subject,
            facts=self.facts,
        )


class ApplicationInventoryPort(Protocol):
    async def running_applications(self) -> Sequence[RunningApplication]: ...

    async def installed_applications(
        self, *, limit: int = 512
    ) -> Sequence[InstalledApplication]: ...


class WindowInventoryPort(Protocol):
    async def foreground_window(self) -> WindowRecord | None: ...


# Ordered most-specific first: "is Chrome running" must be typed before the
# generic "what is running" shape.
_RUNNING_PATTERNS = (
    re.compile(
        r"\b(?:is|are|was|were)\s+(?P<app>[a-z0-9][a-z0-9 _.\-()]{0,63}?)\s+"
        r"(?:still\s+)?(?:open|running|up|active|launched)\b"
    ),
    re.compile(
        r"\b(?:did|have)\s+(?:you|we|i)\s+(?:successfully\s+)?"
        r"(?:open|opened|launch|launched|start|started)\s+"
        r"(?P<app>[a-z0-9][a-z0-9 _.\-()]{0,63}?)\b"
    ),
    re.compile(
        r"\b(?:is|are)\s+(?P<app>[a-z0-9][a-z0-9 _.\-()]{0,63}?)\s+"
        r"(?:still\s+)?(?:alive|going|on)\b"
    ),
)
_INSTALLED_PATTERNS = (
    re.compile(
        r"\b(?:is|are)\s+(?P<app>[a-z0-9][a-z0-9 _.\-()]{0,63}?)\s+installed\b",
    ),
    re.compile(
        r"\bdo\s+i\s+have\s+(?P<app>[a-z0-9][a-z0-9 _.\-()]{0,63}?)\s+installed\b",
    ),
)
_RUNNING_LIST_PATTERNS = (
    re.compile(
        r"\bwhat\b[^\n?]{0,48}?\b(?:applications|apps|programs|processes)\b"
        r"[^\n?]{0,32}?\brunning\b"
    ),
    re.compile(
        r"\bwhich\b[^\n?]{0,48}?\b(?:applications|apps|programs|processes)\b"
        r"[^\n?]{0,32}?\b(?:running|open)\b"
    ),
    re.compile(r"\bwhat(?:'s|\s+is)\s+(?:currently\s+)?running\b"),
    re.compile(
        r"\b(?:list|show)\b[^\n?]{0,32}?\b(?:running|open)\b"
        r"[^\n?]{0,24}?\b(?:applications|apps|programs|processes)\b"
    ),
)
_FOREGROUND_PATTERNS = (
    re.compile(
        r"\b(?:which|what)\b[^\n?]{0,48}?\bwindow\b[^\n?]{0,48}?"
        r"\b(?:focused|focus|active|foreground|front|open)\b"
    ),
    re.compile(r"\bwhat(?:'s|\s+is)\s+(?:in\s+)?(?:the\s+)?focus(?:ed)?\b"),
    re.compile(r"\bwhat\s+am\s+i\s+(?:looking\s+at|on)\b"),
    re.compile(r"\bwhat\s+is\s+currently\s+focused\b"),
)
_RESOURCE_PATTERNS = (
    re.compile(r"\bhow\s+much\s+(?:ram|memory)\b"),
    re.compile(r"\bhow\s+many\s+(?:cpus?|cores?|processors?)\b"),
    re.compile(r"\b(?:ram|memory|cpu|processor)\s+(?:usage|use|load)\b"),
    re.compile(r"\bwhat\s+(?:are\s+)?(?:my\s+)?(?:system\s+)?(?:specs?|hardware)\b"),
)

_STOP_PREFIXES = ("the ", "my ", "a ", "an ", "that ", "this ")
# Words that can sit between "is/are" and "running" without naming an
# application ("what applications are currently running?"). A question whose
# subject is one of these is a list question, not an "is X running" question.
_SUBJECT_STOPWORDS = frozenset(
    {
        "currently",
        "now",
        "still",
        "already",
        "actually",
        "really",
        "there",
        "anything",
        "something",
        "everything",
        "nothing",
        "any",
        "some",
        "all",
        "many",
        "much",
        "more",
        "it",
        "they",
        "them",
        "we",
        "you",
        "i",
        "what",
        "which",
        "how",
        "why",
        "applications",
        "apps",
        "programs",
        "processes",
        "windows",
    }
)


def _clean_subject(value: str) -> str:
    text = " ".join(str(value or "").casefold().split()).strip(" ?!.,;:")
    changed = True
    while changed:
        changed = False
        for prefix in _STOP_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                changed = True
    return text[:_MAX_NAME_LENGTH]


class EnvironmentQuestionService:
    """Type and answer environment questions from adapters, never from a model."""

    def __init__(
        self,
        *,
        applications: ApplicationInventoryPort | None = None,
        windows: WindowInventoryPort | None = None,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
        running_limit: int = 64,
        installed_limit: int = 200,
    ) -> None:
        self.applications = applications
        self.windows = windows
        self.redactor = redactor
        self.running_limit = max(1, min(int(running_limit), 512))
        self.installed_limit = max(1, min(int(installed_limit), 1024))

    @property
    def can_inspect(self) -> bool:
        return self.applications is not None or self.windows is not None

    def classify(self, text: str) -> tuple[EnvironmentQuestionKind, str | None]:
        """Return ``(kind, subject)`` for a user utterance."""

        if not isinstance(text, str) or not text.strip():
            return EnvironmentQuestionKind.NONE, None
        normalized = " ".join(text.casefold().split())
        for pattern in _FOREGROUND_PATTERNS:
            if pattern.search(normalized):
                return EnvironmentQuestionKind.FOREGROUND_WINDOW, None
        # List questions are checked before "is X running" so that
        # "what applications are currently running?" is not read as a question
        # about an application literally named "currently".
        for pattern in _RUNNING_LIST_PATTERNS:
            if pattern.search(normalized):
                return EnvironmentQuestionKind.RUNNING_APPLICATIONS, None
        for pattern in _RUNNING_PATTERNS:
            match = pattern.search(normalized)
            if match is not None:
                subject = _clean_subject(match.group("app"))
                if subject and subject not in _SUBJECT_STOPWORDS:
                    return EnvironmentQuestionKind.APPLICATION_RUNNING, subject
        for pattern in _INSTALLED_PATTERNS:
            match = pattern.search(normalized)
            if match is not None:
                subject = _clean_subject(match.group("app"))
                if subject and subject not in _SUBJECT_STOPWORDS:
                    return EnvironmentQuestionKind.APPLICATION_INSTALLED, subject
        for pattern in _RESOURCE_PATTERNS:
            if pattern.search(normalized):
                return EnvironmentQuestionKind.SYSTEM_RESOURCES, None
        return EnvironmentQuestionKind.NONE, None

    async def answer(self, text: str) -> EnvironmentAnswer | None:
        """Answer an environment question, or return None when it is not one."""

        kind, subject = self.classify(text)
        if kind is EnvironmentQuestionKind.NONE:
            return None
        if kind is EnvironmentQuestionKind.SYSTEM_RESOURCES:
            return self._system_resources()
        if kind is EnvironmentQuestionKind.FOREGROUND_WINDOW:
            return await self._foreground_window()
        if kind is EnvironmentQuestionKind.RUNNING_APPLICATIONS:
            return await self._running_applications()
        if kind is EnvironmentQuestionKind.APPLICATION_RUNNING:
            return await self._application_running(subject or "")
        return await self._application_installed(subject or "")

    # ------------------------------------------------------------------
    # Individual answers
    # ------------------------------------------------------------------

    def _system_resources(self) -> EnvironmentAnswer:
        try:
            import psutil
        except Exception:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.SYSTEM_RESOURCES,
                answer="",
                available=False,
            ).as_unavailable("SYSTEM_MONITOR_UNAVAILABLE")
        try:
            memory = psutil.virtual_memory()
            cpu_count = psutil.cpu_count(logical=True) or 1
            cpu_percent = float(psutil.cpu_percent(interval=None) or 0.0)
        except Exception:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.SYSTEM_RESOURCES, answer="", available=False
            ).as_unavailable("SYSTEM_METRICS_UNAVAILABLE")
        total_gb = memory.total / (1024**3)
        available_gb = memory.available / (1024**3)
        answer = (
            f"This machine reports {cpu_count} logical processors and "
            f"{total_gb:.1f} GB of RAM in total, with {available_gb:.1f} GB available "
            f"and {memory.percent:.0f}% in use. Current CPU load is {cpu_percent:.0f}%."
        )
        return EnvironmentAnswer(
            kind=EnvironmentQuestionKind.SYSTEM_RESOURCES,
            answer=answer,
            available=True,
            facts={
                "cpu_count": cpu_count,
                "total_memory_bytes": int(memory.total),
                "available_memory_bytes": int(memory.available),
                "memory_percent": round(float(memory.percent), 1),
                "cpu_percent": round(cpu_percent, 1),
            },
        )

    async def _foreground_window(self) -> EnvironmentAnswer:
        if self.windows is None:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.FOREGROUND_WINDOW, answer="", available=False
            ).as_unavailable("WINDOW_OBSERVATION_UNAVAILABLE")
        try:
            window = await self.windows.foreground_window()
        except Exception:
            window = None
        if window is None:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.FOREGROUND_WINDOW, answer="", available=False
            ).as_unavailable("NO_FOREGROUND_WINDOW_OBSERVED")
        title = self.redactor.redact(window.title or "").strip()[:_MAX_NAME_LENGTH]
        if title:
            answer = (
                f'The focused window is "{title}", owned by '
                f"{window.application or 'an unknown application'}"
                f"{f' (PID {window.process_id})' if window.process_id else ''}."
            )
        else:
            answer = (
                "The focused window has no title. It is owned by "
                f"{window.application or 'an unknown application'}"
                f"{f' (PID {window.process_id})' if window.process_id else ''}."
            )
        return EnvironmentAnswer(
            kind=EnvironmentQuestionKind.FOREGROUND_WINDOW,
            answer=answer,
            available=True,
            facts={
                "window_id": window.window_id,
                "application": window.application,
                "process_id": window.process_id,
            },
        )

    async def _running_applications(self) -> EnvironmentAnswer:
        if self.applications is None:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.RUNNING_APPLICATIONS, answer="", available=False
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        try:
            running = await self.applications.running_applications()
        except Exception:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.RUNNING_APPLICATIONS, answer="", available=False
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        names = _dedupe_names([app.name for app in running])
        if not names:
            return EnvironmentAnswer(
                kind=EnvironmentQuestionKind.RUNNING_APPLICATIONS,
                answer="I could not observe any running application processes.",
                available=True,
                facts={"count": 0},
            )
        shown = names[:_MAX_LISTED_ITEMS]
        answer = "Running right now: " + ", ".join(shown)
        if len(names) > len(shown):
            answer += f", and {len(names) - len(shown)} more"
        answer += f". That is {len(names)} observed application processes."
        return EnvironmentAnswer(
            kind=EnvironmentQuestionKind.RUNNING_APPLICATIONS,
            answer=answer,
            available=True,
            facts={"count": len(names), "names": shown},
        )

    async def _application_running(self, subject: str) -> EnvironmentAnswer:
        if self.applications is None:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.APPLICATION_RUNNING,
                answer="",
                available=False,
                subject=subject,
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        try:
            running = await self.applications.running_applications()
        except Exception:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.APPLICATION_RUNNING,
                answer="",
                available=False,
                subject=subject,
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        display = subject
        matched = _match_applications(subject, running)
        if matched:
            window_count = sum(len(app.window_ids) for app in matched)
            pids = sorted({app.process_id for app in matched})
            named = next((app.name for app in matched if app.name), subject)
            display = str(named)
            window_text = (
                f" with {window_count} visible window{'s' if window_count != 1 else ''}"
                if window_count
                else " without an observed visible window"
            )
            return EnvironmentAnswer(
                kind=EnvironmentQuestionKind.APPLICATION_RUNNING,
                answer=(
                    f"Yes. {display} is running ({len(pids)} "
                    f"process{'es' if len(pids) != 1 else ''}"
                    f"{f', PID {pids[0]}' if len(pids) == 1 else ''}){window_text}."
                ),
                available=True,
                subject=display,
                facts={"running": True, "process_ids": pids[:8], "window_count": window_count},
            )
        try:
            installed = await self.applications.installed_applications(limit=self.installed_limit)
        except Exception:
            installed = ()
        installed_match = _match_installed(subject, installed)
        if installed_match:
            display = str(installed_match.name)
            return EnvironmentAnswer(
                kind=EnvironmentQuestionKind.APPLICATION_RUNNING,
                answer=f"No. {display} is installed but I do not observe it running.",
                available=True,
                subject=display,
                facts={"running": False, "installed": True},
            )
        return EnvironmentAnswer(
            kind=EnvironmentQuestionKind.APPLICATION_RUNNING,
            answer=(
                f"No. I do not observe {subject or 'that application'} running, and I "
                "could not match that name to an installed application."
            ),
            available=True,
            subject=subject,
            facts={"running": False, "installed": False},
        )

    async def _application_installed(self, subject: str) -> EnvironmentAnswer:
        if self.applications is None:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.APPLICATION_INSTALLED,
                answer="",
                available=False,
                subject=subject,
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        try:
            installed = await self.applications.installed_applications(limit=self.installed_limit)
        except Exception:
            return EnvironmentAnswer(
                EnvironmentQuestionKind.APPLICATION_INSTALLED,
                answer="",
                available=False,
                subject=subject,
            ).as_unavailable("APPLICATION_INVENTORY_UNAVAILABLE")
        match = _match_installed(subject, installed)
        if match is not None:
            return EnvironmentAnswer(
                kind=EnvironmentQuestionKind.APPLICATION_INSTALLED,
                answer=f"Yes. {match.name} is present in the installed application catalog.",
                available=True,
                subject=str(match.name),
                facts={"installed": True},
            )
        return EnvironmentAnswer(
            kind=EnvironmentQuestionKind.APPLICATION_INSTALLED,
            answer=(
                f"No. I could not find {subject or 'that application'} in the installed "
                "application catalog."
            ),
            available=True,
            subject=subject,
            facts={"installed": False},
        )


def _dedupe_names(values: Sequence[str]) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        cleaned = " ".join(str(value or "").split())[:_MAX_NAME_LENGTH]
        if not cleaned:
            continue
        key = _match_key(cleaned)
        if key and key not in seen:
            seen[key] = cleaned
    return [seen[key] for key in sorted(seen)]


def _application_keys(app: RunningApplication) -> tuple[str, ...]:
    """Normalized identity keys for a running application.

    Process names usually carry the ".exe" suffix ("chrome.exe"), so the suffix is
    removed before matching; a user asking about "Chrome" must match that process.
    """

    keys: list[str] = []
    for value in (app.name, _executable_stem(app.executable_path), app.package_family_name):
        text = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
        text = _strip_executable_suffix(text)
        key = _match_key(text)
        if key:
            keys.append(key)
    return tuple(dict.fromkeys(keys))


def _strip_executable_suffix(value: str) -> str:
    text = str(value or "").strip()
    if text.casefold().endswith(".exe"):
        return text[: -len(".exe")]
    return text


def _contains_key(candidate: str, wanted: str) -> bool:
    """Bounded containment used only after exact matching fails.

    Short subjects are never matched by containment: "go" must not match
    "googlechrome". This keeps the answer honest in both directions.
    """

    if len(candidate) < 4 or len(wanted) < 4:
        return False
    return candidate.startswith(wanted) or candidate.endswith(wanted)


def _match_applications(
    subject: str, applications: Sequence[RunningApplication]
) -> list[RunningApplication]:
    wanted = _match_key(subject)
    if not wanted:
        return []
    exact = [app for app in applications if wanted in _application_keys(app)]
    if exact:
        return exact
    return [
        app
        for app in applications
        if any(_contains_key(key, wanted) for key in _application_keys(app))
    ]


def _match_installed(
    subject: str, applications: Sequence[InstalledApplication]
) -> InstalledApplication | None:
    wanted = _match_key(subject)
    if not wanted:
        return None
    for app in applications:
        if wanted == _match_key(app.name):
            return app
    for app in applications:
        if wanted and wanted in _match_key(app.name):
            return app
    return None


def _executable_stem(path: str | None) -> str:
    if not path:
        return ""
    text = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    if text.casefold().endswith(".exe"):
        text = text[: -len(".exe")]
    return text


__all__ = [
    "EnvironmentAnswer",
    "EnvironmentQuestionKind",
    "EnvironmentQuestionService",
]
