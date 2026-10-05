# ARISE coherence milestone — architectural audit

> **Status:** Phase 1 (audit) complete. Phase 2 (implementation) is recorded in
> [`docs/coherence-milestone-implementation.md`](coherence-milestone-implementation.md)
> — files changed, tests added, results, limitations, and the Arena-verified vs.
> real-Windows-verified split.

**Date:** 2026-10-05
**Branch:** `arena/01a10cbb-arise` (baseline `fc822e9`)
**Baseline suite:** `571 passed, 1 skipped, 119 subtests` (`.venv/bin/pytest -q`, Linux/Python 3.11.2)
**Evidence boundary:** all work here was performed on **Linux**. No Windows API, Win32
window, UIA tree, Chrome/Edge/Firefox process, audio device, Vosk/Kokoro model or Gemini
session was exercised. Anything marked *Windows-required* below is unverified by design.

---

## A. Current architecture (as built, not as documented aspirationally)

```text
                 ┌──────────────────────────── React/Vite UI (Tauri shell) ──────────────────┐
                 │   tasks · approvals · events · capabilities · memory · voice controls      │
                 └───────────────────────────────────┬────────────────────────────────────────┘
                                                     │ HTTP /api/v1 + WebSocket /ws/v1 (v1 hello)
                 ┌───────────────────────────────────▼────────────────────────────────────────┐
                 │ FastAPI composition root  `create_app()` / `_build_services()`             │
                 │   auth · origin policy · size limits · event replay · single-instance lock  │
                 └───┬────────────────────────────────────────────────────────────────────────┘
   POST /api/v1/interactions                              POST /api/v1/tasks
     IntentClassifier (deterministic)                              │
        ├─ STATUS_REQUEST ──► engine.list_tasks (deterministic)     │
        ├─ CANCELLATION   ──► engine.cancel                        │
        ├─ may_require_runtime_task (conf ≥ .75) ──► TaskEngine     │
        ├─ CLARIFICATION  ──► template                             │
        └─ QUESTION/CONVERSATION ──► ModelRouter FAST_REASONER ◄────┘
                                     (memory context, optional Brave research)
TaskEngine  ──► GatewayTaskPlanner (strict Pydantic TaskPlan, 1 corrective retry)
           ──► AgentRuntime: policy → grounding → resources → observe → dispatch → verify
           ──► ToolRegistry: system.app_launch · uia.{focus,click,fill,press,invoke} · browser.*
           ──► CompositeEnvironment / CompositeVerifier (per-tool routing)
           ──► SQLite: tasks (optimistic version), events (causal), sessions, memory
AudioHub (dormant) ──► local VAD/wake/ASR ──► LiveConversationProvider (Gemini, replaceable)
                   └──► VoiceConversationBridge ──► TaskEngine (narrow typed tool vocabulary)
```

Trust boundaries are real and enforced: `AuthorizationContext` is minted only in
`TaskEngine.submit`; model output is validated by `GatewayTaskPlanner` and re-validated
by `ActionProposal.to_domain()`; approvals are one-time, exact-fingerprint, expiring;
verification is independent of tool return values (`FactVerifier` refuses to pass without
an `OBSERVED`/`RETRIEVED` evidence record).

## B. Existing capabilities (inventory used by this audit)

