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

---

# Pre-merge forensic review (additive, defect-driven)

A second pass over the branch, asked to validate that the implementation preserves
existing behaviour and closes the real Windows gaps, and to make **no broad changes**.
Four concrete defects were found and fixed; everything else was verified as already
correct and left alone.

## A. Findings

### A1. Multi-application decomposition (audit `#6`) — the audit was wrong, the code was right
The audit claimed multi-application commands "cannot be deterministically decomposed".
That is incorrect as an absolute statement:

* `TaskEngineConfig.max_plan_steps = 64` leaves ample room for a five-step plan.
* `AppLaunchTool.resources_for()` derives a **per-application** lock
  (`desktop.app.<sanitised-name>`), so five launches do not serialise or collide.
* Each step is verified independently by the runtime's `FactVerifier`.
* The behaviour was demonstrated on real Windows: "Open Spotify, calculator, file
  explorer, settings and camera" produced five independent `system.app_launch`
  actions, all verified.

The accurate problem statement is narrower: decomposition is **produced by the model**,
with no prompt rule stating the expectation and no `application_entity` typing in the
deterministic intent hint. That is YELLOW, not RED. The audit table and defect row have
been corrected in place; **no code behaviour was changed** (planner rule 19 is additive
prompt text and the strict validator is untouched).

### A2. Launch-intent flow — verified end to end, one real defect fixed
`launch_intent` is not dropped or rewritten by any layer:

    planner (rule 17) → ActionProposal.parameters → ActionModel.to_domain()
    → ActionContract.parameters (passed by reference, no filtering)
    → AppLaunchTool.execute() reads action.parameters["launch_intent"]
    → provider.launch_application(..., launch_intent=...)
    → backend dispatch

`ToolSpec.parameter_names` is advisory only (prompt + example); it does not filter
parameters, and `AppLaunchTool.validate_parameters` rejects unknown intents before
dispatch.

**Defect found (fixed):** `Win32AppLaunchBackend.activate_application()` refused any
argument-carrying launch unless the descriptor's activation method was `EXECUTABLE`.
Browsers are discovered as **Start Menu shortcuts**, and `_deduplicate_descriptors`
prefers the shortcut record over the executable record, so a shortcut-discovered browser
advertised `new_window_supported` and then refused to honour `force_new_window` — the
feature could not work for the most common case. Fixed: a launch mode that is advertised
now dispatches the **executable** with exactly the advertised switches; a
browser-installed application (PWA) still refuses (a bare browser switch would open the
browser instead of the app), and a descriptor with no executable fails closed.

### A3. Generic behaviour — no application-specific logic exists
Verified by search: no `if Chrome:` / `if WhatsApp:` / `if Spotify:` branch exists
anywhere in `src/`. The only application names in code are data, not behaviour:
`KNOWN_ALIASES` (name→identity resolution table), Playwright's channel names, and a
browser display-name map in diagnostics. Browser capability detection reads Windows'
own `SOFTWARE\Clients\StartMenuInternet` registration, so any registered browser is
treated as a browser and an unregistered application advertises nothing.

### A4. Focus is separate from launch success — verified, and made explicit
`system.app_launch` observation reports separate facts: `application.running` /
`process.running`, `window.open`, `window.visible`, `window.count`,
`window.minimized_count`, `window.foreground`, `application.focused`. Diagnostics report
`focus_requested`, `focus_verified`, `focus_error`, `focus_strategy`, `focus_attempts`,
`focus_owner_matches_foreground`. `window.focused_element` (UIA) remains the
target-control fact.

**Honest limitation found:** a `window.exists` fact was added and then **reverted**
during this review. The bounded desktop snapshot carries *visible* windows only
(`_snapshot_desktop`), so "a window exists but is minimized" is not observable through
this path — publishing the fact would have invented it. Consequently
`window.minimized_count` is structurally `0` with the current snapshot policy; it is
reported as-is rather than approximated, and a minimized application is deliberately
*not* claimed as open.

A successful `SetForegroundWindow` is never treated as proof anywhere: every strategy
ends with an independent `GetForegroundWindow` check, and the launch flow overrides the
adapter's claim with a **fresh snapshot** (`_reconcile_focus_evidence`).

### A5. Focus verification and duplicate launches — one real defect fixed
* Foreground timing: `_await_foreground` polls briefly (5 × 20 ms) per strategy, three
  bounded strategies, and the **fresh snapshot** decides (`_reconcile_focus_evidence`).
