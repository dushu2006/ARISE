# Windows production execution audit — 2026-10-04

## Evidence boundary

This change was developed on **Linux, Python 3.11.2** in Arena. No action in this work was exercised on the user's Windows 11 / Python 3.14 machine. Native Windows execution, Chrome resolution on that machine, NVIDIA responses, desktop visibility, and omnibox accessibility remain unverified.

The supplied Windows output does not contain the rejected plan or the underlying domain exception message. It is **not possible to identify the historical Test 2 field exactly from that output**. The defects below are source-traced and reproduced with explicitly fake backends; they are not a reconstruction of a retained Windows trace.

## 1. Application launch: success could mean an existing background process

Production composition already registered `AppLaunchTool -> WindowsAppLaunchProvider -> Win32AppLaunchBackend` when `desktop.enabled` was true. It did not select a simulator. The native backend uses `subprocess.Popen([resolved_executable], ...)`, psutil, and the shared Win32 window backend. `shell=False` is now explicit; no shell command or arguments are constructed.

The original provider:

- Selected the first process whose **basename** matched an alias.
- Returned that process as a successful reuse even with **zero windows**.
- Suppressed window-enumeration and focus errors.
- For a new process, stopped polling after two liveness checks, without requiring a window.
- Independently re-observed processes, but allowed the verifier to pass on process presence alone.

Thus the old verification was not entirely invented, but it could prove the wrong fact: an already-running Chrome background process, not a visible Chrome application. This is a concrete explanation consistent with Test 1, not proof of which branch ran on the laptop.

### Changed behavior

The native backend declares a visible-window requirement. Native matching uses the resolved executable's normalized full Windows path, not an arbitrary same-name executable. Window observations must belong to a matching process. All matching PIDs are considered, rather than just Chrome's first process.

- Reuse requires an observed visible window; focus errors are not swallowed.
- An existing background process without a window no longer prevents launch dispatch.
- New launches poll for a visible, non-minimized window within the existing bounded timeout.
- Fresh verification requires both the resolved process and a visible, non-minimized window, plus every declared postcondition.
- Missing window-observation backends raise `ADAPTER_UNAVAILABLE` rather than silently returning an empty inventory.
- Observation errors have a bounded category in the diagnostic facts.
- Native subprocess launch explicitly refuses a non-Windows host.

Process-only alternate/fake backends retain their existing behavior unless they declare `requires_visible_window`; the actual production Win32 backend always declares it. Unit tests explicitly enable the native evidence requirement on a fake to test it.

Resolution remains the existing hierarchy: registered aliases, standard Windows paths, App Paths registry, uninstall registry entries, then PATH. Chrome's actual selected path and process/window behavior still need the user's Windows run. Exact-path matching intentionally fails closed when executable identity cannot be read; packaged apps, launchers, and executable handoffs may need separate identity support. This change does not substitute a basename match when access is denied.

## 2. ComputerAdapterError: target grounding, not a missing DI object

`ComputerAdapterError` is the exception in `src/arise/core/computer_ports.py`. There is no resource named `ComputerAdapter` to instantiate or register.

The reported message originates in `AgentRuntime` while calling `WindowsUiaActionTool.resources_for(action)`. That method raises `ComputerAdapterError(INVALID_TARGET)` when the target is absent, its platform is not `windows`, or its `window_id` is missing. The old runtime hid the category and retained only the exception class name.

`ResourceManager` is a lock/lease manager, not a provider dependency container. UIA tools already receive their provider through their constructors during production startup. Their derived locks are:

```
desktop.window.<observed window_id>
desktop.focus
desktop.input
```

The missing operation was **grounding a semantic proposal into an observed identity before deriving these locks**. The new trusted tool hook enumerates windows, respects explicit window/process constraints, matches the requested application (including existing app aliases), requires exactly one matching visible window, and uses the existing semantic resolver. It never invents an HWND or chooses an arbitrary foreground application.

Exercising the previously unused semantic branch found another defect: `reground_stale_target` supplied `automation_id` to `TargetQuery`, which does not have that field. It now explicitly filters stable IDs and uses the supported query fields.

The runtime binds the observed identity before policy approval. A planned step's original fingerprint must match before it can be refined. The engine retains the bound action for confirmation and continuation, so an approval is scoped to the observed identity, not the original ungrounded proposal. A changed/missing stable identity cannot reuse approval. Later leases, fresh observations, foreground checks, human-interference checks, and verification remain in place.

