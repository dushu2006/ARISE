"""Optional OpenAI-compatible embedding adapter for explicitly configured endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from urllib.parse import urlparse

import httpx

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.extensions import MAX_CONTEXT_TEXT_CHARS, EmbeddingPort, EmbeddingResult

MAX_EMBEDDING_RESPONSE_BYTES = 2 * 1024 * 1024


class EmbeddingProviderUnavailable(RuntimeError):
    """The configured embedding endpoint is unavailable or returned invalid data."""


class OpenAICompatibleEmbeddingAdapter(EmbeddingPort):
    """Call an explicitly configured `/embeddings` endpoint with bounded text and output."""

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key_secret_name: str,
        secret_provider: SecretProvider,
        is_cloud: bool,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("embedding base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("embedding base_url cannot contain credentials, query, or fragment")
        if is_cloud and parsed.scheme != "https":
            raise ValueError("cloud embedding endpoints must use HTTPS")
        if not is_cloud and parsed.scheme != "https" and not self._is_loopback(parsed.hostname):
            raise ValueError("non-loopback embedding endpoints must use HTTPS")
        if not model_id.strip() or len(model_id) > 256 or not api_key_secret_name.strip():
            raise ValueError("embedding model and secret reference are required")
        if not 0.1 <= timeout_seconds <= 300:
            raise ValueError("embedding timeout must be between 0.1 and 300 seconds")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key_secret_name = api_key_secret_name
        self.secret_provider = secret_provider
        self.is_cloud = is_cloud
        self.timeout_seconds = timeout_seconds
        self._local_endpoint = not is_cloud and self._is_loopback(parsed.hostname)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(2)

    async def embed(self, text: str, *, correlation_id: str) -> EmbeddingResult:
        del correlation_id  # Correlation data is not sent to a third party.
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CONTEXT_TEXT_CHARS:
            raise ValueError("embedding text must be non-empty and bounded")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            secret = self.secret_provider.get_secret(self.api_key_secret_name)
        except SecretUnavailable:
            if not self._local_endpoint:
                raise EmbeddingProviderUnavailable(
                    "Embedding credential is not available in the OS credential store."
                ) from None
        else:
            headers["Authorization"] = f"Bearer {secret}"
        payload = {"model": self.model_id, "input": text}
        async with self._semaphore:
            try:
                async with self._client.stream(
                    "POST",
                    self._embeddings_url(),
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_EMBEDDING_RESPONSE_BYTES:
                        raise EmbeddingProviderUnavailable(
                            "Embedding response exceeded its size limit."
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_EMBEDDING_RESPONSE_BYTES:
                            raise EmbeddingProviderUnavailable(
                                "Embedding response exceeded its size limit."
                            )
                        body.extend(chunk)
                data = json.loads(body)
                values = self._extract_vector(data)
                return EmbeddingResult(model_id=self.model_id, vector=tuple(values))
            except EmbeddingProviderUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                raise EmbeddingProviderUnavailable(
                    "Embedding provider did not return a valid vector."
                ) from exc

    def _embeddings_url(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/embeddings"
        return f"{self.base_url}/v1/embeddings"

    @staticmethod
    def _extract_vector(payload: object) -> list[float]:
        if not isinstance(payload, dict):
            raise ValueError("embedding response must be an object")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("embedding response must contain one data item")
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
            raise ValueError("embedding response dimension is invalid")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding response contains a non-numeric value")
        return [float(value) for value in vector]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _is_loopback(hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False
