"""Optional, isolated Playwright browser adapter.

Playwright is imported only when :meth:`PlaywrightBrowserProvider.start` is called.
The provider creates a fresh, non-persistent browser context and does not attach to
an existing user browser. DOM observations are bounded, omit form values, and
carry short leases that are rechecked immediately before a browser action.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.computer import (
    BrowserTabRecord,
    ComputerFailureCode,
    CoordinateSpace,
    PerceptionSource,
    Rect,
    ResolutionStatus,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
    TargetResolution,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    Idempotency,
    ObservationLease,
    RiskLevel,
    SecretRef,
    TargetIdentity,
    canonical_json,
    utc_now,
    validate_safe_token,
)
from arise.core.grounding import TargetResolver
from arise.core.ports import (
    EvidenceRecord,
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
    VerificationResult,
    VerificationStatus,
)
from arise.core.redaction import DEFAULT_REDACTOR
from arise.core.resources import ResourceLease, ResourceLeaseLost

_MAX_DOM_ELEMENTS = 512
_MAX_OBSERVATIONS = 256


_DOM_SNAPSHOT_SCRIPT = r"""(limit) => {
  const selector = [
    'button', 'a[href]', 'input:not([type="hidden"])', 'textarea', 'select',
    '[role]', '[tabindex]:not([tabindex="-1"])', '[contenteditable]:not([contenteditable="false"])'
  ].join(',');
  const clean = (value, max = 512) =>
    String(value || '').slice(0, max * 4).replace(/\s+/g, ' ').trim().slice(0, max);
  const textOf = (element) => {
    if (!element || element.isContentEditable) return '';
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const parts = [];
    let size = 0;
    let visitedText = 0;
    while (size < 512 && visitedText < 256 && walker.nextNode()) {
      visitedText += 1;
      const node = walker.currentNode;
      if (node.parentElement?.closest('script,style,noscript,template')) continue;
      const piece = clean(node.nodeValue || '', Math.min(512 - size, 512));
      if (piece) {
        parts.push(piece);
        size += piece.length + 1;
      }
    }
    return clean(parts.join(' '), 512);
  };
  const roleOf = (element, type) => {
    const explicit = clean(element.getAttribute('role'), 64);
    if (explicit) return explicit.split(/\s+/)[0].toLowerCase();
    const tag = element.tagName.toLowerCase();
    if (tag === 'button' || (tag === 'input' &&
        ['button', 'submit', 'reset', 'image'].includes(type))) return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'textarea' || element.isContentEditable) return 'textbox';
    if (tag === 'select') return element.multiple ? 'listbox' : 'combobox';
    if (tag === 'input') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    return 'generic';
  };
  const accessibleName = (element, labels, placeholder, text) => {
    const labelledBy = clean(element.getAttribute('aria-labelledby'), 256)
      .split(/\s+/).filter(Boolean)
      .map((id) => document.getElementById(id))
      .filter(Boolean).map(textOf).filter(Boolean).join(' ');
    const explicit = clean(element.getAttribute('aria-label'), 512);
    const alt = clean(element.getAttribute('alt'), 512);
    const title = clean(element.getAttribute('title'), 512);
    return clean(explicit || labelledBy || labels || alt || placeholder || text || title, 512);
  };
  const rows = [];
  const maxRows = Math.max(1, Math.min(512, Number(limit) || 1));
  const maxVisited = Math.min(8192, maxRows * 16);
  const walker = document.createTreeWalker(
    document.body || document.documentElement,
    NodeFilter.SHOW_ELEMENT
  );
  let visited = 0;
  while (visited < maxVisited && rows.length < maxRows && walker.nextNode()) {
    visited += 1;
    const element = walker.currentNode;
    if (!element.matches(selector)) continue;
    try {
      const tag = element.tagName.toLowerCase();
      const inputType = tag === 'input'
        ? clean(element.getAttribute('type') || 'text', 32).toLowerCase()
        : '';
      const placeholder = clean(element.getAttribute('placeholder'), 256);
      const labels = element.labels
        ? Array.from(element.labels).map(textOf).filter(Boolean).join(' ')
        : '';
      const text = ['input', 'textarea', 'select'].includes(tag) ? '' : textOf(element);
      const role = roleOf(element, inputType);
      const name = accessibleName(element, labels, placeholder, text);
      const autocomplete = clean(element.getAttribute('autocomplete'), 128).toLowerCase();
      const fieldName = clean(element.getAttribute('name'), 128).toLowerCase();
      const sensitive = inputType === 'password' ||
        /(?:password|passwd|secret|token|credential|one-time-code|cc-(?:number|exp|csc)|cvv|cvc|security[-_]?code|(?:^|[^a-z])pin(?:$|[^a-z]))/i.test(
          [name, fieldName, autocomplete].join(' ')
        );
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      const visible = style.display !== 'none' && style.visibility !== 'hidden' &&
        Number(style.opacity) !== 0 && element.getClientRects().length > 0 &&
        rect.width > 0 && rect.height > 0 && !element.closest('[inert]');
      const enabled = !element.matches(':disabled') &&
        element.getAttribute('aria-disabled') !== 'true' && !element.closest('[inert]');
      const hierarchy = [];
      let parent = element.parentElement;
      while (parent && hierarchy.length < 6) {
        const parentName = clean(
          parent.getAttribute('aria-label') || parent.getAttribute('title') ||
            parent.getAttribute('role') || parent.tagName.toLowerCase(),
          128
        );
        if (parentName) hierarchy.unshift(parentName);
        parent = parent.parentElement;
      }
      rows.push({
        role, name, tag,
        id: clean(element.getAttribute('id'), 256),
        test_id: clean(element.getAttribute('data-testid'), 256),
        label: clean(labels, 256),
        placeholder,
        text: sensitive ? '' : text,
        input_type: inputType,
        sensitive,
        contenteditable: Boolean(element.isContentEditable),
        visible,
        enabled,
        checked: ('checked' in element) ? Boolean(element.checked) : null,
        selected_index: (tag === 'select') ? Number(element.selectedIndex) : null,
        hierarchy,
        bounds: [rect.x, rect.y, rect.width, rect.height]
      });
    } catch (_) {
      // One unusual/custom element must not abort the bounded DOM snapshot.
    }
  }
  return rows;
}"""


@dataclass(frozen=True, slots=True)
class _PageObservation:
    page_id: str
    target_fingerprint: str | None
    state_hash: str
    expires_at: float
    candidates: tuple[TargetCandidate, ...]


def validate_browser_url(url: str, *, allow_private_network: bool = False) -> str:
    """Accept only bounded HTTP(S) URLs without user-info or unsafe local hosts.

    Private/loopback destinations are denied by default. An application that
    intentionally automates a local development service must opt in explicitly.
    """

    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ValueError("browser URL must be non-empty and at most 4096 characters")
    if "\\" in url or any(ord(character) < 0x20 or character.isspace() for character in url):
        raise ValueError(
            "browser URL cannot contain backslashes, whitespace, or control characters"
        )
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("browser URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("browser URL must use HTTP or HTTPS and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser URL must not include embedded credentials")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("browser URL port is outside the valid range")
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("browser URL host is invalid") from exc
    if "%" in ascii_host:
        raise ValueError("percent-encoded browser hosts are not supported")
    if len(ascii_host) > 253:
        raise ValueError("browser URL host exceeds the size limit")
    if not allow_private_network:
        if ascii_host == "localhost" or ascii_host.endswith((".localhost", ".local")):
            raise ValueError("local browser destinations are disabled")
        try:
            address = ipaddress.ip_address(ascii_host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("private or non-global browser destinations are disabled")
        if address is None and re.fullmatch(
            r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", ascii_host
        ):
            raise ValueError("ambiguous numeric browser hosts are disabled")
    return url


def validate_browser_egress_url(
    url: str,
    *,
    allow_private_network: bool = False,
    allowed_domains: Sequence[str] = (),
) -> str:
    """Validate browser navigation, subresource, or WebSocket egress URLs."""

    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ValueError("browser egress URL must be non-empty and at most 4096 characters")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme in {"ws", "wss"}:
        if scheme == "ws" and not allow_private_network:
            raise ValueError("plaintext ws:// browser egress is disabled on public networks")
        http_equivalent = urlunsplit(
            ("https" if scheme == "wss" else "http", parsed.netloc, parsed.path, parsed.query, "")
        )
        validate_browser_url(http_equivalent, allow_private_network=allow_private_network)
    else:
        validate_browser_url(url, allow_private_network=allow_private_network)

    if allowed_domains:
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        normalized_domains = tuple(
            d.strip().lower().rstrip(".") for d in allowed_domains if d and d.strip()
        )
        if normalized_domains and not any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in normalized_domains
        ):
            raise ValueError("browser destination host is not in the configured domain allowlist")
    return url


def verify_browser_dns_binding(
    url: str,
    *,
    allow_private_network: bool = False,
    dns_resolver: Any | None = None,
    pinned_hosts: dict[str, frozenset[str]] | None = None,
) -> tuple[str, ...]:
    """Resolve and verify that a browser hostname does not bind or rebind to private IPs."""

    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    if not hostname:
        raise ValueError("browser URL has no hostname for DNS verification")
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not allow_private_network and not literal_ip.is_global:
            raise ValueError("private or non-global IP literal is forbidden")
        return (str(literal_ip),)

    if dns_resolver is None:
        import socket

        infos = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        resolved_ips = tuple(sorted({str(info[4][0]) for info in infos if info and info[4]}))
    else:
        raw_ips = dns_resolver(hostname)
        resolved_ips = tuple(sorted({str(ip) for ip in raw_ips}))

    if not resolved_ips:
        raise ValueError("DNS resolution returned no addresses for browser host")

    for ip_text in resolved_ips:
        addr = ipaddress.ip_address(ip_text)
        if not allow_private_network and not addr.is_global:
            raise ValueError("DNS rebinding to a non-global/private address was blocked")

    if pinned_hosts is not None:
        previous = pinned_hosts.get(hostname)
        current_set = frozenset(resolved_ips)
        if previous is None:
            pinned_hosts[hostname] = current_set
        elif not (previous & current_set):
            raise ValueError("DNS rebinding detected: host changed its resolved IP set mid-session")
    return resolved_ips


def _playwright_browsers_root() -> Path | None:
    """Return Playwright's browser registry directory for this host, if configured/present."""

    import os
    import sys

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "ms-playwright" if local else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _playwright_chromium_installed() -> bool:
    """Verify a launchable Chromium browser binary exists, not just the Python package.

    An installed `playwright` wheel without downloaded browsers cannot start a context, so
    discovery must not report it as an available browser.
    """

    root = _playwright_browsers_root()
    if root is None or not root.is_dir():
        return False
    browser_prefixes = ("chromium", "chromium_headless_shell")
    try:
        entries = [entry for entry in root.iterdir() if entry.name.startswith(browser_prefixes)]
    except OSError:
        return False
    launchable_names = {"chrome", "chrome.exe", "headless_shell"}
    for entry in sorted(entries):
        try:
            for candidate in entry.rglob("*"):
                if candidate.name in launchable_names and candidate.is_file():
                    return True
        except OSError:
            continue
    return False


