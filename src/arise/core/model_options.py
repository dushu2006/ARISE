"""Validation for additional, provider-specific model request-body options."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

_CREDENTIAL_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "password",
    "passwd",
    "secret",
    "credential",
)
_CREDENTIAL_KEYS = frozenset(
    {
        "auth",
        "token",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "privatekey",
    }
)
_RESERVED_REQUEST_FIELDS = frozenset({"model", "messages", "maxtokens", "temperature", "stream"})
_MAX_PROVIDER_OPTIONS_BYTES = 16_384


def validate_provider_options(value: object) -> dict[str, Any]:
    """Return a defensive JSON copy of safe, bounded provider request options.

    Provider options are additional JSON body fields, not a place for credentials
    or overrides of routing and stream-control fields. API credentials remain
    resolved separately by the provider's secret reference and sent in headers.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("model provider_options must be a JSON object")

    def validate_nested(item: Any, *, depth: int, top_level: bool = False) -> None:
        if depth > 8:
            raise ValueError("model provider_options nesting exceeds the maximum depth")
        if isinstance(item, Mapping):
            if len(item) > 128:
                raise ValueError("model provider_options objects may contain at most 128 fields")
            for key, nested_value in item.items():
                if not isinstance(key, str) or not key.strip() or len(key) > 128:
                    raise ValueError("model provider_options keys must be bounded strings")
                normalized_key = "".join(char for char in key.casefold() if char.isalnum())
                if normalized_key in _CREDENTIAL_KEYS or any(
                    marker in normalized_key for marker in _CREDENTIAL_KEY_FRAGMENTS
                ):
                    raise ValueError("model provider_options cannot contain credentials")
                if top_level and normalized_key in _RESERVED_REQUEST_FIELDS:
                    raise ValueError(
                        "model provider_options cannot override standard request fields"
                    )
                validate_nested(nested_value, depth=depth + 1)
        elif isinstance(item, list):
            if len(item) > 128:
                raise ValueError("model provider_options lists may contain at most 128 items")
            for nested_value in item:
                validate_nested(nested_value, depth=depth + 1)
        elif item is None or isinstance(item, (str, bool, int)):
            return
        elif isinstance(item, float) and math.isfinite(item):
            return
        else:
            raise ValueError("model provider_options must contain JSON-compatible values")

    if len(value) > 64:
        raise ValueError("model provider_options may contain at most 64 fields")
    validate_nested(value, depth=0, top_level=True)

    def json_compatible(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: json_compatible(nested_value) for key, nested_value in item.items()}
        if isinstance(item, list):
            return [json_compatible(nested_value) for nested_value in item]
        return item

    try:
        encoded = json.dumps(
            json_compatible(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise ValueError("model provider_options must contain JSON-compatible values") from None
    if len(encoded.encode("utf-8")) > _MAX_PROVIDER_OPTIONS_BYTES:
        raise ValueError("model provider_options exceed the maximum size")
    return json.loads(encoded)