* HWND/PID ownership: `WindowFocusEvidence` carries `foreground_window_id`,
  `foreground_process_id` and `target_process_id`; `owner_matches_foreground` is
  reported separately from `focus_verified`.
* Packaged-app identity: `_window_owner_verified` matches on AUMID / package family from
  `app_user_model_id_for_window()` and `package_family_name_for_pid()`, so
  `ApplicationFrameHost`-hosted apps (Calculator, Settings, Camera) verify by app
  identity rather than by the hosting process.
* Stale observations and leases: the runtime observes immediately before dispatch,
  re-validates the lease "as close to dispatch as possible", and the adapter re-observes
  after the focus attempt; `FactVerifier.verify()` takes its **own** fresh observation,
  so a long wait can never make verification stale.
* Windows focus refusal: an already-open, ownership-verified, visible window satisfies
  reuse even when focus is refused, and **no process is spawned**
  (`tests/test_production_desktop_path.py::
  test_reuse_focus_failure_is_recorded_and_never_duplicates_the_application` and
  `test_unfocusable_existing_window_is_reused_with_unverified_focus`). Dispatch happens
  **once**, before the poll loop, so a failed confirmation can never spawn a duplicate.
* **Defect found (fixed):** the last activation strategy injects a synthetic Alt keypress
  (global input). Because activation is retried on every launch-poll iteration (~0.15 s),
  a slow launch could inject Alt several times per second into the user's session. It is
  now rate-limited to once per 5 seconds; the ordinary `SetForegroundWindow` still runs
  when the nudge is withheld.

### A6. Browser-installed applications (PWA / "WhatsApp Web") — generic support added
The reported failure was `requested_application = WhatsApp Web, candidate_count = 0,
APPLICATION_NOT_FOUND`, i.e. **no candidate at all**, so the cause is discovery/resolution,
not activation. Three generic gaps were found and fixed — none of them names a vendor or
an application:

1. **Discovery** (`_launches_browser_installed_app`): PWA detection previously matched
   only `--app-id=` / `--app=`. It now also recognises any shortcut whose target is a
   browser **registered with Windows** and whose arguments select an application (an
   `--app*` switch or a non-switch target). A plain browser shortcut (no selecting
   argument, e.g. `--profile-directory` only) is still the browser.
2. **Resolution** (`_bounded_name_match`): matching was exact-normalised-name only, so an
   application listed as "WhatsApp" was unreachable when the user said "WhatsApp Web"
   (and vice versa). A **bounded whole-name containment** rule was added at the lowest
   catalog priority (85, below exact 120 / alias 110 / process 100 / known-alias 95, above
   PATH fallbacks 80). Short names never match by containment ("word" does not reach
   "WordPad"); two equally plausible matches remain `APPLICATION_AMBIGUOUS`.
3. **Verification** (`_web_app_title_matches`): a PWA window shows the *site's* title,
   which changes as the page changes, so exact title equality made correct launches
   unverifiable. A bounded prefix/suffix match is used, while the strong check — the
   window's owning **executable** — is unchanged, and the window must still be one the
   launch actually changed.
4. **Entry-point identity** (`launch_arguments`): two Start Menu shortcuts pointing at the
   same executable with different arguments used to collapse into one catalog entry (one
   silently becoming an alias of the other, so launching by name could open the wrong
   entry point). The descriptor now carries bounded launch arguments and the canonical
   identity distinguishes those entry points; identical entry points found in the user and
   common Start Menu roots still deduplicate.

Whether the specific "WhatsApp Web" shortcut on the reporting machine is discovered can
only be confirmed on that machine (section F).

## B. Regressions found

**None in the Arena gate.** The full suite passed before and after every fix (602 → 620
tests, the delta being new tests). Real-Windows CI then found one genuine defect — see
section G, which also records its fix. No existing test was weakened: the five `test_production_desktop_path`
rewrites from the earlier stage were re-verified against the new contract, and no test in
this review was changed to accommodate new behaviour.

Two behaviour changes are intentional and are the fixes themselves:
* argument-carrying launch modes now dispatch the executable (previously refused for
  shortcut-discovered browsers);
* synthetic Alt input is now rate-limited (previously unbounded).

Both fail closed, and both are covered by new tests.

## C. Exact files requiring changes (this review)

```
src/arise/adapters/windows_app_launch.py    +67/-7   launch-mode dispatch, PWA title match,
                                                     bounded name containment, helpers
src/arise/adapters/windows_app_discovery.py +67/-1   launch_arguments field + identity,
                                                     generic browser-installed app detection,
                                                     dedupe pass-through
src/arise/adapters/windows_uia.py           +43/-7   Alt-nudge rate limit (threading lock)
tests/test_launch_intent_and_pwa.py         new      18 tests
docs/coherence-milestone-audit.md                    audit #6 / row 14 corrected
docs/coherence-milestone-implementation.md           this section
```

