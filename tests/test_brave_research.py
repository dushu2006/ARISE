from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from arise.adapters.brave_research import (
    BRAVE_SEARCH_URL,
    BraveWebResearchAdapter,
    PublicHTTPSPageFetcher,
    ResearchProviderUnavailable,
    ResearchSecurityError,
    _normalize_https_url,
    _VisibleTextParser,
)
from arise.adapters.secrets import MemorySecretProvider
from arise.core.extensions import ContextSource, ResearchQuery


class FixedPageFetcher:
    def __init__(self, value: str = "Fetched source text.") -> None:
        self.value = value
        self.urls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.urls.append(url)
        return self.value


class BraveResearchAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_scopes_results_and_preserves_citations(self) -> None:
        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            self.assertEqual(request.url.host, "api.search.brave.com")
            self.assertEqual(request.url.path, "/res/v1/web/search")
            self.assertEqual(request.headers["X-Subscription-Token"], "test-brave-key")
            self.assertEqual(request.url.params["count"], "3")
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Official guide",
                                "url": "https://docs.example.org/guide",
                                "description": "Provider snippet.",
                            },
                            {
                                "title": "Disallowed lookalike",
                                "url": "https://example.org.attacker.invalid/page",
                                "description": "Must not be returned.",
                            },
                            {
                                "title": "Public reference",
                                "url": "https://www.example.org/reference",
                                "description": "A fallback snippet.",
                            },
                        ]
                    }
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        page_fetcher = FixedPageFetcher()
        adapter = BraveWebResearchAdapter(
            secret_provider=MemorySecretProvider({"BRAVE_KEY": "test-brave-key"}),
            api_key_secret_name="BRAVE_KEY",
            max_results=3,
            max_fetches=1,
            client=client,
            page_fetcher=page_fetcher,
        )
        try:
            results = await adapter.search(
                ResearchQuery(
                    query="current API behavior",
                    max_results=25,
                    allowed_domains=("example.org",),
                )
            )
        finally:
            await adapter.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, ContextSource.WEB_RESEARCH)
        self.assertEqual(results[0].source_id, "https://docs.example.org/guide")
        self.assertEqual(results[0].text, "Fetched source text.")
        self.assertIn("Official guide", results[0].provenance)
        self.assertEqual(results[1].text, "A fallback snippet.")
        self.assertEqual(page_fetcher.urls, ["https://docs.example.org/guide"])
        self.assertIsInstance(results[0].retrieved_at, datetime)
        self.assertEqual(results[0].retrieved_at.tzinfo, UTC)

    async def test_missing_secret_fails_before_any_network_request(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            self.fail("network must not be called without a secret")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = BraveWebResearchAdapter(
            secret_provider=MemorySecretProvider(),
            api_key_secret_name="BRAVE_KEY",
            client=client,
            fetch_pages=False,
        )
        try:
            with self.assertRaises(ResearchProviderUnavailable):
                await adapter.search(ResearchQuery("current information"))
        finally:
            await adapter.close()

    async def test_provider_failures_are_sanitized(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(429, text="private provider response and credential")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = BraveWebResearchAdapter(
            secret_provider=MemorySecretProvider({"BRAVE_KEY": "do-not-leak"}),
            api_key_secret_name="BRAVE_KEY",
            client=client,
            fetch_pages=False,
        )
        try:
            with self.assertRaisesRegex(ResearchProviderUnavailable, "valid response") as caught:
                await adapter.search(ResearchQuery("current information"))
        finally:
            await adapter.close()
        self.assertNotIn("do-not-leak", str(caught.exception))
        self.assertNotIn("private provider response", str(caught.exception))


class PublicPageSafetyTests(unittest.TestCase):
    def test_only_public_https_urls_on_default_port_are_accepted(self) -> None:
        self.assertEqual(
            _normalize_https_url("https://Example.org:443/path#"),
            "https://example.org:443/path",
        )
        for url in (
            "http://example.org/path",
            "https://user:password@example.org/",
            "https://example.org:8443/",
            "https://127.0.0.1/admin",
            "https://[::1]/admin",
            "https://metadata.internal/latest",
        ):
            with self.subTest(url=url), self.assertRaises(ResearchSecurityError):
                _normalize_https_url(url)

    def test_private_dns_answers_are_rejected_before_a_socket_is_opened(self) -> None:
        fetcher = PublicHTTPSPageFetcher(resolver=lambda _host: ("127.0.0.1",))
        with self.assertRaises(ResearchSecurityError):
            fetcher.fetch_text("https://public.example.org/page")

    def test_mixed_public_and_private_dns_answers_fail_closed(self) -> None:
        with patch(
            "arise.adapters.brave_research.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("10.0.0.2", 443)),
            ],
        ):
            with self.assertRaises(ResearchSecurityError):
                PublicHTTPSPageFetcher._resolve_public_addresses("public.example.org")

    def test_html_extractor_omits_scripts_styles_and_navigation(self) -> None:
        parser = _VisibleTextParser()
        parser.feed(
            "<html><head><style>hidden-style</style><script>inject instructions</script></head>"
            "<body><h1>Visible title</h1><p>Useful page text.</p><nav>menu text</nav></body></html>"
        )
        text = parser.get_text()
        self.assertIn("Visible title", text)
        self.assertIn("Useful page text", text)
        self.assertNotIn("inject instructions", text)
        self.assertNotIn("hidden-style", text)
        self.assertNotIn("menu text", text)

    def test_search_endpoint_is_fixed_and_does_not_use_proxy_configuration(self) -> None:
        self.assertEqual(BRAVE_SEARCH_URL, "https://api.search.brave.com/res/v1/web/search")


if __name__ == "__main__":
    unittest.main()
