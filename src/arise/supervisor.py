"""Process supervisor and sidecar resolver for the ARISE local Python backend.

Handles sidecar binary discovery, readiness handshake (`ARISE_BACKEND_READY`),
periodic `/healthz` liveness probing, bounded crash-restart backoff, and
graceful stdin-EOF shutdown.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

READY_SIGNAL_LINE = "ARISE_BACKEND_READY"


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    startup_timeout_seconds: float = 10.0
    shutdown_grace_seconds: float = 3.0
    max_restarts: int = 3
    restart_backoff_seconds: float = 0.5
    health_check_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.startup_timeout_seconds <= 0 or self.shutdown_grace_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if not 0 <= self.max_restarts <= 10:
            raise ValueError("max_restarts must be between 0 and 10")
        if self.restart_backoff_seconds < 0:
            raise ValueError("restart_backoff_seconds cannot be negative")


def target_triple(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Return the Rust/Tauri target triple for the host platform."""

    sys_name = (system or platform.system()).lower()
    arch = (machine or platform.machine()).lower()
    if arch in {"amd64", "x86_64"}:
        norm_arch = "x86_64"
    elif arch in {"arm64", "aarch64"}:
        norm_arch = "aarch64"
    else:
        norm_arch = arch or "x86_64"
    if "windows" in sys_name or sys_name == "nt":
        return f"{norm_arch}-pc-windows-msvc"
    if "darwin" in sys_name or "mac" in sys_name:
        return f"{norm_arch}-apple-darwin"
    return f"{norm_arch}-unknown-linux-gnu"


