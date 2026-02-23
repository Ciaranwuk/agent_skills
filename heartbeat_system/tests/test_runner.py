from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from heartbeat_system.adapters.null_responder import NullResponder
from heartbeat_system.runner import run_heartbeat_once


@dataclass
class _StubResponder:
    text: str = "HEARTBEAT_OK"
    exc: Exception | None = None
    calls: int = 0

    def respond(self, request):  # noqa: ANN001 - protocol test double
        del request
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"text": self.text}


def _config(path: Path, **overrides):
    cfg = {
        "enabled": True,
        "heartbeat_file": str(path),
        "ack_token": "HEARTBEAT_OK",
        "ack_max_chars": 12,
    }
    cfg.update(overrides)
    return cfg


class TestRunner(unittest.TestCase):
    def test_run_once_disabled_returns_skipped_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("prompt", encoding="utf-8")
            responder = _StubResponder(text="non-ack")

            result = run_heartbeat_once(
                config=_config(heartbeat_file, enabled=False),
                responder=responder,
                reason="manual-test",
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(result["run_reason"], "manual-test")
        self.assertEqual(responder.calls, 0)

    def test_run_once_empty_heartbeat_file_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("   \n\t", encoding="utf-8")
            responder = _StubResponder(text="non-ack")

            result = run_heartbeat_once(config=_config(heartbeat_file), responder=responder)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "empty-heartbeat-file")
        self.assertEqual(responder.calls, 0)

    def test_run_once_ack_only_response_skips_with_ack_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("prompt", encoding="utf-8")

            result = run_heartbeat_once(
                config=_config(heartbeat_file),
                responder=_StubResponder(text="HEARTBEAT_OK"),
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ack-only")
        self.assertEqual(result["output_text"], "")

    def test_run_once_ack_only_with_null_responder_skips_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("prompt", encoding="utf-8")

            result = run_heartbeat_once(
                config=_config(heartbeat_file),
                responder=NullResponder(),
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ack-only")
        self.assertEqual(result["output_text"], "")

    def test_run_once_non_ack_or_non_trivial_remainder_runs(self) -> None:
        cases = [
            ("send alert now", "send alert now"),
            (
                "HEARTBEAT_OK this remainder is definitely non-trivial",
                "this remainder is definitely non-trivial",
            ),
        ]

        for response_text, expected_output in cases:
            with self.subTest(response_text=response_text):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
                    heartbeat_file.write_text("prompt", encoding="utf-8")

                    result = run_heartbeat_once(
                        config=_config(heartbeat_file, ack_max_chars=10),
                        responder=_StubResponder(text=response_text),
                    )

                self.assertEqual(result["status"], "ran")
                self.assertEqual(result["reason"], "delivered")
                self.assertEqual(result["output_text"], expected_output)

    def test_run_once_responder_exception_returns_failed_adapter_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "HEARTBEAT.md"
            heartbeat_file.write_text("prompt", encoding="utf-8")
            responder = _StubResponder(exc=RuntimeError("boom\nnewline"))

            result = run_heartbeat_once(config=_config(heartbeat_file), responder=responder)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "adapter-exception")
        self.assertEqual(result["error"], "RuntimeError: boom newline")
        self.assertEqual(result["run_reason"], "manual")


if __name__ == "__main__":
    unittest.main()
