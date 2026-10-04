from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from arise.adapters.openai_compatible import MAX_MODEL_RESPONSE_BYTES, OpenAICompatibleProvider
from arise.adapters.secrets import MemorySecretProvider
from arise.core.errors import CapabilityUnavailableError, ProviderUnavailableError
from arise.core.events import InMemoryEventStore
from arise.core.model_gateway import ModelRouter
from arise.core.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelSelectionRequest,
)


class StaticProvider:
    provider_id = "local-test"
    model_ids = ("model-a",)
    is_cloud = False
    max_concurrent_requests = 2

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests = 0

    def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
        return role is ModelRole.PLANNER and modalities == frozenset({"text"})

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests += 1
        if self.fail:
            raise RuntimeError("provider body must not be surfaced")
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model_id=request.model_id or "model-a",
            content='{"steps":[]}',
            latency_ms=1,
        )


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    def make_request(self, *, model_id: str | None = None) -> ModelRequest:
        return ModelRequest(
            role=ModelRole.PLANNER,
            messages=(ModelMessage(role="user", content="Plan a task"),),
            model_id=model_id,
            stream=False,
            timeout_seconds=1,
        )

    async def test_router_requires_registered_eligible_provider(self) -> None:
        router = ModelRouter()
        with self.assertRaises(CapabilityUnavailableError):
            await router.complete(self.make_request())

        provider = StaticProvider()
        router.register(provider)
        result = await router.complete(self.make_request())
        self.assertEqual(result.provider_id, "local-test")
        self.assertEqual(provider.requests, 1)
        self.assertEqual(router.status()[0].status.value, "available")

    async def test_model_lifecycle_events_are_correlated_and_do_not_store_prompts(self) -> None:
        events = InMemoryEventStore()
        provider = StaticProvider()
        router = ModelRouter(events=events)
        router.register(provider)
        request = self.make_request().model_copy(
            update={"task_id": "task-1", "session_id": "session-1"}
        )

        await router.complete(request)
        recorded = events.read_after()

        self.assertEqual(
            [event.event_type for event in recorded],
            ["MODEL_REQUEST_STARTED", "MODEL_RESPONSE_COMPLETED"],
        )
        self.assertTrue(all(event.task_id == "task-1" for event in recorded))
        self.assertTrue(all(event.session_id == "session-1" for event in recorded))
        self.assertTrue(all(event.correlation_id == request.correlation_id for event in recorded))
        self.assertNotIn("Plan a task", repr([event.payload for event in recorded]))
        self.assertTrue(all(event.source == "model-router" for event in recorded))

    async def test_fallback_and_cancellation_are_journaled_without_exception_text(self) -> None:
        class FailingProvider(StaticProvider):
            provider_id = "a-failing"

        class SuccessfulProvider(StaticProvider):
            provider_id = "z-success"

        events = InMemoryEventStore()
        router = ModelRouter(events=events)
        router.register(FailingProvider(fail=True))
        router.register(SuccessfulProvider())
        await router.complete(self.make_request())
        types = [event.event_type for event in events.read_after()]
        self.assertEqual(
            types,
            [
                "MODEL_REQUEST_STARTED",
                "MODEL_REQUEST_FAILED",
                "MODEL_FALLBACK_SELECTED",
                "MODEL_REQUEST_STARTED",
                "MODEL_RESPONSE_COMPLETED",
            ],
        )
        self.assertNotIn("provider body", repr([event.payload for event in events.read_after()]))

        class BlockingProvider(StaticProvider):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self.started.set()
                await self.release.wait()
                return await super().complete(request)

        cancellation_events = InMemoryEventStore()
        blocking = BlockingProvider()
        cancellable = ModelRouter(events=cancellation_events)
        cancellable.register(blocking)
        pending = asyncio.create_task(cancellable.complete(self.make_request()))
        await asyncio.wait_for(blocking.started.wait(), timeout=1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(
            [event.event_type for event in cancellation_events.read_after()],
            ["MODEL_REQUEST_STARTED", "MODEL_REQUEST_CANCELLED"],
        )

    async def test_cloud_route_requires_explicit_policy_and_privacy(self) -> None:
        class CloudProvider(StaticProvider):
            provider_id = "cloud-test"
            is_cloud = True

        router = ModelRouter(allow_cloud=False)
        router.register(CloudProvider())
        selection = ModelSelectionRequest(
            role=ModelRole.PLANNER,
            task_type="test",
            privacy="cloud_allowed",
        )
        with self.assertRaises(CapabilityUnavailableError):
            await router.complete(self.make_request(), selection=selection)

    async def test_provider_calls_respect_the_shared_concurrency_limit(self) -> None:
        class BlockingProvider(StaticProvider):
            def __init__(self) -> None:
                super().__init__()
                self.release = asyncio.Event()
                self.two_started = asyncio.Event()
                self.active = 0
                self.max_active = 0

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active == 2:
                    self.two_started.set()
                try:
                    await self.release.wait()
                    return await super().complete(request)
                finally:
                    self.active -= 1

        provider = BlockingProvider()
        router = ModelRouter(max_concurrent_requests=2)
        router.register(provider)
        requests = [self.make_request() for _ in range(4)]
        pending = [asyncio.create_task(router.complete(request)) for request in requests]
        await asyncio.wait_for(provider.two_started.wait(), timeout=1)
        self.assertEqual(provider.active, 2)
        self.assertEqual(provider.max_active, 2)

        provider.release.set()
        responses = await asyncio.gather(*pending)
        self.assertEqual(len(responses), 4)
        self.assertEqual(provider.max_active, 2)

    async def test_provider_failures_are_sanitized(self) -> None:
        router = ModelRouter(circuit_failure_threshold=1)
        router.register(StaticProvider(fail=True))
        with self.assertRaises(ProviderUnavailableError) as caught:
            await router.complete(self.make_request())
        self.assertNotIn("provider body", str(caught.exception))
        self.assertEqual(router.status()[0].status.value, "degraded")

    async def test_openai_compatible_adapter_uses_secret_reference_not_payload(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": '{"ok":true}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            provider_id="mock-local",
            base_url="http://127.0.0.1:1234/v1",
            model_id="model-a",
            api_key_secret_name="LOCAL_MODEL_KEY",
            secret_provider=MemorySecretProvider({"LOCAL_MODEL_KEY": "not-in-db"}),
            is_cloud=False,
            client=client,
        )
        request = self.make_request()
        response = await provider.complete(request)
        self.assertEqual(response.request_id, request.request_id)
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(calls[0].url.path, "/v1/chat/completions")
        self.assertEqual(calls[0].headers["authorization"], "Bearer not-in-db")
        self.assertNotIn("not-in-db", calls[0].content.decode())
        self.assertEqual(provider.provider_options, {})
        self.assertEqual(
            set(json.loads(calls[0].content)),
            {"model", "messages", "max_tokens", "temperature", "stream"},
        )
        await client.aclose()

    async def test_provider_options_are_serialized_without_credentials(self) -> None:
        calls: list[httpx.Request] = []
        api_key = "nvidia-test-secret-marker"
        provider_options = {"chat_template_kwargs": {"enable_thinking": False}}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ARISE NVIDIA GATEWAY OK"}}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            provider_id="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            model_id="nvidia/nemotron-test",
            api_key_secret_name="NVIDIA_API_KEY",
            secret_provider=MemorySecretProvider({"NVIDIA_API_KEY": api_key}),
            is_cloud=True,
            provider_options=provider_options,
            client=client,
        )
        router = ModelRouter(allow_cloud=True)
        router.register(provider)
        selection = ModelSelectionRequest(
            role=ModelRole.PLANNER,
            task_type="nvidia-gateway-regression",
            privacy="cloud_allowed",
        )
        try:
            response = await router.complete(self.make_request(), selection=selection)
            request_payload = json.loads(calls[0].content)

            self.assertEqual(response.content, "ARISE NVIDIA GATEWAY OK")
            self.assertEqual(request_payload["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(provider.provider_options, provider_options)
            self.assertEqual(calls[0].headers["authorization"], f"Bearer {api_key}")
            self.assertNotIn(api_key, calls[0].content.decode())
            self.assertNotIn("api_key", calls[0].content.decode().casefold())
            status = router.status()[0]
            self.assertEqual(status.provider_id, "nvidia")
            self.assertEqual(status.status.value, "available")
        finally:
            await client.aclose()

    async def test_provider_options_are_sent_with_streaming_requests(self) -> None:
        calls: list[httpx.Request] = []
        api_key = "stream-test-secret-marker"
        provider_options = {
            "chat_template_kwargs": {"enable_thinking": False},
            "top_k": 16,
        }
        sse = (
            'data: {"choices":[{"delta":{"content":"Final "},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse.encode("utf-8"),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            provider_id="nvidia-stream",
            base_url="https://integrate.api.nvidia.com/v1",
            model_id="nvidia/nemotron-test",
            api_key_secret_name="NVIDIA_API_KEY",
            secret_provider=MemorySecretProvider({"NVIDIA_API_KEY": api_key}),
            is_cloud=True,
            provider_options=provider_options,
            supports_streaming=True,
            client=client,
        )
        try:
            chunks = [chunk async for chunk in provider.stream(self.make_request())]
            request_payload = json.loads(calls[0].content)

            self.assertEqual("".join(chunk.text_delta for chunk in chunks), "Final answer")
            self.assertTrue(any(chunk.is_final for chunk in chunks))
            self.assertTrue(request_payload["stream"])
            self.assertEqual(request_payload["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(request_payload["top_k"], 16)
            self.assertEqual(calls[0].headers["authorization"], f"Bearer {api_key}")
            self.assertNotIn(api_key, calls[0].content.decode())
        finally:
            await client.aclose()

    async def test_provider_response_size_is_bounded_and_sanitized(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (MAX_MODEL_RESPONSE_BYTES + 1))
            )
        )
        provider = OpenAICompatibleProvider(
            provider_id="bounded-local",
            base_url="http://127.0.0.1:1234/v1",
            model_id="model-a",
            api_key_secret_name="LOCAL_MODEL_KEY",
            secret_provider=MemorySecretProvider(),
            is_cloud=False,
            client=client,
        )
        try:
            with self.assertRaises(ProviderUnavailableError) as caught:
                await provider.complete(self.make_request())
            self.assertNotIn("xxxxx", str(caught.exception))
        finally:
            await client.aclose()

    async def test_remote_plaintext_endpoint_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider(
                provider_id="cloud",
                base_url="http://models.example.test/v1",
                model_id="model-a",
                api_key_secret_name="KEY",
                secret_provider=MemorySecretProvider({"KEY": "secret"}),
                is_cloud=True,
            )