def discover_available_browsers(*, collector: Any | None = None) -> dict[str, Any]:
    """Discover installed/running browsers and isolated Playwright Chromium availability."""

    import importlib.util

    playwright_installed = importlib.util.find_spec("playwright") is not None
    discovered_names: list[str] = []
    if collector is not None:
        try:
            snapshot = collector.collect()
            discovered_names = list(snapshot.browsers)
        except Exception:
            discovered_names = []
    else:
        try:
            from arise.adapters.diagnostics import EnvironmentDiagnosticsCollector

            snapshot = EnvironmentDiagnosticsCollector().collect()
            discovered_names = list(snapshot.browsers)
        except Exception:
            discovered_names = []
    # The two halves are reported separately on purpose: a downloaded browser binary says
    # nothing about whether the optional wheel exists, and an installed wheel says nothing
    # about whether a browser can actually be launched. Availability requires both.
    chromium_installed = _playwright_chromium_installed()
    launchable = playwright_installed and chromium_installed
    if launchable and "Chromium (Playwright)" not in discovered_names:
        discovered_names.append("Chromium (Playwright)")
    if launchable:
        reason = None
    elif chromium_installed and not playwright_installed:
        reason = (
            "A Chromium browser binary is present but the optional Playwright package is not "
            "installed; install the browser extra."
        )
    elif not playwright_installed:
        reason = (
            "The optional Playwright package is not installed; install the browser extra and run "
            "`python -m playwright install chromium`."
        )
    else:
        reason = (
            "Playwright is installed but no Chromium browser binary was found; "
            "run `python -m playwright install chromium`."
        )
    return {
        "playwright_installed": playwright_installed,
        "chromium_installed": chromium_installed,
        "browsers": tuple(discovered_names),
        "isolated_adapter": "PlaywrightBrowserProvider" if launchable else None,
        "isolated_adapter_reason": reason,
    }


def redact_browser_url(url: str) -> str:
    """Produce a display-safe URL; query, fragment, credentials, and opaque paths go."""

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "about:blank" if url == "about:blank" else "[URL REDACTED]"
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        netloc = host if port is None else f"{host}:{port}"
        safe_segments: list[str] = []
        hide_next = False
        for segment in parsed.path.split("/"):
            decoded = segment
            if hide_next or re.search(
                r"(?i)(password|passwd|secret|token|credential|auth)", decoded
            ):
                safe_segments.append("[REDACTED]")
                hide_next = bool(
                    re.search(r"(?i)(password|passwd|secret|token|credential|auth)", decoded)
                )
            elif len(decoded) >= 24 and re.fullmatch(r"[A-Za-z0-9%._~-]+", decoded):
                safe_segments.append("[REDACTED]")
            else:
                safe_segments.append(DEFAULT_REDACTOR.redact(decoded)[:256])
                hide_next = False
        path = "/".join(safe_segments)[:1024]
        return urlunsplit((parsed.scheme.lower(), netloc, path or "/", "", ""))[:2048]
    except (UnicodeError, ValueError):
        return "[URL REDACTED]"


