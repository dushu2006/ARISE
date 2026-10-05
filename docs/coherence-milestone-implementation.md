# Coherence milestone — implementation log (Phase 2)

Companion to `docs/coherence-milestone-audit.md` (the A–I architectural audit, Phase 1).
This document records what was actually implemented, what was preserved, how it was
tested, and — explicitly — which claims are **Arena-verified** versus which still
require **real Windows verification**.

Delivery order follows the request: the audit was produced first and no source file was
changed until it existed.

---

## 1. Summary of architectural changes

Six additive, integration-first stages. Nothing was rewritten, no framework was added, and
no new app-specific (Chrome/WhatsApp/Camera) branch exists anywhere.

| Stage | Defects | Architectural change |
|---|---|---|
| 1. Environment intelligence | `#1` | New `EnvironmentQuestionService` types and answers system/environment questions from the existing adapters. It is consulted **before** the model router in both the text path (`/api/v1/interactions`) and the voice informational path, and only for classifications that are *not* commands. It has no execution or verification authority: it is read-only observation, and it fails closed ("I cannot inspect that on this machine right now") instead of guessing. |
| 2. Focus + observation | `#3` | Focus is now a **verified evidence object**, not a `BOOL` return. `WindowsUiaProvider.focus_window_evidence` tries bounded Windows activation strategies, then re-reads `GetForegroundWindow` and compares the owning PID. New failure code `FOCUS_FAILED` means "the window exists but is not foreground"; `WINDOW_NOT_FOUND` is reserved for "no such window". `system.app_launch` now reports `window.open` / `window.visible` / `window.count` / `window.minimized_count` / `window.foreground` / `application.focused` as separate facts: running ≠ window open ≠ focused. |
| 3. Launch-intent capabilities | `#2` | `ApplicationDescriptor` advertises `new_window_supported` / `new_instance_supported` with their argument switches, discovered from what Windows itself registers (vendor-neutral registered-browser lookup, cached). `force_new_window` / `force_new_instance` now work for applications that advertise them and **fail closed** with `LAUNCH_MODE_UNSUPPORTED` otherwise. Arguments are dispatched as a `shell=False` list and validated against a bounded switch pattern. Reuse now succeeds on an ownership-verified visible window even when focus was refused, and never spawns a duplicate because focus could not be taken. |
| 4. Lifecycle + waiting | `#4`, `#5` | `TaskStatus` gains `RECEIVED`, `RESUMING` and the four named wait states (`WAITING_FOR_APPLICATION`, `WAITING_FOR_BROWSER`, `WAITING_FOR_EXTERNAL_RESULT`, `WAITING_FOR_VERIFICATION`), plus a persisted `waiting_reason` and `resume_count`. New `core/waits.py` `WaitCoordinator` implements the required completion hierarchy: **event → job/state → controlled polling**, polling with adaptive backoff (`min → max`, default 0.5 s → 15 s) instead of a fixed 0.15 s hammer. New read-only tool `system.wait_for_condition` lets a plan wait for an observed fact, bounded by `min(parameter, action timeout, 900 s)`, and names the wait in task state while doing so. |
| 5. Streaming speech | `#9` | Output playback is serialized by an `asyncio.Lock`, so an acknowledgement can never interleave with a streamed response. The per-turn "provider audio was played" state is now separate from the *telemetry* "first audio" flag — the conflated flag was the exact cause of dropped middle segments. Provider audio is never spoken a second time by local TTS. |
| 6. Planner decomposition + wait rules | `#6` | Two additive prompt rules (no schema change, validation unchanged): one step per distinct application/deliverable for multi-entity commands, and "waiting is an action, not an assumption" (use `system.wait_for_condition` with a bounded timeout, never assume external completion). System-resource questions and multi-app decomposition are now expressible; the strict validator was **not** loosened anywhere. |

## 2. Files changed

**Modified (13):**

