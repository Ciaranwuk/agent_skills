import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import morning_triage_telegram_notifier, run_morning_triage_digest
from telegram_channel.api import TelegramApiError

REAL_SIDECAR_PATH = Path(
    "/home/cwilson/projects/_vault_staging/00_Inbox/morning_triage/runs/MORNING-TRIAGE-RUN-20260330-013001.json"
)


class _TelegramClientStub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sent_messages: list[dict[str, str]] = []

    def send_message(self, *, chat_id, text, reply_to_message_id=None):
        self.sent_messages.append(
            {
                "chat_id": str(chat_id),
                "text": str(text),
                "reply_to_message_id": "" if reply_to_message_id is None else str(reply_to_message_id),
            }
        )
        return {"ok": True}


class _TelegramClientFactory:
    def __init__(self) -> None:
        self.instances: list[_TelegramClientStub] = []

    def __call__(self, token: str) -> _TelegramClientStub:
        client = _TelegramClientStub(token)
        self.instances.append(client)
        return client


class MorningTriageTelegramNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.staging_root = self.root / "_vault_staging"
        self.runs_dir = self.staging_root / "00_Inbox/morning_triage/runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write_sidecar(
        self,
        name: str,
        *,
        status: str = "nothing_urgent",
        urgent_items: list[dict[str, object]] | None = None,
        follow_ups: list[dict[str, object]] | None = None,
    ) -> Path:
        sidecar_path = self.runs_dir / name
        report_path = sidecar_path.with_suffix(".md")
        payload = {
            "run_id": sidecar_path.stem,
            "status": status,
            "generated_at": "2026-03-28T02:30:01+00:00",
            "lookback_hours": 12,
            "mode": "deterministic",
            "jobs_considered": 3,
            "job_statuses": [],
            "ticket_context": {
                "total_open": 31,
                "lane_counts": {"In Progress": 3, "Blocked": 4, "Backlog": 24},
                "spotlight_tickets": [
                    {"ticket_key": "MC-0007"},
                    {"ticket_key": "MC-0008"},
                    {"ticket_key": "MC-0103"},
                ],
            },
            "artifacts_considered": [],
            "urgent_items": urgent_items or [],
            "follow_ups": follow_ups
            or [
                {
                    "title": "Workflow suggestion: Create a short morning checklist for recurring manual review loops",
                    "detail": "Benefit: Save operator time on repeated low-risk review steps.",
                }
            ],
            "outputs": {
                "run_report": report_path.as_posix(),
                "json_sidecar": sidecar_path.as_posix(),
            },
            "parse_issues": [],
        }
        sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report_path.write_text("# report\n", encoding="utf-8")
        return sidecar_path

    def test_run_uses_latest_sidecar_and_sends_message(self) -> None:
        self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023000.json")
        latest = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023100.json")
        args = morning_triage_telegram_notifier.build_parser().parse_args(
            ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
        )
        factory = _TelegramClientFactory()
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "token-123"}, clear=False):
            payload = morning_triage_telegram_notifier.run(args, api_client_factory=factory)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["sidecar_path"], latest.as_posix())
        self.assertEqual(factory.instances[0].token, "token-123")
        self.assertIn("Morning triage: nothing urgent overnight.", factory.instances[0].sent_messages[0]["text"])
        self.assertIn("Workflow suggestion: Create a short morning checklist", factory.instances[0].sent_messages[0]["text"])

    def test_run_uses_token_from_env_file_when_process_env_is_sparse(self) -> None:
        latest = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023100.json")
        env_file = self.root / "scheduler.env"
        env_file.write_text("CHANNEL_TOKEN=token-from-file\n", encoding="utf-8")
        args = morning_triage_telegram_notifier.build_parser().parse_args(
            ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
        )
        factory = _TelegramClientFactory()
        with mock.patch.dict(os.environ, {"AGENT_SKILLS_ENV_FILE": str(env_file)}, clear=True):
            payload = morning_triage_telegram_notifier.run(args, api_client_factory=factory)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["sidecar_path"], latest.as_posix())
        self.assertEqual(factory.instances[0].token, "token-from-file")

    def test_format_message_includes_urgent_section_when_attention_required(self) -> None:
        sidecar = self._write_sidecar(
            "MORNING-TRIAGE-RUN-20260328-023200.json",
            status="attention_required",
            urgent_items=[
                {
                    "title": "deep-sleep-link-synthesis-nightly failed",
                    "detail": "Run `run-1` failed. error_code=`timeout`.",
                }
            ],
        )
        payload = json.loads(sidecar.read_text(encoding="utf-8"))

        message = morning_triage_telegram_notifier.format_message(payload, sidecar_path=sidecar)

        self.assertIn("Morning triage: attention required.", message)
        self.assertIn("Urgent:", message)
        self.assertIn("deep-sleep-link-synthesis-nightly failed", message)
        self.assertIn(sidecar.with_suffix(".md").as_posix(), message)

    def test_format_message_includes_deep_sleep_connection_preview(self) -> None:
        sidecar = self._write_sidecar(
            "MORNING-TRIAGE-RUN-20260328-023250.json",
            follow_ups=[
                {
                    "title": "Deep-sleep highlights available",
                    "detail": (
                        "Clusters `1`, high-confidence connections `1`, "
                        "report `00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260328-013500.md`. "
                        "New connections: decision affects log "
                        "(`SESSION-20260328`, `DEC-20260327-example`)."
                    ),
                }
            ],
        )
        payload = json.loads(sidecar.read_text(encoding="utf-8"))

        message = morning_triage_telegram_notifier.format_message(payload, sidecar_path=sidecar)

        self.assertIn("Deep-sleep highlights available", message)
        self.assertIn("New connections: decision affects log (`SESSION-20260328`, `DEC-20260327-example`)", message)

    def test_format_message_accepts_real_worker_jobs_considered_shape(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260330-013001.json")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["jobs_considered"] = [
            "vault-staging-sync-daily",
            "deep-sleep-link-synthesis-nightly",
            "surprise-me-workflow-improvement-nightly",
        ]

        message = morning_triage_telegram_notifier.format_message(payload, sidecar_path=sidecar)

        self.assertIn("Jobs checked: 3", message)

    def test_format_message_matches_real_staged_sidecar_shape(self) -> None:
        if not REAL_SIDECAR_PATH.is_file():
            self.skipTest(f"real staged sidecar fixture missing: {REAL_SIDECAR_PATH}")
        payload = json.loads(REAL_SIDECAR_PATH.read_text(encoding="utf-8"))

        message = morning_triage_telegram_notifier.format_message(payload, sidecar_path=REAL_SIDECAR_PATH)

        self.assertIn("Morning triage: nothing urgent overnight.", message)
        self.assertIn("Jobs checked: 3", message)
        self.assertIn("Urgent items: 0", message)
        self.assertIn("Mission Control:", message)
        self.assertIn("- Open tickets: 36", message)
        self.assertIn("- In Progress: 3", message)
        self.assertIn("- Blocked: 4", message)
        self.assertIn("- Backlog: 29", message)
        self.assertIn("Spotlight: MC-0007, MC-0008, MC-0009, MC-0006, MC-0103", message)
        self.assertIn("Follow-ups:", message)
        self.assertIn("- Deep-sleep highlights available", message)
        self.assertIn("\n\n- Mission Control ticket context", message)
        self.assertIn("\n\n- Workflow suggestion: Create a short morning checklist for recurring manual review loops", message)
        self.assertIn("- Mission Control ticket context", message)
        self.assertIn("- Workflow suggestion: Create a short morning checklist for recurring manual review loops", message)
        self.assertIn(f"Report: {payload['outputs']['run_report']}", message)

    def test_main_returns_failure_when_sidecars_missing(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = morning_triage_telegram_notifier.main(
                ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("no morning-triage sidecars found", payload["error"])

    def test_main_can_downgrade_send_failure_when_flag_enabled(self) -> None:
        self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023300.json")

        class _FailingTelegramClient:
            def __init__(self, token: str) -> None:
                self.token = token

            def send_message(self, *, chat_id, text, reply_to_message_id=None):
                raise TelegramApiError(
                    operation="sendMessage",
                    kind="network-error",
                    transient=True,
                    description="telegram unavailable",
                )

        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"MORNING_TRIAGE_TELEGRAM_BOT_TOKEN": "token-123"}, clear=False):
            with mock.patch.object(
                morning_triage_telegram_notifier,
                "TelegramApiClient",
                _FailingTelegramClient,
            ):
                with redirect_stdout(stdout):
                    exit_code = morning_triage_telegram_notifier.main(
                        [
                            "--staging-root",
                            str(self.staging_root),
                            "--chat-id",
                            "5946121750",
                            "--allow-send-failure",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "send_failed_nonfatal")
        self.assertNotIn("token-123", stdout.getvalue())

    def test_wrapper_fails_when_notifier_fails(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023400.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "nothing_urgent",
            "jobs_considered": 3,
            "urgent_items": 0,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_morning_triage_digest.morning_triage_digest, "run", return_value=worker_payload):
            with mock.patch.object(
                run_morning_triage_digest.morning_triage_telegram_notifier,
                "run",
                side_effect=run_morning_triage_digest.morning_triage_telegram_notifier.MorningTriageNotifierError("token missing"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_morning_triage_digest.main(
                        [
                            "--notify-chat-id",
                            "5946121750",
                            "--db-path",
                            "/tmp/mission_control.sqlite",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-hours",
                            "12",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["worker"]["status"], "nothing_urgent")
        self.assertEqual(payload["notification"]["status"], "failed")
        self.assertIn("morning-triage notifier failed", stderr.getvalue())

    def test_wrapper_marks_unexpected_notifier_errors_as_failed(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023402.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "nothing_urgent",
            "jobs_considered": 3,
            "urgent_items": 0,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_morning_triage_digest.morning_triage_digest, "run", return_value=worker_payload):
            with mock.patch.object(
                run_morning_triage_digest.morning_triage_telegram_notifier,
                "run",
                side_effect=ValueError("boom"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_morning_triage_digest.main(
                        [
                            "--notify-chat-id",
                            "5946121750",
                            "--db-path",
                            "/tmp/mission_control.sqlite",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-hours",
                            "12",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["notification"]["status"], "failed")
        self.assertEqual(payload["notification"]["sidecar_path"], sidecar.as_posix())
        self.assertIn("unexpected notifier error: ValueError: boom", payload["notification"]["reason"])
        self.assertIn("morning-triage notifier failed", stderr.getvalue())

    def test_wrapper_can_downgrade_notifier_failure(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023401.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "nothing_urgent",
            "jobs_considered": 3,
            "urgent_items": 0,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_morning_triage_digest.morning_triage_digest, "run", return_value=worker_payload):
            with mock.patch.object(
                run_morning_triage_digest.morning_triage_telegram_notifier,
                "run",
                side_effect=run_morning_triage_digest.morning_triage_telegram_notifier.MorningTriageNotifierError("token missing"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_morning_triage_digest.main(
                        [
                            "--allow-notify-failure",
                            "--notify-chat-id",
                            "5946121750",
                            "--db-path",
                            "/tmp/mission_control.sqlite",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-hours",
                            "12",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["notification"]["status"], "failed")
        self.assertIn("morning-triage notifier failed", stderr.getvalue())

    def test_wrapper_uses_exact_worker_sidecar_for_notification(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260328-023500.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "nothing_urgent",
            "jobs_considered": 3,
            "urgent_items": 0,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        captured = {}

        def _fake_notifier(args, *, api_client_factory=None):
            captured["sidecar_path"] = args.sidecar_path
            captured["chat_id"] = args.chat_id
            return {"status": "sent", "sidecar_path": args.sidecar_path}

        with mock.patch.object(run_morning_triage_digest.morning_triage_digest, "run", return_value=worker_payload):
            with mock.patch.object(run_morning_triage_digest.morning_triage_telegram_notifier, "run", side_effect=_fake_notifier):
                with redirect_stdout(io.StringIO()):
                    exit_code = run_morning_triage_digest.main(
                        [
                            "--notify-chat-id",
                            "5946121750",
                            "--db-path",
                            "/tmp/mission_control.sqlite",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-hours",
                            "12",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["chat_id"], "5946121750")
        self.assertEqual(captured["sidecar_path"], sidecar.as_posix())


if __name__ == "__main__":
    unittest.main()
