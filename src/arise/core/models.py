"""Versioned Pydantic domain/API-boundary contracts for Phase 1.

Sensitive authority is intentionally absent from ActionProposal: trusted request
context is injected by the task engine when the proposal becomes an ActionContract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    ConditionOperator,
    Idempotency,
    RiskLevel,
    TargetIdentity,
    validate_safe_token,
)
from arise.core.tasks import StepStatus, TaskStatus, VerificationStatus


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_version: int = Field(default=1, ge=1)


class RequestSource(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    API = "api"
    SCHEDULE = "schedule"


class VoiceState(StrEnum):
    DORMANT = "dormant"
    ACTIVATING = "activating"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    EXECUTING = "executing"
    WAITING_FOR_USER = "waiting_for_user"
    DEACTIVATING = "deactivating"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class MicrophoneStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class VoiceProviderStatus(StrEnum):
    UNCONFIGURED = "unconfigured"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    AUTHENTICATION_FAILURE = "authentication_failure"
    QUOTA_LIMITED = "quota_limited"
    NETWORK_FAILURE = "network_failure"
    PROVIDER_ERROR = "provider_error"


class VoiceMetricSnapshot(ContractModel):
    count: int = Field(default=0, ge=0)
    last_latency_ms: float | None = Field(default=None, ge=0)
    max_latency_ms: float | None = Field(default=None, ge=0)


class VoiceStatusSnapshot(ContractModel):
    """Safe operator snapshot; never contains transcript, audio, or credentials."""

    state: VoiceState = VoiceState.DORMANT
    microphone_status: MicrophoneStatus = MicrophoneStatus.NOT_CONFIGURED
    provider_status: VoiceProviderStatus = VoiceProviderStatus.UNCONFIGURED
    provider_id: str | None = Field(default=None, max_length=128)
    wake_word: str = Field(default="ARISE", min_length=1, max_length=32)
    wake_word_enabled: bool = False
    active_session_id: str | None = Field(default=None, max_length=128)
    active_task_id: str | None = Field(default=None, max_length=128)
    inactivity_timeout_seconds: int = Field(default=30, ge=5, le=3600)
    last_error_code: str | None = Field(default=None, max_length=64)
    updated_at: datetime = Field(default_factory=_utc_now)
    telemetry: dict[str, VoiceMetricSnapshot] = Field(default_factory=dict)


class UserRequest(ContractModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str = Field(min_length=1, max_length=16_384)
    source: RequestSource = RequestSource.TEXT
    received_at: datetime = Field(default_factory=_utc_now)
    locale: str | None = Field(default=None, max_length=32)
    allow_web_research: bool = False

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request text cannot be blank")
        return value

    @field_validator("request_id", "session_id")
    @classmethod
    def validate_request_ids(cls, value: str) -> str:
        validate_safe_token(value, "request/session identifier")
        return value


class TargetModel(ContractModel):
    platform: str = Field(min_length=1, max_length=64)
    application: str | None = Field(default=None, max_length=256)
    process_id: int | None = Field(default=None, gt=0)
    window_id: str | None = None
    browser_profile: str | None = None
    page_id: str | None = None
    account: str | None = None
    workspace: str | None = None
    container: str | None = None
    object_id: str | None = None
    role: str | None = None
    semantic_name: str | None = None
    stable_id: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    display_id: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def to_domain(self) -> TargetIdentity:
        return TargetIdentity(
            platform=self.platform,
            application=self.application,
            process_id=self.process_id,
            window_id=self.window_id,
            browser_profile=self.browser_profile,
            page_id=self.page_id,
            account=self.account,
            workspace=self.workspace,
            container=self.container,
            object_id=self.object_id,
            role=self.role,
            semantic_name=self.semantic_name,
            stable_id=self.stable_id,
            locator=self.locator,
            display_id=self.display_id,
            bounds=self.bounds,
            confidence=self.confidence,
        )


class ConditionModel(ContractModel):
    key: str = Field(min_length=1, max_length=128)
    operator: ConditionOperator = ConditionOperator.EQUALS
    expected: Any = None
    description: str = Field(default="", max_length=512)

    def to_domain(self) -> Condition:
        return Condition(
            key=self.key,
            operator=self.operator,
            expected=self.expected,
            description=self.description,
        )


class ActionProposal(ContractModel):
    """Model/planner proposal. User authority is attached outside this object."""

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str = Field(min_length=1, max_length=128)
    target: TargetModel | None = None
    risk: RiskLevel
    parameters: dict[str, Any] = Field(default_factory=dict)
    preconditions: tuple[ConditionModel, ...] = ()
    postconditions: tuple[ConditionModel, ...] = ()
    required_resources: tuple[str, ...] = ()
    idempotency: Idempotency = Idempotency.UNKNOWN
    idempotency_key: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    rollback_strategy: str | None = Field(default=None, max_length=512)
    verification_strategy: str = Field(default="observed_postconditions", max_length=128)

    def to_domain(self, *, task_id: str, authority: AuthorizationContext) -> ActionContract:
        return ActionContract(
            task_id=task_id,
            action_id=self.action_id,
            tool_name=self.tool_name,
            target=self.target.to_domain() if self.target is not None else None,
            risk=self.risk,
            authority=authority,
            parameters=self.parameters,
            preconditions=tuple(item.to_domain() for item in self.preconditions),
            postconditions=tuple(item.to_domain() for item in self.postconditions),
            required_resources=self.required_resources,
            idempotency=self.idempotency,
            idempotency_key=self.idempotency_key,
            timeout_seconds=self.timeout_seconds,
            rollback_strategy=self.rollback_strategy,
            verification_strategy=self.verification_strategy,
            schema_version=self.schema_version,
        )


class StepRetryPolicy(ContractModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    retry_on_blocked: bool = False


class StepFallbackPolicy(ContractModel):
    strategy: Literal["none", "fallback_action", "reground", "abort"] = "none"
    fallback_action: ActionProposal | None = None
    reason: str = Field(default="", max_length=512)


class PlanStep(ContractModel):
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=1024)
    action: ActionProposal
    depends_on: tuple[str, ...] = ()
    condition: ConditionModel | None = None
    skip_when_condition_false: bool = True
    parallel_safe: bool = False
    verification_checkpoint: bool = True
    retry_policy: StepRetryPolicy = Field(default_factory=StepRetryPolicy)
    fallback_policy: StepFallbackPolicy = Field(default_factory=StepFallbackPolicy)

    @model_validator(mode="after")
    def populate_default_description(self) -> PlanStep:
        if not self.description.strip():
            object.__setattr__(self, "description", self.title)
        return self

    @property
    def tool_name(self) -> str:
        return self.action.tool_name

    @property
    def target(self) -> TargetModel | None:
        return self.action.target

    @property
    def parameters(self) -> dict[str, Any]:
        return self.action.parameters

    @property
    def preconditions(self) -> tuple[ConditionModel, ...]:
        return self.action.preconditions

    @property
    def expected_postconditions(self) -> tuple[ConditionModel, ...]:
        return self.action.postconditions

    @property
    def verification_method(self) -> str:
        return self.action.verification_strategy

    @property
    def risk_level(self) -> RiskLevel:
        return self.action.risk

    @property
    def required_resources(self) -> tuple[str, ...]:
        return self.action.required_resources

    @property
    def timeout(self) -> float:
        return self.action.timeout_seconds


class TaskPlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    goal: str = Field(min_length=1, max_length=16_384)
    steps: tuple[PlanStep, ...] = Field(max_length=256)
    created_at: datetime = Field(default_factory=_utc_now)
    planner_id: str = Field(min_length=1, max_length=128)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_clarification_state(self) -> TaskPlan:
        if not self.steps and not self.needs_clarification:
            raise ValueError("a plan must include steps or request clarification")
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("clarification plans need a user-facing question")
        if self.needs_clarification and self.steps:
            raise ValueError("clarification plans cannot contain executable steps")
        return self

    @field_validator("steps")
    @classmethod
    def validate_unique_steps(cls, steps: tuple[PlanStep, ...]) -> tuple[PlanStep, ...]:
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step IDs must be unique")
        step_ids = set(ids)
        for step in steps:
            unknown = set(step.depends_on) - step_ids
            if unknown:
                raise ValueError(f"step {step.step_id} has unknown dependencies")
        return steps


class TaskContext(ContractModel):
    task_id: str
    user_intent_id: str
    session_id: str
    relevant_memory_ids: tuple[str, ...] = ()
    environment_snapshot_id: str | None = None
    current_step_id: str | None = None
    summary: str = Field(default="", max_length=8192)


class ActionResult(ContractModel):
    action_id: str
    status: Literal["succeeded", "failed", "unknown", "blocked"]
    summary: str = Field(max_length=4096)
    verification_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class VerificationResultModel(ContractModel):
    verification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    status: Literal["passed", "failed", "unknown"]
    level: int = Field(default=0, ge=0, le=5)
    evidence_ids: tuple[str, ...] = ()
    summary: str = Field(max_length=4096)
    verified_at: datetime = Field(default_factory=_utc_now)


class EnvironmentFact(ContractModel):
    key: str
    value: Any = None
    source: Literal["observed", "retrieved", "inferred", "assumed", "unknown"]
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime = Field(default_factory=_utc_now)


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    REQUIRES_CONFIGURATION = "requires_configuration"


class DisplayInfo(ContractModel):
    display_id: str = Field(min_length=1, max_length=256)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    scale: float | None = Field(default=None, gt=0)
    dpi_x: int | None = Field(default=None, gt=0)
    dpi_y: int | None = Field(default=None, gt=0)
    primary: bool | None = None
    availability: CapabilityStatus | None = None


class ActiveWindowInfo(ContractModel):
    available: bool
    title: str | None = Field(default=None, max_length=2048)
    application: str | None = Field(default=None, max_length=256)
    process_id: int | None = Field(default=None, gt=0)
    window_id: str | None = Field(default=None, max_length=256)
    reason_unavailable: str | None = Field(default=None, max_length=128)


class ApplicationInfo(ContractModel):
    name: str = Field(min_length=1, max_length=256)
    process_id: int | None = Field(default=None, gt=0)
    source: Literal["running_process", "installed_registry", "user_provided"] = "running_process"
    available: bool = True


class EnvironmentSnapshot(ContractModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=_utc_now)
    operating_system: str
    os_version: str
    architecture: str
    cpu_count: int = Field(ge=1)
    total_memory_bytes: int | None = Field(default=None, ge=0)
    gpu_names: tuple[str, ...] = Field(default=(), max_length=32)
    displays: tuple[DisplayInfo, ...] = Field(default=(), max_length=32)
    active_window: ActiveWindowInfo | None = None
    running_applications: tuple[ApplicationInfo, ...] = Field(default=(), max_length=512)
    installed_applications: tuple[ApplicationInfo, ...] = Field(default=(), max_length=512)
    browsers: tuple[str, ...] = Field(default=(), max_length=64)
    terminals: tuple[str, ...] = Field(default=(), max_length=64)
    audio_input_devices: tuple[str, ...] = Field(default=(), max_length=64)
    audio_output_devices: tuple[str, ...] = Field(default=(), max_length=64)
    network_status: Literal["online", "offline", "unknown"] = "unknown"
    unavailable_fields: tuple[str, ...] = Field(default=(), max_length=128)


class ModelRole(StrEnum):
    PLANNER = "planner"
    FAST_REASONER = "fast_reasoner"
    DEEP_REASONER = "deep_reasoner"
    VISION = "vision"
    OCR = "ocr"
    ASR = "asr"
    TTS = "tts"
    EMBEDDING = "embedding"
    SAFETY = "safety"


class ModelMessage(ContractModel):
    role: Literal["system", "user", "assistant"]
    content: str | tuple[dict[str, Any], ...]


class ModelRequest(ContractModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    session_id: str | None = None
    role: ModelRole
    messages: tuple[ModelMessage, ...]
    model_id: str | None = None
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    stream: bool = True
    required_modalities: frozenset[str] = frozenset({"text"})


class ModelResponse(ContractModel):
    request_id: str
    provider_id: str
    model_id: str
    content: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_ms: float = Field(ge=0)


class ModelStreamChunk(ContractModel):
    request_id: str
    provider_id: str
    model_id: str
    sequence: int = Field(ge=0)
    text_delta: str = ""
    is_final: bool = False
    finish_reason: str | None = None


class ModelSelectionRequest(ContractModel):
    role: ModelRole
    task_type: str
    complexity: Literal["low", "medium", "high"] = "medium"
    latency_budget_ms: int = Field(default=10_000, ge=1, le=600_000)
    context_tokens: int = Field(default=4096, ge=1, le=2_000_000)
    required_modalities: frozenset[str] = frozenset({"text"})
    preferred_provider: str | None = None
    privacy: Literal["local_only", "balanced", "cloud_allowed"] = "balanced"


class ModelSelection(ContractModel):
    provider_id: str
    model_id: str
    reason: str
    fallbacks: tuple[tuple[str, str], ...] = ()


class ErrorInfoModel(ContractModel):
    error_code: str
    category: str
    message: str
    retryable: bool
    severity: str
    component: str
    operation: str | None = None
    cause: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProgressUpdate(ContractModel):
    task_id: str
    step_id: str | None = None
    stage: str
    message: str
    percent: float | None = Field(default=None, ge=0, le=100)
    timestamp: datetime = Field(default_factory=_utc_now)
    source: str


class TaskEvent(ContractModel):
    event_id: str
    event_type: str
    task_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class SystemEvent(TaskEvent):
    component: str


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HealthSnapshot(ContractModel):
    status: HealthStatus
    checked_at: datetime = Field(default_factory=_utc_now)
    app_name: str
    app_version: str
    uptime_seconds: float = Field(ge=0)
    database_status: CapabilityStatus
    model_status: CapabilityStatus
    active_tasks: int = Field(ge=0)
    queued_tasks: int = Field(ge=0)
    degraded_reasons: tuple[str, ...] = ()


class ProviderStatus(ContractModel):
    provider_id: str
    status: CapabilityStatus
    latency_ms: float | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None
    model_ids: tuple[str, ...] = ()


class Capability(ContractModel):
    name: str
    version: str
    status: CapabilityStatus
    availability: Literal[
        "available",
        "degraded",
        "unavailable",
        "disabled",
        "requires_configuration",
        "deferred",
    ]
    health: HealthStatus
    requirements: tuple[str, ...] = ()
    adapter: str | None = None
    limitations: tuple[str, ...] = ()


class DiagnosticsSnapshot(ContractModel):
    checked_at: datetime = Field(default_factory=_utc_now)
    environment: EnvironmentSnapshot
    capabilities: tuple[Capability, ...]
    providers: tuple[ProviderStatus, ...] = ()
    database_schema_version: int = Field(ge=1)
    database_path_kind: Literal["default", "configured"]


class ConfirmationRequest(ContractModel):
    confirmation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    action_id: str
    risk: RiskLevel
    target_summary: str = Field(max_length=256)
    action_summary: str = Field(max_length=512)
    expires_at: datetime
    contract_fingerprint: str


class Artifact(ContractModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_type: str
    name: str
    location: str
    created_at: datetime = Field(default_factory=_utc_now)
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationTurn(ContractModel):
    turn_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    speaker: Literal["user", "assistant", "system"]
    text: str = Field(max_length=16_384)
    created_at: datetime = Field(default_factory=_utc_now)
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(ContractModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    principal_id: str = "local-user"
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    locale: str = "en"
    turns: tuple[ConversationTurn, ...] = ()


class TaskStepSnapshot(ContractModel):
    action_id: str
    contract_fingerprint: str
    tool_name: str
    risk: int = Field(ge=0, le=4)
    status: StepStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    verification_status: VerificationStatus | None = None
    status_reason: str | None = None


class TaskSnapshot(ContractModel):
    task_id: str
    request_id: str
    session_id: str
    parent_task_id: str | None = None
    correlation_id: str
    goal: str
    state: TaskStatus
    created_at: datetime
    updated_at: datetime
    status_reason: str | None = None
    steps: tuple[TaskStepSnapshot, ...] = ()
    version: int = Field(ge=0)

    @classmethod
    def from_record(cls, record: Any) -> TaskSnapshot:
        return cls(
            task_id=record.task_id,
            request_id=record.request_id,
            session_id=record.session_id,
            parent_task_id=record.parent_task_id,
            correlation_id=record.correlation_id,
            goal=record.goal,
            state=record.status.value,
            created_at=record.created_at,
            updated_at=record.updated_at,
            status_reason=record.status_reason,
            steps=tuple(TaskStepSnapshot.model_validate(step.to_dict()) for step in record.steps),
            version=record.version,
        )


class TaskDetail(ContractModel):
    task: TaskSnapshot
    confirmations: tuple[ConfirmationRequest, ...] = ()
    accepts_user_input: bool = False