| Layer | Implementation | Notes |
|---|---|---|
| Task engine | `core/engine.py` | bounded queue, workers, per-task deadline, approval resume, crash recovery, child tasks, descendant cancellation |
| Task state machine | `core/tasks.py` | `created/queued/understanding/planning/ready/running/waiting{,_model,_resource,_user,_auth}/requires_user_input/verifying/recovering/interrupted/partially_completed/unknown/failed/cancelled/blocked/completed` + step machine + transition guards |
| Planner | `core/planner.py`, `core/planner_contract.py` | `planner-contract-5`, live Pydantic schema fragments in the prompt, single corrective retry, sanitized diagnostics, consequential-postcondition gate |
| Runtime loop | `core/runtime.py` | observe → ground → policy → resources → dispatch → verify, `UNKNOWN` on ambiguous outcomes |
| Perception/grounding | `core/grounding.py`, `core/computer.py`, `adapters/perception.py` | source-priority resolver (API → DOM → UIA → OCR → vision → coordinate), ambiguity refusal, coordinate safety gate |
| App discovery | `adapters/windows_app_discovery.py` | Start Apps (packaged AUMID), Start Menu `.lnk` via `IShellLinkW`, uninstall registry, app/PWA (`--app-id=`/`--app=`) detection, dedup by canonical identity |
| App resolution | `WindowsApplicationResolver` | catalog-first ranking, alias/App Paths/PATH fallback, **fails closed on ambiguity** (`APPLICATION_AMBIGUOUS`) |
| App launch | `adapters/windows_app_launch.py` | baseline-before-dispatch, `shell=False`, executable/package identity window ownership, fresh re-observation, focus attempt never trusted as evidence |
| UIA | `adapters/windows_uia.py` | HWND-tree adapter, `GetGUIThreadInfo` keyboard focus, semantic grounding, freshness leases, human-interference detection |
| Browser | `adapters/browser_playwright.py` | isolated non-persistent Chromium, DOM snapshot, redacted URLs, per-page leases |
| Voice | `core/voice.py`, `core/voice_bridge.py`, `adapters/{audio_local,gemini_live}.py` | dormant-first, local wake/VAD/ASR before cloud, generation fences, barge-in, narrow bridge to TaskEngine |
| Model gateway | `core/model_gateway.py` | provider-neutral roles (planner/fast/deep/vision/ocr/asr/tts/embedding), privacy routing, circuit status, escalation |
| Memory | `adapters/sqlite.py` (repository), `core/personalization.py`, `core/extensions.py` | consent-gated semantic/episodic memory, working + short-term memory, procedural workflows, deletion epoch, principal isolation, payment/credential governance |
| Security | `core/policy.py`, `core/redaction.py`, `adapters/secrets.py` | risk floors, scoped approval, redaction, secret-by-reference |
| Events | `core/event_bus.py`, `adapters/sqlite.py` | durable append + in-process fanout + replay from cursor |

## C. Green / yellow / red gap matrix

Legend: **GREEN** = correct and covered · **YELLOW** = partially implemented · **RED** = missing or wrong.

