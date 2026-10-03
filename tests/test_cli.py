from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from arise.adapters.sqlite import SQLiteDatabase
from arise.cli import main
from arise.config.settings import AppSettings, DatabaseSettings


class CliTests(unittest.TestCase):
    def test_backup_command_creates_timestamped_snapshot_under_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AppSettings(
                data_dir=root / "data",
                database=DatabaseSettings(path=root / "source.sqlite3"),
            )
            source = SQLiteDatabase(settings.database_path)
            source.close()
            output = io.StringIO()
            with patch("arise.cli.get_settings", return_value=settings), redirect_stdout(output):
                result = main(["backup"])
            self.assertEqual(result, 0)
            destination = Path(json.loads(output.getvalue())["backup_path"])
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.parent, settings.data_dir / "backups")
            snapshot = SQLiteDatabase(destination)
            snapshot.close()


if __name__ == "__main__":
    unittest.main()