Unavailable adapters, ambiguous windows, absent controls, and invalid targets block with their actual bounded adapter category. No input occurs during grounding.

## 3. Planner acceptance versus domain conversion

Previously, `GatewayTaskPlanner` accepted `TaskPlan.model_validate(...)` plus the consequential-postcondition check. TaskEngine subsequently called `ActionProposal.to_domain()`, which enforces additional domain constraints.

Reproduced examples of the mismatch:

| Pydantic proposal | Domain rejection |
| --- | --- |
| `action_id` containing spaces | `action_id must be a bounded identifier` |
| Resource name containing spaces | `resource name must be a bounded identifier` |
| Blank `idempotency_key` | `idempotency_key cannot be blank` |
| `exists` condition with `expected: true` | `EXISTS conditions do not take an expected value` |

These examples are **not claims about the exact lost Windows payload**.

Before reporting acceptance, the planner now invokes the **same `ActionProposal.to_domain()` method** as execution, for primary and fallback actions, and converts step conditions too. A rejection contains an indexed path and constraint, for example:

```
steps.0.action: postconditions.0: EXISTS conditions do not take an expected value
```

Contract diagnostics omit arbitrary JSON dictionary keys/values. TaskEngine also retains the sanitized domain constraint in its rejection event instead of dropping it. Prompt guidance now spells out these constraints. The diagnostic revision is `planner-contract-4`; the existing maximum of two attempts is unchanged. Corrections receive the constraint, not only an opaque exception name.

Acceptance is not policy approval or proof of resource/desktop availability. Runtime policy, target freshness, engine contextual admission, and required verification remain independent gates.

## 4. Actual execution hierarchy and Chrome limitation

```
POST /api/v1/interactions
  -> configured model router / GatewayTaskPlanner
  -> TaskPlan -> ActionProposal.to_domain() -> ActionContract
  -> TaskEngine -> AgentRuntime
  -> trusted semantic grounding (observation only)
  -> PolicyEngine / exact-action confirmation where required
  -> ResourceManager exclusive leases
  -> CompositeEnvironment fresh observation
  -> registered tool -> provider -> native backend
  -> fresh provider observation / CompositeVerifier
  -> verified completion, or truthful blocked/failed/unknown result
```

For UIA actions, the observed target fingerprint and observation lease must still agree before dispatch. Keyboard-focus facts are obtained from actual observed control focus, not inferred from a successful click call.

**The existing `Win32UiaBackend` is HWND-based. It does not walk Microsoft's full COM UI Automation accessibility tree.** It uses native Win32 window/control discovery, including child HWND enumeration and `GetGUIThreadInfo` for focus. Chrome's omnibox must not be assumed to have its own HWND. This patch does not claim or add full COM UIA omnibox support. If semantic resolution cannot see the control, it reports that HWND observation limitation and blocks.

The existing perception/browser hierarchy was not used to silently retarget the action. Playwright DOM tools do not represent Chrome's browser-chrome omnibox, and a screenshot or coordinate alone cannot prove the required focused-element fact. No coordinate opt-in, confirmation, or postcondition was bypassed to make the example appear to succeed.

## 5. Per-tool production composition

All rows below are **REAL PRODUCTION PATH by source/composition inspection**, and **ENVIRONMENT-BLOCKED for native execution in Arena**. None meets a claim of Windows production readiness based on this work alone. Providers are not simulated at normal production startup; fake dependencies are injected only by tests.

| Tool | Minimum risk | Registered implementation / provider | Native dependency / dispatch | Verification and limitations |
| --- | --- | --- | --- | --- |
| `system.app_launch` | R1 | `AppLaunchTool` / `WindowsAppLaunchProvider` | `Win32AppLaunchBackend`, resolver, `Popen(shell=False)`, psutil, shared Win32 window backend | Bounded pre-dispatch baseline plus fresh executable-verified new/activated window evidence, then declared conditions; see section 8; real launch unverified |
| `uia.invoke` | R2 | `WindowsUiaActionTool` / `WindowsUiaProvider` | `Win32UiaBackend.invoke_node`, `BM_CLICK`; existing explicitly opted-in coordinate fallback | Fresh observed postconditions; HWND controls only |
| `uia.click` | R3 | Same tool/provider, click operation | Same invoke backend, grounded candidate required | R3 confirmation unchanged; click return does not prove keyboard focus |
| `uia.fill` | R2 | Same tool/provider, fill operation | `set_node_value`, `WM_SETTEXT` | Fresh observed non-sensitive value conditions; HWND controls only |
| `uia.fill_secret` | R3 | Same tool/provider plus configured `SecretProvider` | Secret reference resolution and same native value setter | Existing secret redaction and verification limitations remain; no secret values in new diagnostics |
| `uia.focus` | R1 | Same tool/provider, focus operation | Foreground operation and Win32 focus message | Foreground/focus notifications are not proof of actual keyboard focus; fresh focus observation required |
| `uia.press` | R3 | Same tool/provider, press operation | Bounded existing Win32 key-message backend | Existing supported key set only; declared effect must be observed |

