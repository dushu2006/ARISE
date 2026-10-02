from __future__ import annotations

import asyncio
import unittest

import httpx

from arise.adapters.openai_compatible import MAX_MODEL_RESPONSE_BYTES, OpenAICompatibleProvider
from arise.adapters.secrets import MemorySecretProvider
from arise.core.errors import CapabilityUnavailableError, ProviderUnavailableError
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
