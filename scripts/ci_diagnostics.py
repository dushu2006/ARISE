"""Run CI commands and surface concise failures as GitHub check annotations.

GitHub's full job logs are stored separately from check-run metadata. These
annotations preserve the useful pytest/cargo failure summary in the check API
as well as in the runner log, which makes failures easier to triage.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAX_ANNOTATIONS = 30


def _escape_command_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _pytest_diagnostics(output: str) -> list[tuple[str | None, str]]:
    diagnostics: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", line.strip())
        if not match:
            continue
        test_id, reason = match.groups()
        message = f"{test_id}: {reason}" if reason else test_id
        if message in seen:
            continue
        seen.add(message)
        path = test_id.split("::", maxsplit=1)[0]
        if path.startswith(("tests/", "tests\\")) and not Path(_ROOT, path).is_file():
            path = None
        elif not path.startswith(("tests/", "tests\\")):
            path = None
        diagnostics.append((path, message))
        if len(diagnostics) >= _MAX_ANNOTATIONS:
            break
    if not diagnostics:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("E   "):
                diagnostics.append((None, stripped[4:]))
                if len(diagnostics) >= _MAX_ANNOTATIONS:
                    break
    return diagnostics


def _cargo_diagnostics(output: str) -> list[tuple[str | None, str]]:
    lines = output.splitlines()
    diagnostics: list[tuple[str | None, str]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"^(?:"
        r"error(?:\[[^]]+\])?:|"
        r"Caused by:|"
        r"failed to (?:run|compile)|"
        r"could not compile|"
        r"thread '.+' panicked"
        r")",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not pattern.search(stripped):
            continue
        message = stripped
        # Include the next short context line for build-script diagnostics such
        # as a missing icon path, while keeping annotations within GitHub limits.
        for following in lines[index + 1 : index + 3]:
            context = following.strip()
            if context and not context.startswith(("warning:", "note:")):
                message = f"{message} — {context}"
                break
        if message in seen:
            continue
        seen.add(message)
        diagnostics.append((None, message[:1000]))
        if len(diagnostics) >= _MAX_ANNOTATIONS:
            break
    return diagnostics


def _emit_annotations(kind: str, diagnostics: list[tuple[str | None, str]]) -> None:
    for path, message in diagnostics:
        properties = [f"file={path}"] if path else []
        properties.append(f"title={kind} failure")
        escaped = _escape_command_data(message)
        print(f"::error {','.join(properties)}::{escaped}", flush=True)


def _command_for(mode: str) -> tuple[list[str], Path]:
    if mode == "pytest":
        return (
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "--color=no",
            ],
            _ROOT,
        )
    if mode == "cargo":
        return (
            [
                "cargo",
                "check",
                "--color=never",
                "--manifest-path",
                "src-tauri/Cargo.toml",
            ],
            _ROOT / "frontend",
        )
    raise ValueError(f"unsupported CI diagnostic command: {mode}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in {"pytest", "cargo"}:
        print("usage: ci_diagnostics.py {pytest|cargo}", file=sys.stderr)
        return 2
    mode = args[0]
    command, cwd = _command_for(mode)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        detail = f"Could not start validation: {type(exc).__name__}: {exc}"
        print(f"Could not start {mode} validation: {type(exc).__name__}: {exc}")
        _emit_annotations(mode, [(None, detail)])
        return 127

    output = result.stdout or ""
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    if output:
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
    if result.returncode:
        diagnostics = (
            _pytest_diagnostics(output) if mode == "pytest" else _cargo_diagnostics(output)
        )
        if not diagnostics:
            tail = [line.strip() for line in output.splitlines()[-10:] if line.strip()]
            diagnostics = [(None, line[:1000]) for line in tail[:_MAX_ANNOTATIONS]]
        if not diagnostics:
            diagnostics = [
                (None, f"{mode} validation exited with status {result.returncode} and no output.")
            ]
        _emit_annotations(mode, diagnostics)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
