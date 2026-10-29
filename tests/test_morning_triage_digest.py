import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import morning_triage_digest, morning_triage_telegram_notifier, run_morning_triage_digest
from telegram_channel.api import TelegramApiError


class FrozenDateTime(datetime):
    current = datetime(2026, 3, 26, 7, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value


class MorningTriageDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "mission_control.sqlite"
        self.staging_root = self.root / "_vault_staging"
        self.staging_root.mkdir()
        self._init_db()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE cron_jobs (
                    job_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    command TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    paused_at TEXT,
                    last_run_at TEXT,
                    last_success_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE cron_job_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    exit_code INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    duration_ms INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    project TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    owner TEXT,
                    due_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _insert_job(
        self,
        *,
        job_id: str,
        name: str,
        max_retries: int = 0,
        state: str = "active",
        last_run_at: str | None = None,
        last_success_at: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO cron_jobs (
                    job_id, name, owner, command, schedule, timezone, timeout_seconds,
                    max_retries, state, failure_streak, paused_at, last_run_at, last_success_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    "agent:test",
                    "python3 worker.py",
                    "0 1 * * *",
                    "Europe/London",
                    300,
                    max_retries,
                    state,
                    0,
                    None,
                    last_run_at,
                    last_success_at,
                    "2026-03-26T00:00:00+00:00",
                    "2026-03-26T00:00:00+00:00",
                ),
            )

    def _insert_run(
        self,
        *,
        run_id: str,
        job_id: str,
        state: str,
        scheduled_for: str,
        attempt: int = 1,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO cron_job_runs (
                    run_id, job_id, trigger_type, scheduled_for, started_at, finished_at,
                    state, attempt, exit_code, error_code, error_message, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    "scheduled",
                    scheduled_for,
                    started_at,
                    finished_at,
                    state,
                    attempt,
                    0 if state == "succeeded" else 1,
                    error_code,
                    error_message,
                    1000,
                ),
            )

    def _insert_ticket(
        self,
        *,
        ticket_key: str,
        title: str,
        lane: str,
        project: str = "Capability stream",
        priority: str = "P2",
        updated_at: str = "2026-03-26T00:00:00+00:00",
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tickets (
                    ticket_key, title, description, project, lane, priority, owner, due_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_key,
                    title,
                    "",
                    project,
                    lane,
                    priority,
                    None,
                    None,
                    "2026-03-26T00:00:00+00:00",
                    updated_at,
                ),
            )

    def _write_sidecar(self, rel_path: str, payload: dict[str, object]) -> Path:
        path = self.staging_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(f"# {payload['run_id']}\n", encoding="utf-8")
        return path

    def _write_surprise_me_registry(self, payload: dict[str, object]) -> Path:
        path = self.staging_root / "00_Inbox/surprise_me/idea_registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _args(self, *extra: str):
        return morning_triage_digest.build_parser().parse_args(
            [
                "--db-path",
                str(self.db_path),
                "--staging-root",
                str(self.staging_root),
                "--lookback-hours",
                "12",
                *extra,
            ]
        )

    def _run(self, *extra: str) -> dict[str, object]:
        args = self._args(*extra)
        with mock.patch.object(morning_triage_digest, "datetime", FrozenDateTime):
            return morning_triage_digest.run(args)

    def test_run_creates_markdown_and_json_sidecar(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )

        result = self._run()

        runs_dir = self.staging_root / "00_Inbox/morning_triage/runs"
        self.assertEqual(result["status"], "attention_required")
        self.assertEqual(sorted(path.name for path in runs_dir.glob("*.md")), ["MORNING-TRIAGE-RUN-20260326-070000.md"])
        self.assertEqual(sorted(path.name for path in runs_dir.glob("*.json")), ["MORNING-TRIAGE-RUN-20260326-070000.json"])

    def test_run_prioritizes_failures_above_success_highlights(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-deep",
            name="deep-sleep-link-synthesis-nightly",
            last_run_at="2026-03-26T01:35:00+00:00",
            last_success_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_job(
            job_id="job-extra",
            name="db-backup-nightly",
            max_retries=2,
            last_run_at="2026-03-26T03:01:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-deep",
            job_id="job-deep",
            state="succeeded",
            scheduled_for="2026-03-26T01:30:00+00:00",
            started_at="2026-03-26T01:30:00+00:00",
            finished_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise",
            job_id="job-surprise",
            state="timed_out",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:05:00+00:00",
            error_code="timeout",
            error_message="worker exceeded timeout",
        )
        self._insert_run(
            run_id="run-extra",
            job_id="job-extra",
            state="failed",
            scheduled_for="2026-03-26T03:00:00+00:00",
            started_at="2026-03-26T03:00:00+00:00",
            finished_at="2026-03-26T03:01:00+00:00",
            error_code="exit_1",
            error_message="non-zero exit",
        )
        self._write_sidecar(
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013500.json",
            {
                "run_id": "DEEP-SLEEP-RUN-20260326-013500",
                "status": "succeeded",
                "clusters_found": 2,
                "connections": [
                    {
                        "confidence": "medium",
                        "relation_type": "shared_theme",
                        "source_notes": [
                            "20_Work_Logs/LOG-20260325.md",
                            "20_Work_Logs/LOG-20260326.md",
                        ],
                    },
                    {
                        "confidence": "high",
                        "relation_type": "decision_affects_log",
                        "source_notes": [
                            "00_Inbox/SESSION-20260326.md",
                            "10_Decisions/DEC-20260326-example.md",
                        ],
                    },
                    {
                        "confidence": "high",
                        "relation_type": "follow_up_gap",
                        "source_notes": [
                            "00_Inbox/SESSION-20260324.md",
                            "20_Work_Logs/LOG-20260324.md",
                        ],
                    },
                ],
            },
        )

        result = self._run()
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "attention_required")
        self.assertEqual([item["state"] for item in payload["urgent_items"][:2]], ["failed", "timed_out"])
        self.assertEqual([item["job_name"] for item in payload["urgent_items"][:2]], ["db-backup-nightly", "surprise-me-workflow-improvement-nightly"])
        highlight = next(item for item in payload["follow_ups"] if item["type"] == "deep_sleep_highlight")
        self.assertIn("high-confidence connections `2`", highlight["detail"])
        self.assertIn(
            "decision affects log (`SESSION-20260326`, `DEC-20260326-example`)",
            highlight["detail"],
        )
        self.assertIn(
            "follow-up gap (`SESSION-20260324`, `LOG-20260324`)",
            highlight["detail"],
        )
        self.assertIn("+1 more", highlight["detail"])

    def test_run_ignores_failure_superseded_by_later_success(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-deep",
            name="deep-sleep-link-synthesis-nightly",
            last_run_at="2026-03-26T01:35:00+00:00",
            last_success_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:06:00+00:00",
            last_success_at="2026-03-26T02:06:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-deep",
            job_id="job-deep",
            state="succeeded",
            scheduled_for="2026-03-26T01:30:00+00:00",
            started_at="2026-03-26T01:30:00+00:00",
            finished_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise-failed",
            job_id="job-surprise",
            state="failed",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:01:00+00:00",
            error_code="non_zero_exit",
            error_message="temporary failure",
        )
        self._insert_run(
            run_id="run-surprise-success",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:05:00+00:00",
            started_at="2026-03-26T02:05:00+00:00",
            finished_at="2026-03-26T02:06:00+00:00",
        )
        self._write_sidecar(
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013500.json",
            {
                "run_id": "DEEP-SLEEP-RUN-20260326-013500",
                "status": "succeeded",
                "clusters_found": 1,
                "connections": [{"confidence": "high"}],
            },
        )
        self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020600.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-020600",
                "status": "succeeded",
                "selected_idea": {
                    "title": "Reduce repeated morning review friction",
                    "expected_benefit": "Faster review",
                    "next_action": "Review the staged note",
                },
            },
        )

        result = self._run()
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "nothing_urgent")
        self.assertFalse(any(item["job_name"] == "surprise-me-workflow-improvement-nightly" and item["type"] == "failed_run" for item in payload["urgent_items"]))
        workflow_suggestion = next(item for item in payload["follow_ups"] if item["type"] == "workflow_suggestion")
        self.assertEqual(workflow_suggestion["title"], "Workflow suggestion: Reduce repeated morning review friction")
        self.assertIn("Triage:", workflow_suggestion["detail"])
        self.assertTrue(any(item["type"] == "ticket_lane_summary" for item in payload["follow_ups"]))

    def test_run_prefers_first_sidecar_after_successful_worker_run_not_later_manual_rerun(self) -> None:
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:06:00+00:00",
            last_success_at="2026-03-26T02:06:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise-success",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:06:00+00:00",
        )
        expected_sidecar = self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020600.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-020600",
                "status": "succeeded",
                "selected_idea": {
                    "title": "Cron-produced idea",
                    "expected_benefit": "Use the scheduled artifact.",
                    "next_action": "proposal_only",
                },
                "follow_up_recommendation": "Review the scheduled artifact.",
            },
        )
        self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-050000.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-050000",
                "status": "succeeded",
                "selected_idea": {
                    "title": "Later manual rerun idea",
                    "expected_benefit": "This should not replace the overnight artifact.",
                    "next_action": "proposal_only",
                },
                "follow_up_recommendation": "Review the later artifact.",
            },
        )

        result = self._run("--job-name", "surprise-me-workflow-improvement-nightly")
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        workflow_suggestion = next(item for item in payload["follow_ups"] if item["type"] == "workflow_suggestion")
        self.assertEqual(workflow_suggestion["title"], "Workflow suggestion: Cron-produced idea")
        self.assertEqual(workflow_suggestion["artifact_path"], expected_sidecar.relative_to(self.staging_root).as_posix())

    def test_run_marks_missing_expected_run_as_urgent(self) -> None:
        self._insert_job(job_id="job-sync", name="vault-staging-sync-daily")
        self._insert_job(job_id="job-deep", name="deep-sleep-link-synthesis-nightly")
        self._insert_job(job_id="job-surprise", name="surprise-me-workflow-improvement-nightly")

        result = self._run()
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "attention_required")
        self.assertTrue(any(item["type"] == "missing_recent_run" and item["job_name"] == "surprise-me-workflow-improvement-nightly" for item in payload["urgent_items"]))

    def test_run_degrades_for_unreadable_and_stale_sidecars(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-deep",
            name="deep-sleep-link-synthesis-nightly",
            last_run_at="2026-03-26T01:35:00+00:00",
            last_success_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
            last_success_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-deep",
            job_id="job-deep",
            state="succeeded",
            scheduled_for="2026-03-26T01:30:00+00:00",
            started_at="2026-03-26T01:30:00+00:00",
            finished_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:05:00+00:00",
        )
        unreadable = self.staging_root / "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020500.json"
        unreadable.parent.mkdir(parents=True, exist_ok=True)
        unreadable.write_text("{not-json}\n", encoding="utf-8")
        self._write_sidecar(
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013000.json",
            {
                "run_id": "DEEP-SLEEP-RUN-20260326-013000",
                "status": "succeeded",
                "clusters_found": 1,
                "connections": [{"confidence": "high"}],
            },
        )

        result = self._run()
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "attention_required")
        self.assertGreaterEqual(len(payload["parse_issues"]), 2)
        self.assertTrue(any("SURPRISE-ME-RUN-20260326-020500.json" in issue["path"] for issue in payload["parse_issues"]))
        self.assertTrue(any("stale" in issue["error"] for issue in payload["parse_issues"]))
        self.assertTrue(any(item["type"] in {"stale_artifact", "unreadable_artifact"} for item in payload["urgent_items"]))

    def test_run_returns_nothing_urgent_for_clean_window(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-deep",
            name="deep-sleep-link-synthesis-nightly",
            last_run_at="2026-03-26T01:35:00+00:00",
            last_success_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
            last_success_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-deep",
            job_id="job-deep",
            state="succeeded",
            scheduled_for="2026-03-26T01:30:00+00:00",
            started_at="2026-03-26T01:30:00+00:00",
            finished_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:05:00+00:00",
        )
        self._write_sidecar(
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013500.json",
            {
                "run_id": "DEEP-SLEEP-RUN-20260326-013500",
                "status": "succeeded",
                "clusters_found": 2,
                "connections": [
                    {
                        "confidence": "high",
                        "relation_type": "decision_affects_log",
                        "source_notes": [
                            "00_Inbox/SESSION-20260326.md",
                            "10_Decisions/DEC-20260326-example.md",
                        ],
                    }
                ],
            },
        )
        self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020500.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-020500",
                "status": "succeeded",
                "selected_idea": {
                    "title": "Reduce repeated morning review friction",
                    "expected_benefit": "Faster review",
                    "next_action": "Review the staged note",
                    "source_notes": [
                        "00_Inbox/SESSION-20260326.md",
                        "20_Work_Logs/LOG-20260326.md",
                        "10_Decisions/DEC-20260326-example.md",
                    ],
                },
                "inputs": {
                    "context_files": ["START_HERE.md", "30_Projects/PROFILE-ciaran.md"],
                    "deep_sleep_inputs": ["00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013500.json"],
                },
            },
        )
        self._insert_ticket(
            ticket_key="MC-0102",
            title="Investigate Telegram voice-message handling workflow",
            lane="Backlog",
        )
        self._insert_ticket(
            ticket_key="MC-0103",
            title="MISSION-CRON surprise-me parent still active",
            lane="In Progress",
        )

        result = self._run()
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "nothing_urgent")
        self.assertEqual(payload["urgent_items"], [])
        self.assertEqual([item["type"] for item in payload["follow_ups"]], ["deep_sleep_highlight", "ticket_lane_summary", "workflow_suggestion"])
        self.assertEqual(payload["ticket_context"]["lane_counts"]["Backlog"], 1)
        self.assertEqual(payload["ticket_context"]["lane_counts"]["In Progress"], 1)
        surprise_detail = next(item["detail"] for item in payload["follow_ups"] if item["type"] == "workflow_suggestion")
        self.assertIn("3 notes", surprise_detail)
        self.assertIn("2 context files", surprise_detail)
        self.assertIn("1 deep-sleep input", surprise_detail)
        self.assertIn("Triage:", surprise_detail)
        self.assertIn("Benefit:", surprise_detail)
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("## Mission Control Ticket Context", report_text)
        self.assertIn("`MC-0103` `In Progress` `P2`", report_text)

    def test_surprise_me_workflow_suggestion_uses_follow_up_recommendation_and_reason(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
            last_success_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:05:00+00:00",
        )
        self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020500.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-020500",
                "status": "succeeded",
                "follow_up_recommendation": "Review `manual-loops-000089033` and decide whether to create a ticket or manual note.",
                "selected_idea": {
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "expected_benefit": "Save operator time on repeated low-risk review steps.",
                    "next_action": "proposal_only",
                    "rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                    "source_notes": ["00_Inbox/SESSION-20260326.md"],
                },
                "inputs": {
                    "context_files": ["START_HERE.md"],
                    "deep_sleep_inputs": [],
                },
            },
        )

        result = self._run("--job-name", "vault-staging-sync-daily", "--job-name", "surprise-me-workflow-improvement-nightly")
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        workflow_suggestion = next(item for item in payload["follow_ups"] if item["type"] == "workflow_suggestion")
        self.assertEqual(
            workflow_suggestion["title"],
            "Workflow suggestion: Create a short morning checklist for recurring manual review loops",
        )
        self.assertIn("Triage: Review `manual-loops-000089033` and decide whether to create a ticket or manual note.", workflow_suggestion["detail"])
        self.assertIn("Reason: Recent notes spend repeated effort on manual checks and re-reading.", workflow_suggestion["detail"])

    def test_surprise_me_workflow_suggestion_is_suppressed_when_registry_marks_it_discarded(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
            last_success_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-surprise",
            job_id="job-surprise",
            state="succeeded",
            scheduled_for="2026-03-26T02:00:00+00:00",
            started_at="2026-03-26T02:00:00+00:00",
            finished_at="2026-03-26T02:05:00+00:00",
        )
        self._write_sidecar(
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260326-020500.json",
            {
                "run_id": "SURPRISE-ME-RUN-20260326-020500",
                "status": "succeeded",
                "selected_idea": {
                    "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "expected_benefit": "Save operator time on repeated low-risk review steps.",
                    "next_action": "proposal_only",
                    "source_notes": [
                        "00_Inbox/SESSION-20260326.md",
                        "20_Work_Logs/LOG-20260326.md",
                    ],
                },
            },
        )
        self._write_surprise_me_registry(
            {
                "version": 1,
                "ideas": {
                    "manual-loop-reduction-recurring-manual-review-loops": {
                        "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                        "category": "manual_loop_reduction",
                        "title": "Create a short morning checklist for recurring manual review loops",
                        "review_status": "discarded",
                        "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                        "last_seen_run_id": "SURPRISE-ME-RUN-20260326-020500",
                        "first_seen_at": "2026-03-25T01:02:03+00:00",
                        "last_seen_at": "2026-03-26T02:05:00+00:00",
                        "times_seen": 2,
                        "times_selected": 1,
                        "latest_idea_id": "manual-loops-000089033",
                        "seen_idea_ids": ["manual-loops-000089033"],
                        "source_notes": [
                            "00_Inbox/SESSION-20260326.md",
                            "20_Work_Logs/LOG-20260326.md",
                        ],
                        "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                        "latest_next_action": "proposal_only",
                        "latest_rationale": "Recent notes keep repeating the same manual queue checks.",
                        "review_note": "Already covered by the current morning checklist.",
                        "reviewed_at": "2026-03-26T06:30:00+00:00",
                    }
                },
            }
        )

        result = self._run("--job-name", "surprise-me-workflow-improvement-nightly")
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "nothing_urgent")
        self.assertFalse(any(item["type"] == "workflow_suggestion" for item in payload["follow_ups"]))

    def test_run_marks_recently_blocked_ticket_updated_inside_window(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_ticket(
            ticket_key="MC-0096",
            title="Validate surprise-me Telegram delivery in host scheduler environment",
            lane="Blocked",
            updated_at="2026-03-26T18:00:00+00:00",
        )

        result = self._run("--job-name", "vault-staging-sync-daily")
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "attention_required")
        self.assertTrue(any(item["type"] == "recently_blocked_ticket" and item["run_id"] == "MC-0096" for item in payload["urgent_items"]))

    def test_ticket_context_spotlight_order_is_deterministic(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_run(
            run_id="run-sync",
            job_id="job-sync",
            state="succeeded",
            scheduled_for="2026-03-26T01:00:00+00:00",
            started_at="2026-03-26T01:00:00+00:00",
            finished_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_ticket(
            ticket_key="MC-0105",
            title="Old blocked ticket",
            lane="Blocked",
            updated_at="2026-03-26T10:00:00+00:00",
        )
        self._insert_ticket(
            ticket_key="MC-0106",
            title="Fresh blocked ticket",
            lane="Blocked",
            updated_at="2026-03-26T18:00:00+00:00",
        )
        self._insert_ticket(
            ticket_key="MC-0107",
            title="In-progress ticket",
            lane="In Progress",
            updated_at="2026-03-26T17:00:00+00:00",
        )
        self._insert_ticket(
            ticket_key="MC-0108",
            title="Ready ticket",
            lane="Ready",
            updated_at="2026-03-26T16:00:00+00:00",
        )

        result = self._run("--job-name", "vault-staging-sync-daily")
        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(
            [ticket["ticket_key"] for ticket in payload["ticket_context"]["spotlight_tickets"]],
            ["MC-0106", "MC-0105", "MC-0107", "MC-0108"],
        )

    def test_main_fails_closed_for_output_collision(self) -> None:
        runs_dir = self.staging_root / "00_Inbox/morning_triage/runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "MORNING-TRIAGE-RUN-20260326-070000.md").write_text("# existing\n", encoding="utf-8")
        (runs_dir / "MORNING-TRIAGE-RUN-20260326-070000.json").write_text("{}\n", encoding="utf-8")

        stdout = io.StringIO()
        with mock.patch.object(morning_triage_digest, "datetime", FrozenDateTime):
            with mock.patch("sys.argv", ["morning_triage_digest", "--db-path", str(self.db_path), "--staging-root", str(self.staging_root)]):
                with redirect_stdout(stdout):
                    exit_code = morning_triage_digest.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("collision", payload["error"].lower())

    def test_main_fails_closed_for_unsafe_sidecar_path(self) -> None:
        self._insert_job(
            job_id="job-sync",
            name="vault-staging-sync-daily",
            last_run_at="2026-03-26T01:05:00+00:00",
            last_success_at="2026-03-26T01:05:00+00:00",
        )
        self._insert_job(
            job_id="job-deep",
            name="deep-sleep-link-synthesis-nightly",
            last_run_at="2026-03-26T01:35:00+00:00",
            last_success_at="2026-03-26T01:35:00+00:00",
        )
        self._insert_job(
            job_id="job-surprise",
            name="surprise-me-workflow-improvement-nightly",
            last_run_at="2026-03-26T02:05:00+00:00",
            last_success_at="2026-03-26T02:05:00+00:00",
        )
        self._insert_run(
            run_id="run-deep",
            job_id="job-deep",
            state="succeeded",
            scheduled_for="2026-03-26T01:30:00+00:00",
            started_at="2026-03-26T01:30:00+00:00",
            finished_at="2026-03-26T01:35:00+00:00",
        )
        outside = self.root / "outside.json"
        outside.write_text("{\"run_id\": \"outside\"}\n", encoding="utf-8")
        link_path = self.staging_root / "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-013500.json"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(outside)

        stdout = io.StringIO()
        with mock.patch.object(morning_triage_digest, "datetime", FrozenDateTime):
            with mock.patch("sys.argv", ["morning_triage_digest", "--db-path", str(self.db_path), "--staging-root", str(self.staging_root)]):
                with redirect_stdout(stdout):
                    exit_code = morning_triage_digest.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("unsafe", payload["error"].lower())


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

    def _write_sidecar(self, name: str, *, status: str = "attention_required") -> Path:
        sidecar_path = self.runs_dir / name
        report_path = sidecar_path.with_suffix(".md")
        payload = {
            "run_id": sidecar_path.stem,
            "status": status,
            "jobs_considered": 3,
            "ticket_context": {
                "total_open": 2,
                "lane_counts": {"In Progress": 1, "Blocked": 1},
                "spotlight_tickets": [{"ticket_key": "MC-0101"}, {"ticket_key": "MC-0102"}],
            },
            "urgent_items": [
                {
                    "title": "surprise-me-workflow-improvement-nightly timed_out",
                    "detail": "Run `run-surprise` timed_out.",
                    "severity": "high",
                    "type": "timed_out_run",
                }
            ]
            if status == "attention_required"
            else [],
            "follow_ups": [{"title": "Mission Control ticket context", "detail": "2 open ticket(s)"}],
            "outputs": {
                "run_report": report_path.as_posix(),
                "json_sidecar": sidecar_path.as_posix(),
            },
        }
        sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report_path.write_text("# report\n", encoding="utf-8")
        return sidecar_path

    def test_run_uses_latest_sidecar_and_sends_message(self) -> None:
        self._write_sidecar("MORNING-TRIAGE-RUN-20260326-070000.json", status="nothing_urgent")
        latest = self._write_sidecar("MORNING-TRIAGE-RUN-20260326-080000.json")
        args = morning_triage_telegram_notifier.build_parser().parse_args(
            ["--staging-root", str(self.staging_root), "--chat-id", "5946121750"]
        )
        factory = _TelegramClientFactory()
        with mock.patch.dict("os.environ", {"CHANNEL_TOKEN": "token-123"}, clear=False):
            payload = morning_triage_telegram_notifier.run(args, api_client_factory=factory)

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["sidecar_path"], latest.as_posix())
        self.assertEqual(factory.instances[0].token, "token-123")
        message = factory.instances[0].sent_messages[0]["text"]
        self.assertIn("Morning triage: attention required.", message)
        self.assertIn("Urgent:", message)
        self.assertIn("surprise-me-workflow-improvement-nightly timed_out", message)
        self.assertIn(latest.with_suffix(".md").as_posix(), message)

    def test_main_can_downgrade_send_failure_when_flag_enabled(self) -> None:
        self._write_sidecar("MORNING-TRIAGE-RUN-20260326-080000.json")

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
        with mock.patch.dict("os.environ", {"MORNING_TRIAGE_TELEGRAM_BOT_TOKEN": "token-123"}, clear=False):
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
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260326-090000.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "attention_required",
            "jobs_considered": 3,
            "urgent_items": 1,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(run_morning_triage_digest.morning_triage_digest, "run", return_value=worker_payload):
            with mock.patch.object(
                run_morning_triage_digest.morning_triage_telegram_notifier,
                "run",
                side_effect=run_morning_triage_digest.morning_triage_telegram_notifier.MorningTriageNotifierError(
                    "token missing"
                ),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_morning_triage_digest.main(
                        [
                            "--notify-chat-id",
                            "5946121750",
                            "--staging-root",
                            str(self.staging_root),
                            "--db-path",
                            str(self.root / "mission_control.sqlite"),
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["worker"]["status"], "attention_required")
        self.assertEqual(payload["notification"]["status"], "failed")
        self.assertIn("morning-triage notifier failed", stderr.getvalue())

    def test_wrapper_uses_exact_worker_sidecar_for_notification(self) -> None:
        sidecar = self._write_sidecar("MORNING-TRIAGE-RUN-20260326-091500.json")
        worker_payload = {
            "run_id": sidecar.stem,
            "status": "nothing_urgent",
            "jobs_considered": 3,
            "urgent_items": 0,
            "output_file": sidecar.with_suffix(".md").as_posix(),
        }
        captured: dict[str, str] = {}

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
                            "--staging-root",
                            str(self.staging_root),
                            "--db-path",
                            str(self.root / "mission_control.sqlite"),
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["chat_id"], "5946121750")
        self.assertEqual(captured["sidecar_path"], sidecar.as_posix())


if __name__ == "__main__":
    unittest.main()
