"""Cross-process database-instance ownership for the local backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class InstanceLockError(RuntimeError):
    """Another backend process already owns this database path."""


class DatabaseInstanceLock:
    """Non-blocking OS lock held for one backend lifespan.

    The lock is adjacent to the database, so two app processes configured with
    the same SQLite file share the same ownership boundary even if their other
    settings differ. OS locks are released automatically when a process exits.
    """

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            raise ValueError("in-memory databases do not need a process lock")
        database = Path(raw_path)
        self.path = database.with_name(f"{database.name}.instance.lock")
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            raise RuntimeError("database instance lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        except BaseException:
            stream.close()
            raise

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise InstanceLockError("Another ARISE backend already owns this database.") from exc
        except BaseException:
            stream.close()
            raise
        self._stream = stream

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the descriptor still releases the OS lock.
            pass
        finally:
            stream.close()

    def __enter__(self) -> DatabaseInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