Startup with `desktop.enabled` registers all seven tools. UIA tools share one provider/backend; native app launch uses the same window backend. `CompositeEnvironment` and `CompositeVerifier` route to those same providers. `AgentRuntime` owns a real `ResourceManager` for exclusive locks. No missing singleton adapter was papered over with a fake.

Host capability discovery now probes access to an input desktop and a foreground window rather than merely testing `sys.platform == "win32"`. The capability text discloses the HWND-only limitation. A positive probe means the desktop is accessible at that instant, not that every action or accessibility control works.

## 6. One-command Windows diagnosis

On the user's Windows checkout, with the existing real NVIDIA configuration:

```powershell
python scripts/live_nvidia_planner_check.py --diagnostics --request "Open Chrome, click the address bar, and type example.com"
```

The script does **not** grant approval. If a grounded R3 action requires confirmation, that remains a user decision. An unavailable omnibox may block before confirmation because no trustworthy target exists.

The polling loop also waits for the plan worker to settle: a transient `partially_completed` between steps no longer causes premature backend shutdown. A waiting approval is reported without approving it.

Normal output stays concise. `--diagnostics` adds bounded metadata for provider/backend selection, tool risk floors, target fingerprint/window/process identity, postcondition keys/operators/types (not sensitive expected values), resource waiting/acquisition, dispatch status, observation IDs/state hashes, verification evidence references, and recent process/visible-window observations. Launch metadata separately identifies resolution, reuse versus spawn, the dispatched PID, and a hashed executable identity; it is explicitly not verification evidence. It does not dump parameters, model output, executable paths, UI text, secret references, or credentials. Generic completion now says **backend reports completed**, not **execution verified**.

Required external environment:

- `ARISE__DESKTOP__ENABLED=true` if desktop tools are not already enabled.
- Installed Chrome resolvable through the existing Windows resolution hierarchy.
- ARISE running as the interactive desktop user, on an accessible/unlocked desktop, with permissions to inspect the target process. Windows foreground/UIPI restrictions are not bypassed.
- Existing NVIDIA provider/model/credentials configuration. No new API credentials or optional UIA package were added.
- Normal user approval for consequential actions. Do not disable policy to run the diagnostic.

## 7. Validation and status

Validation in Arena:

- Targeted launch/UIA/planner/runtime/new-regression tests: **115 passed, 23 subtests passed**.
- Full Python suite: **432 passed, 115 subtests passed**.
- One existing FastAPI/Starlette test-client deprecation warning; no test failures.
- Ruff check, formatting of changed code, compileall, and `git diff --check`: passed. Unrelated existing formatting drift in the large native/server files was preserved.
- Existing test files were not rewritten. New regressions are in `tests/test_production_desktop_path.py` and explicitly use fake/mocked dependencies.
- Ran the live script with desktop registration enabled in Arena: all seven tools selected native provider/backend classes, then stopped **ENVIRONMENT-BLOCKED** because no model provider was configured. This was composition inspection, not NVIDIA or Windows execution.

| Evidence category | Status |
| --- | --- |
| REAL WINDOWS VERIFIED | **None from this change**; user's laptop is the final integration environment |
| SIMULATED/FAKE | New regressions and existing Python tests; source/composition tests include mocked native calls |
| REPLAY | No retained real Windows payload/trace was replayed |
| ENVIRONMENT-BLOCKED | Arena is Linux; no interactive Windows desktop or configured NVIDIA provider |
| EXTERNAL CONFIGURATION | Windows interactive session, Chrome installation, NVIDIA configuration, normal approvals |

Still unresolved/unverified: the exact historical Test 2 object/field; actual Windows 11/Python 3.14 behavior; actual Chrome path/PIDs/windows; omnibox accessibility; focus/click/type effects; secret-entry native behavior; packaged executable handoffs. The new diagnostic is intended to make the next Windows run specific rather than falsely successful.

No commit or push was performed.

