from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