| # | Required capability | Current implementation | Status | Defect | Required change |
|---|---|---|---|---|---|
| 1 | Coherent startup → ready → interact | `lifespan` starts engine, recovers incomplete tasks; health/capabilities endpoints | GREEN | — | none |
| 2 | Goal → workflow planning | `GatewayTaskPlanner` + strict `TaskPlan` + topological engine | GREEN | — | none |
| 3 | Actions vs tasks separation | `ActionContract` vs `TaskRecord`/plan steps | GREEN | — | none |
| 4 | Full task lifecycle incl. `RECEIVED`, `RESUMING`, `WAITING_FOR_*` | `TaskStatus` has `waiting/_model/_resource/_user/_auth`, `recovering`, `verifying`; **no** `RECEIVED`, `RESUMING`, `WAITING_FOR_APPLICATION`, `WAITING_FOR_BROWSER`, `WAITING_FOR_EXTERNAL_RESULT`, `WAITING_FOR_VERIFICATION`; **no persisted "what am I waiting for" field** | YELLOW | `#4` | add states + `waiting_reason` persisted on the record |
| 5 | Long-running tasks with completion hierarchy (event → job id → app state → poll) | only bounded polling inside adapters; **no engine-level wait primitive, no external-task tracking, no adaptive backoff utility** | RED | `#5` | `core/waits.py` coordinator + `system.wait_for_condition` tool + waiting states |
| 6 | Conversation reliability (mic→VAD→wake→ASR→normalize→route) | dormant-first `AudioHub`, local Vosk/WebRTC VAD, utterance identity | YELLOW | Windows/provider validation only | none (real-device validation) |
| 7 | General vs **system/environment** questions | intent classifier routes every non-action question to `FAST_REASONER`; **no deterministic environment answer path** → "Is Chrome open?" is answered by an LLM with no environment facts | RED | `#1` | `core/environment_questions.py` + route in `/api/v1/interactions` and voice responder |
| 8 | Commands become tasks | `may_require_runtime_task` + confidence ≥ 0.75 → `TaskEngine.submit` | GREEN | — | none |
| 9 | Launch semantics `reuse / launch_if_not_running / force_new_window / force_new_instance` | reuse + launch implemented and verified by fresh observation; `force_new_window`/`force_new_instance` **always raise** `ADAPTER_UNAVAILABLE` (no capability advertisement exists anywhere) | RED | `#2` | descriptor capability fields + generic browser-class detection + argument-carrying dispatch + new-window-only verification |
| 10 | Separate focus states (running/visible/foreground/focus-requested/focus-verified/control-focused) | `WindowRecord` has `visible/minimized/foreground`; UIA has `window.foreground`, `window.focused_element`; app-launch `observe()` exposes **only** `application.running`, `process.running`, `window.open`, `window_ids` | YELLOW/RED | `#3` | add `window.visible`, `window.foreground`, `application.focused`, `focus_verified`, `focus_error`, `focus_strategy` facts |
| 11 | Real focus verification (fresh observation, ownership by PID/exe/package) | `_sync_focus_window` calls `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` **once**, ignores the BOOL result, has **no retry/attach-thread strategy**, then raises `WINDOW_NOT_FOUND` — conflating "cannot focus" with "no window" | RED | `#3` | multi-strategy focus + bounded retry + fresh foreground verification + distinct `FOCUS_FAILED` code |
| 12 | Launch verification fail-closed | baseline-before-dispatch + fresh observation + executable/package identity | GREEN | — | preserve |
| 13 | Generic catalog-first discovery | Start Apps / Start Menu `.lnk` / registry / PWA / App Paths / PATH; ambiguity fails closed | GREEN | App Paths and PATH are **fallback-only**, not catalog sources | optionally promote App Paths into the catalog (low risk, deferred) |
| 14 | Multi-application commands decomposed | planner receives an **untrusted deterministic intent hint** with action/target spans, but **no prompt rule** telling it to emit one step per application, and no entity-typing for "Photos and Camera" | YELLOW | `#6` | planner rule + `application_entity` typing in the hint |
| 15 | Continue after opening (GUI follow-up) | `uia.*` tools ground a semantic target from a fresh observation; but the UIA backend is **HWND-based** so Chrome's custom-drawn omnibox is not addressable, and Playwright attaches a *fresh* context, never the user's browser | YELLOW/RED | `#7` | document; no Chrome hack; CDP-attach is a separate opt-in capability |
| 16 | Perception hierarchy | `TargetResolver` enforces source priority; coordinate fallback denied by default | GREEN | — | none |
| 17 | Semantic GUI grounding | `TargetCandidate` + `TargetResolver` + `ground_action` before policy | GREEN | — | none |
| 18 | Bounded scroll/search loop | **no scroll tool and no "reveal target by scrolling" loop** | RED | `#8` | `uia.scroll` tool + bounded re-search loop in grounding (scoped) |
| 19 | Dynamic UI / stale references | observation leases, `is_current`, `reground_stale_target`, `EnvironmentChangeDetector` | GREEN | — | none |
| 20 | Planner prompt carries live schemas | `_live_field_schema`, condition/verification/fallback fragments generated from Pydantic | GREEN | duplicate rule "17." in the prompt list (cosmetic) | tidy |
| 21 | Planner retry with classified error | one retry + `correction_instruction(category, detail)` + `PlannerDiagnostic(provider, model, contract_version, attempts, category, detail)` | GREEN | — | none |
| 22 | Dynamic model routing | role-based router with privacy/capability filters | GREEN | — | none |
| 23 | Gemini Live is a voice adapter only | `VoiceConversationBridge` narrow vocabulary; spoken completion requires runtime `COMPLETED` | GREEN | — | none |
| 24 | Voice state machine + barge-in | `VoiceState` machine, generation fences, `interrupt()` forwarding | GREEN | — | none |
| 25 | Streaming response correctness | generation fences + duplicate/non-increasing audio guards **but**: (a) **no speech lock** — `acknowledge_locally` and the receive loop can interleave playback; (b) local TTS of the **second and later** transcript segments is permanently suppressed by the `not self._first_audio_recorded` gate once any chunk has played | RED | `#9` | separate "provider audio played" flag from the telemetry flag; serialize output through one lock |
| 26 | Memory tiers | working / short-term / episodic / semantic / procedural / personalization | GREEN | — | none |
| 27 | LLM cannot mutate memory directly | consent-gated `store()` path + governance + deletion epoch | GREEN | — | none |
| 28 | RAG = structured + vector, not a DB swap | SQLite + optional embeddings + lexical fallback | GREEN | — | none |
| 29 | Scoped retrieval ("reply to Rahul") | memory retrieval by query text, principal/session scoped | GREEN | no structured contact preference type | deferred (retrieval already works) |
| 30 | Credentials never in JSON/SQLite/prompts/memory | `SecretRef`, keyring adapter, governance pattern | GREEN | — | none |
| 31 | Browser/URL knowledge + reuse of logged-in sessions | Playwright is deliberately isolated (ADR-0002); **no CDP attach** | YELLOW | by design | document |
| 32 | Environment intelligence | `EnvironmentDiscovery` (diagnostics) is **only** exposed through `/api/v1/diagnostics`; the assistant never consults it when answering | RED | `#1` | same fix as #7 |
| 33 | Startup application catalog | resolver catalog with 30 s cache | GREEN | — | none |
| 34 | Running-application intelligence | `WindowsAppLaunchProvider.running_applications()`, window ownership by exe/package | GREEN | not reachable from conversation | same fix as #7 |
| 35 | Task context (current step, expected/observed state, waiting condition) | `ActionStep` status/reason, events; **no persisted waiting condition or expected state** | YELLOW | `#4` | `waiting_reason`, `current_step_id`, observed/expected state in the task record |
| 36 | Observe → plan → act → verify loop | enforced per action in `AgentRuntime` | GREEN | — | none |
| 37 | Explicit verification | postconditions + `FactVerifier` + per-adapter verifiers | GREEN | — | none |
| 38 | Layered error codes | `ComputerFailureCode` (25 codes) + `PlanFailureCategory` + `AriseError` classification | GREEN | missing `FOCUS_FAILED`, `LAUNCH_MODE_UNSUPPORTED` | add two codes |
| 39 | Fail closed | ambiguity, unknown ownership, missing verification all fail closed | GREEN | — | preserve |
| 40 | Security/risk preserved | risk floors, approvals, postcondition gate | GREEN | — | preserve |
| 41–62 | Process/quality requirements | — | — | — | this document + staged implementation |

