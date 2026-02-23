from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from heartbeat_system.heartbeat_file import load_heartbeat_prompt


class TestHeartbeatFile(unittest.TestCase):
    def test_missing_file_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "HEARTBEAT.md"

            loaded = load_heartbeat_prompt(missing)

            self.assertEqual(loaded.path, str(missing))
            self.assertEqual(loaded.text, "")
            self.assertTrue(loaded.is_empty)

    def test_whitespace_only_content_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text(" \n\t\n", encoding="utf-8")

            loaded = load_heartbeat_prompt(heartbeat_file)

            self.assertEqual(loaded.path, str(heartbeat_file))
            self.assertEqual(loaded.text, " \n\t\n")
            self.assertTrue(loaded.is_empty)

    def test_non_empty_content_is_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("Run heartbeat checks.", encoding="utf-8")

            loaded = load_heartbeat_prompt(heartbeat_file)

            self.assertEqual(loaded.path, str(heartbeat_file))
            self.assertEqual(loaded.text, "Run heartbeat checks.")
            self.assertFalse(loaded.is_empty)


if __name__ == "__main__":
    unittest.main()