Nothing else was touched: planner validation, contracts, risk/policy, memory, security,
voice, tasks, runtime and the server are unchanged by this review.

## D. Tests added/modified

`tests/test_launch_intent_and_pwa.py` (new, 18 tests):

* **Launch intent (6):** the advertised switch is dispatched to the executable through the
  real `activate_application`; a browser-installed app never receives a browser switch;
  a launch mode without an executable fails closed; unsupported intents never dispatch
  (`force_new_window`, `force_new_instance`); the real backend's argument rules.
* **Observation facts (3):** a visible window can be unfocused; foreground and process are
  reported separately; a minimized window is not claimed as open.
* **Browser-installed apps (6):** shortcut arguments are discovered as a distinct entry
  point; two entry points of one executable stay distinct; the same entry point in two
  Start Menu roots deduplicates; PWA title verification tolerates the site's own dynamic
  title and rejects a different app in the same browser; bounded name matching is not
  fuzzy; the resolver matches a shorter installed label and keeps genuinely ambiguous
  matches ambiguous.
* **Focus input injection (3):** the Alt nudge is injected at most once per interval while
  the ordinary activation still runs; it may inject again after the interval; the ordinary
  strategies inject nothing.

## E. Validation result (Arena, after the fixes)

```
$ .venv/bin/python -m pytest -q tests/
620 passed, 1 skipped, 140 subtests passed in 36.2s

$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python -m ruff format --check .
1 file would be reformatted, 129 files already formatted
  -> test_nvidia_gateway.py  (pre-existing at HEAD, untouched by any of this work)

$ .venv/bin/python -m compileall -q src tests
exit 0
```

Type checking: unchanged from before — the repository configures no type checker
(`pyproject.toml` dev extras are `pytest`, `pytest-asyncio`, `ruff`), so the type gate is
`ruff`'s `E/F/I/UP/B` rules plus `compileall`, both clean.

Diff churn for this review: **163 insertions, 14 deletions across 3 source files**, plus
one new test file — no reformatting of unrelated code, no moved or renamed modules.

## F. Real-Windows tests to run after merging (not claimable from Arena)

```powershell
# 0. Setup
python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"; .\.venv\Scripts\pip install pytest-subtests
.\.venv\Scripts\python -m pytest -q tests

# 1. Multi-application command (regression guard for audit #6)
#    "Open Spotify, calculator, file explorer, settings and camera"
#    Expect: five system.app_launch steps, five independent verifications,
#            one failure must not erase the others' diagnostics.

# 2. Launch intents
#    "Open Chrome"                 -> reuse_existing_if_available: no new process
#    "Open a new Chrome window"    -> force_new_window: NEW window id required
#    "Open another Notepad instance" -> force_new_instance (or LAUNCH_MODE_UNSUPPORTED)
#    "Open a new window" for an app that does not advertise it -> LAUNCH_MODE_UNSUPPORTED
#    Check the diagnostic: launch_intent, mode, activation_argument_count.

# 3. Focus separation (run ARISE from a background console)
#    Chrome already open -> "Open Chrome"
#    Expect: no duplicate process; focus_verified true/false matching reality;
#            focus_strategy recorded; never WINDOW_NOT_FOUND for a live window.
#    Then a locked-foreground case: expect focus_verified=false, launch still succeeds,
#            and NO duplicate instance.

# 4. Packaged applications
#    Calculator, Settings, Photos, Camera -> AUMID/package identity verified,
#    window owned by ApplicationFrameHost still verifies by app identity.

# 5. Browser-installed applications (PWA) - the previously failing case
#    Install a PWA from Edge and from Chrome (e.g. WhatsApp Web), then:
#      a) Confirm the shortcut is discovered:
#         check the catalog entry name, activation_method, is_web_app, shortcut_path
#         and (on the machine) the shortcut target/arguments.
#      b) "Open WhatsApp Web" (and "Open WhatsApp") -> resolves and launches the PWA,
#         verified by executable identity + a window the launch changed.
#      c) Confirm the browser itself is still launched by its own shortcut and is NOT
#         misdetected as a PWA.
#      d) "Open a new window" for the PWA -> LAUNCH_MODE_UNSUPPORTED (by design).

# 6. Long-running wait
#    system.wait_for_condition on a real file/process event: event-driven wake,
#    bounded backoff, and a timeout that never claims completion.

# 7. Voice
#    Real device: wake, barge-in mid-sentence, three-segment response.
#    Expect: all three segments audible, no interleaving, no stale generation.
```

