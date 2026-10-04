"""Live ARISE planner check against the real configured model provider.

Run this on the machine that has the production configuration (provider, model,
credentials, and tool registration) already in place:

    python scripts/live_nvidia_planner_check.py
    python scripts/live_nvidia_planner_check.py --request "Open Chrome"

The script composes the real application with ``create_app(get_settings())``,
serves it on a loopback HTTP socket, submits the request through
``POST /api/v1/interactions``, polls the real task, and prints sanitized results
together with the sanitized planner diagnostic from
``GET /api/v1/diagnostics/planner``.

It never prints credentials, Authorization headers, prompts, or model responses.
Exit codes:

    0  planner accepted a TaskPlan (clarification or executable)
    2  the planner produced an invalid plan (InvalidPlan) and it was rejected
    3  the environment is blocked (no provider, no credentials, or no network)
    4  the request could not be submitted or observed
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx
import uvicorn

from arise.config.settings import get_settings
from arise.server import create_app

TERMINAL_STATES = {
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "unknown",
    "partially_completed",
    "interrupted",
    "requires_user_input",
    "waiting_user",
    "waiting_auth",
}
PENDING_STATES = {"queued", "understanding", "planning", "ready", "recovering"}
LAUNCH_TOOL_MARKERS = ("launch", "open_app", "app.start")


def _print_outcome(task: dict, diagnostic: dict, tools: tuple[str, ...]) -> int:
    state = task.get("state")
    reason = task.get("status_reason") or ""
    print(f"task state: {state}")
    print(f"status reason: {reason}")
    for step in task.get("steps", []):
        print(
            "  step: tool={tool} risk={risk} status={status} reason={reason}".format(
                tool=step.get("tool_name"),
                risk=step.get("risk"),
                status=step.get("status"),
                reason=step.get("status_reason"),
            )
        )
    print("planner diagnostic (sanitized):")
    print(json.dumps(diagnostic, indent=2, sort_keys=True))
    print("registered tools:", ", ".join(tools) if tools else "(none)")
    launch_tools = [name for name in tools if any(m in name for m in LAUNCH_TOOL_MARKERS)]
    print(
        "application-launch tool registered:",
        "yes (" + ", ".join(launch_tools) + ")" if launch_tools else "no",
    )

    if state in PENDING_STATES:
        print("VERDICT: ENVIRONMENT-BLOCKED (task never left the queue)")
        return 3
    if diagnostic.get("accepted"):
        if state == "completed":
            print(
                "VERDICT: PLANNER ACCEPTED; BACKEND REPORTS COMPLETED "
                "(visible Windows behavior requires confirmation on this machine)"
            )
        elif state == "requires_user_input":
            print("VERDICT: PLANNER ACCEPTED (clarification plan accepted by TaskEngine)")
        else:
            print(
                "VERDICT: PLANNER ACCEPTED; EXECUTION NOT VERIFIED "
                f"(state={state}, reason={reason})"
            )
        return 0
    if diagnostic.get("category") in {
        "response_not_json",
        "response_not_object",
        "schema_invalid",
    }:
        print(f"VERDICT: PLANNER REJECTED (category={diagnostic.get('category')})")
        return 2
    print(f"VERDICT: ENVIRONMENT-BLOCKED (no accepted plan; state={state}, reason={reason})")
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default="Open Chrome", help="user request text to submit")
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="seconds to wait for a terminal state"
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="print bounded execution-path metadata (no parameters, UI text, or credentials)",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    app = create_app(settings)
    services = app.state.services
    tools = tuple(spec.name for spec in services.tools.list_specs())
    provider_status = [(item.provider_id, item.status.value) for item in services.router.status()]
    print("ARISE live planner check")
    print(f"host: {sys.platform}; Python {sys.version_info.major}.{sys.version_info.minor}")
    print(f"started at: {datetime.now(UTC).isoformat()}")
    print(f"model provider configured: {bool(services.router.providers())}")
    print(f"provider status: {provider_status or '(none)'}")
    print(f"model id: {settings.model.model_id or '(unset)'}")
    print(
        "api auth:",
        "enabled (bearer token in use)" if services.api_token else "disabled (loopback)",
    )
    if arguments.diagnostics:
        composition = []
        for spec in services.tools.list_specs():
            if not (spec.name.startswith("uia.") or spec.name == "system.app_launch"):
                continue
            tool = services.tools.get(spec.name)
            provider = getattr(tool, "provider", None)
            backend = getattr(provider, "_backend", None)
            composition.append(
                {
                    "tool": spec.name,
                    "risk_floor": int(spec.minimum_risk),
                    "provider": type(provider).__name__,
                    "backend": type(backend).__name__,
                    "static_resources": list(spec.required_resources),
                    "target_scope": spec.target_scope,
                    "requires_visible_window": bool(
                        getattr(backend, "requires_visible_window", False)
                    ),
                }
            )
        print("production composition (registration is not execution evidence):")
        print(json.dumps(composition, indent=2))
    planner = services.engine.planner
    planner_kind = type(planner).__name__ if hasattr(planner, "last_diagnostic") else "unavailable"
    print(f"planner: {planner_kind}")
    if not services.router.providers():
        print(
            "VERDICT: ENVIRONMENT-BLOCKED "
            "(no model provider is configured/composed in this environment)"
        )
        return 3

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("VERDICT: ENVIRONMENT-BLOCKED (the application did not start)")
        return 4
    sockets = getattr(server, "servers", [])
    port = sockets[0].sockets[0].getsockname()[1] if sockets else 0
    base_url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {services.api_token}"} if services.api_token else {}

    request_id = f"live-check-{uuid.uuid4()}"
    body = {
        "request_id": request_id,
        "session_id": f"live-check-session-{uuid.uuid4()}",
        "text": arguments.request,
        "source": "api",
    }
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
            response = client.post("/api/v1/interactions", json=body)
            print(f"POST /api/v1/interactions -> {response.status_code}")
            if response.status_code != 200:
                print("VERDICT: ENVIRONMENT-BLOCKED (interaction was not accepted)")
                print(f"detail: {response.text[:200]}")
                return 4
            payload = response.json()
            print(
                "response: outcome={outcome} intent={intent}".format(
                    outcome=payload.get("outcome"), intent=payload.get("intent")
                )
            )
            task_id = (payload.get("task") or {}).get("task_id")
            if not task_id:
                print("VERDICT: ENVIRONMENT-BLOCKED (no task was created)")
                return 4
            print(f"task id: {task_id}")
            task: dict = {}
            end = time.monotonic() + arguments.timeout
            while time.monotonic() < end:
                detail = client.get(f"/api/v1/tasks/{task_id}")
                if detail.status_code != 200:
                    print(f"task query status: {detail.status_code}")
                    break
                task = detail.json().get("task") or {}
                # PARTIALLY_COMPLETED is also a transient between plan steps.
                # Do not stop the backend while the next step is still grounding.
                if task.get("state") in TERMINAL_STATES and task_id not in services.engine._active:
                    break
                time.sleep(0.5)
            diagnostic_response = client.get("/api/v1/diagnostics/planner")
            diagnostic = (
                diagnostic_response.json()
                if diagnostic_response.status_code == 200
                else {"category": "unknown"}
            )
        if arguments.diagnostics:
            launch = services.app_launch_provider
            if launch is not None:
                print("launch dispatch diagnostic (not verification evidence):")
                print(json.dumps(launch.launch_diagnostic, indent=2))
                # Only native observation facts, never application names/paths or UI text.
                observations = list(launch._observations.values())[-8:]
                print("bounded launch observation evidence:")
                print(
                    json.dumps(
                        [
                            {
                                "observation_id": observation.lease_id,
                                "state_hash": observation.state_hash,
                                "observation_error": observation.facts.get("observation.error"),
                                "process_id": observation.facts.get("process_id"),
                                "process_running": observation.facts.get("process.running"),
                                "visible_window_open": observation.facts.get("window.open"),
                                "window_count": len(observation.facts.get("window_ids", ())),
                            }
                            for observation in observations
                        ],
                        indent=2,
                    )
                )
            allowed = {
                "EXECUTION_PATH",
                "RESOURCE_WAITING",
                "RESOURCE_ACQUIRED",
                "RESOURCE_RELEASED",
                "OBSERVATION_CREATED",
                "EXECUTION_RESULT",
                "VERIFICATION_EVIDENCE",
            }
            events = services.event_store.read_after(task_id=task_id, limit=500)
            print("bounded execution diagnostic (first 500 task events; values/UI text omitted):")
            print(
                json.dumps(
                    [
                        {
                            "event": event.event_type,
                            "action_id": event.step_id,
                            "payload": event.to_dict()["payload"],
                        }
                        for event in events
                        if event.event_type in allowed
                    ],
                    indent=2,
                )
            )
        post_status = [(item.provider_id, item.status.value) for item in services.router.status()]
        print(f"provider status after the run: {post_status}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return _print_outcome(task, diagnostic, tools)


if __name__ == "__main__":
    raise SystemExit(main())
