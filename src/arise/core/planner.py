"""Typed task planning over the model router; proposals carry no authority.

The model is asked for exactly one JSON object that matches the current TaskPlan
contract, and every response is still parsed and validated strictly. A single
bounded corrective retry is allowed for malformed or schema-invalid output; a
plan that cannot be validated remains an ``InvalidPlan`` with a sanitized,
auditable failure category.
"""

from __future__ import annotations

import json

from arise.core.contracts import RiskLevel
from arise.core.errors import CapabilityUnavailableError, ProviderUnavailableError
from arise.core.extensions import ContextQuery, MemoryPort, ResearchQuery, WebResearchPort
from arise.core.intent import IntentClassifier
from arise.core.model_gateway import ModelRouter
from arise.core.models import (
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelSelectionRequest,
    PlannerDiagnostic,
    TaskPlan,
    UserRequest,
)
from arise.core.personalization import PersonalizationStore, ProceduralMemoryStore
from arise.core.planner_contract import (
    build_system_prompt,
    correction_instruction,
    describe_validation_failure,
    normalize_json_transport,
)
from arise.core.planning import InvalidPlan, PlanFailureCategory, PlannerUnavailable, TaskPlanner
from arise.core.ports import ToolRegistry
from arise.core.tasks import TaskRecord

# Identifies the prompt/response contract revision for audit records.
PLANNER_CONTRACT_VERSION = "planner-contract-3"

MAX_PLANNER_ATTEMPTS = 2


