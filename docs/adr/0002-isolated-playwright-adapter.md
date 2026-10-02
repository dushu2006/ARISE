# ADR-0002: Build an opt-in Playwright adapter on the existing runtime

- **Status:** Accepted for experimental Phase 2 work; production registration deferred
- **Date:** 2026-10-02

## Context

Phase 2 needs browser DOM grounding and deterministic page actions without creating a second task, policy, or verification lifecycle. The Phase 1 architecture already supplies `TargetIdentity`, `ActionContract`, `ObservationLease`, `EnvironmentPort`, `ActionTool`, `PolicyEngine`, `ResourceManager`, and `VerifierPort`. Importing or configuring a browser must not silently give the production API access to a user's existing browser session.

## Decision

- Implement `PlaywrightBrowserProvider` as an optional adapter. Importing the module does not import Playwright or start a process; the caller must explicitly invoke `start()`.
- Start a fresh, non-persistent Chromium browser context. Do not attach to an existing browser, CDP endpoint, or persistent user profile. Disable downloads and service workers by default.
- Treat the provider as the page-scoped `EnvironmentPort` and browser inspection/action implementation. Register operation-specific `ActionTool`s in the existing `ToolRegistry`; let `AgentRuntime` retain ownership of policy, approvals, resources, cancellation, timeout, and independent verification.
- Keep browser action risk floors in adapter-owned `ToolSpec`s. Use a dynamic `browser.page.<opaque-page-id>` resource so same-page actions serialize under the existing `ResourceManager`.
- Bound DOM inspection and observation retention; never read form values. Mark password/sensitive controls with HTML input type and a sensitive flag. Credential entry requires a `SecretRef` and an explicitly configured secret provider.
- Accept only bounded HTTP(S) navigation URLs, reject embedded credentials, redact URL query/fragment material from outputs, and block private/loopback destinations by default. A caller can opt into local destinations only through explicit configuration.
- Do not register the adapter in the shipped API composition root until real-runtime tests, security review, user-visible action summaries, and capability reporting are ready.

## Consequences

### Positive

- Browser actions reuse existing authority, approval, state, resource, and verification contracts.
- A DOM candidate cannot dispatch directly; the runtime checks fresh observed state and the adapter checks page state, candidate identity, uniqueness, visibility, and enabled state again.
- Browser startup is explicit and isolated from the user's everyday profile; optional dependency installation is not automatic.
- Sensitive form contents are not returned in DOM observations or action result metadata.

### Trade-offs and known gaps

- Fake page/locator tests do not establish Playwright version compatibility, Chromium behavior, or Windows stability.
- The API does not instantiate/register the adapter; the browser capability remains unavailable by default.
- The context route guard rejects common private/numeric HTTP(S) hosts, including redirects, but cannot prevent DNS rebinding or a public hostname resolving to a private address; WebSocket traffic is not comprehensively filtered. Production use should add controlled network egress or a hardened request broker.
- DOM text and URL redaction is best-effort; sensitive data may still appear in unusual labels, page titles, or identifiers. Do not treat redaction as a secret scanner.
- No browser-specific approval preview UI exists yet. Consequential browser operations remain blocked by the policy engine unless the host application provides an authenticated, exact-scope approval flow.

## Revisit when

- The optional adapter is composed into the application and advertised as a capability.
- A supported real Playwright/Chromium matrix and Windows integration tests are available.
- URL egress restrictions, approval previews, reconciliation evidence, and page/account scope behavior are reviewed for production.
