from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.check_disk_space_alert import DEFAULT_PATH, DiskStatus, inspect_disk, run_check


class DiskSpaceAlertTests(unittest.TestCase):
    def test_default_path_is_laptop_data_disk(self) -> None:
        self.assertEqual(DEFAULT_PATH, "/mnt/c")

    def test_inspect_disk_uses_free_and_total_bytes(self) -> None:
        status = inspect_disk("/data", threshold_bytes=10, usage_fn=lambda _: SimpleNamespace(free=9, total=100))
        self.assertEqual(status.free_bytes, 9)
        self.assertEqual(status.total_bytes, 100)
        self.assertTrue(status.below_threshold)

    def test_sends_only_on_transition_into_low_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            api = Mock()
            factory = Mock(return_value=api)
            status = DiskStatus("/", 9, 100, 10)

            self.assertEqual(run_check(status=status, state_path=state_path, token="token", chat_id="55", api_factory=factory), "alert-sent")
            self.assertEqual(run_check(status=status, state_path=state_path, token="token", chat_id="55", api_factory=factory), "unchanged")
            self.assertEqual(api.send_message.call_count, 1)
            self.assertTrue(json.loads(state_path.read_text())["alert_active"])

    def test_sends_recovery_when_space_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text('{"alert_active": true}', encoding="utf-8")
            api = Mock()
            status = DiskStatus("/", 11, 100, 10)

            result = run_check(status=status, state_path=state_path, token="token", chat_id="55", api_factory=lambda _: api)

            self.assertEqual(result, "recovery-sent")
            self.assertEqual(api.send_message.call_count, 1)
            self.assertIn("recovered", api.send_message.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
