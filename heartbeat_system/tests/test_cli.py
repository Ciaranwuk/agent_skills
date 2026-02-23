from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from unittest.mock import patch

from heartbeat_system import cli
from heartbeat_system.api import HeartbeatUnavailableError


class TestCli(unittest.TestCase):
    def test_build_parser_run_once_defaults(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["run-once"])

        self.assertEqual(args.command, "run-once")
        self.assertEqual(args.reason, "manual")
        self.assertEqual(args.heartbeat_file, "HEARTBEAT.md")
        self.assertEqual(args.ack_token, "HEARTBEAT_OK")
        self.assertEqual(args.ack_max_chars, 300)
        self.assertFalse(args.include_reasoning)
        self.assertFalse(args.disabled)

    def test_main_run_once_emits_json_contract_and_maps_flags(self) -> None:
        captured = {}

        def _fake_run_once(*, config, reason):  # noqa: ANN001 - test stub
            captured["config"] = config
            captured["reason"] = reason
            return {
                "status": "ran",
                "reason": "delivered",
                "output_text": "ok",
                "run_reason": reason,
            }

        with patch.object(cli, "run_once", _fake_run_once), patch.object(
            sys,
            "argv",
            [
                "heartbeat_system",
                "run-once",
                "--reason",
                "smoke",
                "--heartbeat-file",
                "HB.md",
                "--ack-token",
                "ACK",
                "--ack-max-chars",
                "42",
                "--include-reasoning",
                "--disabled",
            ],
        ), patch("sys.stdout", new_callable=StringIO) as stdout:
            code = cli.main()

        payload = json.loads(stdout.getvalue().strip())

        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "contract": "heartbeat.operator",
                "contract_version": "1.0",
                "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                "error_code": None,
                "error_reason": None,
                "ok": True,
                "status": "ran",
                "reason": "delivered",
                "output_text": "ok",
                "run_reason": "smoke",
            },
        )
        self.assertEqual(captured["reason"], "smoke")
        self.assertFalse(captured["config"].enabled)
        self.assertEqual(captured["config"].heartbeat_file, "HB.md")
        self.assertEqual(captured["config"].ack_token, "ACK")
        self.assertEqual(captured["config"].ack_max_chars, 42)
        self.assertTrue(captured["config"].include_reasoning)

    def test_main_run_once_unavailable_emits_failed_json(self) -> None:
        def _raising_run_once(*, config, reason):  # noqa: ANN001 - test stub
            del config, reason
            raise HeartbeatUnavailableError("runner missing")

        with patch.object(cli, "run_once", _raising_run_once), patch.object(
            sys,
            "argv",
            ["heartbeat_system", "run-once"],
        ), patch("sys.stdout", new_callable=StringIO) as stdout:
            code = cli.main()

        payload = json.loads(stdout.getvalue().strip())

        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["reason"], "runner-unavailable")
        self.assertEqual(payload["contract"], "heartbeat.operator")
        self.assertEqual(payload["contract_version"], "1.0")
        self.assertEqual(payload["contract_metadata"], {"name": "heartbeat.operator", "version": "1.0"})
        self.assertEqual(payload["error_code"], "runner-unavailable")
        self.assertEqual(payload["error_reason"], "runner-unavailable")
        self.assertFalse(payload["ok"])
        self.assertIn("runner missing", payload["error"])

    def test_main_run_once_invalid_config_emits_machine_readable_error(self) -> None:
        def _raising_run_once(*, config, reason):  # noqa: ANN001 - test stub
            del config, reason
            raise ValueError("ack_max_chars must be > 0")

        with patch.object(cli, "run_once", _raising_run_once), patch.object(
            sys,
            "argv",
            ["heartbeat_system", "run-once"],
        ), patch("sys.stdout", new_callable=StringIO) as stdout:
            code = cli.main()

        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["reason"], "invalid-config")
        self.assertEqual(payload["error_code"], "invalid-config")
        self.assertEqual(payload["error_reason"], "invalid-config")
        self.assertFalse(payload["ok"])
        self.assertIn("ack_max_chars", payload["error"])

    def test_main_phase3_commands_emit_json(self) -> None:
        cases = [
            (
                ["heartbeat_system", "status"],
                "get_status",
                {"status": "idle", "running": False},
                [{}],
                True,
            ),
            (
                ["heartbeat_system", "last-event"],
                "get_last_event",
                {"status": "empty", "event": None},
                [{}],
                True,
            ),
            (
                ["heartbeat_system", "wake", "--reason", "manual"],
                "wake",
                {"status": "not-running", "accepted": False},
                [{"reason": "manual"}],
                False,
            ),
            (
                ["heartbeat_system", "enable"],
                "enable_heartbeat",
                {"status": "ok", "enabled": True},
                [{}],
                True,
            ),
            (
                ["heartbeat_system", "disable"],
                "disable_heartbeat",
                {"status": "ok", "enabled": False},
                [{}],
                True,
            ),
        ]

        for argv, fn_name, expected, expected_calls, expected_ok in cases:
            with self.subTest(command=argv[1]):
                calls = []

                def _stub(**kwargs):  # noqa: ANN003 - simple capture
                    calls.append(kwargs)
                    payload = {}
                    payload.update(expected)
                    return payload

                with patch.object(cli, fn_name, _stub), patch.object(sys, "argv", argv), patch(
                    "sys.stdout", new_callable=StringIO
                ) as stdout:
                    code = cli.main()

                payload = json.loads(stdout.getvalue().strip())

                self.assertEqual(code, 0)
                self.assertEqual(
                    payload,
                    {
                        **expected,
                        "contract": "heartbeat.operator",
                        "contract_version": "1.0",
                        "contract_metadata": {"name": "heartbeat.operator", "version": "1.0"},
                        "error_code": None,
                        "error_reason": None,
                        "ok": expected_ok,
                    },
                )
                self.assertEqual(calls, expected_calls)


if __name__ == "__main__":
    unittest.main()
