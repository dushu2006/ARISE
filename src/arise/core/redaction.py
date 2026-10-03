"""Best-effort local redaction for diagnostics and persisted user text.

This is a privacy guard, not a secret scanner or credential-management system.
Users should still avoid putting credentials in prompts. Secret values are never
accepted by the secret-reference contracts or written by this module itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class SecretRedactor:
    """Redact common credential-shaped values without logging the originals."""

    _PATTERNS = (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|(?:api|access|refresh|auth|id|session|bearer)?[_-]?token|"
            r"authorization|client[_-]?secret|private[_-]?key|credential|password|passwd|secret)"
            r"\b\s*[:=]\s*)([^\s,;]+)"
        ),
        re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    )

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("redaction input must be text")
        value = text
        for index, pattern in enumerate(self._PATTERNS):
            if index < 2:
                value = pattern.sub(lambda match: match.group(1) + "[REDACTED]", value)
            else:
                value = pattern.sub("[REDACTED]", value)
        return value

    def redact_object(self, value: Any) -> Any:
        """Recursively redact strings before user-controlled metadata is stored."""

        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                safe_key = str(key)
                if re.search(
                    r"(?i)(api[_-]?key|(?:api|access|refresh|auth|id|session|bearer)?[_-]?token|authorization|private[_-]?key|password|passwd|secret|credential)",
                    safe_key,
                ):
                    redacted[safe_key] = "[REDACTED]"
                else:
                    redacted[safe_key] = self.redact_object(item)
            return redacted
        if isinstance(value, (tuple, list)):
            return [self.redact_object(item) for item in value]
        return value


DEFAULT_REDACTOR = SecretRedactor()
