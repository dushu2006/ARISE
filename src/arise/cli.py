"""Small, deterministic CLI demo for the ARISE core runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    Idempotency,
    RiskLevel,
    TargetIdentity,
    TrustLevel,
)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arise", description="ARISE runtime foundation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="run the isolated in-memory verification demo")
    args = parser.parse_args(argv)

    if args.command == "demo":
        print(json.dumps(asyncio.run(_run_demo()), indent=2))
        return 0
    return 2