**Arena-verified (this sandbox):** all routing, resolution, discovery, identity,
dispatch, focus-evidence, wait and lifecycle *logic*, with fakes standing in for Win32,
COM, the registry, UIA, PWAs and audio devices; the full suite, lint, format and compile.

**Windows-only pending:** every statement about real Windows behaviour — foreground-lock
refusal and strategy outcomes, real PWA shortcut representation and discovery, real
launch/reuse/new-window semantics per application, ApplicationFrameHost identity for
packaged apps, DOM/CDP access to the user's browser (still a gap, audit `#7`), audio
device behaviour, and all timing/performance characteristics.

## G. Regressions found by real-Windows CI after the PR was opened (fixed)

Section B's "no regressions" claim held for the Arena gate. The first real-Windows
execution of the suite (PR #14, `windows-latest` × Python 3.11 and 3.12) found one
failure that Linux cannot produce, and it was **a product defect, not a flaky test**:

```
tests/test_production_desktop_path.py::test_preexisting_application_window_is_not_evidence_of_a_new_launch
  Failed: DID NOT RAISE ComputerAdapterError
```

### Root cause

`WindowsApplicationResolver._rank_fallback_matches` gave an explicitly registered alias
priority over OS path fallbacks (`90` vs `80`) by testing **object identity**
(`item is custom`). The review-stage change that advertises `--new-window` support from
generic OS metadata rebuilds the fallback descriptors through
`advertise_launch_capabilities`, so after that rebuild no descriptor is the original
object: the alias silently fell back to `80`, tied with the `known_alias_path` /
`registry_app_paths` / `PATH` candidates, and deduplication kept the OS-path descriptor
— which carries the alias defaults (`allow_reuse=True`).

Consequence on a real Windows host: an explicitly registered application's configuration
(`allow_reuse`, launch arguments, web-app entry point) was discarded whenever the host
registers a browser (i.e. on essentially every Windows machine), and a request that must
spawn a new instance reused the pre-existing window instead. Invisible on Linux, where
the registered-browser list is empty and the rebuild is skipped.

Reproduced deterministically by emulating the host metadata (environment variables, App
Paths, `PATH`, registered-browser list) — verified across four host states, before and
after the fix.

### Fix

`src/arise/adapters/windows_app_launch.py` (+23/-8): the registered-alias flag is
captured **before** capability advertising and carried through the rebuild as a
`(descriptor, is_registered_alias)` pair, so the alias keeps score `90` and wins against
OS path fallbacks on every host. Canonical identity was deliberately *not* used as the
marker: a path fallback can resolve to the very same executable, which would tie again.
Generic capability advertising is unchanged — the resolved application still advertises
`new_window_supported` from OS metadata.

### Tests added (2, in `tests/test_production_desktop_path.py`)

* `test_registered_alias_keeps_its_configuration_when_host_registers_a_browser` — the
  alias' `allow_reuse=False` survives on a host that registers the browser, and the
  generic `--new-window` capability still applies.
* `test_preexisting_window_is_not_reused_when_host_registers_a_browser` — full
  launch/verify contract (no reuse, spawn attempted, `ACTION_VERIFICATION_FAILED`) with
  the host pinned; this is the CI failure reproduced on demand.

Both **fail without the fix** (the second with the exact CI message, `DID NOT RAISE`)
and pass with it. A new `_emulate_browser_host()` helper pins the OS metadata the
resolver reads, so no test in this file depends on whether Chrome is installed on the
machine running the suite. These are fakes: they do not exercise Win32, COM, PowerShell
or the registry.

### Validation (Arena, after the fix)

```
$ .venv/bin/python -m pytest -q
622 passed, 1 skipped, 140 subtests passed in 42.65s

$ .venv/bin/python -m ruff check src/arise tests scripts
All checks passed!
$ .venv/bin/python -m ruff format --check src/arise tests scripts
109 files already formatted
$ .venv/bin/python -m compileall -q src tests scripts
exit 0
```

**Arena-verified:** the resolution logic, the ranking/dedup precedence, the two new
regression tests, and that they fail without the fix.

**Windows-only pending:** that real Windows metadata (registered-browser list, App Paths,
Start Menu) produces the same descriptor set this emulation assumes. The Windows CI run
of the suite remains the only real-Windows evidence, and it is the gate for this fix.