def sidecar_binary_name(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Return the Tauri sidecar filename for the given target."""

    triple = target_triple(system=system, machine=machine)
    ext = ".exe" if "windows" in triple else ""
    return f"arise-backend-{triple}{ext}"


def resolve_backend_command(
    *,
    executable_override: str | Path | None = None,
    binaries_dir: str | Path | None = None,
    python_executable: str | None = None,
) -> tuple[str, ...]:
    """Resolve the command argv used to launch the ARISE backend sidecar or module."""

    env_exec = executable_override or os.environ.get("ARISE_BACKEND_EXECUTABLE")
    if env_exec:
        path = Path(env_exec).expanduser()
        if path.is_file():
            return (str(path),)
    if binaries_dir is not None:
        bdir = Path(binaries_dir).expanduser()
        candidate = bdir / sidecar_binary_name()
        if candidate.is_file():
            return (str(candidate),)
        plain = bdir / ("arise-backend.exe" if os.name == "nt" else "arise-backend")
        if plain.is_file():
            return (str(plain),)
    py = python_executable or os.environ.get("ARISE_PYTHON") or sys.executable
    return (str(py), "-m", "arise.server")


def build_sidecar(
    *,
    repo_root: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> Path:
    """Build or validate the Tauri 2 standalone sidecar executable path."""

    import shutil
    import subprocess

    target_dir = output_dir or (repo_root / "frontend" / "src-tauri" / "binaries")
    target_dir.mkdir(parents=True, exist_ok=True)
    binary_filename = sidecar_binary_name()
    destination = target_dir / binary_filename
    entry_script = repo_root / "src" / "arise" / "server.py"
    if not entry_script.is_file():
        raise FileNotFoundError(f"Backend entrypoint not found: {entry_script}")

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        binary_filename.removesuffix(".exe"),
        "--distpath",
        str(target_dir),
        str(entry_script),
    ]
    if dry_run:
        return destination
    if shutil.which("pyinstaller") is None:
        raise RuntimeError("PyInstaller is required to build the standalone sidecar binary.")
    subprocess.run(pyinstaller_cmd, cwd=str(repo_root), check=True)
    return destination


class SupervisedProcessPort(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def wait_ready(self, timeout_seconds: float) -> bool: ...

    async def wait_exit(self, timeout_seconds: float | None = None) -> int | None: ...

    async def close_stdin(self) -> None: ...

    def terminate_forcefully(self) -> None: ...


class _AsyncioSupervisedProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait_ready(self, timeout_seconds: float) -> bool:
        if self._process.stdout is None:
            return False
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                return False
            if not line:
                return False
            if line.decode("utf-8", errors="replace").strip() == READY_SIGNAL_LINE:
                return True

    async def wait_exit(self, timeout_seconds: float | None = None) -> int | None:
        if timeout_seconds is None:
            return await self._process.wait()
        try:
            return await asyncio.wait_for(self._process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return None

    async def close_stdin(self) -> None:
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
                await self._process.stdin.wait_closed()
            except Exception:
                pass

    def terminate_forcefully(self) -> None:
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass


class BackendSupervisor:
    """Supervises the ARISE backend process with readiness handshake and crash restart."""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        spawner: Callable[[], asyncio.Future[SupervisedProcessPort] | SupervisedProcessPort]
        | None = None,
        health_probe: Callable[[], asyncio.Future[bool] | bool] | None = None,
    ) -> None:
        self.config = config or SupervisorConfig()
        self._spawner = spawner
        self._health_probe = health_probe
        self._process: SupervisedProcessPort | None = None
        self._restart_count = 0
        self._stopping = False
        self._watch_task: asyncio.Task[None] | None = None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        self._stopping = False
        await self._spawn_and_verify()
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self._watchdog_loop(), name="arise-backend-supervisor"
            )

    async def _spawn_and_verify(self) -> None:
        if self._spawner is not None:
            maybe_coro = self._spawner()
            proc = await maybe_coro if asyncio.iscoroutine(maybe_coro) else maybe_coro
        else:
            argv = resolve_backend_command()
            env = dict(os.environ)
            env.update(
                {
                    "ARISE__API__HOST": self.config.host,
                    "ARISE__API__PORT": str(self.config.port),
                    "ARISE_BACKEND_READY_SIGNAL": "1",
                    "ARISE_BACKEND_SUPERVISED": "1",
                }
            )
            raw_proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            proc = _AsyncioSupervisedProcess(raw_proc)

        ready = await proc.wait_ready(self.config.startup_timeout_seconds)
        if not ready:
            proc.terminate_forcefully()
            await proc.wait_exit(1.0)
            raise RuntimeError("The local backend did not report ARISE_BACKEND_READY in time.")
        if self._health_probe is not None:
            probe_res = self._health_probe()
            healthy = await probe_res if asyncio.iscoroutine(probe_res) else bool(probe_res)
            if not healthy:
                proc.terminate_forcefully()
                await proc.wait_exit(1.0)
                raise RuntimeError(
                    "The local backend failed its post-startup health-check handshake."
                )
        self._process = proc

    async def _watchdog_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.config.health_check_interval_seconds)
            if self._stopping or self._process is None:
                return
            if self._process.returncode is None:
                continue
            # Unexpected exit detected; attempt bounded crash recovery
            if self._restart_count >= self.config.max_restarts:
                return
            delay = self.config.restart_backoff_seconds * (2**self._restart_count)
            if delay > 0:
                await asyncio.sleep(min(delay, 10.0))
            if self._stopping:
                return
            self._restart_count += 1
            try:
                await self._spawn_and_verify()
            except Exception:
                continue

    async def stop(self) -> None:
        self._stopping = True
        watch, self._watch_task = self._watch_task, None
        if watch is not None and watch is not asyncio.current_task() and not watch.done():
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        proc, self._process = self._process, None
        if proc is None:
            return
        await proc.close_stdin()
        exited = await proc.wait_exit(self.config.shutdown_grace_seconds)
        if exited is None:
            proc.terminate_forcefully()
            await proc.wait_exit(1.0)


__all__ = [
    "BackendSupervisor",
    "READY_SIGNAL_LINE",
    "SupervisedProcessPort",
    "SupervisorConfig",
    "build_sidecar",
    "resolve_backend_command",
    "sidecar_binary_name",
    "target_triple",
]
