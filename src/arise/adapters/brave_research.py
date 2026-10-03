"""Opt-in Brave Search and public-page retrieval adapter.

Search credentials are resolved by reference at request time. Provider snippets and fetched
page text are untrusted context only; callers must not treat them as instructions or authority.
Page requests use HTTPS, reject non-public DNS answers, pin the validated address for the TLS
connection, disable environment proxies, and validate each redirect independently.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.extensions import (
    MAX_CONTEXT_TEXT_CHARS,
    ContextSource,
    ResearchQuery,
    RetrievedContext,
    WebResearchPort,
)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


class ResearchProviderUnavailable(RuntimeError):
    """The optional search provider is not configured or could not complete a request."""


class ResearchSecurityError(ValueError):
    """An external source URL or resolved address violates the network policy."""


class ResearchFetchError(RuntimeError):
    """A public source could not be fetched within the configured limits."""


class PageFetcher(Protocol):
    """Port for extracting bounded text from a public HTTPS page."""

    def fetch_text(self, url: str) -> str: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, *, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), timeout=self.timeout
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


class PublicHTTPSPageFetcher(PageFetcher):
    """Fetch small public HTTPS pages without proxies, cookies, or unpinned DNS lookups."""

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
    _DROP_TAGS = frozenset(
        {"script", "style", "noscript", "svg", "iframe", "form", "nav", "header", "footer", "aside"}
    )
    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_bytes: int = 512 * 1024,
        max_redirects: int = 3,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 60:
            raise ValueError("page fetch timeout must be between 0.1 and 60 seconds")
        if not 1024 <= max_bytes <= 4 * 1024 * 1024:
            raise ValueError("page fetch limit must be between 1 KiB and 4 MiB")
        if not 0 <= max_redirects <= 5:
            raise ValueError("page redirect limit must be between zero and five")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver or self._resolve_public_addresses

    def fetch_text(self, url: str) -> str:
        current_url = _normalize_https_url(url)
        for redirect_number in range(self.max_redirects + 1):
            parts = urlsplit(current_url)
            hostname = _ascii_hostname(parts.hostname or "")
            addresses = tuple(self.resolver(hostname))
            if not addresses:
                raise ResearchFetchError("The source host did not resolve to an address.")
            if any(not _is_public_address(address) for address in addresses):
                raise ResearchSecurityError("The source host resolved to a non-public address.")

            response: tuple[int, Message, bytes] | None = None
            last_error: OSError | http.client.HTTPException | ssl.SSLError | TimeoutError | None = (
                None
            )
            for address in addresses:
                connection = _PinnedHTTPSConnection(
                    hostname,
                    address,
                    timeout=self.timeout_seconds,
                )
                try:
                    target = parts.path or "/"
                    if parts.query:
                        target = f"{target}?{parts.query}"
                    connection.request(
                        "GET",
                        target,
                        headers={
                            "Host": hostname,
                            "User-Agent": "ARISE-Research/1.0 (+local user-initiated lookup)",
                            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                            "Accept-Encoding": "identity",
                            "Connection": "close",
                        },
                    )
                    raw = connection.getresponse()
                    if raw.status in self._REDIRECT_STATUSES:
                        location = raw.getheader("Location")
                        if not location:
                            raise ResearchFetchError("The source returned an invalid redirect.")
                        response = (raw.status, raw.headers, location.encode("utf-8"))
                        break
                    if raw.status != 200:
                        raise ResearchFetchError("The source returned a non-success response.")
                    length = raw.getheader("Content-Length")
                    if length is not None:
                        try:
                            if int(length) < 0 or int(length) > self.max_bytes:
                                raise ResearchFetchError(
                                    "The source exceeded the response-size limit."
                                )
                        except ValueError as exc:
                            raise ResearchFetchError(
                                "The source returned an invalid response length."
                            ) from exc
                    body = bytearray()
                    while len(body) <= self.max_bytes:
                        chunk = raw.read(min(8192, self.max_bytes + 1 - len(body)))
                        if not chunk:
                            break
                        body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ResearchFetchError("The source exceeded the response-size limit.")
                    response = (raw.status, raw.headers, bytes(body))
                    break
                except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
                    last_error = exc
                finally:
                    connection.close()
            if response is None:
                raise ResearchFetchError("The public source could not be reached.") from last_error

            status, headers, body = response
            if status in self._REDIRECT_STATUSES:
                if redirect_number >= self.max_redirects:
                    raise ResearchFetchError("The source exceeded the redirect limit.")
                current_url = _normalize_https_url(
                    urljoin(current_url, body.decode("utf-8", errors="replace"))
                )
                continue
            content_type = headers.get("Content-Type", "").lower()
            if not any(
                content_type.startswith(media_type)
                for media_type in ("text/html", "application/xhtml+xml", "text/plain")
            ):
                raise ResearchFetchError("The source is not a supported text document.")
            message = Message()
            message["Content-Type"] = headers.get("Content-Type", "text/html")
            charset = message.get_content_charset() or "utf-8"
            text = body.decode(charset, errors="replace")
            if content_type.startswith("text/plain"):
                extracted = text
            else:
                parser = _VisibleTextParser()
                try:
                    parser.feed(text)
                    extracted = parser.get_text()
                except Exception as exc:
                    raise ResearchFetchError("The source document could not be parsed.") from exc
            normalized = re.sub(r"\n\s*\n+", "\n\n", extracted).strip()
            return normalized[:MAX_CONTEXT_TEXT_CHARS]
        raise ResearchFetchError("The source could not be retrieved.")

    @staticmethod
    def _resolve_public_addresses(hostname: str) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                answers = socket.getaddrinfo(
                    hostname,
                    443,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except OSError as exc:
                raise ResearchFetchError("The source host could not be resolved.") from exc
            addresses = tuple(
                dict.fromkeys(answer[4][0].split("%", maxsplit=1)[0] for answer in answers)
            )
        else:
            addresses = (str(literal),)
        if not addresses or any(not _is_public_address(address) for address in addresses):
            raise ResearchSecurityError("The source host resolved to a non-public address.")
        return addresses


class _VisibleTextParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "main",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if self._hidden:
            if tag not in PublicHTTPSPageFetcher._VOID_TAGS:
                self._hidden.append(tag)
            return
        if tag in PublicHTTPSPageFetcher._DROP_TAGS:
            if tag not in PublicHTTPSPageFetcher._VOID_TAGS:
                self._hidden.append(tag)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._hidden:
            try:
                index = len(self._hidden) - 1 - self._hidden[::-1].index(tag)
            except ValueError:
                return
            del self._hidden[index:]
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        value = re.sub(r"\s+", " ", data)
        if value:
            self._parts.append(value)

    def get_text(self) -> str:
        return re.sub(r"[ \t]+", " ", "".join(self._parts))


def _normalize_https_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        hostname = _ascii_hostname(parts.hostname or "")
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise ResearchSecurityError("The source URL is invalid.") from exc
    if (
        parts.scheme.lower() != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise ResearchSecurityError("Only public HTTPS source URLs on port 443 are allowed.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname or hostname.endswith((".local", ".internal", ".localhost")):
            raise ResearchSecurityError("The source URL hostname is not public DNS.") from None
    else:
        if not _is_public_address(str(address)):
            raise ResearchSecurityError("The source URL is not a public address.")
    return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _ascii_hostname(hostname: str) -> str:
    if not hostname or "%" in hostname or "\\" in hostname:
        raise ResearchSecurityError("The source URL hostname is invalid.")
    return hostname.encode("idna").decode("ascii").lower().rstrip(".")


def _is_public_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not (
        parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_multicast
    )


def _domain_allowed(hostname: str, domains: tuple[str, ...]) -> bool:
    if not domains:
        return True
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


class BraveWebResearchAdapter(WebResearchPort):
    """Brave Search API client with bounded, provenance-preserving source extraction."""

    def __init__(
        self,
        *,
        secret_provider: SecretProvider,
        api_key_secret_name: str = "BRAVE_SEARCH_API_KEY",
        max_results: int = 8,
        max_fetches: int = 5,
        timeout_seconds: float = 10.0,
        max_source_bytes: int = 512 * 1024,
        fetch_pages: bool = True,
        client: httpx.AsyncClient | None = None,
        page_fetcher: PageFetcher | None = None,
    ) -> None:
        if not api_key_secret_name.strip():
            raise ValueError("a secret reference is required")
        if not 1 <= max_results <= 25 or not 0 <= max_fetches <= 10:
            raise ValueError("research result and page-fetch limits are invalid")
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("research timeout must be between 0.1 and 120 seconds")
        if not 1024 <= max_source_bytes <= 4 * 1024 * 1024:
            raise ValueError("research source size must be between 1 KiB and 4 MiB")
        self.secret_provider = secret_provider
        self.api_key_secret_name = api_key_secret_name
        self.max_results = max_results
        self.max_fetches = max_fetches
        self.timeout_seconds = timeout_seconds
        self.fetch_pages = fetch_pages
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._page_fetcher = page_fetcher or PublicHTTPSPageFetcher(
            timeout_seconds=timeout_seconds,
            max_bytes=max_source_bytes,
        )
        self._semaphore = asyncio.Semaphore(2)

    async def search(self, query: ResearchQuery) -> Sequence[RetrievedContext]:
        try:
            api_key = self.secret_provider.get_secret(self.api_key_secret_name)
        except SecretUnavailable as exc:
            raise ResearchProviderUnavailable(
                "The configured Brave Search credential is unavailable."
            ) from exc
        count = min(query.max_results, self.max_results)
        params = {"q": query.query, "count": str(count), "safesearch": "moderate"}
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": "ARISE-Research/1.0",
        }
        async with self._semaphore:
            try:
                async with self._client.stream(
                    "GET",
                    BRAVE_SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_SEARCH_RESPONSE_BYTES:
                        raise ResearchProviderUnavailable(
                            "The search provider response was too large."
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_SEARCH_RESPONSE_BYTES:
                            raise ResearchProviderUnavailable(
                                "The search provider response was too large."
                            )
                        body.extend(chunk)
                payload = json.loads(body)
            except ResearchProviderUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise ResearchProviderUnavailable(
                    "The web research provider did not return a valid response."
                ) from exc

        hits = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
        if not isinstance(hits, list):
            raise ResearchProviderUnavailable(
                "The web research provider returned an invalid result set."
            )
        contexts: list[RetrievedContext] = []
        fetch_count = 0
        for index, hit in enumerate(hits[:count]):
            if not isinstance(hit, dict):
                continue
            title = self._bounded_text(hit.get("title"), 512)
            snippet = self._bounded_text(hit.get("description"), MAX_CONTEXT_TEXT_CHARS)
            raw_url = hit.get("url")
            if not title or not isinstance(raw_url, str):
                continue
            try:
                source_url = _normalize_https_url(raw_url)
                host = _ascii_hostname(urlsplit(source_url).hostname or "")
            except ResearchSecurityError:
                continue
            if not _domain_allowed(host, query.allowed_domains):
                continue

            text = ""
            if self.fetch_pages and fetch_count < self.max_fetches:
                fetch_count += 1
                try:
                    text = await asyncio.to_thread(self._page_fetcher.fetch_text, source_url)
                except ResearchSecurityError:
                    # Do not return even the snippet when DNS or a redirect violates egress policy.
                    continue
                except (ResearchFetchError, OSError, TimeoutError):
                    text = ""
            if not text.strip():
                text = snippet
            if not text.strip():
                continue
            provenance = f"Brave Search · {title} · {host}"
            contexts.append(
                RetrievedContext(
                    source=ContextSource.WEB_RESEARCH,
                    source_id=source_url,
                    text=text[:MAX_CONTEXT_TEXT_CHARS],
                    provenance=provenance[:2048],
                    retrieved_at=datetime.now(UTC),
                    relevance=max(0.0, min(1.0, 1.0 - index / max(1, count))),
                )
            )
        return tuple(contexts)

    @staticmethod
    def _bounded_text(value: object, maximum: int) -> str:
        return value.strip()[:maximum] if isinstance(value, str) else ""

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
