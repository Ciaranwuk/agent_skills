import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import run_surprise_me_nightly, surprise_me_telegram_notifier
from telegram_channel.api import TelegramApiError


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


class SurpriseMeTelegramNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.staging_root = self.root / "_vault_staging"
        self.runs_dir = self.staging_root / "00_Inbox/surprise_me/runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write_sidecar(
        self,
        name: str,
        *,
        status: str = "succeeded",
        title: str = "Draft a deterministic review helper for `MC-0079`",
    ) -> Path:
        sidecar_path = self.runs_dir / name
        report_path = sidecar_path.with_suffix(".md")
        payload = {
            "run_id": sidecar_path.stem,
            "status": status,
            "summary": title if status == "succeeded" else "no high-confidence improvement tonight",
            "notes_scanned": 5,
            "selected_idea": (
                {
                    "title": title,
                    "expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                    "next_action": "proposal_only",
                    "risk_level": "low",
                }
                if status == "succeeded"
                else None
            ),
            "follow_up_recommendation": "Review the report in the morning.",
            "outputs": {
                "run_report": report_path.as_posix(),
                "json_sidecar": sidecar_path.as_posix(),
            },
            "artifacts_created": [report_path.as_posix(), sidecar_path.as_posix()],
        }
        sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report_path.write_text("# report\n", encoding="utf-8")
        return sidecar_path

    def test_run_uses_latest_sidecar_and_sends_message(self) -> None:
        self._write_sidecar("SURPRISE-ME-RUN-20260326-020000.json", title="Older")
        latest = self._write_sidecar("SURPRISE-ME-RUN-20260326-030000.json", title="Newest")
        args = surprise_me_telegram_notifier.build_parser().parse_args(
            ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
        )
        factory = _TelegramClientFactory()
        with mock.patch.dict(os.environ, {"CHANNEL_TOKEN": "token-123"}, clear=False):
            payload = surprise_me_telegram_notifier.run(args, api_client_factory=factory)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["delivery_channel"], "telegram")
        self.assertEqual(payload["failure_stage"], "")
        self.assertEqual(payload["sidecar_path"], latest.as_posix())
        self.assertEqual(payload["report_path"], latest.with_suffix(".md").as_posix())
        self.assertGreater(payload["message_length"], 0)
        self.assertEqual(factory.instances[0].token, "token-123")
        self.assertIn("Idea: Newest", factory.instances[0].sent_messages[0]["text"])
        self.assertIn(latest.with_suffix(".md").as_posix(), factory.instances[0].sent_messages[0]["text"])

    def test_run_uses_token_from_env_file_when_process_env_is_sparse(self) -> None:
        latest = self._write_sidecar("SURPRISE-ME-RUN-20260326-030000.json", title="Newest")
        env_file = self.root / "scheduler.env"
        env_file.write_text("CHANNEL_TOKEN=token-from-file\n", encoding="utf-8")
        args = surprise_me_telegram_notifier.build_parser().parse_args(
            ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
        )
        factory = _TelegramClientFactory()
        with mock.patch.dict(os.environ, {"AGENT_SKILLS_ENV_FILE": str(env_file)}, clear=True):
            payload = surprise_me_telegram_notifier.run(args, api_client_factory=factory)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(factory.instances[0].token, "token-from-file")
        self.assertIn(latest.with_suffix(".md").as_posix(), factory.instances[0].sent_messages[0]["text"])

    def test_format_message_handles_no_findings(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-040000.json", status="no_findings")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))

        message = surprise_me_telegram_notifier.format_message(payload, sidecar_path=sidecar)

        self.assertIn("Surprise-me nightly: no high-confidence improvement tonight", message)
        self.assertIn("Notes scanned: 5", message)
        self.assertIn(sidecar.with_suffix(".md").as_posix(), message)

    def test_format_message_includes_expected_success_fields(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-045000.json", title="Sharper morning triage handoff")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))

        message = surprise_me_telegram_notifier.format_message(payload, sidecar_path=sidecar)

        self.assertEqual(
            message,
            "\n".join(
                [
                    "Surprise-me nightly: succeeded",
                    "Run: SURPRISE-ME-RUN-20260326-045000",
                    "Idea: Sharper morning triage handoff",
                    "Benefit: Reduce repeated verification and re-reading around one concrete work artifact.",
                    "Risk: low",
                    "Next: proposal_only",
                    "Follow-up: Review the report in the morning.",
                    f"Report: {sidecar.with_suffix('.md').as_posix()}",
                ]
            ),
        )

    def test_main_returns_failure_when_sidecars_missing(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = surprise_me_telegram_notifier.main(
                ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["delivery_channel"], "telegram")
        self.assertEqual(payload["failure_stage"], "resolving_sidecar")
        self.assertIn("no surprise-me sidecars found", payload["error"])

    def test_main_can_downgrade_send_failure_when_flag_enabled(self) -> None:
        self._write_sidecar("SURPRISE-ME-RUN-20260326-050000.json")

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
        with mock.patch.dict(os.environ, {"SURPRISE_ME_TELEGRAM_BOT_TOKEN": "token-123"}, clear=False):
            with mock.patch.object(
                surprise_me_telegram_notifier,
                "TelegramApiClient",
                _FailingTelegramClient,
            ):
                with redirect_stdout(stdout):
                    exit_code = surprise_me_telegram_notifier.main(
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
        self.assertEqual(payload["failure_stage"], "sending_message")
        self.assertEqual(payload["run_id"], "SURPRISE-ME-RUN-20260326-050000")
        self.assertEqual(payload["chat_id"], "5946121750")
        self.assertGreater(payload["message_length"], 0)
        self.assertNotIn("token-123", stdout.getvalue())

    def test_wrapper_keeps_worker_success_when_notifier_fails(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-050000.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "succeeded",
            "notes_scanned": 5,
            "candidate_connections": 1,
            "candidate_ideas": 3,
            "selected_idea_id": "idea-1",
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_surprise_me_nightly.surprise_me_workflow_improvement, "run", return_value=worker_payload):
            with mock.patch.object(
                run_surprise_me_nightly.surprise_me_telegram_notifier,
                "run",
                side_effect=run_surprise_me_nightly.surprise_me_telegram_notifier.SurpriseMeNotifierError("token missing"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_surprise_me_nightly.main(
                        [
                            "--telegram-notify",
                            "--notify-chat-id",
                            "5946121750",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-days",
                            "1",
                            "--include-sessions",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["worker"]["status"], "succeeded")
        self.assertEqual(payload["notification"]["status"], "failed")
        self.assertEqual(payload["notification"]["delivery_channel"], "telegram")
        self.assertIn("surprise-me notifier failed", stderr.getvalue())
        persisted_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(persisted_sidecar["notification"]["status"], "failed")
        self.assertEqual(persisted_sidecar["notification"]["reason"], "token missing")
        report_text = sidecar.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("## Notification", report_text)
        self.assertIn("Status: `failed`", report_text)
        self.assertIn("Delivery channel: `telegram`", report_text)
        self.assertIn("Reason: `token missing`", report_text)

    def test_wrapper_skips_notification_by_default(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-055000.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "succeeded",
            "notes_scanned": 5,
            "candidate_connections": 1,
            "candidate_ideas": 3,
            "selected_idea_id": "idea-1",
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_surprise_me_nightly.surprise_me_workflow_improvement, "run", return_value=worker_payload):
            with mock.patch.object(run_surprise_me_nightly.surprise_me_telegram_notifier, "run") as notifier_run:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_surprise_me_nightly.main(
                        [
                            "--notify-chat-id",
                            "5946121750",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-days",
                            "1",
                            "--include-sessions",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["worker"]["status"], "succeeded")
        self.assertEqual(payload["notification"]["status"], "skipped")
        self.assertEqual(payload["notification"]["reason"], "morning-digest-owned")
        notifier_run.assert_not_called()
        self.assertEqual("", stderr.getvalue())
        persisted_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(persisted_sidecar["notification"]["status"], "skipped")
        self.assertEqual(persisted_sidecar["notification"]["reason"], "morning-digest-owned")

    def test_wrapper_only_notifies_when_explicitly_enabled(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-060000.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "succeeded",
            "notes_scanned": 5,
            "candidate_connections": 1,
            "candidate_ideas": 3,
            "selected_idea_id": "idea-1",
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        captured = {}

        def _fake_notifier(args, *, api_client_factory=None):
            captured["sidecar_path"] = args.sidecar_path
            captured["chat_id"] = args.chat_id
            return {"status": "sent", "sidecar_path": args.sidecar_path}

        with mock.patch.object(run_surprise_me_nightly.surprise_me_workflow_improvement, "run", return_value=worker_payload):
            with mock.patch.object(run_surprise_me_nightly.surprise_me_telegram_notifier, "run", side_effect=_fake_notifier):
                with redirect_stdout(io.StringIO()):
                    exit_code = run_surprise_me_nightly.main(
                        [
                            "--telegram-notify",
                            "--notify-chat-id",
                            "5946121750",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-days",
                            "1",
                            "--include-sessions",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["chat_id"], "5946121750")
        self.assertEqual(captured["sidecar_path"], sidecar.as_posix())
        persisted_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(persisted_sidecar["notification"]["status"], "sent")
        self.assertEqual(persisted_sidecar["notification"]["chat_id"], "5946121750")
        self.assertEqual(persisted_sidecar["notification"]["delivery_channel"], "telegram")
        self.assertIn("message_length", persisted_sidecar["notification"])

    def test_wrapper_persists_success_notification_details_to_sidecar_and_report(self) -> None:
        sidecar = self._write_sidecar("SURPRISE-ME-RUN-20260326-080000.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "succeeded",
            "notes_scanned": 5,
            "candidate_connections": 1,
            "candidate_ideas": 3,
            "selected_idea_id": "idea-1",
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }

        def _fake_notifier(args, *, api_client_factory=None):
            return {
                "status": "sent",
                "chat_id": "5946121750",
                "run_id": sidecar.stem,
                "sidecar_path": args.sidecar_path,
                "report_path": sidecar.with_suffix(".md").as_posix(),
                "message_text": "surprise-me notification text",
            }

        with mock.patch.object(run_surprise_me_nightly.surprise_me_workflow_improvement, "run", return_value=worker_payload):
            with mock.patch.object(run_surprise_me_nightly.surprise_me_telegram_notifier, "run", side_effect=_fake_notifier):
                with redirect_stdout(io.StringIO()):
                    exit_code = run_surprise_me_nightly.main(
                        [
                            "--telegram-notify",
                            "--notify-chat-id",
                            "5946121750",
                            "--staging-root",
                            str(self.staging_root),
                            "--lookback-days",
                            "1",
                            "--include-sessions",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        persisted_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(persisted_sidecar["notification"]["status"], "sent")
        self.assertEqual(persisted_sidecar["notification"]["chat_id"], "5946121750")
        self.assertEqual(persisted_sidecar["notification"]["run_id"], sidecar.stem)
        self.assertEqual(persisted_sidecar["notification"]["report_path"], sidecar.with_suffix(".md").as_posix())
        self.assertEqual(
            persisted_sidecar["notification"]["message_length"],
            len("surprise-me notification text"),
        )
        self.assertTrue(persisted_sidecar["notification"]["attempted_at"])
        report_text = sidecar.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("## Notification", report_text)
        self.assertIn("Status: `sent`", report_text)
        self.assertIn("Chat ID: `5946121750`", report_text)
        self.assertIn(f"Message length: `{len('surprise-me notification text')}`", report_text)


if __name__ == "__main__":
    unittest.main()
