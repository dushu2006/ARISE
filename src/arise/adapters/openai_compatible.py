"""OpenAI-compatible HTTP model adapter with secret-by-reference credentials."""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.errors import ProviderUnavailableError
from arise.core.models import ModelMessage, ModelRequest, ModelResponse, ModelRole

MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024


class OpenAICompatibleProvider:
    """Minimal non-streaming chat-completions adapter.

    A request is sent only when this adapter is explicitly registered in the
    router. Cloud endpoints require HTTPS and cloud routing is separately
    gated by ModelRouter policy. Error bodies and credentials are not surfaced.
    """

    _SUPPORTED_ROLES = frozenset(ModelRole)
    _SUPPORTED_MODALITIES = frozenset({"text"})
    supports_streaming = False

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        model_id: str,
        api_key_secret_name: str,
        secret_provider: SecretProvider,
        is_cloud: bool,
        max_concurrent_requests: int = 4,
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("model base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("model base_url cannot contain credentials, query, or fragment data")
        if is_cloud and parsed.scheme != "https":
            raise ValueError("cloud model endpoints must use HTTPS")
        if not is_cloud and parsed.scheme != "https" and not self._is_loopback(parsed.hostname):
            raise ValueError("non-loopback model endpoints must use HTTPS")
        if not provider_id.strip() or not model_id.strip() or not api_key_secret_name.strip():
            raise ValueError("provider, model, and secret reference are required")
        if max_concurrent_requests < 1 or timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("model limits and timeouts must be positive")
        self.provider_id = provider_id
        self.model_ids = (model_id,)
        self.is_cloud = is_cloud
        self.max_concurrent_requests = max_concurrent_requests
        self.base_url = base_url.rstrip("/")
        self.api_key_secret_name = api_key_secret_name
        self.secret_provider = secret_provider
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            trust_env=False,
        )
        self._owns_client = client is None
        self._local_endpoint = not is_cloud and self._is_loopback(parsed.hostname)

    def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
        return role in self._SUPPORTED_ROLES and modalities <= self._SUPPORTED_MODALITIES

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.stream:
            raise ProviderUnavailableError(
                "This model adapter does not support streaming responses.",
                component="model-provider",
                operation="complete",
                retryable=False,
            )
        if not self.supports(request.role, request.required_modalities):
            raise ProviderUnavailableError(
                "Model provider does not support the requested role or modalities.",
                component="model-provider",
                operation="complete",
            )
        model_id = request.model_id or self.model_ids[0]
        messages: list[dict[str, str]] = []
        for message in request.messages:
            content = self._plain_text(message)
            messages.append({"role": message.role, "content": content})
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        try:
            api_key = self.secret_provider.get_secret(self.api_key_secret_name)
        except SecretUnavailable:
            if not self._local_endpoint:
                raise ProviderUnavailableError(
                    "Model credential is not configured in the OS credential store.",
                    component="model-provider",
                    operation="resolve-secret",
                    retryable=False,
                ) from None
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.perf_counter()
        try:
            response_timeout = httpx.Timeout(
                min(request.timeout_seconds, self.timeout_seconds),
                connect=min(request.timeout_seconds, self.connect_timeout_seconds),
            )
            async with self._client.stream(
                "POST",
                self._chat_completions_url(),
                headers=headers,
                json=payload,
                timeout=response_timeout,
            ) as response:
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length is not None and int(declared_length) > MAX_MODEL_RESPONSE_BYTES:
                    raise ValueError("model provider response exceeded the size limit")
                response_body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(response_body) + len(chunk) > MAX_MODEL_RESPONSE_BYTES:
                        raise ValueError("model provider response exceeded the size limit")
                    response_body.extend(chunk)
            body = json.loads(response_body)
            choices = body.get("choices") if isinstance(body, Mapping) else None
            if not isinstance(choices, list) or not choices:
                raise ValueError("missing choices")
            message = choices[0].get("message", {})
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("response content is not text")
            usage = body.get("usage", {}) if isinstance(body, Mapping) else {}
            return ModelResponse(
                request_id=request.request_id,
                provider_id=self.provider_id,
                model_id=model_id,
                content=content,
                input_tokens=self._optional_int(usage.get("prompt_tokens")),
                output_tokens=self._optional_int(usage.get("completion_tokens")),
                finish_reason=choices[0].get("finish_reason"),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                "Model provider request timed out.",
                component="model-provider",
                operation="complete",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ProviderUnavailableError(
                "Model provider rejected or failed the request.",
                component="model-provider",
                operation="complete",
                retryable=status in {408, 425, 429, 500, 502, 503, 504},
                metadata={"http_status": status},
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailableError(
                "Model provider response was unavailable or malformed.",
                component="model-provider",
                operation="complete",
                retryable=isinstance(exc, httpx.HTTPError),
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    @staticmethod
    def _plain_text(message: ModelMessage) -> str:
        if isinstance(message.content, str):
            return message.content
        text_parts: list[str] = []
        for part in message.content:
            text = part.get("text")
            if not isinstance(text, str):
                raise ProviderUnavailableError(
                    "This model adapter accepts text messages only.",
                    component="model-provider",
                    operation="serialize-message",
                    retryable=False,
                )
            text_parts.append(text)
        return "\n".join(text_parts)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _is_loopback(hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False
