"""Working memory, short-term memory, procedural workflow reuse, and personalization.

All data managed here is principal-scoped and untrusted with respect to execution authority.
Saved procedural workflows and user preferences never bypass the ARISE PolicyEngine,
ResourceManager, AgentRuntime, or independent VerifierPort.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from arise.adapters.sqlite import SQLiteDatabase
from arise.core.contracts import utc_now, validate_safe_token
from arise.core.extensions import EmbeddingPort, EmbeddingResult, validate_memory_write_governance
from arise.core.models import (
    ActionProposal,
    ConversationTurn,
    PlanStep,
    TargetModel,
    TaskPlan,
)
from arise.core.redaction import DEFAULT_REDACTOR, SecretRedactor

_WORD_RE = re.compile(r"[a-z0-9]{2,}")


@dataclass(frozen=True, slots=True)
class WorkingMemorySnapshot:
    """Bounded per-task working memory state with an optional absolute expiry."""

    task_id: str
    principal_id: str
    goal: str
    current_step_id: str | None = None
    observations: Mapping[str, Any] = field(default_factory=dict)
    scratchpad: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.task_id, "working memory task_id")
        validate_safe_token(self.principal_id, "working memory principal_id")
        if not self.goal.strip() or len(self.goal) > 16_384:
            raise ValueError("working memory goal must be non-empty and bounded")
        if len(self.scratchpad) > 64:
            raise ValueError("working memory scratchpad cannot exceed 64 notes")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("working memory expires_at must be timezone-aware")

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class WorkingMemoryStore:
    """Bounded in-memory store for active task working contexts.

    Entries are bounded twice: by LRU capacity (`max_tasks`) and by absolute expiry
    (`default_ttl_seconds` unless a caller supplies `expires_at`). Expired snapshots are
    dropped on read and by explicit purge, so a stale task context cannot be re-served.
    """

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        default_ttl_seconds: float = 1800.0,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
    ) -> None:
        self.max_tasks = max(1, max_tasks)
        self.default_ttl_seconds = max(1.0, float(default_ttl_seconds))
        self.redactor = redactor
        self._entries: OrderedDict[str, WorkingMemorySnapshot] = OrderedDict()

    def upsert(
        self,
        *,
        task_id: str,
        principal_id: str,
        goal: str,
        current_step_id: str | None = None,
        observations: Mapping[str, Any] | None = None,
        note: str | None = None,
        expires_at: datetime | None = None,
    ) -> WorkingMemorySnapshot:
        now = utc_now()
        existing = self._entries.get(task_id)
        if existing is not None and existing.is_expired(now):
            self._entries.pop(task_id, None)
            existing = None
        obs = dict(existing.observations) if existing is not None else {}
        if observations:
            obs.update(self.redactor.redact_object(dict(observations)))
        notes = list(existing.scratchpad) if existing is not None else []
        if note and note.strip():
            notes.append(self.redactor.redact(note.strip())[:1024])
            notes = notes[-64:]
        snapshot = WorkingMemorySnapshot(
            task_id=task_id,
            principal_id=principal_id,
            goal=self.redactor.redact(goal),
            current_step_id=current_step_id
            if current_step_id is not None
            else (existing.current_step_id if existing else None),
            observations=obs,
            scratchpad=tuple(notes),
            updated_at=now,
            expires_at=(
                expires_at
                if expires_at is not None
                else now + timedelta(seconds=self.default_ttl_seconds)
            ),
        )
        self._entries[task_id] = snapshot
        self._entries.move_to_end(task_id)
        while len(self._entries) > self.max_tasks:
            self._entries.popitem(last=False)
        return snapshot

    def get(self, task_id: str, *, principal_id: str) -> WorkingMemorySnapshot | None:
        entry = self._entries.get(task_id)
        if entry is None or entry.principal_id != principal_id:
            return None
        if entry.is_expired(utc_now()):
            self._entries.pop(task_id, None)
            return None
        return entry

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Drop every snapshot past its expiry and return how many were removed."""

        checked = now or utc_now()
        stale = [task_id for task_id, snap in self._entries.items() if snap.is_expired(checked)]
        for task_id in stale:
            self._entries.pop(task_id, None)
        return len(stale)

    def clear_task(self, task_id: str, *, principal_id: str) -> bool:
        entry = self._entries.get(task_id)
        if entry is None or entry.principal_id != principal_id:
            return False
        self._entries.pop(task_id, None)
        return True


