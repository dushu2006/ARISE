"""Provider-neutral model gateway/router contracts and bounded routing logic."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from arise.core.errors import CapabilityUnavailableError, ProviderUnavailableError
from arise.core.events import EventEnvelope, EventSeverity, EventStore
from arise.core.models import (
    CapabilityStatus,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelSelection,
    ModelSelectionRequest,
    ModelStreamChunk,
    ProviderStatus,
)
from arise.core.retry import CircuitBreaker, CircuitOpenError, CircuitState


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
        max_queued_requests: int = 32,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: float = 30.0,
        events: EventStore | None = None,
    ) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        if max_queued_requests < 1:
            raise ValueError("max_queued_requests must be positive")
        self.allow_cloud = allow_cloud
        self.max_concurrent_requests = max_concurrent_requests
        self.max_queued_requests = max_queued_requests
        self.events = events
        self._providers: dict[str, _ProviderRuntime] = {}
        self._failure_threshold = circuit_failure_threshold
        self._recovery_seconds = circuit_recovery_seconds
        self._queued_requests = 0

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
        try:
            selected = self.select(selector)
        except CapabilityUnavailableError:
            self._record_model_event(
                "MODEL_REQUEST_FAILED",
                request,
                severity=EventSeverity.WARNING,
                payload={
                    "request_id": request.request_id,
                    "role": request.role.value,
                    "attempt": 0,
                    "provider_id": None,
                    "error_code": "MODEL_NO_ELIGIBLE_PROVIDER",
                },
            )
            raise
        candidates = self._candidates(selector)
        candidates.sort(key=lambda item: item.provider.provider_id != selected.provider_id)
        last_error: BaseException | None = None
        last_failed_provider: str | None = None
        fallback_reason: str | None = None
        attempt = 0
        for runtime in candidates:
            provider = runtime.provider
            if request.stream and not getattr(provider, "supports_streaming", True):
                last_error = CapabilityUnavailableError(
                    "The selected model provider does not support streaming responses.",
                    component="model-router",
                    operation="complete",
                )
                last_failed_provider = provider.provider_id
                fallback_reason = "MODEL_STREAMING_UNSUPPORTED"
                self._record_model_event(
                    "MODEL_REQUEST_FAILED",
                    request,
                    severity=EventSeverity.WARNING,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt + 1,
                        "provider_id": provider.provider_id,
                        "error_code": fallback_reason,
                    },
                )
                continue
            if not runtime.breaker.allow_request():
                last_error = CircuitOpenError("model provider circuit is open")
                last_failed_provider = provider.provider_id
                fallback_reason = "MODEL_CIRCUIT_OPEN"
                self._record_model_event(
                    "MODEL_REQUEST_FAILED",
                    request,
                    severity=EventSeverity.WARNING,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt + 1,
                        "provider_id": provider.provider_id,
                        "error_code": fallback_reason,
                    },
                )
                continue
            half_open_probe = runtime.breaker.state is CircuitState.HALF_OPEN
            if request.model_id is not None and request.model_id not in provider.model_ids:
                if half_open_probe:
                    runtime.breaker.record_cancelled()
                last_error = CapabilityUnavailableError(
                    "Requested model ID is not exposed by the selected provider.",
                    component="model-router",
                    operation="complete",
                )
                last_failed_provider = provider.provider_id
                fallback_reason = "MODEL_ID_UNAVAILABLE"
                self._record_model_event(
                    "MODEL_REQUEST_FAILED",
                    request,
                    severity=EventSeverity.WARNING,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt + 1,
                        "provider_id": provider.provider_id,
                        "error_code": fallback_reason,
                    },
                )
                continue
            model_id = request.model_id or self._select_model(provider, selector)
            routed_request = request.model_copy(update={"model_id": model_id})
            if last_failed_provider is not None:
                self._record_model_event(
                    "MODEL_FALLBACK_SELECTED",
                    request,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt + 1,
                        "from_provider_id": last_failed_provider,
                        "provider_id": provider.provider_id,
                        "reason_code": fallback_reason or "MODEL_PROVIDER_UNAVAILABLE",
                    },
                )
            attempt += 1
            self._record_model_event(
                "MODEL_REQUEST_STARTED",
                request,
                payload={
                    "request_id": request.request_id,
                    "role": request.role.value,
                    "attempt": attempt,
                    "provider_id": provider.provider_id,
                    "model_id": model_id,
                    "stream_requested": request.stream,
                },
            )
            started = time.perf_counter()
            if self._queued_requests >= self.max_queued_requests:
                if half_open_probe:
                    runtime.breaker.record_cancelled()
                self._record_model_event(
                    "MODEL_QUEUE_BACKPRESSURE",
                    request,
                    severity=EventSeverity.WARNING,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "queued_requests": self._queued_requests,
                        "max_queued_requests": self.max_queued_requests,
                    },
                )
                raise ProviderUnavailableError(
                    "Model router queue backpressure limit reached.",
                    component="model-router",
                    operation="complete",
                    retryable=True,
                )
            self._queued_requests += 1
            dequeued = False
            try:
                async with asyncio.timeout(request.timeout_seconds), runtime.semaphore:
                    self._queued_requests = max(0, self._queued_requests - 1)
                    dequeued = True
                    response = await asyncio.wait_for(
                        provider.complete(routed_request), timeout=request.timeout_seconds
                    )
                if (
                    not isinstance(response, ModelResponse)
                    or response.request_id != request.request_id
                    or response.provider_id != provider.provider_id
                    or response.model_id != model_id
                ):
                    raise ProviderUnavailableError(
                        "Model provider returned a malformed or mismatched response.",
                        component="model-router",
                        operation="complete",
                    )
            except asyncio.CancelledError:
                if half_open_probe:
                    runtime.breaker.record_cancelled()
                if not dequeued:
                    self._queued_requests = max(0, self._queued_requests - 1)
                self._record_model_event(
                    "MODEL_REQUEST_CANCELLED",
                    request,
                    severity=EventSeverity.INFO,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt,
                        "provider_id": provider.provider_id,
                        "model_id": model_id,
                    },
                )
                raise
            except Exception as exc:
                if not dequeued:
                    self._queued_requests = max(0, self._queued_requests - 1)
                    if half_open_probe:
                        runtime.breaker.record_cancelled()
                else:
                    runtime.breaker.record_failure()
                    runtime.failures += 1
                last_error = exc
                last_failed_provider = provider.provider_id
                fallback_reason = (
                    "MODEL_QUEUE_TIMEOUT"
                    if not dequeued and isinstance(exc, TimeoutError)
                    else self._failure_code(exc)
                )
                self._record_model_event(
                    "MODEL_REQUEST_FAILED",
                    request,
                    severity=EventSeverity.WARNING,
                    payload={
                        "request_id": request.request_id,
                        "role": request.role.value,
                        "attempt": attempt,
                        "provider_id": provider.provider_id,
                        "model_id": model_id,
                        "error_code": fallback_reason,
                    },
                )
                continue
            runtime.breaker.record_success()
            runtime.failures = 0
            runtime.last_success_at = datetime.now(UTC)
            runtime.last_latency_ms = (time.perf_counter() - started) * 1000
            self._record_model_event(
                "MODEL_RESPONSE_COMPLETED",
                request,
                payload={
                    "request_id": request.request_id,
                    "role": request.role.value,
                    "attempt": attempt,
                    "provider_id": response.provider_id,
                    "model_id": response.model_id,
                    "latency_ms": round(runtime.last_latency_ms, 3),
                },
            )
            return response
        if isinstance(last_error, CapabilityUnavailableError):
            raise last_error
        raise ProviderUnavailableError(
            "All compatible model providers failed or were unavailable.",
            component="model-router",
            operation="complete",
            retryable=True,
            metadata={"provider_count": len(candidates)},
        ) from last_error

    async def complete_with_escalation(
        self,
        request: ModelRequest,
        *,
        selection: ModelSelectionRequest | None = None,
        escalate_on_high_complexity: bool = True,
    ) -> ModelResponse:
        """Route to fast/primary provider first and escalate to DEEP_REASONER when needed."""

        selector = selection or ModelSelectionRequest(
            role=request.role,
            task_type=request.role.value,
            privacy="cloud_allowed" if self.allow_cloud else "local_only",
            required_modalities=request.required_modalities,
            preferred_provider=None,
        )
        if escalate_on_high_complexity and selector.complexity == "high":
            deep_selector = selector.model_copy(update={"role": ModelRole.DEEP_REASONER})
            deep_req = request.model_copy(update={"role": ModelRole.DEEP_REASONER})
            if self._candidates(deep_selector):
                try:
                    return await self.complete(deep_req, selection=deep_selector)
                except (CapabilityUnavailableError, ProviderUnavailableError):
                    pass
        try:
            return await self.complete(request, selection=selector)
        except (CapabilityUnavailableError, ProviderUnavailableError):
            deep_selector = selector.model_copy(update={"role": ModelRole.DEEP_REASONER})
            deep_req = request.model_copy(update={"role": ModelRole.DEEP_REASONER})
            if self._candidates(deep_selector):
                self._record_model_event(
                    "MODEL_ESCALATED_DEEP_REASONER",
                    request,
                    payload={"request_id": request.request_id, "from_role": request.role.value},
                )
                return await self.complete(deep_req, selection=deep_selector)
            raise

    async def stream(
        self,
        request: ModelRequest,
        *,
        selection: ModelSelectionRequest | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Stream chunks from a streaming-capable provider with fallback and circuit protection."""

        stream_request = request.model_copy(update={"stream": True})
        selector = selection or ModelSelectionRequest(
            role=stream_request.role,
            task_type=stream_request.role.value,
            privacy="cloud_allowed" if self.allow_cloud else "local_only",
            required_modalities=stream_request.required_modalities,
            preferred_provider=None,
        )
        selected = self.select(selector)
        candidates = self._candidates(selector)
        candidates.sort(key=lambda item: item.provider.provider_id != selected.provider_id)
        last_error: BaseException | None = None
        for runtime in candidates:
            provider = runtime.provider
            if not getattr(provider, "supports_streaming", False):
                continue
            if (
                stream_request.model_id is not None
                and stream_request.model_id not in provider.model_ids
            ):
                continue
            if self._queued_requests >= self.max_queued_requests:
                raise ProviderUnavailableError(
                    "Model router queue backpressure limit reached.",
                    component="model-router",
                    operation="stream",
                    retryable=True,
                )
            if not runtime.breaker.allow_request():
                continue
            half_open_probe = runtime.breaker.state is CircuitState.HALF_OPEN
            model_id = stream_request.model_id or self._select_model(provider, selector)
            routed = stream_request.model_copy(update={"model_id": model_id})
            stream_fn = getattr(provider, "stream", None)
            started = time.perf_counter()
            emitted = False
            self._queued_requests += 1
            dequeued = False
            try:
                # The streaming deadline includes admission wait, not just network I/O.
                async with asyncio.timeout(routed.timeout_seconds), runtime.semaphore:
                    self._queued_requests -= 1
                    dequeued = True
                    if callable(stream_fn):
                        seq = 0
                        final = False
                        async with aclosing(stream_fn(routed)) as chunks:
                            async for chunk in chunks:
                                if (
                                    not isinstance(chunk, ModelStreamChunk)
                                    or chunk.request_id != routed.request_id
                                    or chunk.provider_id != provider.provider_id
                                    or chunk.model_id != model_id
                                    or chunk.sequence != seq
                                ):
                                    raise ValueError("malformed model stream identity or sequence")
                                emitted = True
                                final = chunk.is_final
                                yield chunk
                                seq += 1
                                if final:
                                    break
                        if not final:
                            raise ValueError("model stream ended without a final chunk")
                    else:
                        resp = await provider.complete(routed.model_copy(update={"stream": False}))
                        if (
                            not isinstance(resp, ModelResponse)
                            or resp.request_id != routed.request_id
                            or resp.provider_id != provider.provider_id
                            or resp.model_id != model_id
                        ):
                            raise ValueError("malformed model response identity")
                        emitted = True
                        yield ModelStreamChunk(
                            request_id=routed.request_id,
                            provider_id=provider.provider_id,
                            model_id=model_id,
                            sequence=0,
                            text_delta=resp.content,
                            is_final=True,
                            finish_reason=resp.finish_reason or "stop",
                        )
                runtime.breaker.record_success()
                runtime.failures = 0
                runtime.last_success_at = datetime.now(UTC)
                runtime.last_latency_ms = (time.perf_counter() - started) * 1000
                return
            except (asyncio.CancelledError, GeneratorExit):
                if half_open_probe:
                    runtime.breaker.record_cancelled()
                raise
            except Exception as exc:
                if dequeued:
                    runtime.breaker.record_failure()
                    runtime.failures += 1
                elif half_open_probe:
                    runtime.breaker.record_cancelled()
                last_error = exc
                if emitted:
                    # Never concatenate a replacement answer onto already-delivered output.
                    raise ProviderUnavailableError(
                        "Model stream interrupted after output; no fallback was attempted.",
                        component="model-router",
                        operation="stream",
                        retryable=False,
                    ) from None
            finally:
                if not dequeued:
                    self._queued_requests = max(0, self._queued_requests - 1)
        raise ProviderUnavailableError(
            "No streaming model provider succeeded.",
            component="model-router",
            operation="stream",
            retryable=True,
        ) from last_error

    def _record_model_event(
        self,
        event_type: str,
        request: ModelRequest,
        *,
        payload: dict[str, str | int | float | bool | None],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        if self.events is None:
            return
        self.events.append(
            EventEnvelope(
                event_type=event_type,
                task_id=request.task_id,
                session_id=request.session_id,
                correlation_id=request.correlation_id,
                source="model-router",
                severity=severity,
                payload=payload,
            )
        )

    @staticmethod
    def _failure_code(error: BaseException) -> str:
        if isinstance(error, TimeoutError):
            return "MODEL_PROVIDER_TIMEOUT"
        if isinstance(error, CircuitOpenError):
            return "MODEL_CIRCUIT_OPEN"
        if isinstance(error, CapabilityUnavailableError):
            return "MODEL_CAPABILITY_UNAVAILABLE"
        if isinstance(error, ProviderUnavailableError):
            return "MODEL_PROVIDER_UNAVAILABLE"
        return "MODEL_PROVIDER_FAILED"

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