class PlaywrightBrowserProvider:
    """Fresh-context Playwright adapter for browser DOM and guarded actions.

    The provider is deliberately opt-in: construct it, call ``await start()``,
    then register its operation-specific action tools. It never reuses a regular
    user profile or silently installs Playwright/browser binaries.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        profile_id: str = "arise-isolated",
        default_timeout_seconds: float = 10.0,
        observation_lease_seconds: float = 3.0,
        max_pages: int = 8,
        max_dom_elements: int = _MAX_DOM_ELEMENTS,
        allow_private_network: bool = False,
        allowed_domains: Sequence[str] = (),
        dns_resolver: Any | None = None,
        allow_stale_regrounding: bool = False,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        validate_safe_token(profile_id, "browser profile_id")
        if not 0.1 <= default_timeout_seconds <= 120:
            raise ValueError("default browser timeout must be between 0.1 and 120 seconds")
        if not 0.1 <= observation_lease_seconds <= 60:
            raise ValueError("observation lease must be between 0.1 and 60 seconds")
        if not 1 <= max_pages <= 32:
            raise ValueError("max_pages must be between 1 and 32")
        if not 1 <= max_dom_elements <= _MAX_DOM_ELEMENTS:
            raise ValueError(f"max_dom_elements must be between 1 and {_MAX_DOM_ELEMENTS}")
        self.headless = headless
        self.profile_id = profile_id
        self.default_timeout_seconds = default_timeout_seconds
        self.observation_lease_seconds = observation_lease_seconds
        self.max_pages = max_pages
        self.max_dom_elements = max_dom_elements
        self.allow_private_network = allow_private_network
        self.allowed_domains = tuple(
            d.strip().lower() for d in allowed_domains if isinstance(d, str) and d.strip()
        )
        self.dns_resolver = dns_resolver
        self.allow_stale_regrounding = allow_stale_regrounding
        self.secret_provider = secret_provider
        self._resolver = TargetResolver()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._page_ids: dict[int, str] = {}
        self._default_page_id: str | None = None
        self._observations: OrderedDict[str, _PageObservation] = OrderedDict()
        self._pinned_hosts: dict[str, frozenset[str]] = {}
        self._reground_count = 0
        self._crash_recovery_count = 0
        self._navigation_recovery_count = 0

    @property
    def reground_count(self) -> int:
        return self._reground_count

    @property
    def crash_recovery_count(self) -> int:
        return self._crash_recovery_count

    @property
    def navigation_recovery_count(self) -> int:
        return self._navigation_recovery_count

    def discover_browsers(self, *, collector: Any | None = None) -> dict[str, Any]:
        """Return discovered host browsers and isolated Playwright status."""

        return discover_available_browsers(collector=collector)

    @property
    def started(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        """Explicitly launch Chromium in a new ephemeral context."""

        if self.started:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "Optional Playwright support is not installed; install the browser extra.",
                source=PerceptionSource.BROWSER_DOM,
            ) from exc
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context = await self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
            )
            await self._context.route("**/*", self._guard_route)
            self._context.on("page", self._register_page)
            page = await self._context.new_page()
            self._default_page_id = self._register_page(page)
        except Exception as exc:
            await self.close()
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                f"Isolated Chromium context could not be started ({type(exc).__name__}).",
                source=PerceptionSource.BROWSER_DOM,
            ) from None

    async def close(self) -> None:
        """Close the isolated context and browser; safe to call more than once."""

        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = self._browser = self._playwright = None
        self._pages.clear()
        self._page_ids.clear()
        self._default_page_id = None
        self._observations.clear()
        for item, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
            if item is None:
                continue
            try:
                await getattr(item, method)()
            except Exception:
                # Cleanup is best-effort; no browser content enters diagnostics.
                pass

    async def __aenter__(self) -> PlaywrightBrowserProvider:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def _require_started(self) -> None:
        if not self.started:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "The isolated Playwright browser has not been explicitly started.",
                source=PerceptionSource.BROWSER_DOM,
            )

    async def _guard_route(self, route: Any, request: Any) -> None:
        """Block unsupported schemes, WebSockets/subrequests to private hosts, and DNS rebinding."""

        request_url = str(request.url)
        try:
            scheme = urlsplit(request_url).scheme.lower()
        except ValueError:
            await route.abort("blockedbyclient")
            return
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        try:
            validate_browser_egress_url(
                request_url,
                allow_private_network=self.allow_private_network,
                allowed_domains=self.allowed_domains,
            )
            if self.dns_resolver is not None:
                verify_browser_dns_binding(
                    request_url,
                    allow_private_network=self.allow_private_network,
                    dns_resolver=self.dns_resolver,
                    pinned_hosts=self._pinned_hosts,
                )
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def reground_stale_target(self, candidate: TargetCandidate) -> TargetCandidate:
        """Re-observe a browser page and re-resolve a stale semantic target safely."""

        identity = candidate.descriptor.identity
        page_id = identity.page_id
        if identity.platform != "browser" or page_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser stale-target re-resolution requires a browser page target.",
                source=PerceptionSource.BROWSER_DOM,
            )
        self._sync_pages()
        page = self._page(page_id)
        new_observation_id = uuid.uuid4().hex
        candidates, _state_hash, _title, _safe_url, _url_hash = await self._capture_page(
            page, page_id, new_observation_id, remember=True
        )
        exact = [
            item
            for item in candidates
            if item.descriptor.identity.fingerprint == identity.fingerprint
            and item.descriptor.visible
            and item.descriptor.enabled
        ]
        if len(exact) == 1:
            self._reground_count += 1
            return exact[0]
        if len(exact) > 1:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_AMBIGUOUS,
                "Stale browser target matches multiple DOM elements after re-observation.",
                source=PerceptionSource.BROWSER_DOM,
            )
        if identity.semantic_name:
            resolution = self._resolver.resolve(
                TargetQuery(
                    semantic_name=identity.semantic_name,
                    role=identity.role,
                    page_id=page_id,
                    allowed_sources=(PerceptionSource.BROWSER_DOM,),
                ),
                candidates,
            )
            if resolution.status is ResolutionStatus.RESOLVED and resolution.selected is not None:
                self._reground_count += 1
                return resolution.selected
            if resolution.status is ResolutionStatus.AMBIGUOUS:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_AMBIGUOUS,
                    "Stale browser target is ambiguous after DOM mutation.",
                    source=PerceptionSource.BROWSER_DOM,
                )
        raise ComputerAdapterError(
            ComputerFailureCode.TARGET_STALE,
            "The stale browser target could not be uniquely re-resolved on the current page.",
            source=PerceptionSource.BROWSER_DOM,
        )

    async def recover_after_crash(self, *, replacement_context: Any | None = None) -> str:
        """Clean up crashed page/context state and restore an isolated browser page."""

        self._observations.clear()
        self._pages.clear()
        self._page_ids.clear()
        self._default_page_id = None
        if replacement_context is not None:
            self._context = replacement_context
            self._sync_pages()
            if self._default_page_id is None and hasattr(replacement_context, "new_page"):
                page = await replacement_context.new_page()
                self._default_page_id = self._register_page(page)
        elif self._context is not None and hasattr(self._context, "new_page"):
            self._sync_pages()
            if not self._pages:
                page = await self._context.new_page()
                self._default_page_id = self._register_page(page)
        else:
            await self.close()
            await self.start()
        if self._default_page_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "Browser crash recovery could not open a replacement page.",
                source=PerceptionSource.BROWSER_DOM,
            )
        self._crash_recovery_count += 1
        return self._default_page_id

    async def recover_navigation(
        self, page_id: str, *, fallback_url: str = "https://example.com/"
    ) -> BrowserTabRecord:
        """Invalidate stale page observations after a failed navigation and restore a safe URL."""

        for obs_id, record in tuple(self._observations.items()):
            if record.page_id == page_id:
                self._observations.pop(obs_id, None)
        tab = await self.navigate(
            page_id,
            fallback_url,
            timeout_seconds=self.default_timeout_seconds,
            expected_observation=None,
        )
        self._navigation_recovery_count += 1
        return tab

    def _register_page(self, page: Any) -> str:
        page_key = id(page)
        existing = self._page_ids.get(page_key)
        if existing is not None:
            return existing
        if len(self._pages) >= self.max_pages:
            try:
                asyncio.create_task(page.close())
            except RuntimeError:
                pass
            raise ComputerAdapterError(
                ComputerFailureCode.CAPABILITY_UNAVAILABLE,
                "The isolated browser reached its configured page limit.",
                source=PerceptionSource.BROWSER_DOM,
            )
        page_id = uuid.uuid4().hex
        self._pages[page_id] = page
        self._page_ids[page_key] = page_id
        set_default_timeout = getattr(page, "set_default_timeout", None)
        if callable(set_default_timeout):
            set_default_timeout(int(self.default_timeout_seconds * 1000))
        return page_id

    def _page(self, page_id: str) -> Any:
        self._require_started()
        try:
            validate_safe_token(page_id, "browser page_id")
            page = self._pages[page_id]
        except (KeyError, ValueError) as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.BROWSER_NOT_FOUND,
                "The isolated browser page is not available.",
                source=PerceptionSource.BROWSER_DOM,
            ) from exc
        if getattr(page, "is_closed", lambda: False)():
            self._pages.pop(page_id, None)
            self._page_ids.pop(id(page), None)
            raise ComputerAdapterError(
                ComputerFailureCode.BROWSER_NOT_FOUND,
                "The isolated browser page has been closed.",
                source=PerceptionSource.BROWSER_DOM,
            )
        return page

    def _sync_pages(self) -> None:
        if self._context is None:
            return
        for page in tuple(getattr(self._context, "pages", ())):
            if id(page) not in self._page_ids:
                self._register_page(page)
        live_ids = {id(page) for page in tuple(getattr(self._context, "pages", ()))}
        for page_id, page in tuple(self._pages.items()):
            if id(page) not in live_ids or getattr(page, "is_closed", lambda: False)():
                self._pages.pop(page_id, None)
                self._page_ids.pop(id(page), None)
        if self._default_page_id not in self._pages:
            self._default_page_id = next(iter(self._pages), None)

    async def list_tabs(self) -> Sequence[BrowserTabRecord]:
        self._require_started()
        self._sync_pages()
        records = []
        for page_id, page in self._pages.items():
            try:
                raw_url = str(page.url)
                title = DEFAULT_REDACTOR.redact(str(await page.title()))[:2048]
            except Exception:
                raw_url, title = "about:blank", ""
            records.append(
                BrowserTabRecord(
                    page_id=page_id,
                    browser="Chromium (isolated)",
                    title=title,
                    url=redact_browser_url(raw_url),
                    active=page_id == self._default_page_id,
                    profile_id=self.profile_id,
                )
            )
        return tuple(records)

    @property
    def default_page_id(self) -> str | None:
        """Opaque page ID for the initially created isolated tab, if still open."""

        return self._default_page_id

    def page_identity(self, page_id: str) -> TargetIdentity:
        """Build the stable, page-scoped target required by browser actions."""

        self._page(page_id)
        return TargetIdentity(
            platform="browser",
            application="chromium",
            browser_profile=self.profile_id,
            page_id=page_id,
            object_id="page",
            role="document",
            semantic_name="Browser page",
            stable_id=page_id,
            locator={"kind": "page"},
        )

    async def inspect(
        self, page_id: str, *, max_elements: int = _MAX_DOM_ELEMENTS
    ) -> Sequence[TargetCandidate]:
        self._validate_element_limit(max_elements)
        self._sync_pages()
        page = self._page(page_id)
        observation_id = uuid.uuid4().hex
        candidates, state_hash, _title, _safe_url, _raw_url_hash = await self._capture_page(
            page, page_id, observation_id
        )
        self._remember(
            observation_id,
            _PageObservation(
                page_id=page_id,
                target_fingerprint=None,
                state_hash=state_hash,
                expires_at=time.monotonic() + self.observation_lease_seconds,
                candidates=candidates,
            ),
        )
        return candidates

    async def resolve(self, page_id: str, query: TargetQuery) -> TargetResolution:
        candidates = tuple(await self.inspect(page_id))
        if query.page_id is not None and query.page_id != page_id:
            return TargetResolution(
                ResolutionStatus.NOT_FOUND,
                (),
                reason="The query is scoped to a different browser page.",
            )
        return self._resolver.resolve(query, candidates)

    async def observe(self, action: ActionContract) -> ObservationLease:
        """Create a short lease over a bounded page DOM snapshot for AgentRuntime."""

        self._sync_pages()
        target = action.target
        page_id = target.page_id if target is not None else self._default_page_id
        if page_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.BROWSER_NOT_FOUND,
                "No isolated browser page is available to observe.",
                source=PerceptionSource.BROWSER_DOM,
            )
        page = self._page(page_id)
        observation_id = uuid.uuid4().hex
        candidates, state_hash, title, safe_url, raw_url_hash = await self._capture_page(
            page, page_id, observation_id
        )
        target_fingerprint = target.fingerprint if target is not None else None
        now = utc_now()
        deadline = time.monotonic() + self.observation_lease_seconds
        lease = ObservationLease(
            lease_id=observation_id,
            target_fingerprint=target_fingerprint,
            state_hash=state_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=self.observation_lease_seconds),
            monotonic_deadline=deadline,
            facts={
                "browser.page_id": page_id,
                "browser.url": safe_url,
                "browser.url_hash": raw_url_hash,
                "browser.title": title,
                "browser.dom_hash": state_hash,
                "browser.target_count": len(candidates),
                "browser.target_fingerprints": [
                    item.descriptor.identity.fingerprint for item in candidates
                ],
            },
            source=EvidenceSource.OBSERVED,
            confidence=1.0,
        )
        self._remember(
            observation_id,
            _PageObservation(
                page_id=page_id,
                target_fingerprint=target_fingerprint,
                state_hash=state_hash,
                expires_at=deadline,
                candidates=candidates,
            ),
        )
        return lease

    async def is_current(self, observation: ObservationLease) -> bool:
        if not observation.is_valid(monotonic_now=time.monotonic()):
            return False
        record = self._observations.get(observation.lease_id)
        if (
            record is None
            or record.expires_at <= time.monotonic()
            or record.state_hash != observation.state_hash
            or record.target_fingerprint != observation.target_fingerprint
        ):
            return False
        try:
            page = self._page(record.page_id)
            _candidates, current_hash, _title, _safe_url, _url_hash = await self._capture_page(
                page, record.page_id, observation.lease_id, remember=False
            )
        except Exception:
            return False
        return current_hash == record.state_hash

    async def verify(
        self,
        action: ActionContract,
        pre_observation: ObservationLease | None = None,
        post_observation: ObservationLease | None = None,
        outcome: ExecutionOutcome | None = None,
    ) -> VerificationResult:
        del pre_observation
        if outcome is not None and outcome.status is ExecutionStatus.UNKNOWN:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                level=0,
                summary="Browser execution outcome is unknown; postconditions cannot be verified.",
                evidence=(),
            )
        observation = post_observation or await self.observe(action)
        failed = [
            condition.key
            for condition in action.postconditions
            if not condition.evaluate(observation.facts)
        ]
        evidence = (
            EvidenceRecord(
                source=observation.source.value,
                observation_id=observation.lease_id,
                state_hash=observation.state_hash,
                statement=f"Browser DOM snapshot verified ({observation.state_hash[:12]}).",
                captured_at=observation.created_at,
            ),
        )
        if failed:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                level=2,
                summary=f"Browser postconditions failed: {', '.join(failed)}",
                evidence=evidence,
            )
        return VerificationResult(
            status=VerificationStatus.PASSED,
            level=2,
            summary="All browser postconditions verified against fresh DOM observation.",
            evidence=evidence,
        )

    def candidates_for_observation(
        self, observation: ObservationLease
    ) -> tuple[TargetCandidate, ...]:
        record = self._observations.get(observation.lease_id)
        if (
            record is None
            or record.expires_at <= time.monotonic()
            or record.state_hash != observation.state_hash
            or record.target_fingerprint != observation.target_fingerprint
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The browser observation has expired or is no longer current.",
                source=PerceptionSource.BROWSER_DOM,
            )
        return record.candidates

    async def navigate(
        self,
        page_id: str,
        url: str,
        *,
        timeout_seconds: float,
        expected_observation: ObservationLease | None = None,
    ) -> BrowserTabRecord:
        try:
            validate_browser_url(url, allow_private_network=self.allow_private_network)
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "The requested browser destination was rejected by URL policy.",
                source=PerceptionSource.BROWSER_DOM,
            ) from exc
        page = self._page(page_id)
        if expected_observation is not None:
            await self._ensure_observation_current(expected_observation, page_id=page_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=max(1, int(timeout_seconds * 1000)),
                )
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Navigation timed out after dispatch may have begun; reconcile before retrying.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Navigation may have dispatched but its result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        self._sync_pages()
        try:
            title = DEFAULT_REDACTOR.redact(str(await page.title()))[:2048]
        except Exception:
            title = ""
        return BrowserTabRecord(
            page_id=page_id,
            browser="Chromium (isolated)",
            title=title,
            url=redact_browser_url(str(page.url)),
            active=page_id == self._default_page_id,
            profile_id=self.profile_id,
        )

    async def click(self, candidate: TargetCandidate, *, timeout_seconds: float) -> str:
        _page, locator = await self._resolve_fresh(candidate, timeout_seconds=timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds):
                await locator.click(timeout=max(1, int(timeout_seconds * 1000)))
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Click timed out after dispatch may have begun; reconcile before retrying.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Click may have dispatched but its result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return "Browser click dispatched; verify the declared postconditions independently."

    async def fill(
        self,
        candidate: TargetCandidate,
        text: str | SecretRef,
        *,
        timeout_seconds: float,
    ) -> str:
        page, locator = await self._resolve_fresh(candidate, timeout_seconds=timeout_seconds)
        del page
        locator_data = candidate.descriptor.identity.locator
        sensitive = bool(locator_data.get("sensitive", False))
        input_type = str(locator_data.get("input_type", ""))
        tag = str(locator_data.get("tag", ""))
        contenteditable = bool(locator_data.get("contenteditable", False))
        editable_input_types = {
            "text",
            "search",
            "email",
            "url",
            "tel",
            "password",
            "number",
            "date",
            "time",
            "datetime-local",
            "month",
            "week",
        }
        if not (
            tag == "textarea"
            or contenteditable
            or (tag == "input" and input_type in editable_input_types)
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                "The grounded browser target is not an editable text control.",
                source=PerceptionSource.BROWSER_DOM,
            )
        if sensitive and not isinstance(text, SecretRef):
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "Password and sensitive browser fields require a secret reference.",
                source=PerceptionSource.BROWSER_DOM,
            )
        if isinstance(text, SecretRef) and not sensitive:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "Secret references may only be entered into a sensitive browser field.",
                source=PerceptionSource.BROWSER_DOM,
            )
        value = self._resolve_sensitive_text(text)
        if len(value) > 16_384:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser form text exceeds the configured size limit.",
                source=PerceptionSource.BROWSER_DOM,
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                await locator.fill(value, timeout=max(1, int(timeout_seconds * 1000)))
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Text entry timed out after dispatch may have begun; reconcile before retrying.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Text entry may have dispatched but its result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return "Browser text entry dispatched; verify the declared postconditions independently."

    async def press(
        self,
        candidate: TargetCandidate,
        key: str,
        *,
        timeout_seconds: float,
    ) -> str:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9+_-]{1,64}", key):
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Keyboard descriptor is malformed.",
                source=PerceptionSource.BROWSER_DOM,
            )
        _page, locator = await self._resolve_fresh(candidate, timeout_seconds=timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds):
                await locator.press(key, timeout=max(1, int(timeout_seconds * 1000)))
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Keyboard action timed out after dispatch may have begun.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Keyboard action may have dispatched but its result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return (
            "Browser keyboard action dispatched; verify the declared postconditions independently."
        )

    async def select_option(
        self,
        candidate: TargetCandidate,
        value: str,
        *,
        timeout_seconds: float,
    ) -> str:
        if not isinstance(value, str) or len(value) > 1024:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser selection value exceeds the configured limit.",
                source=PerceptionSource.BROWSER_DOM,
            )
        _page, locator = await self._resolve_fresh(candidate, timeout_seconds=timeout_seconds)
        if candidate.descriptor.identity.locator.get("tag") != "select":
            raise ComputerAdapterError(
                ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                "The resolved browser target is not a select control.",
                source=PerceptionSource.BROWSER_DOM,
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                await locator.select_option(
                    value=value, timeout=max(1, int(timeout_seconds * 1000))
                )
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Select operation timed out after dispatch may have begun.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Select operation may have dispatched but its result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return "Browser select operation dispatched; verify the selected value independently."

    async def scroll_into_view(self, candidate: TargetCandidate, *, timeout_seconds: float) -> str:
        _page, locator = await self._resolve_fresh(candidate, timeout_seconds=timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds):
                await locator.scroll_into_view_if_needed(
                    timeout=max(1, int(timeout_seconds * 1000))
                )
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Scrolling timed out after the viewport change may have begun.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Scrolling may have begun but the viewport result is not known.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return "Grounded browser target scrolled into view."

    def _resolve_secret(self, secret: SecretRef) -> str:
        if self.secret_provider is None:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "No secret provider is configured for browser credential entry.",
                source=PerceptionSource.BROWSER_DOM,
            )
        try:
            value = self.secret_provider.get_secret(secret.name)
        except SecretUnavailable:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "The referenced browser secret is unavailable.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "The browser secret provider rejected the reference.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        if not isinstance(value, str) or not value or len(value) > 16_384:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "The referenced browser secret is invalid or exceeds the size limit.",
                source=PerceptionSource.BROWSER_DOM,
            )
        return value

    def _resolve_sensitive_text(self, text: str | SecretRef) -> str:
        if isinstance(text, SecretRef):
            return self._resolve_secret(text)
        if not isinstance(text, str):
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser text must be a string or a typed secret reference.",
                source=PerceptionSource.BROWSER_DOM,
            )
        return text

    async def _ensure_observation_current(
        self, observation: ObservationLease, *, page_id: str
    ) -> None:
        record = self._observations.get(observation.lease_id)
        if (
            record is None
            or record.page_id != page_id
            or record.expires_at <= time.monotonic()
            or record.state_hash != observation.state_hash
            or record.target_fingerprint != observation.target_fingerprint
            or not await self.is_current(observation)
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The browser page changed after observation; reobserve before dispatch.",
                source=PerceptionSource.BROWSER_DOM,
            )

    async def _resolve_fresh(
        self, candidate: TargetCandidate, *, timeout_seconds: float
    ) -> tuple[Any, Any]:
        identity = candidate.descriptor.identity
        page_id = identity.page_id
        if identity.platform != "browser" or page_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser actions require a browser page target.",
                source=PerceptionSource.BROWSER_DOM,
            )
        self._sync_pages()
        page = self._page(page_id)
        record = self._observations.get(candidate.descriptor.observation_id)
        if record is None or record.expires_at <= time.monotonic() or record.page_id != page_id:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The browser target observation is missing or expired.",
                source=PerceptionSource.BROWSER_DOM,
            )
        observed_matches = [
            item
            for item in record.candidates
            if item.descriptor.identity.fingerprint == identity.fingerprint
        ]
        if not observed_matches:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The browser target was not present in its source observation.",
                source=PerceptionSource.BROWSER_DOM,
            )
        if len(observed_matches) != 1:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_AMBIGUOUS,
                "The source observation contains duplicate browser target identities.",
                source=PerceptionSource.BROWSER_DOM,
            )
        current, current_hash, _title, _safe_url, _url_hash = await self._capture_page(
            page,
            page_id,
            candidate.descriptor.observation_id,
            max_elements=self.max_dom_elements,
            remember=False,
        )
        if current_hash != record.state_hash:
            if not self.allow_stale_regrounding:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "The browser DOM changed after target observation; reobserve before dispatch.",
                    source=PerceptionSource.BROWSER_DOM,
                )
            matching_stale = [
                item
                for item in current
                if item.descriptor.identity.fingerprint == identity.fingerprint
                and item.descriptor.visible
                and item.descriptor.enabled
            ]
            if len(matching_stale) != 1:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "The browser DOM changed and the target could not be uniquely re-resolved.",
                    source=PerceptionSource.BROWSER_DOM,
                )
            self._reground_count += 1
        matching = [
            item for item in current if item.descriptor.identity.fingerprint == identity.fingerprint
        ]
        if not matching:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "The grounded browser element no longer exists.",
                source=PerceptionSource.BROWSER_DOM,
            )
        if len(matching) != 1:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_AMBIGUOUS,
                "The grounded browser identity matches multiple DOM elements.",
                source=PerceptionSource.BROWSER_DOM,
            )
        fresh = matching[0]
        if not fresh.descriptor.visible or not fresh.descriptor.enabled:
            raise ComputerAdapterError(
                ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                "The grounded browser element is hidden or disabled.",
                source=PerceptionSource.BROWSER_DOM,
            )
        try:
            locator = self._locator_for(page, fresh)
        except ComputerAdapterError:
            raise
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.INTERNAL_ADAPTER_ERROR,
                "The semantic browser locator could not be constructed.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        try:
            async with asyncio.timeout(timeout_seconds):
                count = await locator.count()
                if count == 0:
                    raise ComputerAdapterError(
                        ComputerFailureCode.TARGET_NOT_FOUND,
                        "The semantic browser locator no longer identifies an element.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
                if count != 1:
                    raise ComputerAdapterError(
                        ComputerFailureCode.TARGET_AMBIGUOUS,
                        "The semantic browser locator is not unique.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
                if not await locator.is_visible() or not await locator.is_enabled():
                    raise ComputerAdapterError(
                        ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                        "The semantic browser target is hidden or disabled.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.TIMEOUT,
                "Fresh browser target resolution exceeded its time limit.",
                retryable=True,
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except ComputerAdapterError:
            raise
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "The browser target could not be validated before dispatch.",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        return page, locator

    @staticmethod
    def _locator_for(page: Any, candidate: TargetCandidate) -> Any:
        locator = candidate.descriptor.identity.locator
        test_id = locator.get("test_id")
        stable_id = locator.get("id")
        role = locator.get("role")
        name = locator.get("name")
        label = locator.get("label")
        placeholder = locator.get("placeholder")
        text = locator.get("text")
        if test_id:
            return page.get_by_test_id(str(test_id))
        if stable_id:
            return page.locator(f"[id={json.dumps(str(stable_id))}]")
        if role and role != "generic" and name:
            return page.get_by_role(str(role), name=str(name), exact=True)
        if label:
            return page.get_by_label(str(label), exact=True)
        if placeholder:
            return page.get_by_placeholder(str(placeholder), exact=True)
        if text:
            return page.get_by_text(str(text), exact=True)
        raise ComputerAdapterError(
            ComputerFailureCode.INVALID_TARGET,
            "The observed browser target has no usable semantic locator.",
            source=PerceptionSource.BROWSER_DOM,
        )

    async def _capture_page(
        self,
        page: Any,
        page_id: str,
        observation_id: str,
        *,
        max_elements: int | None = None,
        remember: bool = True,
    ) -> tuple[tuple[TargetCandidate, ...], str, str, str, str]:
        self._validate_element_limit(max_elements or self.max_dom_elements)
        try:
            async with asyncio.timeout(self.default_timeout_seconds):
                raw_rows = await page.evaluate(
                    _DOM_SNAPSHOT_SCRIPT, max_elements or self.max_dom_elements
                )
                raw_url = str(page.url)
                raw_title = str(await page.title())
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.TIMEOUT,
                "Browser DOM inspection exceeded its configured time limit.",
                retryable=True,
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        except Exception as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                f"The isolated browser DOM could not be inspected ({type(exc).__name__}).",
                source=PerceptionSource.BROWSER_DOM,
            ) from None
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            raise ComputerAdapterError(
                ComputerFailureCode.INTERNAL_ADAPTER_ERROR,
                "The browser DOM adapter returned a malformed snapshot.",
                source=PerceptionSource.BROWSER_DOM,
            )
        rows = tuple(
            self._normalize_row(item)
            for item in raw_rows[: (max_elements or self.max_dom_elements)]
            if isinstance(item, Mapping)
        )
        candidates = tuple(
            self._candidate_from_row(row, page_id=page_id, observation_id=observation_id)
            for row in rows
        )
        safe_title = DEFAULT_REDACTOR.redact(raw_title)[:2048]
        safe_url = redact_browser_url(raw_url)
        url_hash = hashlib.sha256(raw_url.encode("utf-8", errors="replace")).hexdigest()
        digest_input = {
            "url_hash": url_hash,
            "title": safe_title,
            "dom": [self._row_for_hash(row) for row in rows],
        }
        state_hash = hashlib.sha256(canonical_json(digest_input).encode("utf-8")).hexdigest()
        if remember:
            self._remember(
                observation_id,
                _PageObservation(
                    page_id=page_id,
                    target_fingerprint=None,
                    state_hash=state_hash,
                    expires_at=time.monotonic() + self.observation_lease_seconds,
                    candidates=candidates,
                ),
            )
        return candidates, state_hash, safe_title, safe_url, url_hash

    @staticmethod
    def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
        def safe_text(key: str, maximum: int = 512) -> str:
            value = row.get(key, "")
            if not isinstance(value, (str, int, float)):
                return ""
            return DEFAULT_REDACTOR.redact(str(value)).strip()[:maximum]

        bounds_raw = row.get("bounds")
        bounds: tuple[float, float, float, float] | None = None
        if (
            isinstance(bounds_raw, Sequence)
            and not isinstance(bounds_raw, (str, bytes))
            and len(bounds_raw) == 4
        ):
            try:
                values = tuple(float(value) for value in bounds_raw)
                if (
                    all(abs(value) < 10_000_000 for value in values)
                    and values[2] > 0
                    and values[3] > 0
                ):
                    bounds = values  # type: ignore[assignment]
            except (TypeError, ValueError):
                pass
        hierarchy_raw = row.get("hierarchy", ())
        hierarchy = (
            tuple(
                DEFAULT_REDACTOR.redact(item).strip()[:128]
                for item in hierarchy_raw[:6]
                if isinstance(item, str) and item.strip()
            )
            if isinstance(hierarchy_raw, Sequence) and not isinstance(hierarchy_raw, (str, bytes))
            else ()
        )
        checked = row.get("checked")
        selected_index = row.get("selected_index")
        return {
            "role": safe_text("role", 64).lower(),
            "name": safe_text("name"),
            "tag": safe_text("tag", 32).lower(),
            "id": safe_text("id", 256),
            "test_id": safe_text("test_id", 256),
            "label": safe_text("label", 256),
            "placeholder": safe_text("placeholder", 256),
            "text": "" if bool(row.get("sensitive", False)) else safe_text("text"),
            "input_type": safe_text("input_type", 32).lower(),
            "sensitive": row.get("sensitive") is True,
            "contenteditable": row.get("contenteditable") is True,
            "visible": row.get("visible") is True,
            "enabled": row.get("enabled") is True,
            "checked": checked if isinstance(checked, bool) else None,
            "selected_index": (
                selected_index if isinstance(selected_index, int) and selected_index >= -1 else None
            ),
            "hierarchy": hierarchy,
            "bounds": bounds,
        }

    @staticmethod
    def _row_for_hash(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: list(value) if key == "bounds" and value is not None else value
            for key, value in row.items()
        }

    def _candidate_from_row(
        self, row: Mapping[str, Any], *, page_id: str, observation_id: str
    ) -> TargetCandidate:
        name = str(row["name"] or row["label"] or row["placeholder"] or row["text"])
        role = str(row["role"] or "generic")
        test_id = str(row["test_id"] or "")
        element_id = str(row["id"] or "")
        label = str(row["label"] or "")
        placeholder = str(row["placeholder"] or "")
        text = str(row["text"] or "")
        tag = str(row["tag"] or "")
        input_type = str(row["input_type"] or "")
        sensitive = bool(row["sensitive"])
        hierarchy = tuple(row["hierarchy"])
        locator: dict[str, Any] = {
            "role": role,
            "name": name,
            "test_id": test_id or None,
            "id": element_id or None,
            "label": label or None,
            "placeholder": placeholder or None,
            "text": text or None,
            "tag": tag,
            "input_type": input_type,
            "sensitive": sensitive,
            "contenteditable": bool(row["contenteditable"]),
        }
        bounds = row["bounds"]
        rect = None
        coordinate_space = None
        if bounds is not None:
            rect = Rect(*bounds)
            coordinate_space = CoordinateSpace.BROWSER_VIEWPORT_CSS
        if role and name:
            selector_quality = SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME
        elif label:
            selector_quality = SelectorQuality.LABEL
        elif placeholder:
            selector_quality = SelectorQuality.PLACEHOLDER
        elif test_id:
            selector_quality = SelectorQuality.TEST_ID
        elif text:
            selector_quality = SelectorQuality.EXACT_TEXT
        elif element_id:
            selector_quality = SelectorQuality.STABLE_ATTRIBUTE
        else:
            selector_quality = SelectorQuality.STRUCTURAL
        stable_id = test_id or element_id or None
        identity = TargetIdentity(
            platform="browser",
            application="chromium",
            browser_profile=self.profile_id,
            page_id=page_id,
            object_id=stable_id,
            role=role,
            semantic_name=name or None,
            stable_id=stable_id,
            locator=locator,
        )
        descriptor = TargetDescriptor(
            identity=identity,
            source=PerceptionSource.BROWSER_DOM,
            observed_at=utc_now(),
            observation_id=observation_id,
            bounds=rect,
            coordinate_space=coordinate_space,
            selector_quality=selector_quality,
            visible=bool(row["visible"]),
            enabled=bool(row["enabled"]),
            automation_id=stable_id,
            hierarchy=hierarchy,
        )
        evidence = (
            "browser DOM semantic role/name",
            "form values omitted from observation",
        )
        return TargetCandidate(descriptor, selector_quality.score, evidence)

    def _remember(self, observation_id: str, record: _PageObservation) -> None:
        self._observations[observation_id] = record
        self._observations.move_to_end(observation_id)
        now = time.monotonic()
        for key, value in tuple(self._observations.items()):
            if value.expires_at <= now:
                self._observations.pop(key, None)
        while len(self._observations) > _MAX_OBSERVATIONS:
            self._observations.popitem(last=False)

    @staticmethod
    def _validate_element_limit(max_elements: int) -> None:
        if not isinstance(max_elements, int) or not 1 <= max_elements <= _MAX_DOM_ELEMENTS:
            raise ValueError(f"max_elements must be between 1 and {_MAX_DOM_ELEMENTS}")


class PlaywrightActionTool:
    """Policy-facing tool for one operation on the isolated Playwright browser."""

    _POLICY = {
        "click": (RiskLevel.R3, Idempotency.UNKNOWN, "dispatches a browser click"),
        "fill": (RiskLevel.R2, Idempotency.UNKNOWN, "enters non-secret form text"),
        "fill_secret": (RiskLevel.R3, Idempotency.UNKNOWN, "enters a referenced secret"),
        "press": (RiskLevel.R3, Idempotency.UNKNOWN, "dispatches a keyboard action"),
        "scroll": (RiskLevel.R1, Idempotency.IDEMPOTENT, "scrolls a grounded DOM target"),
        "select": (RiskLevel.R2, Idempotency.UNKNOWN, "changes a browser select value"),
        "navigate": (RiskLevel.R2, Idempotency.UNKNOWN, "navigates an isolated browser page"),
    }

    def __init__(self, provider: PlaywrightBrowserProvider, operation: str) -> None:
        if operation not in self._POLICY:
            raise ValueError("unsupported Playwright action operation")
        self.provider = provider
        self.operation = operation
        risk, idempotency, effect = self._POLICY[operation]
        self._spec = ToolSpec(
            name=f"browser.{operation}",
            version="1.0.0",
            description=f"{operation.title()} a grounded target in the isolated browser.",
            minimum_risk=risk,
            required_capabilities=frozenset({"browser.control"}),
            required_resources=(),
            declared_side_effects=(effect,),
            idempotency=idempotency,
            max_result_bytes=4096,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def resources_for(self, action: ActionContract) -> tuple[str, ...]:
        target = action.target
        if target is None or target.platform != "browser" or target.page_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Browser action requires a page-scoped target identity.",
                source=PerceptionSource.BROWSER_DOM,
            )
        validate_safe_token(target.page_id, "browser page_id")
        return (f"browser.page.{target.page_id}",)

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        allowed = {
            "click": set(),
            "fill": {"text"},
            "fill_secret": {"text"},
            "press": {"key"},
            "scroll": set(),
            "select": {"value"},
            "navigate": {"url"},
        }[self.operation]
        if set(parameters) != allowed:
            raise ValueError("browser action parameters do not match the operation schema")
        if self.operation == "fill":
            text = parameters["text"]
            if not isinstance(text, str) or len(text) > 16_384:
                raise ValueError("browser.fill requires bounded non-secret text")
        elif self.operation == "fill_secret":
            if not isinstance(parameters["text"], SecretRef):
                raise ValueError("browser.fill_secret requires a SecretRef, not a value")
        elif self.operation == "press":
            key = parameters["key"]
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9+_-]{1,64}", key):
                raise ValueError("browser.press requires a bounded key descriptor")
        elif self.operation == "select":
            value = parameters["value"]
            if not isinstance(value, str) or len(value) > 1024:
                raise ValueError("browser.select requires a bounded string option")
        elif self.operation == "navigate":
            validate_browser_url(
                parameters["url"], allow_private_network=self.provider.allow_private_network
            )

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        started_at = utc_now()
        try:
            await resources.ensure_valid()
        except ResourceLeaseLost:
            return self._pre_dispatch_failure("RESOURCE_LEASE_LOST", started_at)

        try:
            if not await self.provider.is_current(observation):
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "Browser state changed after observation.",
                    source=PerceptionSource.BROWSER_DOM,
                )
            target = action.target
            if target is None or target.page_id is None:
                raise ComputerAdapterError(
                    ComputerFailureCode.INVALID_TARGET,
                    "Browser action lacks an explicit page identity.",
                    source=PerceptionSource.BROWSER_DOM,
                )
            parameters = action.parameters
            if self.operation == "navigate":
                await self.provider.navigate(
                    target.page_id,
                    str(parameters["url"]),
                    timeout_seconds=action.timeout_seconds,
                    expected_observation=observation,
                )
                summary = "Browser navigation dispatched; postconditions must verify the result."
            else:
                candidates = self.provider.candidates_for_observation(observation)
                matches = [
                    item
                    for item in candidates
                    if item.descriptor.identity.fingerprint == target.fingerprint
                ]
                if not matches:
                    raise ComputerAdapterError(
                        ComputerFailureCode.TARGET_STALE,
                        "The action target was not present in its source observation.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
                if len(matches) != 1:
                    raise ComputerAdapterError(
                        ComputerFailureCode.TARGET_AMBIGUOUS,
                        "The action target identifies multiple observed DOM elements.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
                candidate = matches[0]
                if (
                    self.operation == "fill_secret"
                    and not candidate.descriptor.identity.locator.get("sensitive", False)
                ):
                    raise ComputerAdapterError(
                        ComputerFailureCode.PERMISSION_DENIED,
                        "Secret references may only be entered into a sensitive browser field.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
                try:
                    await resources.ensure_valid()
                except ResourceLeaseLost:
                    return self._pre_dispatch_failure("RESOURCE_LEASE_LOST", started_at)
                if self.operation == "click":
                    summary = await self.provider.click(
                        candidate, timeout_seconds=action.timeout_seconds
                    )
                elif self.operation in {"fill", "fill_secret"}:
                    summary = await self.provider.fill(
                        candidate, parameters["text"], timeout_seconds=action.timeout_seconds
                    )
                elif self.operation == "press":
                    summary = await self.provider.press(
                        candidate, str(parameters["key"]), timeout_seconds=action.timeout_seconds
                    )
                elif self.operation == "scroll":
                    summary = await self.provider.scroll_into_view(
                        candidate, timeout_seconds=action.timeout_seconds
                    )
                elif self.operation == "select":
                    summary = await self.provider.select_option(
                        candidate,
                        str(parameters["value"]),
                        timeout_seconds=action.timeout_seconds,
                    )
                else:
                    raise ComputerAdapterError(
                        ComputerFailureCode.CAPABILITY_UNAVAILABLE,
                        "The browser action operation is unavailable.",
                        source=PerceptionSource.BROWSER_DOM,
                    )
        except ComputerAdapterError as exc:
            if exc.code is ComputerFailureCode.ACTION_UNKNOWN_OUTCOME:
                raise
            return self._pre_dispatch_failure(exc.code.value, started_at)

        await resources.ensure_valid()
        return ExecutionOutcome(
            status=ExecutionStatus.SUCCEEDED,
            summary=summary,
            side_effect_may_have_occurred=self.operation != "scroll",
            result_metadata={},
            started_at=started_at,
            finished_at=utc_now(),
        )

    @staticmethod
    def _pre_dispatch_failure(code: str, started_at: datetime) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=ExecutionStatus.FAILED,
            summary=f"Browser action was not dispatched ({code}).",
            side_effect_may_have_occurred=False,
            result_metadata={},
            started_at=started_at,
            finished_at=utc_now(),
        )


def register_playwright_tools(
    registry: ToolRegistry, provider: PlaywrightBrowserProvider
) -> tuple[PlaywrightActionTool, ...]:
    """Register the browser's separately risk-rated operations in a tool registry."""

    tools = tuple(
        PlaywrightActionTool(provider, operation)
        for operation in ("click", "fill", "fill_secret", "press", "scroll", "select", "navigate")
    )
    for tool in tools:
        registry.register(tool)
    return tools


__all__ = [
    "PlaywrightActionTool",
    "PlaywrightBrowserProvider",
    "discover_available_browsers",
    "redact_browser_url",
    "register_playwright_tools",
    "validate_browser_egress_url",
    "validate_browser_url",
    "verify_browser_dns_binding",
]