## D. Exact defects discovered (source-traced)

| ID | Defect | Location | Impact |
|---|---|---|---|
| `#1` | **System/environment questions are answered by an LLM with no environment facts.** `interact_with_text` handles STATUS/CANCELLATION/command/clarification and then falls through to `ModelRouter`. "Is Chrome open?", "What applications are running?", "Which window is focused?", "How much RAM do I have?", "Did you successfully open Camera?" all reach `FAST_REASONER` with no observed state. | `src/arise/server.py:2060-2300`, `src/arise/core/intent.py` | Hallucinated system state; fails requirement 7 and Phase A tests 3–4 |
| `#2` | **`force_new_window` / `force_new_instance` are unconditionally rejected.** `launch_application` raises `ADAPTER_UNAVAILABLE` before resolution, so "Open a new Chrome window" can never succeed, and the failure is not scoped to the resolved application's capabilities. Also `launch_process()` cannot pass arguments. | `src/arise/adapters/windows_app_launch.py:1165-1170`, `:1251`, `Win32AppLaunchBackend.launch_process` | Phase D impossible; requirement 9 unmet |
| `#3` | **Focus is requested once, never verified as a distinct state, and "cannot focus" is reported as "window not found".** `SetForegroundWindow` fails silently under the Windows foreground lock when the calling process is not foreground; the code then raises `WINDOW_NOT_FOUND`, which (a) makes `AppLaunchTool` fail a launch that actually worked, (b) makes `_reuse_existing_window` fall through to spawning a **new** instance because reuse looked unconfirmed, and (c) yields `focus_failed=false` + `focused=false` reports because `observe()` never even measures foreground/focus for `system.app_launch`. | `src/arise/adapters/windows_uia.py:487-509`, `src/arise/adapters/windows_app_launch.py:1332-1402`, `:1782-1860` | Requirement 10, 11, 43 and Phase B/C failures |
| `#4` | **Task lifecycle is missing states and cannot express "what am I waiting for".** No `RECEIVED`, `RESUMING`, `WAITING_FOR_APPLICATION/BROWSER/EXTERNAL_RESULT/VERIFICATION`; `TaskRecord` has `status_reason` but nothing that names the wait target, and nothing survives restart to decide whether a wait can resume. | `src/arise/core/tasks.py:26-60`, `:400-414` | Requirements 4, 5, 35, 46, 47, 48 |
| `#5` | **No event-driven waiting primitive.** Every wait in the codebase is a fixed-interval `asyncio.sleep` loop inside an adapter (`_LAUNCH_POLL_INTERVAL_SECONDS = 0.15`). There is no subscription to `EventBroker`, no adaptive backoff, no external job/task tracking, and no way for a plan to say "wait until X is true (up to N minutes)". | `src/arise/adapters/windows_app_launch.py:1554`, `src/arise/core/event_bus.py` | Requirements 5, 46, 47; "give this to Arena and wait" is impossible |
| `#6` | **Multi-application commands are not deterministically decomposed.** The planner gets an intent hint but no rule that each application entity becomes its own step; "Photos and Camera" is one target span. | `src/arise/core/planner_contract.py`, `src/arise/core/intent.py:_structured_command` | Requirement 14, Phase E |
| `#7` | **No browser-chrome/DOM access to the user's real browser.** UIA is HWND-based (Chrome omnibox is custom-drawn); Playwright starts an isolated context (ADR-0002). "Open Chrome and click the address bar" cannot be verified today. | `src/arise/adapters/windows_uia.py`, `docs/adr/0002` | Phase F/G; **must not be hacked** — reported as a real gap |
| `#8` | **No scroll/reveal loop and no scroll tool.** Requirement 18's bounded "scroll → re-observe → re-search" loop does not exist. | `src/arise/adapters/windows_uia.py` (operations: invoke/click/fill/fill_secret/focus/press) | Requirement 18 |
| `#9` | **Streaming speech can drop middle segments.** `speak_text` sets `_first_audio_recorded = True` when it plays its first local TTS chunk; the `OUTPUT_TRANSCRIPT` handler then refuses to speak any later final transcript for the rest of the turn (`and not self._first_audio_recorded`). Local TTS is also not serialized against `acknowledge_locally`, so a template acknowledgement can interleave with a streamed response. | `src/arise/core/voice.py:1275-1350`, `:1785-1800` | Requirement 25 — exactly the "sentence 2 disappeared" symptom |

