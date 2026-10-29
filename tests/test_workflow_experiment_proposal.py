import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import workflow_experiment_proposal


class FrozenDateTime(datetime):
    current = datetime(2026, 7, 14, 1, 45, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value


class WorkflowExperimentProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.vault_root = self.root / "vault"
        self.staging_root = self.root / "_vault_staging"
        self.vault_root.mkdir()
        self.staging_root.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write_vault(self, rel_path: str, content: str) -> Path:
        path = self.vault_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _args(self, *extra: str):
        return workflow_experiment_proposal.build_parser().parse_args(
            [
                "--vault-root",
                str(self.vault_root),
                "--staging-root",
                str(self.staging_root),
                *extra,
            ]
        )

    def test_run_writes_markdown_and_json_sidecar_for_read_only_proposal(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260714.md",
            "# SESSION-20260714\n\nReviewed the same manual Telegram restart check again before morning triage.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260714.md",
            "# LOG-20260714\n\nVerified the manual Telegram restart check again and noted repeated review friction.\n",
        )

        args = self._args("--lookback-days", "1", "--include-sessions", "--include-work-logs")
        with mock.patch.object(workflow_experiment_proposal, "datetime", FrozenDateTime):
            result = workflow_experiment_proposal.run(args)

        self.assertEqual(result["status"], "succeeded")
        report_path = Path(str(result["output_file"]))
        sidecar_path = report_path.with_suffix(".json")
        self.assertTrue(report_path.is_file())
        self.assertTrue(sidecar_path.is_file())
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "WORKFLOW-EXPERIMENT-RUN-20260714-014500")
        self.assertIn("read-only proposal artifact", payload["selected_experiment"]["safety_boundary"])
        self.assertEqual(payload["workflow_suggestion"]["contract_version"], "1.0")
        self.assertEqual(payload["workflow_suggestion"]["lane"], "operational_workflow")
        self.assertEqual(
            payload["workflow_suggestion"]["diagnostics"]["input_scope"],
            "local_notes_only",
        )
        self.assertEqual(
            payload["selected_experiment"]["source_notes"],
            ["00_Inbox/SESSION-20260714.md", "20_Work_Logs/LOG-20260714.md"],
        )
        self.assertIn("## Proposal", report_path.read_text(encoding="utf-8"))

    def test_context_file_must_stay_under_vault_root(self) -> None:
        args = self._args("--include-sessions", "--context-file", "../outside.md")

        with self.assertRaises(workflow_experiment_proposal.WorkflowExperimentError):
            workflow_experiment_proposal.run(args)

    def test_direct_script_entry_point_resolves_package_import(self) -> None:
        script_path = Path(workflow_experiment_proposal.__file__).resolve()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=script_path.parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: workflow_experiment_proposal", completed.stdout)


if __name__ == "__main__":
    unittest.main()
