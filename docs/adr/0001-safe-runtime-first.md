# ADR-0001: Establish a safe, provider-neutral runtime before desktop integrations

- **Status:** Accepted
- **Date:** 2026-10-02

## Context

The repository began as an empty project, while the ARISE product specification describes many subsystems: a desktop shell, voice, model routing, memory, Windows/browser automation, policy, verification, recovery, and packaging. Implementing a broad but untestable mock would create the appearance of capability without real grounding or safety.

The most consequential invariant is that a model does not own authority or directly control the operating system. Before adding models or GUI adapters, ARISE needs a stable action contract and a testable control path.

## Decision

Start with a standard-library Python core and narrow ports:

- `ActionContract`, `TargetIdentity`, and deterministic conditions;
- `TaskRecord` state machine with explicit `UNKNOWN`;
- centralized `PolicyEngine` and exact-scope expiring approvals;
- `ResourceManager` with atomic/cancellable leases;
- `EnvironmentPort`, `ActionTool`, and independent `VerifierPort`;
- append-only event and task repository contracts;
- in-memory simulator and SQLite local adapters.

Do not add Tauri/React, provider SDKs, FastAPI, Windows APIs, or browser frameworks until their adapter boundaries and integration needs can be tested. The in-memory tools are only test/demo fixtures, not a production substitute.

## Consequences

### Positive

- Safety/state semantics can be exercised without a live desktop or cloud credential.
- A tool timeout after dispatch becomes `UNKNOWN`, preventing duplicate side effects.
- Risk floors come from registered tool metadata, so a planner cannot label a purchase/send/delete operation as harmless.
- Domain contracts remain independent of the eventual OS/browser/model framework.
- No runtime dependency is needed for the first testable slice.

### Trade-offs

- There is no user-facing desktop app or natural-language experience yet.
- SQLite and the in-process resource manager are local foundations, not distributed services.
- Task records intentionally avoid persisting raw action parameters, so durable action rehydration requires a later protected action journal or safe replan flow.
- A standard-library verifier only proves facts supplied by its environment adapter; Windows and provider evidence are not present.

## Revisit when

- The local gateway and frontend need versioned API/event contracts.
- Windows UI Automation or browser protocol adapters are introduced.
- Authenticated approval needs to cross a process boundary.
- A production task-recovery requirement needs encrypted, redacted action persistence.
