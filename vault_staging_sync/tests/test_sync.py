from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vault_staging_sync.cli import main as cli_main
from vault_staging_sync.sync import EMPTY_STAGING_INDEX, sync_staging_to_vault


class VaultStagingSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging_root = self.root / "_vault_staging"
        self.canonical_root = self.root / "canonical"
        self.staging_root.mkdir()
        self.canonical_root.mkdir()
        (self.staging_root / "INDEX.md").write_text(EMPTY_STAGING_INDEX, encoding="utf-8")
        (self.canonical_root / "INDEX.md").write_text(EMPTY_STAGING_INDEX, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_copies_new_non_mergeable_file_without_touching_staging_by_default(self) -> None:
        staged_file = self.staging_root / "30_Projects" / "artifact.md"
        staged_file.parent.mkdir(parents=True)
        staged_file.write_text("payload\n", encoding="utf-8")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["collisions"], [])
        self.assertEqual((self.canonical_root / "30_Projects" / "artifact.md").read_text(encoding="utf-8"), "payload\n")
        self.assertTrue(staged_file.exists())
        self.assertIn(
            {"path": "30_Projects/artifact.md", "action": "copied", "detail": None},
            payload["actions"],
        )

    def test_merges_session_note_additively_and_idempotently(self) -> None:
        relative_path = Path("30_Projects/SESSION-20260319.md")
        staged_file = self.staging_root / relative_path
        canonical_file = self.canonical_root / relative_path
        staged_file.parent.mkdir(parents=True)
        canonical_file.parent.mkdir(parents=True)
        canonical_file.write_text("# Session\nCanonical body\n", encoding="utf-8")
        staged_file.write_text("# Session\nStaged body\n", encoding="utf-8")

        first = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )
        second = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        merged_text = canonical_file.read_text(encoding="utf-8")
        self.assertIn("Canonical body", merged_text)
        self.assertIn("Staged body", merged_text)
        self.assertEqual(merged_text.count("Staged body"), 1)
        self.assertIn("vault-staging-sync merge", merged_text)
        self.assertIn(
            {"path": "30_Projects/SESSION-20260319.md", "action": "merged", "detail": "appended staged markdown as an idempotent merge block"},
            first["actions"],
        )
        self.assertIn(
            {"path": "30_Projects/SESSION-20260319.md", "action": "unchanged", "detail": None},
            second["actions"],
        )

    def test_merges_index_rows_without_overwriting_existing_rows(self) -> None:
        canonical_index = """# Work Session Index

## Sessions
| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-03-18 | core | canonical row | 20_Work_Logs/LOG-20260318.md | none | /repo/a.py | alpha |
"""
        staged_index = """# Work Session Index

## Sessions
| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-03-18 | core | canonical row | 20_Work_Logs/LOG-20260318.md | none | /repo/a.py | alpha |
| 2026-03-19 | core | staged row | 20_Work_Logs/LOG-20260319.md | none | /repo/b.py | beta |
"""
        (self.canonical_root / "INDEX.md").write_text(canonical_index, encoding="utf-8")
        (self.staging_root / "INDEX.md").write_text(staged_index, encoding="utf-8")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        merged_text = (self.canonical_root / "INDEX.md").read_text(encoding="utf-8")
        self.assertEqual(merged_text.count("canonical row"), 1)
        self.assertEqual(merged_text.count("staged row"), 1)
        self.assertEqual(payload["collisions"], [])
        self.assertIn(
            {"path": "INDEX.md", "action": "merged", "detail": "merged index rows additively without overwriting canonical rows"},
            payload["actions"],
        )

    def test_prune_resets_staging_index_when_only_index_remains(self) -> None:
        staged_file = self.staging_root / "20_Work_Logs" / "LOG-20260319.md"
        staged_file.parent.mkdir(parents=True)
        staged_file.write_text("log body\n", encoding="utf-8")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune=True,
        )

        self.assertFalse(staged_file.exists())
        self.assertEqual((self.staging_root / "INDEX.md").read_text(encoding="utf-8"), EMPTY_STAGING_INDEX)
        self.assertEqual(payload["pruned"], ["20_Work_Logs/LOG-20260319.md"])

    def test_keeps_surprise_me_run_artifacts_in_staging_when_pruning(self) -> None:
        staged_file = self.staging_root / "00_Inbox" / "surprise_me" / "runs" / "SURPRISE-ME-RUN-20260319-010000.json"
        staged_file.parent.mkdir(parents=True)
        staged_file.write_text('{"run_id":"SURPRISE-ME-RUN-20260319-010000"}\n', encoding="utf-8")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune=True,
        )

        self.assertTrue(staged_file.exists())
        self.assertFalse((self.canonical_root / "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260319-010000.json").exists())
        self.assertIn(
            {"path": "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260319-010000.json", "action": "retained", "detail": "artifact remains staging-owned"},
            payload["actions"],
        )
        self.assertEqual(payload["pruned"], [])
        self.assertEqual((self.staging_root / "INDEX.md").read_text(encoding="utf-8"), EMPTY_STAGING_INDEX)

    def test_keeps_all_automated_run_artifacts_in_staging_when_pruning(self) -> None:
        run_paths = [
            Path("00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260319-010000.md"),
            Path("00_Inbox/morning_triage/runs/MORNING-TRIAGE-RUN-20260319-010000.json"),
            Path("00_Inbox/workflow_experiments/runs/WORKFLOW-EXPERIMENT-RUN-20260319-010000.md"),
            Path("00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260319-010000.json"),
        ]
        for relative_path in run_paths:
            staged_file = self.staging_root / relative_path
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            staged_file.write_text("automated run artifact\n", encoding="utf-8")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune=True,
        )

        self.assertEqual(payload["pruned"], [])
        for relative_path in run_paths:
            self.assertTrue((self.staging_root / relative_path).exists())
            self.assertFalse((self.canonical_root / relative_path).exists())
            self.assertIn(
                {"path": str(relative_path), "action": "retained", "detail": "artifact remains staging-owned"},
                payload["actions"],
            )

    def test_collision_leaves_canonical_and_staged_files_unchanged(self) -> None:
        relative_path = Path("attachments/blob.bin")
        staged_file = self.staging_root / relative_path
        canonical_file = self.canonical_root / relative_path
        staged_file.parent.mkdir(parents=True)
        canonical_file.parent.mkdir(parents=True)
        staged_file.write_bytes(b"staged")
        canonical_file.write_bytes(b"canonical")

        payload = sync_staging_to_vault(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune=True,
        )

        self.assertEqual(canonical_file.read_bytes(), b"canonical")
        self.assertEqual(staged_file.read_bytes(), b"staged")
        self.assertEqual(payload["collisions"], ["attachments/blob.bin"])
        self.assertEqual(payload["pruned"], [])

    def test_cli_fail_on_collision_returns_non_zero(self) -> None:
        relative_path = Path("attachments/blob.bin")
        staged_file = self.staging_root / relative_path
        canonical_file = self.canonical_root / relative_path
        staged_file.parent.mkdir(parents=True)
        canonical_file.parent.mkdir(parents=True)
        staged_file.write_bytes(b"staged")
        canonical_file.write_bytes(b"canonical")

        argv = [
            "vault_staging_sync",
            "--staging-root",
            str(self.staging_root),
            "--canonical-root",
            str(self.canonical_root),
            "--fail-on-collision",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", new_callable=StringIO) as stdout:
            exit_code = cli_main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertEqual(payload["collisions"], ["attachments/blob.bin"])


class PromoteScriptTests(unittest.TestCase):
    def test_wrapper_script_is_executable(self) -> None:
        script_path = Path("/home/cwilson/projects/agent_skills/scripts/promote_vault_staging.py")
        mode = script_path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
