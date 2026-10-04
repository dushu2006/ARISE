from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from arise.supervisor import BackendSupervisor, SupervisorConfig


class SupervisedBackendProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_pipe_eof_stops_backend_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment.update(
                {
                    "ARISE_BACKEND_READY_SIGNAL": "1",
                    "ARISE_BACKEND_SUPERVISED": "1",
                    "ARISE__API__HOST": "127.0.0.1",
                    "ARISE__API__PORT": "0",
                    "ARISE__DATA_DIR": str(root),
                    "ARISE__DATABASE__PATH": str(root / "lifecycle.sqlite3"),
                    "ARISE__SECURITY__ENVIRONMENT": "test",
                }
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "arise.server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            assert process.stdout is not None
            assert process.stdin is not None
            try:
                ready = False
                async with asyncio.timeout(20):
                    while line := await process.stdout.readline():
                        if line.strip() == b"ARISE_BACKEND_READY":
                            ready = True
                            break
                self.assertTrue(ready, "supervised backend did not report readiness")

                process.stdin.close()
                await process.stdin.wait_closed()
                await asyncio.wait_for(process.wait(), timeout=15)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_supervisor_restarts_a_real_backend_after_process_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as socket_probe:
                socket_probe.bind(("127.0.0.1", 0))
                port = int(socket_probe.getsockname()[1])

            environment = {
                "ARISE__DATA_DIR": str(root),
                "ARISE__DATABASE__PATH": str(root / "supervised.sqlite3"),
                "ARISE__API__HOST": "127.0.0.1",
                "ARISE__API__PORT": str(port),
                "ARISE__SECURITY__ENVIRONMENT": "test",
            }

            async def health_probe() -> bool:
                def get_health() -> bool:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                            return response.status == 200
                    except OSError:
                        return False

                return await asyncio.to_thread(get_health)

            supervisor = BackendSupervisor(
                SupervisorConfig(
                    host="127.0.0.1",
                    port=port,
                    startup_timeout_seconds=15,
                    shutdown_grace_seconds=3,
                    max_restarts=1,
                    restart_backoff_seconds=0.5,
                    health_check_interval_seconds=0.05,
                ),
                health_probe=health_probe,
            )
            with patch.dict(os.environ, environment):
                try:
                    await supervisor.start()
                    assert supervisor._process is not None
                    first_pid = supervisor._process._process.pid
                    supervisor._process.terminate_forcefully()

                    async with asyncio.timeout(20):
                        while supervisor.restart_count < 1 or not supervisor.is_running:
                            await asyncio.sleep(0.05)

                    assert supervisor._process is not None
                    restarted_pid = supervisor._process._process.pid
                    self.assertNotEqual(first_pid, restarted_pid)
                    self.assertTrue(await health_probe())
                finally:
                    await supervisor.stop()
            self.assertFalse(supervisor.is_running)


if __name__ == "__main__":
    unittest.main()