## 8. Chromium-style app launch: window-owner association (follow-up, 2026-10-04)

### Evidence boundary

This follow-up was developed on **Linux, Python 3.11.2** in Arena with explicitly fake
backends. **No real Windows run was performed for this change**, and nothing here is a
claim of Windows verification. The Window/Chrome behavior below is source-traced and
reproduced against the fake backends; the user's laptop remains the integration
environment.

### Reported observation

With a configured NVIDIA provider the planner accepted `Open Chrome` on attempt 1, the task
was created, `system.app_launch` dispatched, and the task ended `UNKNOWN`:

```
The tool failed after dispatch (WINDOW_NOT_FOUND); the external effect is unknown.
```

### Root cause (reproduced with fake backends on the previous revision)

1. **Association was anchored on the spawned PID.** `launch_process()` returns the
   `subprocess.Popen` PID. A Chromium launcher can exit immediately after handing the
   request to an already running browser process, and the visible top-level window can be
   owned by a *different* process of the same installation. The previous loop either adopted
   an arbitrary same-executable PID as the "launched process" or timed out, so a
   pre-existing Chrome process/window could be mistaken for — or block — this launch.
2. **No pre-dispatch baseline existed.** Nothing recorded which application processes and
   visible windows already existed, so the code could not tell a pre-existing Chrome window
   from one produced by this dispatch.
3. **Window ownership had no per-window executable identity.** `Win32UiaBackend` recorded
   only the owner PID and the process *name* on each `WindowRecord`; `WindowRecord.executable_path`
   was always `None`. The only executable-identity check available was a second lookup keyed
   by PID.
4. **`focus_window` failures escaped the launch path.** Both the reuse branch and the spawn
   branch called `focus_window` and let its `WINDOW_NOT_FOUND` (raised when
   `SetForegroundWindow` does not actually change the foreground window) propagate. That
   exception is exactly what `AgentRuntime` reports as
   `tool failed after dispatch (WINDOW_NOT_FOUND)`. On a background sidecar this is the
   normal Windows outcome for an unfocused Chrome window, so a *successful* hand-off was
   reported as an unknown external effect.
5. **The reuse branch trusted the focus implementation's own return record** instead of an
   independent fresh observation.

### Implemented association algorithm (`WindowsAppLaunchProvider`)

1. **Resolve** the executable as before (unchanged hierarchy; `shell=False` unchanged).
2. **Baseline before dispatch** (`_snapshot_desktop`): one bounded read-only snapshot of all
   processes (PID → observed executable path) and all *visible top-level* windows. Process
   count is capped at 4096; the visible-window inventory is capped at 128, at which point the
   native backend truncates its own enumeration, so the launch fails closed with
   `ENVIRONMENT_CHANGED` rather than risk treating a pre-existing window as new. The
   application-scoped baseline is derived with the *normalized observed executable path*
   (`ntpath.normcase(ntpath.normpath(path))`); unknown identities never match.
3. **Idempotent reuse first.** A pre-existing window is reusable only if its owner identity is
   exe-verified and its selection is deterministic (foreground → restored → larger → lowest
   window id). The focus attempt is always issued and its failure is recorded, never
   swallowed; reuse is accepted only when a *fresh* snapshot (not the focus return value)
   shows that window visible, restored and foreground. Otherwise the provider does not
   pretend the launch happened — it dispatches the executable, which is the platform-supported
   way for Chrome's single-instance logic to activate its own browser process/window.
4. **Dispatch** the resolved executable with `shell=False` and the existing executable
   resolution; record `dispatched_process_id` for diagnosis only.
5. **Poll for dispatch-explained windows** (`_poll_window_evidence`), each cycle building a
   fresh process+window snapshot and classifying every visible, non-minimized window whose
   owner identity is executable-verified:
   - `new_window` — the window id did not exist in the bounded baseline;
   - `activated_existing_window` — a pre-existing application window is foreground *now* and
     was not foreground (or was minimized) in the baseline.
   Unchanged pre-existing windows (and any window whose owner executable cannot be observed,
   or whose window-record and process-snapshot identities conflict) are never evidence. The
   selected window must be observed twice consecutively; simultaneous candidates are ordered
   deterministically (new before activated, foreground, larger area, lowest window id).