class ShortTermConversationMemory:
    """Sliding-window short-term conversational memory per session."""

    def __init__(
        self,
        *,
        max_sessions: int = 64,
        max_turns_per_session: int = 24,
        max_chars_per_turn: int = 4000,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
    ) -> None:
        self.max_sessions = max(1, max_sessions)
        self.max_turns_per_session = max(1, max_turns_per_session)
        self.max_chars_per_turn = max(1, max_chars_per_turn)
        self.redactor = redactor
        self._sessions: OrderedDict[tuple[str, str], list[ConversationTurn]] = OrderedDict()

    def append_turn(self, *, principal_id: str, turn: ConversationTurn) -> ConversationTurn:
        validate_safe_token(principal_id, "short-term memory principal_id")
        text = self.redactor.redact(turn.text)
        if len(text) > self.max_chars_per_turn:
            text = text[: self.max_chars_per_turn]
        safe_turn = turn.model_copy(
            update={
                "text": text,
                "metadata": self.redactor.redact_object(turn.metadata),
            }
        )
        key = (principal_id, safe_turn.session_id)
        turns = self._sessions.setdefault(key, [])
        turns.append(safe_turn)
        if len(turns) > self.max_turns_per_session:
            del turns[: len(turns) - self.max_turns_per_session]
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        return safe_turn

    def recent_turns(
        self,
        *,
        principal_id: str,
        session_id: str,
        limit: int = 12,
    ) -> tuple[ConversationTurn, ...]:
        turns = self._sessions.get((principal_id, session_id), [])
        return tuple(turns[-max(1, limit) :])

    def clear_session(self, *, principal_id: str, session_id: str) -> bool:
        return self._sessions.pop((principal_id, session_id), None) is not None