```
frontend/src/types.ts                       TaskState union + TaskSnapshot.waiting_reason/resume_count
src/arise/adapters/windows_app_discovery.py launch capability fields, registered-browser detection, advertisement
src/arise/adapters/windows_app_launch.py    focus evidence, open≠focused facts, launch intents, argument dispatch
src/arise/adapters/windows_uia.py           focus_window_evidence, multi-strategy activation, FOCUS_FAILED
src/arise/core/computer.py                  FOCUS_FAILED, LAUNCH_MODE_UNSUPPORTED, FocusStrategy, WindowFocusEvidence
src/arise/core/engine.py                    RECEIVED on submit; waiting-aware recovery after restart
src/arise/core/models.py                    TaskSnapshot carries waiting_reason/resume_count
src/arise/core/planner_contract.py          app_launch fact guidance, decomposition rule 19, waiting rule 20
src/arise/core/runtime.py                   observed-fact allow-list; begin_wait/end_wait status sink
src/arise/core/tasks.py                     new states, transitions, waiting_reason/resume_count, begin_wait/end_wait
src/arise/core/voice.py                     serialized output, per-turn provider-audio state
src/arise/server.py                         environment-question routing, wait-tool registration + status sink wiring
tests/test_production_desktop_path.py       5 tests rewritten to the new open≠focused contract
```

**Added (4):**

```
src/arise/core/environment_questions.py  deterministic environment Q&A (no model)
src/arise/core/waits.py                  WaitCoordinator: event-driven wait + adaptive polling
src/arise/adapters/environment_wait.py   system.wait_for_condition tool
tests/test_coherence_milestone.py        30 tests for stages 1–6
```

Plus `docs/coherence-milestone-audit.md` (Phase 1 deliverable).

## 3. New capabilities

1. Environment/system questions answered from observed state, in text and voice, with no
   model call and no invented facts (`is X running`, `what is running`, `which window is
   focused`, `is X installed`, system resources).
2. Distinct, verified focus states: `focus_verified`, `focus_strategy`, `focus_error`,
   `owner_matches_foreground`; `FOCUS_FAILED` distinct from `WINDOW_NOT_FOUND`.
3. `system.app_launch` observation now separates `application.running` / `window.open` /
   `window.visible` / `window.count` / `window.minimized_count` / `window.foreground` /
   `application.focused`.
4. Generic launch intents: `reuse_existing_if_available`, `launch_if_not_running`,
   `force_new_window`, `force_new_instance`, with `LAUNCH_MODE_UNSUPPORTED` when the
   resolved application does not advertise the mode.
5. Named task wait states with a persisted `waiting_reason`, resumed/reported across restarts.
6. `system.wait_for_condition`: bounded, event-preferring, side-effect-free waiting on any
   observed fact, with adaptive backoff and cancellation.
7. Multi-application decomposition guidance and explicit "wait, don't assume" planning.
8. Serialized, non-dropping streamed speech output.

## 4. Capabilities preserved (regression-checked)

Planner strict validation and plan-contract-5 behaviour (never loosened); planner cannot
execute; application identity verification; baseline/fresh window observation; fail-closed
launch; executable/package identity matching; risk model R0–R4 and policy gates;
authorization/trust context copied at the trusted boundary; postcondition verification with
observed/retrieved evidence and observation leases; memory deletion epoch and principal
isolation; streaming turn/session correctness with generation fences; cancellation and
admission barriers; secret redaction in events and logs; SQLite + vector split with no
credentials stored; `shell=False` process dispatch; existing diagnostics keys
(`focus_verified`, `focus_failed`, `focus_error`, `focus_strategy`, `mode`,
`activation_method`) — note `reuse_focus_error`/`confirmation_failed` were replaced by the
single, honest `focus_error` key.

## 5. Tests added

`tests/test_coherence_milestone.py` — 30 tests:

* **Environment questions (9):** general questions and commands are never treated as
  environment questions; running yes/no answered from processes; process names without paths
  match (`chrome` ↔ `chrome.exe`); running list is deduplicated; foreground-window and
  installed questions read adapters; failing/absent adapters produce a truthful
  "unavailable", never a guess.
