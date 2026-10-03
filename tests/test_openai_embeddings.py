from __future__ import annotations

import unittest

import httpx

from arise.adapters.openai_embeddings import (
    EmbeddingProviderUnavailable,
    OpenAICompatibleEmbeddingAdapter,
)
from arise.adapters.secrets import MemorySecretProvider
from arise.core.extensions import EmbeddingResult


class OpenAICompatibleEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_openai_compatible_response_is_validated(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "http://127.0.0.1:1234/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer local-key")
            self.assertEqual(request.read(), b'{"model":"local-embed","input":"safe text"}')
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.2, -0.4, 0.7]}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = OpenAICompatibleEmbeddingAdapter(
            base_url="http://127.0.0.1:1234/v1",
            model_id="local-embed",
            api_key_secret_name="LOCAL_KEY",
            secret_provider=MemorySecretProvider({"LOCAL_KEY": "local-key"}),
            is_cloud=False,
            client=client,
        )
        try:
            result = await adapter.embed("safe text", correlation_id="task-1")
        finally:
            await adapter.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(result, EmbeddingResult("local-embed", (0.2, -0.4, 0.7)))

    async def test_invalid_vector_and_provider_error_are_sanitized(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json={"data": [{"embedding": [0, 0, 0]}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = OpenAICompatibleEmbeddingAdapter(
            base_url="http://localhost:8000",
            model_id="local-embed",
            api_key_secret_name="LOCAL_KEY",
            secret_provider=MemorySecretProvider(),
            is_cloud=False,
            client=client,
        )
        try:
            with self.assertRaisesRegex(EmbeddingProviderUnavailable, "valid vector"):
                await adapter.embed("safe text", correlation_id="task-1")
        finally:
            await adapter.close()

    def test_endpoint_policy_rejects_remote_http_and_cloud_http(self) -> None:
        for base_url, is_cloud in (
            ("http://192.168.1.20:8000/v1", False),
            ("http://embedding.example.org/v1", True),
            ("https://user:pass@example.org/v1", True),
        ):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                OpenAICompatibleEmbeddingAdapter(
                    base_url=base_url,
                    model_id="embedding-model",
                    api_key_secret_name="EMBEDDING_KEY",
                    secret_provider=MemorySecretProvider(),
                    is_cloud=is_cloud,
                )


if __name__ == "__main__":
    unittest.main()
