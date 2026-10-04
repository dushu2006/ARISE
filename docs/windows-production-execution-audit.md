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
| `system.app_launch` | R1 | `AppLaunchTool` / `WindowsAppLaunchProvider` | `Win32AppLaunchBackend`, resolver, `Popen(shell=False)`, psutil, shared Win32 window backend | Fresh exact-executable process + visible window + declared conditions; real launch unverified |
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
