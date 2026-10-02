"""Provider-neutral model gateway/router contracts and bounded routing logic."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from arise.core.errors import CapabilityUnavailableError, ProviderUnavailableError
from arise.core.models import (
    CapabilityStatus,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelSelection,
    ModelSelectionRequest,
    ProviderStatus,
)
from arise.core.retry import CircuitBreaker, CircuitOpenError


class ModelProvider(Protocol):
    provider_id: str
    model_ids: tuple[str, ...]
    is_cloud: bool
    max_concurrent_requests: int
    supports_streaming: bool

    def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(slots=True)
class _ProviderRuntime:
    provider: ModelProvider
    semaphore: asyncio.Semaphore
    breaker: CircuitBreaker
    failures: int = 0
    last_success_at: datetime | None = None
    last_latency_ms: float | None = None


class ModelRouter:
    """Selects only registered providers and applies privacy/circuit limits."""

    def __init__(
        self,
        *,
        allow_cloud: bool = False,
        max_concurrent_requests: int = 4,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: float = 30.0,
    ) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        self.allow_cloud = allow_cloud
        self.max_concurrent_requests = max_concurrent_requests
        self._providers: dict[str, _ProviderRuntime] = {}
        self._failure_threshold = circuit_failure_threshold
        self._recovery_seconds = circuit_recovery_seconds

    def register(self, provider: ModelProvider) -> None:
        if not provider.provider_id.strip():
            raise ValueError("provider_id cannot be empty")
        if provider.provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider.provider_id}")
        capacity = min(max(1, provider.max_concurrent_requests), self.max_concurrent_requests)
        self._providers[provider.provider_id] = _ProviderRuntime(
            provider=provider,
            semaphore=asyncio.Semaphore(capacity),
            breaker=CircuitBreaker(
                failure_threshold=self._failure_threshold,
                recovery_seconds=self._recovery_seconds,
            ),
        )

    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def select(self, request: ModelSelectionRequest) -> ModelSelection:
        candidates = self._candidates(request)
        if not candidates:
            raise CapabilityUnavailableError(
                "No configured model provider satisfies this role, modality, and privacy policy.",
                component="model-router",
                operation="select",
            )
        runtime = candidates[0]
        model_id = self._select_model(runtime.provider, request)
        fallback_options = tuple(
            (item.provider.provider_id, self._select_model(item.provider, request))
            for item in candidates[1:]
        )
        privacy_reason = (
            "local provider selected"
            if not runtime.provider.is_cloud
            else "cloud use was explicitly enabled"
        )
        return ModelSelection(
            provider_id=runtime.provider.provider_id,
            model_id=model_id,
            reason=f"Compatible provider selected; {privacy_reason}.",
            fallbacks=fallback_options,
        )

    async def complete(
        self,
        request: ModelRequest,
        *,
        selection: ModelSelectionRequest | None = None,
    ) -> ModelResponse:
        selector = selection or ModelSelectionRequest(
            role=request.role,
            task_type=request.role.value,
            privacy="cloud_allowed" if self.allow_cloud else "local_only",
            required_modalities=request.required_modalities,
            preferred_provider=None,
        )
        selected = self.select(selector)
        candidates = self._candidates(selector)
        candidates.sort(key=lambda item: item.provider.provider_id != selected.provider_id)
        last_error: BaseException | None = None
        for runtime in candidates:
            if request.stream and not getattr(runtime.provider, "supports_streaming", True):
                last_error = CapabilityUnavailableError(
                    "The selected model provider does not support streaming responses.",
                    component="model-router",
                    operation="complete",
                )
                continue
            if not runtime.breaker.allow_request():
                last_error = CircuitOpenError("model provider circuit is open")
                continue
            provider = runtime.provider
            if request.model_id is not None and request.model_id not in provider.model_ids:
                last_error = CapabilityUnavailableError(
                    "Requested model ID is not exposed by the selected provider.",
                    component="model-router",
                    operation="complete",
                )
                continue
            model_id = request.model_id or self._select_model(provider, selector)
            routed_request = request.model_copy(update={"model_id": model_id})
            started = time.perf_counter()
            try:
                async with runtime.semaphore:
                    response = await asyncio.wait_for(
                        provider.complete(routed_request), timeout=request.timeout_seconds
                    )
                if (
                    not isinstance(response, ModelResponse)
                    or response.request_id != request.request_id
                ):
                    raise ProviderUnavailableError(
                        "Model provider returned a malformed or mismatched response.",
                        component="model-router",
                        operation="complete",
                    )
                runtime.breaker.record_success()
                runtime.failures = 0
                runtime.last_success_at = datetime.now(UTC)
                runtime.last_latency_ms = (time.perf_counter() - started) * 1000
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.breaker.record_failure()
                runtime.failures += 1
                last_error = exc
        if isinstance(last_error, CapabilityUnavailableError):
            raise last_error
        raise ProviderUnavailableError(
            "All compatible model providers failed or were unavailable.",
            component="model-router",
            operation="complete",
            retryable=True,
            metadata={"provider_count": len(candidates)},
        ) from last_error

    def status(self) -> tuple[ProviderStatus, ...]:
        result: list[ProviderStatus] = []
        for provider_id, runtime in self._providers.items():
            breaker_state = runtime.breaker.state.value
            if breaker_state == "open":
                state = CapabilityStatus.DEGRADED
                error_code = "MODEL_CIRCUIT_OPEN"
            elif runtime.last_success_at is None:
                state = CapabilityStatus.DEGRADED
                error_code = "MODEL_NOT_YET_PROBED"
            else:
                state = CapabilityStatus.AVAILABLE
                error_code = None
            result.append(
                ProviderStatus(
                    provider_id=provider_id,
                    status=state,
                    latency_ms=runtime.last_latency_ms,
                    last_success_at=runtime.last_success_at,
                    error_code=error_code,
                    model_ids=runtime.provider.model_ids,
                )
            )
        return tuple(result)

    async def close(self) -> None:
        for runtime in self._providers.values():
            close = getattr(runtime.provider, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # Shutdown stays best-effort; provider internals are not logged.
                continue

    def _candidates(self, request: ModelSelectionRequest) -> list[_ProviderRuntime]:
        candidates = [
            runtime
            for runtime in self._providers.values()
            if runtime.provider.supports(request.role, request.required_modalities)
            and (
                not runtime.provider.is_cloud
                or (self.allow_cloud and request.privacy == "cloud_allowed")
            )
        ]
        candidates.sort(
            key=lambda runtime: (
                runtime.provider.provider_id != request.preferred_provider,
                runtime.provider.is_cloud,
                runtime.provider.provider_id,
            )
        )
        return candidates

    @staticmethod
    def _select_model(provider: ModelProvider, request: ModelSelectionRequest) -> str:
        if not provider.model_ids:
            raise CapabilityUnavailableError(
                "Configured model provider has no model IDs.",
                component="model-router",
                operation="select",
            )
        return provider.model_ids[0]
