"""Small, deterministic CLI tools for the ARISE runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.adapters.process_lock import InstanceLockError
from arise.adapters.sqlite import SQLiteDatabase
from arise.config.settings import AppSettings, get_settings
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    Idempotency,
    RiskLevel,
    TargetIdentity,
    TrustLevel,
)
from arise.core.errors import DatabaseError
from arise.core.events import InMemoryEventStore
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.tasks import InMemoryTaskRepository, TaskRecord


def build_demo_runtime() -> tuple[
    AgentRuntime, InMemoryTaskRepository, InMemoryEventStore, ActionContract
]:
    target = TargetIdentity(
        platform="simulator",
        application="demo-workspace",
        object_id="demo-project",
        semantic_name="Demo project",
        confidence=1.0,
    )
    environment = InMemoryEnvironment(
        {"application.ready": True, "project.open": False},
        target=target,
    )
    registry = ToolRegistry()
    registry.register(SetFactTool(environment))

    tasks = InMemoryTaskRepository()
    events = InMemoryEventStore()
    policy = PolicyEngine()
    runtime = AgentRuntime(
        tasks=tasks,
        events=events,
        tools=registry,
        policy=policy,
        environment=environment,
        resources=ResourceManager(),
        verifier=FactVerifier(environment),
    )

    authority = AuthorizationContext(
        principal_id="demo-user",
        user_intent_id="demo-intent",
        trust=TrustLevel.USER_INSTRUCTION,
        capabilities=frozenset({"simulator.write"}),
    )
    task = TaskRecord.planned("Open the demo project", authorization=authority)
    tasks.save(task)
    action = ActionContract(
        task_id=task.task_id,
        tool_name="simulator.set_fact",
        target=target,
        risk=RiskLevel.R1,
        authority=authority,
        parameters={"key": "project.open", "value": True},
        preconditions=(Condition("application.ready", expected=True),),
        postconditions=(Condition("project.open", expected=True),),
        required_resources=("simulator-state",),
        idempotency=Idempotency.IDEMPOTENT,
    )
    return runtime, tasks, events, action


async def _run_demo() -> dict[str, object]:
    runtime, tasks, events, action = build_demo_runtime()
    result = await runtime.execute_action(action, final_action=True)
    task = tasks.get(action.task_id)
    return {
        "task_id": action.task_id,
        "task_status": task.status.value if task else "missing",
        "step_status": result.step_status.value,
        "executed": result.executed,
        "verified": result.verification.status.value if result.verification else None,
        "verification_level": result.verification.level if result.verification else None,
        "message": result.message,
        "event_count": len(events.read_after()),
    }


def _run_backup(destination: Path | None, *, settings: AppSettings | None = None) -> Path:
    chosen_settings = settings or get_settings()
    if destination is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = chosen_settings.data_dir / "backups" / f"arise-{timestamp}.sqlite3"
    database = SQLiteDatabase(
        chosen_settings.database_path,
        busy_timeout_ms=chosen_settings.database.busy_timeout_ms,
        acquire_instance_lock=True,
    )
    try:
        return database.backup_to(destination)
    finally:
        database.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arise", description="ARISE runtime and local utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="run the isolated in-memory verification demo")
    backup_parser = subparsers.add_parser(
        "backup", help="create a consistent, no-overwrite SQLite database snapshot"
    )
    backup_parser.add_argument(
        "--destination",
        type=Path,
        help="destination .sqlite3 path (defaults to <DATA_DIR>/backups with a UTC timestamp)",
    )
    args = parser.parse_args(argv)

    if args.command == "demo":
        print(json.dumps(asyncio.run(_run_demo()), indent=2))
        return 0
    if args.command == "backup":
        try:
            destination = _run_backup(args.destination)
        except (DatabaseError, InstanceLockError, OSError, RuntimeError, ValueError) as exc:
            print(f"Database backup failed ({type(exc).__name__}).", file=sys.stderr)
            return 1
        print(json.dumps({"backup_path": str(destination)}, indent=2))
        return 0
    return 2
