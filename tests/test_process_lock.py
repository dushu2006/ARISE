from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arise.adapters.process_lock import DatabaseInstanceLock


class DatabaseInstanceLockTests(unittest.TestCase):
    def test_second_process_cannot_own_the_same_database_and_release_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "shared.sqlite3"
            lock = DatabaseInstanceLock(database_path)
            child_code = "\n".join(
                (
                    "import sys",
                    "from arise.adapters.process_lock import "
                    "DatabaseInstanceLock, InstanceLockError",
                    "lock = DatabaseInstanceLock(sys.argv[1])",
                    "try:",
                    "    lock.acquire()",
                    "except InstanceLockError:",
                    "    raise SystemExit(17)",
                    "else:",
                    "    lock.release()",
                    "    raise SystemExit(0)",
                )
            )

            lock.acquire()
            try:
                blocked = subprocess.run(
                    [sys.executable, "-c", child_code, str(database_path)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                    text=True,
                )
                self.assertEqual(blocked.returncode, 17, blocked.stderr)
            finally:
                lock.release()

            available = subprocess.run(
                [sys.executable, "-c", child_code, str(database_path)],
                capture_output=True,
                check=False,
                timeout=10,
                text=True,
            )
            self.assertEqual(available.returncode, 0, available.stderr)

    def test_in_memory_database_does_not_accept_a_process_lock(self) -> None:
        with self.assertRaises(ValueError):
            DatabaseInstanceLock(":memory:")


if __name__ == "__main__":
    unittest.main()