* **Routing (2, end-to-end HTTP):** an environment question is answered with the model
  provider replaced by an object that raises if called; a command still becomes a task.
* **Wait coordinator (5):** immediate resolve with zero polls; event wake before the poll
  interval; adaptive backoff bounds and bounded work; cancellation outcome; repeated
  predicate failure reported as unavailable rather than success.
* **Wait tool (6):** resolves when the fact becomes true; timeout fails with "no completion
  is claimed"; wait target selects the matching task state; parameter validation; never
  exceeds the action timeout; a broken observer never reports success.
* **Task lifecycle (5):** distinct named wait states exist; `begin_wait`/`end_wait` record
  and resume; `waiting_reason` survives persistence; non-waiting states cannot claim a wait;
  `RECEIVED` is a valid entry state.
* **Voice (3):** every segment of a multi-segment reply is spoken; provider audio is not
  spoken twice; output playback is serialized (no interleaving).

Five pre-existing tests in `tests/test_production_desktop_path.py` were rewritten to the new
contract (open ≠ focused): reuse of an unfocusable window now succeeds without spawning a
duplicate, and focus is reported as `False` rather than assumed.

## 6. Test results

```
$ .venv/bin/python -m pytest -q tests/
602 passed, 1 skipped, 136 subtests passed in 37.7s
```

Baseline before any change: `571 passed, 1 skipped, 119 subtests passed in 30.1s`
(+1 from a split test, +30 new tests; no test was deleted or weakened to make a change pass
except the five explicitly rewritten to the corrected open≠focused contract).

## 7. Lint / format / compile results

```
$ .venv/bin/python -m ruff check .                 # E,F,I,UP,B per pyproject
All checks passed!

$ .venv/bin/python -m ruff format --check .
1 file would be reformatted, 127 files already formatted
  -> test_nvidia_gateway.py  (pre-existing at HEAD, NOT touched by this work)

$ .venv/bin/python -m compileall -q src tests
exit 0
```

Type checking: this repository configures **no** type checker (`pyproject.toml` dev extras
are `pytest`, `pytest-asyncio`, `ruff`; there is no `mypy`/`pyright` config). The type gate
used is therefore `ruff`'s `E/F/I/UP/B` rules plus `compileall`, both clean. A full static
type pass (e.g. `mypy --strict`) was deliberately not introduced, to avoid adding a tool the
project does not use and to avoid thousands of pre-existing findings.

## 8. Known limitations

1. **No DOM/CDP access to the user's real browser** (audit defect `#7`). "Open Chrome and
   click the address bar" still cannot be executed or verified: UIA only sees HWNDs and
   Playwright's context is isolated by design (ADR-0002). This is reported as a real gap, not
   papered over with a Chrome-specific hack.
2. **No scroll/reveal loop or scroll tool** (audit defect `#8`): the bounded
   scroll → re-observe → re-search loop of requirement 18 does not exist.
3. **Environment questions are answered only when an environment adapter is available.** On a
   non-Windows host, or with desktop disabled, the honest answer is "unavailable"; the model is
   deliberately *not* used as a fallback, because inventing environment state is the exact
   failure mode this work removes.
4. **`system.wait_for_condition` needs a plan that uses it.** The planner now has the rule, but
   model-generated plans are not guaranteed to use it; deterministic routing for long-running
   waits may be needed in a later stage.
5. **Wait events are broker-wide per task.** `WaitCoordinator` wakes on events for its task id
   and re-checks the predicate; providers that do not publish events simply fall back to
   adaptive polling (correct, less immediate).
6. **Restart recovery of waits is conservative.** After a restart a task found in a wait state
   becomes `INTERRUPTED` (or `REQUIRES_USER_INPUT` for approval states) and names what it was
   waiting for; it is not auto-resumed, because the plan is not persisted across restarts.