## E. Files/modules responsible

| Area | Files |
|---|---|
| Conversation routing | `src/arise/server.py` (`interact_with_text`, `_answer_voice_question`), `src/arise/core/intent.py` |
| Environment intelligence | `src/arise/adapters/diagnostics.py`, `src/arise/core/capabilities.py`, `src/arise/adapters/windows_app_launch.py` (`running_applications`, `installed_applications`), `src/arise/adapters/windows_uia.py` (`foreground_window`) |
| Task lifecycle | `src/arise/core/tasks.py`, `src/arise/core/engine.py`, `src/arise/adapters/sqlite.py` |
| Waiting | `src/arise/core/event_bus.py`, `src/arise/core/events.py`, (new) `src/arise/core/waits.py` |
| App lifecycle | `src/arise/adapters/windows_app_discovery.py`, `src/arise/adapters/windows_app_launch.py`, `src/arise/adapters/windows_uia.py` (`focus_window`) |
| Planner | `src/arise/core/planner.py`, `src/arise/core/planner_contract.py`, `src/arise/core/models.py` |
| Voice | `src/arise/core/voice.py`, `src/arise/core/voice_bridge.py`, `src/arise/adapters/gemini_live.py` |
| Verification | `src/arise/core/runtime.py`, `src/arise/core/computer.py` (failure codes), `CompositeVerifier` in `server.py` |

## F. Proposed implementation sequence

1. **Stage 1 — environment intelligence** (defect `#1`, capability 7/32/34): new
   `core/environment_questions.py` (deterministic question typing + fact gathering through
   existing adapters), wired into `/api/v1/interactions` and the voice informational
   responder. No new dependencies, no LLM for facts.
2. **Stage 2 — focus, observation and launch verification** (defect `#3`, capability 10/11/12/43):
   multi-strategy `focus_window` with fresh verification and `FOCUS_FAILED`; richer
   `system.app_launch` observation facts; distinct focus states in diagnostics.
3. **Stage 3 — launch-intent capabilities** (defect `#2`, capability 9): capability fields on
   `ApplicationDescriptor`, generic Windows-registered-browser detection, argument-carrying
   dispatch, new-window-only verification, `LAUNCH_MODE_UNSUPPORTED` failure code.
4. **Stage 4 — lifecycle and event-driven waiting** (defects `#4`, `#5`, capability 4/5/35/46/47/48):
   new states + persisted `waiting_reason`, `core/waits.py` coordinator (event subscription +
   adaptive backoff), `system.wait_for_condition` tool.