class GatewayTaskPlanner(TaskPlanner):
    """Ask an explicitly configured model for JSON, then strictly validate it."""

    def __init__(
        self,
        router: ModelRouter,
        tools: ToolRegistry,
        *,
        model_id: str | None = None,
        privacy: str = "local_only",
        max_output_tokens: int = 4096,
        timeout_seconds: float = 90.0,
        max_attempts: int = MAX_PLANNER_ATTEMPTS,
        memory: MemoryPort | None = None,
        research: WebResearchPort | None = None,
        personalization: PersonalizationStore | None = None,
        procedural_memory: ProceduralMemoryStore | None = None,
        allow_memory_context_to_cloud: bool = False,
    ) -> None:
        if privacy not in {"local_only", "balanced", "cloud_allowed"}:
            raise ValueError("invalid model privacy mode")
        if not 1 <= max_attempts <= MAX_PLANNER_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_PLANNER_ATTEMPTS}")
        self.router = router
        self.tools = tools
        self.model_id = model_id
        self.privacy = privacy
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.memory = memory
        self.research = research
        self.personalization = personalization
        self.procedural_memory = procedural_memory
        self.allow_memory_context_to_cloud = allow_memory_context_to_cloud
        self._last_diagnostic = PlannerDiagnostic(contract_version=PLANNER_CONTRACT_VERSION)

    def last_diagnostic(self) -> PlannerDiagnostic:
        """Return the sanitized outcome of the most recent planning attempt."""

        return self._last_diagnostic

    async def _collect_context_messages(
        self, request: UserRequest, task: TaskRecord
    ) -> tuple[list[ModelMessage], frozenset[str]]:
        """Return untrusted context messages and the providers that served them."""

        messages: list[ModelMessage] = []
        context_sources: set[str] = set()
        if (
            self.memory is not None
            and task.authorization is not None
            and (self.privacy == "local_only" or self.allow_memory_context_to_cloud)
        ):
            try:
                memory_context = await self.memory.retrieve(
                    ContextQuery(
                        query=request.text,
                        principal_id=task.authorization.principal_id,
                        session_id=task.session_id,
                        task_id=task.task_id,
                        limit=5,
                    )
                )
            except Exception:
                # Optional memory must not prevent a task from being planned.
                memory_context = ()
            if memory_context:
                context_sources.add("memory")
                serialized_memory = json.dumps(
                    [
                        {
                            "source_id": item.source_id,
                            "provenance": item.provenance,
                            "text": item.text[:1200],
                        }
                        for item in memory_context[:5]
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                messages.append(
                    ModelMessage(
                        role="user",
                        content="UNTRUSTED_SAVED_MEMORY_CONTEXT_JSON: " + serialized_memory,
                    )
                )
        if (
            self.personalization is not None
            and task.authorization is not None
            and (self.privacy == "local_only" or self.allow_memory_context_to_cloud)
        ):
            try:
                profile = self.personalization.get_profile(
                    principal_id=task.authorization.principal_id
                )
                if (
                    profile.preferred_browser
                    or profile.preferred_apps
                    or profile.preferred_response_style != "balanced"
                ):
                    context_sources.add("personalization")
                    messages.append(
                        ModelMessage(
                            role="user",
                            content="UNTRUSTED_USER_PERSONALIZATION_JSON: "
                            + json.dumps(
                                profile.to_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    )
            except Exception:
                pass
        if (
            self.procedural_memory is not None
            and task.authorization is not None
            and (self.privacy == "local_only" or self.allow_memory_context_to_cloud)
        ):
            try:
                matched_wf = self.procedural_memory.match_workflow(
                    principal_id=task.authorization.principal_id,
                    request_text=request.text,
                    require_approved=True,
                )
                if matched_wf is not None:
                    context_sources.add("procedural_memory")
                    messages.append(
                        ModelMessage(
                            role="user",
                            content="UNTRUSTED_MATCHED_PROCEDURAL_WORKFLOW_JSON: "
                            + json.dumps(
                                matched_wf.to_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    )
            except Exception:
                pass
        return messages, frozenset(context_sources)

    async def _collect_research_messages(self, request: UserRequest) -> list[ModelMessage]:
        """Return untrusted research context, failing closed when opted in."""

        if not request.allow_web_research:
            return []
        if self.research is None:
            raise PlannerUnavailable(
                "Web research was requested but no opted-in research provider is available."
            )
        try:
            research_context = await self.research.search(
                ResearchQuery(query=request.text, max_results=8)
            )
        except Exception as exc:
            raise PlannerUnavailable(
                "Requested web research is unavailable; no plan has been produced."
            ) from exc
        if not research_context:
            raise PlannerUnavailable(
                "No usable web sources were returned; no plan has been produced."
            )
        serialized_research = json.dumps(
            [
                {
                    "source_id": item.source_id,
                    "provenance": item.provenance,
                    "text": item.text[:2000],
                }
                for item in research_context[:8]
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return [
            ModelMessage(
                role="user",
                content="UNTRUSTED_EXTERNAL_RESEARCH_JSON: " + serialized_research,
            )
        ]

    @staticmethod
    def _intent_hint_message(request: UserRequest) -> ModelMessage | None:
        classification = IntentClassifier().classify(request.text)
        command = classification.structured_command
        if command is None:
            return None
        serialized_intent = json.dumps(
            {
                "kind": classification.kind.value,
                "confidence": classification.confidence,
                "steps": [
                    {"operation": step.operation, "target_text": step.target_text}
                    for step in command.steps
                ],
                "entities": [
                    {
                        "kind": entity.kind,
                        "value": entity.value,
                        "start": entity.start,
                        "end": entity.end,
                    }
                    for entity in command.entities
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ModelMessage(
            role="user",
            content="UNTRUSTED_DETERMINISTIC_INTENT_HINT_JSON: " + serialized_intent,
        )

    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan:
        specs = self.tools.list_specs()
        if not specs:
            raise PlannerUnavailable("No execution tools are registered.")
        system = build_system_prompt(specs)
        messages = [
            ModelMessage(role="system", content=system),
            ModelMessage(role="user", content=request.text),
        ]
        context_messages, context_sources = await self._collect_context_messages(request, task)
        messages.extend(context_messages)
        messages.extend(await self._collect_research_messages(request))
        intent_hint = self._intent_hint_message(request)
        if intent_hint is not None:
            messages.append(intent_hint)

        selection = ModelSelectionRequest(
            role=ModelRole.PLANNER,
            task_type="desktop_task_planning",
            complexity="medium",
            required_modalities=frozenset({"text"}),
            privacy=self.privacy,
        )
        last_error: InvalidPlan | None = None
        attempt_categories: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            attempt_messages = tuple(messages)
            if last_error is not None:
                # Deterministic correction instruction; the original system
                # prompt, the user request, and every untrusted context message
                # are preserved unchanged.
                attempt_messages = (
                    *attempt_messages,
                    ModelMessage(
                        role="user",
                        content=correction_instruction(
                            last_error.category.value, last_error.detail
                        ),
                    ),
                )
            model_request = ModelRequest(
                task_id=task.task_id,
                session_id=task.session_id,
                correlation_id=task.correlation_id,
                role=ModelRole.PLANNER,
                model_id=self.model_id,
                messages=attempt_messages,
                max_output_tokens=self.max_output_tokens,
                timeout_seconds=self.timeout_seconds,
                stream=False,
                response_format="json_object",
            )
            try:
                response = await self.router.complete(model_request, selection=selection)
            except (CapabilityUnavailableError, ProviderUnavailableError) as exc:
                raise PlannerUnavailable(
                    "No eligible planning provider completed the request."
                ) from exc
            normalized_text, transport_note = normalize_json_transport(response.content)
            try:
                plan = self._validate_response(
                    normalized_text,
                    task,
                    raw_content=response.content,
                    provider_id=response.provider_id,
                    model_id=response.model_id,
                )
            except InvalidPlan as exc:
                last_error = InvalidPlan(
                    str(exc),
                    category=exc.category,
                    detail=exc.detail,
                    attempts=attempt,
                )
                attempt_categories.append(exc.category.value)
                self._record_diagnostic(
                    task=task,
                    accepted=False,
                    category=exc.category.value,
                    detail=exc.detail,
                    attempts=attempt,
                    attempt_categories=tuple(attempt_categories),
                    response_bytes=len(response.content),
                    transport_note=transport_note,
                    provider_id=response.provider_id,
                    model_id=response.model_id,
                    context_sources=context_sources,
                )
                if attempt >= self.max_attempts:
                    break
                continue
            attempt_categories.append("accepted")
            self._record_diagnostic(
                task=task,
                accepted=True,
                category="accepted",
                detail="",
                attempts=attempt,
                attempt_categories=tuple(attempt_categories),
                response_bytes=len(response.content),
                transport_note=transport_note,
                provider_id=response.provider_id,
                model_id=response.model_id,
                context_sources=context_sources,
            )
            return plan
        assert last_error is not None
        raise last_error

    def _validate_response(
        self,
        text: str,
        task: TaskRecord,
        *,
        raw_content: str,
        provider_id: str,
        model_id: str,
    ) -> TaskPlan:
        """Parse and strictly validate one model response; no repair is attempted."""

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            detail = describe_validation_failure(exc)
            if "```" in raw_content:
                detail = f"{detail} markdown_fence_detected".strip()
            raise InvalidPlan(
                "planner response was not valid JSON",
                category=PlanFailureCategory.RESPONSE_NOT_JSON,
                detail=detail,
            ) from exc
        if not isinstance(payload, dict):
            raise InvalidPlan(
                "planner response must be a JSON object",
                category=PlanFailureCategory.RESPONSE_NOT_OBJECT,
                detail=f"json_root_type={type(payload).__name__}",
            )
        # The runtime assigns task identity, goal and planner attribution. Those
        # values cannot be supplied by model output. No other key is removed and
        # no field is invented, so unknown or invalid model output still fails
        # the typed contract below.
        payload = dict(payload)
        payload.pop("task_id", None)
        payload.pop("goal", None)
        payload.pop("planner_id", None)
        payload.pop("plan_id", None)
        payload["task_id"] = task.task_id
        payload["goal"] = task.goal
        payload["planner_id"] = f"{provider_id}/{model_id}"
        try:
            plan = TaskPlan.model_validate(payload)
        except Exception as exc:
            raise InvalidPlan(
                "planner proposal did not satisfy the typed plan contract",
                category=PlanFailureCategory.SCHEMA_INVALID,
                detail=describe_validation_failure(exc),
            ) from exc
        self._validate_consequential_postconditions(plan)
        return plan

    def _validate_consequential_postconditions(self, plan: TaskPlan) -> None:
        """Reject R2+ proposals that omit the policy-required verification contract.

        Trusted tool metadata supplies the risk floor. This is an early planner
        validation only; PolicyEngine remains the final gate and the runtime
        still verifies every declared condition against fresh observations.
        """

        minimum_risk_by_tool = {spec.name: spec.minimum_risk for spec in self.tools.list_specs()}
        for step_index, step in enumerate(plan.steps):
            proposals = [("action", step.action)]
            fallback = step.fallback_policy.fallback_action
            if fallback is not None:
                proposals.append(("fallback_policy.fallback_action", fallback))
            for field_name, proposal in proposals:
                tool_minimum = minimum_risk_by_tool.get(proposal.tool_name, RiskLevel.R0)
                effective_risk = max(int(proposal.risk), int(tool_minimum))
                if effective_risk >= RiskLevel.R2 and not proposal.postconditions:
                    raise InvalidPlan(
                        "consequential planner actions require explicit postconditions",
                        category=PlanFailureCategory.SCHEMA_INVALID,
                        detail=f"steps.{step_index}.{field_name}.postconditions: missing",
                    )

    def _record_diagnostic(
        self,
        *,
        task: TaskRecord,
        category: str,
        detail: str,
        attempts: int,
        attempt_categories: tuple[str, ...],
        response_bytes: int,
        transport_note: str,
        provider_id: str,
        model_id: str,
        accepted: bool,
        context_sources: frozenset[str] = frozenset(),
    ) -> None:
        """Record a sanitized plan negotiation outcome for operators and audits."""

        self._last_diagnostic = PlannerDiagnostic(
            task_id=task.task_id,
            accepted=accepted,
            category=category,
            detail=detail,
            attempts=attempts,
            attempt_categories=attempt_categories,
            response_bytes=response_bytes,
            transport_normalization=transport_note,
            provider_id=provider_id,
            model_id=model_id,
            contract_version=PLANNER_CONTRACT_VERSION,
            context_sources=tuple(sorted(context_sources)),
        )