7. Voice changes are logic-level; real device latency, wake-word behaviour and barge-in
   acoustics can only be confirmed on Windows audio hardware.

## 9. Real-Windows validation commands (required, not yet run here)

```powershell
# 0. Environment
python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"; .\.venv\Scripts\pip install pytest-subtests

# 1. Full suite on the real desktop stack
.\.venv\Scripts\python -m pytest -q tests

# 2. Environment questions (no model, no hallucination)
python -m arise serve            # then:
#   "Is Chrome running?"                 -> observed yes/no, never a guess
#   "What applications are running?"     -> observed list
#   "Which window is in focus?"          -> observed foreground window
#   "How much RAM do I have?"            -> observed resources
#   "Open Chrome"                        -> still becomes a task (not an answer)

# 3. Focus verification (run ARISE from a background console)
#    Chrome already open -> "Open Chrome"
#    Expected: reuse, no duplicate process, focus reported truthfully
#    (focus_verified true/false + focus_strategy), never WINDOW_NOT_FOUND.

# 4. Launch modes
#    "Open a new Chrome window"        -> force_new_window, requires a NEW window id
#    "Open another Notepad instance"   -> force_new_instance
#    "Open a new window" for an app that does not support it -> LAUNCH_MODE_UNSUPPORTED

# 5. Packaged apps / PWA
#    Calculator, Settings, Photos, Camera, and a browser-installed PWA (e.g. WhatsApp Web)
#    -> resolution source and activation method recorded; no app-specific code path.

# 6. Multi-application command
#    "Open Notepad, Calculator and Chrome" -> one step per application, each verified.

# 7. Long-running wait
#    A plan using system.wait_for_condition on a real file/process event:
#    verify event-driven wake, bounded backoff, and that a timeout never claims completion.

# 8. Voice
#    Real device: wake, barge-in mid-sentence, three-segment response.
#    Expected: all three segments audible, no interleaving, no stale generation.

# 9. GUI follow-up (expected to be BLOCKED today - audit defect #7)
#    "Open Chrome and click the address bar"
#    Expected: TARGET_NOT_FOUND / FOCUS_FAILED, never a false success.
```

## 10. Arena-verified vs. Windows-only claims

**Arena-verified (this sandbox, deterministic, reproducible by the suite above):**

* Environment-question typing and routing (including "the model is never called"), fail-closed
  unavailable answers, and process-name matching.
* Wait coordinator behaviour: event wake vs. poll, adaptive backoff bounds, timeout,
  cancellation, predicate-unavailable.
* `system.wait_for_condition` semantics: parameter validation, resolution, timeout with no
  completion claim, wait-target → task status mapping, action-timeout bounding.
* Task lifecycle additions: new states, legal transitions, `waiting_reason`/`resume_count`
  persistence and resume counting, `RECEIVED` entry state, waiting-aware recovery.
* Voice: all segments of a multi-segment turn are spoken, provider audio is not double-spoken,
  playback is serialized.
* The full suite (602 tests), `ruff check`, `ruff format --check` (except one pre-existing
  untouched file) and `compileall` all pass.

**Requires real Windows verification (NOT claimed here):**

* All actual Windows API behaviour: `SetForegroundWindow` under the foreground lock, the
  attach-thread-input and Alt-nudge strategies, `GetForegroundWindow` ownership matching,
  UIA tree contents for real applications.
* Real launch/reuse/new-window/new-instance behaviour for any specific application, including
  the vendor-neutral registered-browser detection reading the Windows registry.
* Whether focus can in fact be taken on this machine/user session for a given app.
* DOM/CDP access to the user's real browser (defect `#7`) — still a gap.
* Audio-device behaviour: wake word, barge-in acoustics, real TTS latency and ordering on
  hardware.
* Multi-application decomposition against real installed apps, and any end-to-end timing or
  performance characteristic of the desktop stack.

No claim in this document asserts that a Windows behaviour was observed on Windows; every
Windows-specific claim above is explicitly listed as pending verification.