5. **Stage 5 — streaming speech correctness** (defect `#9`, capability 25): output serialization
   and correct per-segment gating.
6. **Stage 6 — planner decomposition guidance** (defect `#6`, capability 14).
7. **Stage 7 — tests, docs, lint/format/compile, full suite, Windows validation commands.**

## G. Risk of each change

| Stage | Risk | Mitigation |
|---|---|---|
| 1 | Medium — a new route branch could shadow legitimate commands | Branch runs **after** STATUS/CANCELLATION and **only** for classifications that are not `may_require_runtime_task`; every answer is built from adapters; any unavailable fact yields a truthful "unavailable" answer, never a guess |
| 2 | High — touches the Windows focus path used by every launch | Behaviour is unchanged on hosts where `SetForegroundWindow` already works; new strategies only run after the current one fails; every strategy ends with an independent `GetForegroundWindow` check; fake-backend tests assert both success and failure paths |
| 3 | Medium — argument passing to `Popen` | `shell=False` list form preserved; arguments come only from descriptor capability metadata; unknown intent still fails closed |
| 4 | Medium — new task states could break existing transitions | New members are additive; transitions are explicit; unknown statuses fall through existing recovery paths; no changes to existing terminal semantics |
| 5 | Medium — voice timing changes | Additive lock + split flag; existing generation fences untouched; tests replay multi-segment turns |
| 6 | Low — prompt text only | Rule is additive; validation stays strict; no schema change |

## H. Tests required (Arena, deterministic)

* **Stage 1:** question typing (positive/negative), app-running yes/no/unknown, foreground
  window, memory/CPU answer, principal-safe answers, "no adapters → unavailable not invented",
  route ordering (command still becomes a task; "is X running" never creates a task),
  voice responder path.
* **Stage 2:** fake Win32 backend — success first try, success on retry, permanent failure
  returns `FOCUS_FAILED` (not `WINDOW_NOT_FOUND`), foreground PID ownership mismatch,
  observation facts contain `window.foreground`/`application.focused`/`focus_verified`,
  launch verification failing when focus was requested but not verified, reuse path records
  `reuse_unconfirmed` and does not claim focus.
* **Stage 3:** `force_new_window` supported → arguments dispatched + **new** window required;
  unsupported → `LAUNCH_MODE_UNSUPPORTED`; `force_new_instance` unsupported → same;
  descriptor capability advertisement from fake registered-browser data; no app-specific branch.
* **Stage 4:** wait coordinator event-wake vs poll-wake vs timeout vs cancellation, adaptive
  backoff bounds, `waiting_reason` persisted and restored, new states reachable and
  transitions guarded, tool end-to-end through `AgentRuntime` with a fake environment.
* **Stage 5:** multi-segment turn plays every segment in order; barge-in stops remaining
  segments; acknowledgement cannot interleave; stale generation segments are dropped.
* **Stage 6:** planner prompt contains the decomposition rule; validation unaffected.
* **Regression:** full suite + `ruff check`/`ruff format --check`/`compileall`.

## I. Real-Windows tests required (not claimable from Arena)

All of Phase A–G in the milestone request, plus:

1. Focus on a locked-foreground desktop: run ARISE from a background console and request
   "Open Chrome" — assert `focus_verified` reflects reality and no duplicate Chrome is spawned.
2. Reuse semantics: Chrome already open on a second monitor, minimized, and behind another
   window — assert reuse, not new instance, and accurate focus reporting.
3. `force_new_window` on Chrome/Edge/Firefox and on an app that does not support it (expect
   `LAUNCH_MODE_UNSUPPORTED`).
4. Packaged apps (Calculator, Settings, Photos, Camera) and a browser-installed PWA
   (WhatsApp Web) through Start Menu discovery — assert resolution source and activation.
5. Multi-app command with five applications — five independent steps/verifications, one
   failure does not erase the others' diagnostics.
6. GUI follow-up "Open Chrome and click the address bar" — **expected to be blocked** until a
   CDP/DOM attach capability exists; verify the failure is `TARGET_NOT_FOUND`/`FOCUS_FAILED`
   and not a false success.
7. Long-running external wait: `system.wait_for_condition` on a real file/process event; verify
   event-driven wake and bounded polling.
8. Voice: real device wake, barge-in mid-sentence, three-segment response — verify no dropped
   segment, no interleaving, no resumed old generation.