6. **Confirm by fresh observation** (`_confirm_window_evidence`): focus the selected window,
   then re-snapshot. The focus call's return record is never evidence; a focus failure is
   recorded in the bounded diagnostic (`focus_failed`, `focus_error`) and cannot be turned
   into a foreground claim. A newly created window must still be visible, restored and
   exe-verified; an activated pre-existing window must additionally still be foreground.
   Windows that fail confirmation are excluded and polling continues until the bounded
   deadline, after which the launch fails closed (`ACTION_UNKNOWN_OUTCOME`), as does an early
   exit that leaves no identifiable application process (`ACTION_UNKNOWN_OUTCOME`) or an empty
   poll window (`TIMEOUT`).
7. **Return the observed window owner.** `RunningApplication.process_id` is the PID that owns
   the verified window (which may differ from the launcher PID); `window_ids` is the confirmed
   window. `verify()` re-observes and still requires an executable-verified application process
   *and* a visible, non-minimized application window plus every declared postcondition.

`Win32UiaBackend._window_record_from_hwnd` now records the HWND owner's observed executable
path (`psutil.Process(pid).exe()`, bounded, `None` on access failure) on the window record.
`WindowsUiaProvider.list_windows()` continues to expose sanitized records with
`executable_path=None`, so the exe path stays inside the adapter layer.

### Unchanged safety model

Risk levels, `PolicyEngine`, planner contracts, postconditions, confirmation behavior, UIA
tools and their gates, `shell=False`, and executable resolution are untouched. Verification
was not weakened to process-only success: a running Chrome process without a visible window
still fails verification, and a same-name executable at a different path is still rejected.
Alternate backends that do not declare `requires_visible_window` keep their historical
process-only behavior.

### Regression tests (fake backends, deterministic)

Added/updated in `tests/test_production_desktop_path.py` with a Chromium-like
`MultiProcessLaunchBackend` (launcher PID ≠ window owner PID, launcher hand-off, focus
outcomes, foreground transitions):

| Scenario | Expectation |
| --- | --- |
| Launcher exits after hand-off; browser process owns the new window | success anchored on the browser-window PID |
| Launcher stays alive; a child process of the same executable owns the window | success; owner PID ≠ dispatched PID |
| Window owner has the same process name but a different executable path | fail closed |
| Visible window appears with no observable owner identity | fail closed (`ACTION_UNKNOWN_OUTCOME`) |
| Window-record and process-snapshot owner identities conflict | fail closed (`TIMEOUT`) |
| Pre-existing Chrome window plus an unrelated foreground window | fail closed; the pre-existing window is not evidence and is not focused |
| Pre-existing visible Chrome window, focus confirmed fresh | reuse, no dispatch (idempotent) |
| Focus reports success but foreground never changes; hand-off then activates the window | success only as `activated_existing_window` from a fresh observation |
| Process running, no visible window | launch `TIMEOUT`; `verify()` `FAILED` (process-only success rejected) |
| Focus raises while a new window exists | success on the new-window evidence; `focus_failed` recorded, `focus_verified` false |
| Reuse focus raises and no fresh evidence appears | fail closed |
| Window disappears before confirmation | fail closed (`TIMEOUT`) |
| Several new windows | deterministic selection (foreground, larger) |

Running the new test module against the previous source tree produces 13 failures: 10 of the
13 scenarios above, the updated `test_reuse_focus_failure_is_not_swallowed`, and both
parametrized variants of the runtime regression. Those include the exact user-visible
signature — the reuse path raises `ComputerAdapterError(WINDOW_NOT_FOUND)` out of
`launch_application`, and the runtime reports `StepStatus.UNKNOWN`
(`The tool failed after dispatch (WINDOW_NOT_FOUND)`) — and the opposite defect, where the
focus implementation's own return record is accepted as a false success without any dispatch.
Three scenarios pass on both revisions by design because they are fail-closed guard tests: the
impostor executable, the window with no observable owner, and the running process with no
visible window. Suite totals for this change are recorded in the report; `ruff check`,
`compileall`, `arise demo`, and `git diff --check` pass, and pre-existing `ruff format` drift
in unrelated alias/tool regions was preserved.

### Still required on the user's Windows machine

```
python scripts/live_nvidia_planner_check.py --diagnostics --request "Open Chrome"
```

The diagnostic now also prints the bounded baseline counts, `window_evidence`
(`new_window` / `activated_existing_window`), `window_owner_process_id`, `focus_failed` /
`focus_error` (confirmation attempt), `reuse_focus_error` (unconfirmed reuse activation), and
`focus_verified` — none of which is verification evidence by itself. Real Windows
verification of Chrome cold starts, hand-off to an existing browser process, foreground
restoration, and multi-process window ownership is **not claimed here**.