class LocalDeterministicEmbeddingAdapter(EmbeddingPort):
    """Offline feature-hashed L2-normalized embedding adapter for semantic memory ranking."""

    def __init__(self, *, dimensions: int = 64, model_id: str = "arise-local-hash-v1") -> None:
        if not 8 <= dimensions <= 1024:
            raise ValueError("dimensions must be between 8 and 1024")
        self.dimensions = dimensions
        self.model_id = model_id

    async def embed(self, text: str, *, correlation_id: str) -> EmbeddingResult:
        del correlation_id
        cleaned = (text or "").casefold().strip()
        tokens = _WORD_RE.findall(cleaned)
        features = list(tokens)
        # Add character 3-grams for morphological resilience
        compact = re.sub(r"\s+", " ", cleaned)
        for i in range(max(0, len(compact) - 2)):
            features.append(compact[i : i + 3])
        if not features:
            features = ["empty"]
        vec = [0.0] * self.dimensions
        for feat in features:
            digest = hashlib.sha256(feat.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 1e-12:
            vec[0] = 1.0
            norm = 1.0
        normalized = tuple(round(v / norm, 6) for v in vec)
        return EmbeddingResult(model_id=self.model_id, vector=normalized)


@dataclass(frozen=True, slots=True)
class ProceduralWorkflow:
    """Reusable, user-inspectable procedural playbook with provenance."""

    workflow_id: str
    principal_id: str
    name: str
    description: str
    goal_pattern: str
    steps: tuple[PlanStep, ...]
    provenance_task_id: str | None = None
    approved_by_user: bool = False
    execution_count: int = 0
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_verified_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.workflow_id, "workflow_id")
        validate_safe_token(self.principal_id, "workflow principal_id")
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("workflow name must be non-empty and bounded")
        if not self.description.strip() or len(self.description) > 2048:
            raise ValueError("workflow description must be non-empty and bounded")
        if not self.goal_pattern.strip() or len(self.goal_pattern) > 2048:
            raise ValueError("workflow goal_pattern must be non-empty and bounded")
        if not self.steps or len(self.steps) > 64:
            raise ValueError("workflow must contain between 1 and 64 steps")
        if self.provenance_task_id is not None:
            validate_safe_token(self.provenance_task_id, "provenance_task_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "principal_id": self.principal_id,
            "name": self.name,
            "description": self.description,
            "goal_pattern": self.goal_pattern,
            "steps": [step.model_dump(mode="json") for step in self.steps],
            "provenance_task_id": self.provenance_task_id,
            "approved_by_user": self.approved_by_user,
            "execution_count": self.execution_count,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProceduralWorkflow:
        raw_steps = payload.get("steps") or ()
        return cls(
            workflow_id=str(payload["workflow_id"]),
            principal_id=str(payload["principal_id"]),
            name=str(payload["name"]),
            description=str(payload["description"]),
            goal_pattern=str(payload["goal_pattern"]),
            steps=tuple(PlanStep.model_validate(step) for step in raw_steps),
            provenance_task_id=(
                str(payload["provenance_task_id"])
                if payload.get("provenance_task_id") is not None
                else None
            ),
            approved_by_user=bool(payload.get("approved_by_user", False)),
            execution_count=int(payload.get("execution_count", 0)),
            version=int(payload.get("version", 1)),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            last_verified_at=(
                datetime.fromisoformat(str(payload["last_verified_at"]))
                if payload.get("last_verified_at")
                else None
            ),
        )


class ProceduralMemoryStore:
    """SQLite-backed store for reusable procedural workflows with stale-step detection."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        redactor: SecretRedactor = DEFAULT_REDACTOR,
    ) -> None:
        self.database = database
        self.redactor = redactor
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS procedural_workflows ("
                "workflow_id TEXT PRIMARY KEY, "
                "principal_id TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "goal_pattern TEXT NOT NULL, "
                "approved_by_user INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL, "
                "payload_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflows_principal_updated "
                "ON procedural_workflows(principal_id, updated_at DESC)"
            )

    def save_workflow(
        self,
        *,
        principal_id: str,
        name: str,
        description: str,
        goal_pattern: str,
        steps: Sequence[PlanStep],
        provenance_task_id: str | None = None,
        approved_by_user: bool = False,
        workflow_id: str | None = None,
    ) -> ProceduralWorkflow:
        safe_name = self.redactor.redact(name).strip()
        safe_desc = self.redactor.redact(description).strip()
        safe_pattern = self.redactor.redact(goal_pattern).strip()
        validate_memory_write_governance(
            f"{safe_name}: {safe_desc}", raw_text=f"{name}: {description}"
        )
        now = utc_now()
        wf = ProceduralWorkflow(
            workflow_id=workflow_id or f"wf-{uuid.uuid4().hex[:12]}",
            principal_id=principal_id,
            name=safe_name,
            description=safe_desc,
            goal_pattern=safe_pattern,
            steps=tuple(steps),
            provenance_task_id=provenance_task_id,
            approved_by_user=approved_by_user,
            execution_count=0,
            version=1,
            created_at=now,
            updated_at=now,
        )
        payload = json.dumps(wf.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO procedural_workflows("
                "workflow_id, principal_id, name, goal_pattern, "
                "approved_by_user, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    wf.workflow_id,
                    wf.principal_id,
                    wf.name,
                    wf.goal_pattern,
                    1 if wf.approved_by_user else 0,
                    wf.updated_at.isoformat(),
                    payload,
                ),
            )
        return wf

    def get_workflow(self, *, principal_id: str, workflow_id: str) -> ProceduralWorkflow | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT payload_json FROM procedural_workflows "
                "WHERE workflow_id = ? AND principal_id = ?",
                (workflow_id, principal_id),
            ).fetchone()
        if row is None:
            return None
        return ProceduralWorkflow.from_dict(json.loads(row["payload_json"]))

    def list_workflows(
        self, *, principal_id: str, limit: int = 100
    ) -> tuple[ProceduralWorkflow, ...]:
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM procedural_workflows "
                "WHERE principal_id = ? ORDER BY updated_at DESC LIMIT ?",
                (principal_id, max(1, min(limit, 500))),
            ).fetchall()
        return tuple(ProceduralWorkflow.from_dict(json.loads(r["payload_json"])) for r in rows)

    def update_workflow(
        self,
        *,
        principal_id: str,
        workflow_id: str,
        name: str | None = None,
        description: str | None = None,
        goal_pattern: str | None = None,
        steps: Sequence[PlanStep] | None = None,
        approved_by_user: bool | None = None,
    ) -> ProceduralWorkflow:
        existing = self.get_workflow(principal_id=principal_id, workflow_id=workflow_id)
        if existing is None:
            raise LookupError("procedural workflow does not exist")
        updated = replace(
            existing,
            name=self.redactor.redact(name).strip() if name is not None else existing.name,
            description=(
                self.redactor.redact(description).strip()
                if description is not None
                else existing.description
            ),
            goal_pattern=(
                self.redactor.redact(goal_pattern).strip()
                if goal_pattern is not None
                else existing.goal_pattern
            ),
            steps=tuple(steps) if steps is not None else existing.steps,
            approved_by_user=(
                approved_by_user if approved_by_user is not None else existing.approved_by_user
            ),
            version=existing.version + 1,
            updated_at=utc_now(),
        )
        validate_memory_write_governance(
            f"{updated.name}: {updated.description}",
            raw_text=f"{name or updated.name}: {description or updated.description}",
        )
        payload = json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE procedural_workflows SET name = ?, goal_pattern = ?, "
                "approved_by_user = ?, updated_at = ?, payload_json = ? "
                "WHERE workflow_id = ? AND principal_id = ?",
                (
                    updated.name,
                    updated.goal_pattern,
                    1 if updated.approved_by_user else 0,
                    updated.updated_at.isoformat(),
                    payload,
                    workflow_id,
                    principal_id,
                ),
            )
        return updated

    def record_execution_verified(
        self, *, principal_id: str, workflow_id: str
    ) -> ProceduralWorkflow | None:
        existing = self.get_workflow(principal_id=principal_id, workflow_id=workflow_id)
        if existing is None:
            return None
        now = utc_now()
        updated = replace(
            existing,
            execution_count=existing.execution_count + 1,
            last_verified_at=now,
            updated_at=now,
        )
        payload = json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE procedural_workflows SET updated_at = ?, payload_json = ? "
                "WHERE workflow_id = ? AND principal_id = ?",
                (now.isoformat(), payload, workflow_id, principal_id),
            )
        return updated

    def delete_workflow(self, *, principal_id: str, workflow_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM procedural_workflows WHERE workflow_id = ? AND principal_id = ?",
                (workflow_id, principal_id),
            )
            return cursor.rowcount == 1

    def match_workflow(
        self,
        *,
        principal_id: str,
        request_text: str,
        require_approved: bool = True,
    ) -> ProceduralWorkflow | None:
        """Find the highest-scoring reusable workflow matching a user request."""

        req_tokens = set(_WORD_RE.findall(request_text.casefold()))
        if not req_tokens:
            return None
        best_score = 0.0
        best_wf: ProceduralWorkflow | None = None
        for wf in self.list_workflows(principal_id=principal_id):
            if require_approved and not wf.approved_by_user:
                continue
            pattern_tokens = set(_WORD_RE.findall(f"{wf.name} {wf.goal_pattern}".casefold()))
            if not pattern_tokens:
                continue
            overlap = len(req_tokens & pattern_tokens) / len(pattern_tokens)
            if wf.goal_pattern.casefold() in request_text.casefold():
                overlap = max(overlap, 0.85)
            if overlap >= 0.5 and overlap > best_score:
                best_score = overlap
                best_wf = wf
        return best_wf

    @staticmethod
    def detect_stale_workflow_steps(
        workflow: ProceduralWorkflow,
        observed_facts: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        """Detect workflow steps whose preconditions or locators no longer match live UI state."""

        stale: list[tuple[str, str]] = []
        for step in workflow.steps:
            for pre in step.preconditions:
                cond = pre.to_domain()
                if cond.key in observed_facts and not cond.evaluate(observed_facts):
                    stale.append(
                        (
                            step.step_id,
                            f"Precondition '{cond.key}' drifted from expected value.",
                        )
                    )
                    break
            else:
                target = step.target
                if target is not None and target.semantic_name and target.role:
                    uia_key = f"uia.element.{target.role}.{target.semantic_name}.enabled"
                    dom_key = f"dom.element.{target.role}.{target.semantic_name}.visible"
                    if uia_key in observed_facts and not observed_facts[uia_key]:
                        stale.append(
                            (
                                step.step_id,
                                f"Target '{target.semantic_name}' is missing or disabled in UIA.",
                            )
                        )
                    elif dom_key in observed_facts and not observed_facts[dom_key]:
                        stale.append(
                            (
                                step.step_id,
                                f"Target '{target.semantic_name}' is not visible in DOM.",
                            )
                        )
        return tuple(stale)

    @staticmethod
    def adapt_workflow_to_task_plan(
        workflow: ProceduralWorkflow,
        *,
        task_id: str,
        goal: str,
        target_overrides: Mapping[str, TargetModel] | None = None,
        parameter_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TaskPlan:
        """Instantiate and re-ground a saved workflow into a fresh TaskPlan proposal.

        Every step retains its verification postconditions and still passes through
        TaskEngine._validate_and_bind_plan, PolicyEngine, ResourceManager, and VerifierPort.
        """

        target_map = dict(target_overrides or {})
        param_map = dict(parameter_overrides or {})
        adapted_steps: list[PlanStep] = []
        for step in workflow.steps:
            action = step.action
            new_target = target_map.get(step.step_id, action.target)
            new_params = dict(action.parameters)
            if step.step_id in param_map:
                new_params.update(param_map[step.step_id])
            adapted_action = ActionProposal(
                action_id=f"{step.step_id}-{uuid.uuid4().hex[:8]}",
                tool_name=action.tool_name,
                target=new_target,
                risk=action.risk,
                parameters=new_params,
                preconditions=action.preconditions,
                postconditions=action.postconditions,
                required_resources=action.required_resources,
                idempotency=action.idempotency,
                timeout_seconds=action.timeout_seconds,
                rollback_strategy=action.rollback_strategy,
                verification_strategy=action.verification_strategy,
            )
            adapted_steps.append(
                step.model_copy(
                    update={
                        "action": adapted_action,
                        "verification_checkpoint": True,
                    }
                )
            )
        return TaskPlan(
            plan_id=f"plan-wf-{uuid.uuid4().hex[:10]}",
            task_id=task_id,
            goal=goal,
            steps=tuple(adapted_steps),
            planner_id=f"procedural/{workflow.workflow_id}@v{workflow.version}",
        )


@dataclass(frozen=True, slots=True)
class PersonalizationProfile:
    """User-controlled preferences; never grants execution authority or bypasses policy."""

    principal_id: str
    preferred_browser: str | None = None
    preferred_apps: Mapping[str, str] = field(default_factory=dict)
    preferred_response_style: Literal["concise", "balanced", "detailed"] = "balanced"
    preferred_tts_voice: str | None = None
    preferred_tts_speed: float = 1.0
    approved_workflows: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_safe_token(self.principal_id, "personalization principal_id")
        if self.preferred_browser is not None and len(self.preferred_browser) > 64:
            raise ValueError("preferred_browser exceeds 64 characters")
        if self.preferred_response_style not in {"concise", "balanced", "detailed"}:
            raise ValueError("preferred_response_style must be concise, balanced, or detailed")
        if self.preferred_tts_voice is not None and len(self.preferred_tts_voice) > 128:
            raise ValueError("preferred_tts_voice exceeds 128 characters")
        if not 0.5 <= float(self.preferred_tts_speed) <= 2.0:
            raise ValueError("preferred_tts_speed must be between 0.5 and 2.0")
        clean_apps: dict[str, str] = {}
        for k, v in dict(self.preferred_apps).items():
            validate_safe_token(str(k), "preferred_apps category")
            if not str(v).strip() or len(str(v)) > 256:
                raise ValueError("preferred_apps value must be non-empty and bounded")
            clean_apps[str(k)] = str(v).strip()
        object.__setattr__(self, "preferred_apps", clean_apps)
        for wf_id in self.approved_workflows:
            validate_safe_token(wf_id, "approved_workflow_id")
        object.__setattr__(
            self, "approved_workflows", tuple(dict.fromkeys(self.approved_workflows))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "preferred_browser": self.preferred_browser,
            "preferred_apps": dict(self.preferred_apps),
            "preferred_response_style": self.preferred_response_style,
            "preferred_tts_voice": self.preferred_tts_voice,
            "preferred_tts_speed": self.preferred_tts_speed,
            "approved_workflows": list(self.approved_workflows),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PersonalizationProfile:
        return cls(
            principal_id=str(payload["principal_id"]),
            preferred_browser=(
                str(payload["preferred_browser"])
                if payload.get("preferred_browser") is not None
                else None
            ),
            preferred_apps=dict(payload.get("preferred_apps") or {}),
            preferred_response_style=payload.get("preferred_response_style", "balanced"),
            preferred_tts_voice=(
                str(payload["preferred_tts_voice"])
                if payload.get("preferred_tts_voice") is not None
                else None
            ),
            preferred_tts_speed=float(payload.get("preferred_tts_speed", 1.0)),
            approved_workflows=tuple(payload.get("approved_workflows") or ()),
            updated_at=(
                datetime.fromisoformat(str(payload["updated_at"]))
                if payload.get("updated_at")
                else utc_now()
            ),
        )


class PersonalizationStore:
    """SQLite-backed store for principal personalization profiles."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS personalization_profiles ("
                "principal_id TEXT PRIMARY KEY, "
                "updated_at TEXT NOT NULL, "
                "payload_json TEXT NOT NULL)"
            )

    def get_profile(self, *, principal_id: str) -> PersonalizationProfile:
        validate_safe_token(principal_id, "personalization principal_id")
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT payload_json FROM personalization_profiles WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
        if row is None:
            return PersonalizationProfile(principal_id=principal_id)
        return PersonalizationProfile.from_dict(json.loads(row["payload_json"]))

    def update_profile(
        self,
        *,
        principal_id: str,
        preferred_browser: str | None = None,
        preferred_apps: Mapping[str, str] | None = None,
        preferred_response_style: Literal["concise", "balanced", "detailed"] | None = None,
        preferred_tts_voice: str | None = None,
        preferred_tts_speed: float | None = None,
        approved_workflows: Sequence[str] | None = None,
    ) -> PersonalizationProfile:
        current = self.get_profile(principal_id=principal_id)
        updated = PersonalizationProfile(
            principal_id=principal_id,
            preferred_browser=(
                preferred_browser if preferred_browser is not None else current.preferred_browser
            ),
            preferred_apps=(
                dict(preferred_apps) if preferred_apps is not None else dict(current.preferred_apps)
            ),
            preferred_response_style=(
                preferred_response_style
                if preferred_response_style is not None
                else current.preferred_response_style
            ),
            preferred_tts_voice=(
                preferred_tts_voice
                if preferred_tts_voice is not None
                else current.preferred_tts_voice
            ),
            preferred_tts_speed=(
                float(preferred_tts_speed)
                if preferred_tts_speed is not None
                else current.preferred_tts_speed
            ),
            approved_workflows=(
                tuple(approved_workflows)
                if approved_workflows is not None
                else current.approved_workflows
            ),
            updated_at=utc_now(),
        )
        payload = json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO personalization_profiles("
                "principal_id, updated_at, payload_json) VALUES (?, ?, ?)",
                (principal_id, updated.updated_at.isoformat(), payload),
            )
        return updated

    def reset_profile(self, *, principal_id: str) -> PersonalizationProfile:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM personalization_profiles WHERE principal_id = ?",
                (principal_id,),
            )
        return PersonalizationProfile(principal_id=principal_id)


__all__ = [
    "LocalDeterministicEmbeddingAdapter",
    "PersonalizationProfile",
    "PersonalizationStore",
    "ProceduralMemoryStore",
    "ProceduralWorkflow",
    "ShortTermConversationMemory",
    "WorkingMemorySnapshot",
    "WorkingMemoryStore",
]
