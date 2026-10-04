# Planner/model contract and plan-rejection diagnostics

This document describes how ARISE asks a configured model for a task plan, how
that output is validated, and how an operator can see exactly why a plan was
rejected. It documents existing Phase 1–5 authority boundaries; nothing here
gives a model authority to execute.

## Authority chain (unchanged)

```
user request ──► IntentClassifier (advisory hint only)
             ──► TaskEngine admission (typed task record + trusted AuthorizationContext)
             ──► TaskPlanner.create_plan
                     └─ model proposes exactly one JSON object
             ──► TaskPlan.model_validate (strict typed contract)
             ──► TaskEngine structural validation (identity, limits, dependencies)
             ──► PolicyEngine (capability delegation, risk, confirmation)
             ──► AgentRuntime (parameters, resources, observations, verification)
```

A model response is a *proposal*. It never carries authority: task identity,
goal, planner attribution, approvals, capabilities, evidence, and verification
results are supplied by trusted runtime code.

## Planner request

* `GatewayTaskPlanner` sends one system message (the hardened contract), the
  user's request, and — when enabled and consented — clearly-labelled untrusted
  context messages (`UNTRUSTED_SAVED_MEMORY_CONTEXT_JSON:`,
  `UNTRUSTED_USER_PERSONALIZATION_JSON:`,
  `UNTRUSTED_MATCHED_PROCEDURAL_WORKFLOW_JSON:`,
  `UNTRUSTED_EXTERNAL_RESEARCH_JSON:`, `UNTRUSTED_DETERMINISTIC_INTENT_HINT_JSON:`).
* The system message contains: 16 mandatory output rules, the exact current
  TaskPlan/PlanStep/ActionProposal schema (including the enum spellings read
  from the live contracts), the registered tools with their accepted parameter
  keys, target scope, minimum risk, idempotency, and side effects, and a
  minimal, contract-valid JSON example built from a real registered tool.
* `ModelRequest.response_format="json_object"` is a provider-neutral request
  hint. The OpenAI-compatible adapter maps it to
  `response_format={"type":"json_object"}` only when the provider is configured
  to support it. If the endpoint rejects the field with a client error, the
  adapter degrades once for that provider instance and otherwise leaves the
  request unchanged.

## Response handling (strict, bounded)

1. Surrounding whitespace and a single code fence that wraps the *entire*
   response may be unwrapped as transport formatting.
2. The text must parse as one JSON object. No JSON is searched for inside prose,
   repaired, completed, or synthesized.
3. Runtime-assigned top-level keys (`task_id`, `goal`, `planner_id`, `plan_id`)
   are replaced with trusted values. No other key is removed, and no field is
   invented, so unknown keys, invalid enums, invalid dependencies, and empty
   step lists without clarification remain rejections.
4. `TaskPlan.model_validate` is the authority. Invalid output raises
   `InvalidPlan` with a bounded `PlanFailureCategory`
   (`response_not_json`, `response_not_object`, `schema_invalid`, …).
5. At most one corrective retry is attempted, only for malformed JSON or
   schema-invalid planner output (`ARISE__MODEL__PLANNER_MAX_ATTEMPTS`,
   default 2, maximum 2). The retry appends a deterministic correction
   instruction that names the sanitized category and rejected schema paths — it
   never echoes model text. The original system prompt, user request, and
   untrusted context messages are preserved unchanged. Provider failures,
   timeouts, cancellations, and execution failures are never retried here.

## Rejection diagnostics

* The task's user-facing status reason stays short and now names the failing
  category, for example: `The generated plan was rejected (InvalidPlan:schema_invalid).`
* The audit journal receives a `PLAN_REJECTED` event with the bounded category,
  the sanitized field paths and rule types (for example
  `steps.0.action.risk: enum; reasoning: extra_forbidden`), the attempt count,
  and the task id. Model text, prompts, and credentials are never included.
* `GET /api/v1/diagnostics/planner` (authenticated) returns the sanitized
  snapshot of the most recent plan negotiation: accepted/category/detail,
  attempts, response byte count, transport normalization, provider and model
  ids, and the contract version.
* Engine-side structural rejections use the same mechanism with their own
  categories (`dependency_cycle`, `unknown_dependency`, `step_self_dependency`,
  `duplicate_action_id`, `step_limit_exceeded`, `task_mismatch`,
  `goal_mismatch`, `missing_authority`, `untyped_plan`).

## Verifying a real provider

```bash
python scripts/live_nvidia_planner_check.py --request "Open Chrome"
```

The script composes the real application with `create_app(get_settings())`,
serves it on loopback, posts the request through `/api/v1/interactions`, polls
the real task, prints the sanitized planner diagnostic, and reports one of:

* `PLANNER VERIFIED` — a TaskPlan was accepted by the TaskEngine;
* `PLANNER REJECTED` — the model output stayed invalid (with its category);
* `ENVIRONMENT-BLOCKED` — no provider, credentials, or reachable endpoint.

It never prints credentials, Authorization headers, prompts, or model output.

## Known execution limits (not model-contract issues)

* The production composition registers `uia.*` and (when enabled) `browser.*`
  tools, all of which require a grounded window/page target that the planner
  cannot invent; the contract tells the model to ask one clarification question
  instead of fabricating identifiers.
* No registered tool launches an application, so a request such as
  "Open Chrome" cannot be planned as an executable step in the current
  composition.
* The server composition delegates no tool capabilities to task authority
  (`TaskEngine(capability_grants=...)` is not configured), so even a fully
  valid executable plan is policy-denied with
  `required capability was not delegated: …`. This is a composition/authority
  boundary, not a planner defect; granting capabilities is an explicit operator
  decision that this repository does not make implicitly.
