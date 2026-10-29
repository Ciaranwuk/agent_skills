import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import vault_staging_sync
from scripts.vault_staging_sync import EMPTY_STAGING_INDEX, sync_vault_staging


class VaultStagingSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.staging_root = self.root / "_vault_staging"
        self.canonical_root = self.root / "llm_work_record"
        self.staging_root.mkdir()
        self.canonical_root.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write(self, root: Path, rel_path: str, content: str) -> Path:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_copies_new_files_and_prunes_staging(self) -> None:
        self._write(self.staging_root, "10_Decisions/DEC-1.md", "# decision\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertTrue((self.canonical_root / "10_Decisions/DEC-1.md").is_file())
        self.assertEqual(summary.copied, ["10_Decisions/DEC-1.md", "INDEX.md"])
        self.assertFalse((self.staging_root / "10_Decisions/DEC-1.md").exists())
        self.assertEqual((self.staging_root / "INDEX.md").read_text(encoding="utf-8"), EMPTY_STAGING_INDEX)

    def test_merges_session_note_additively_once(self) -> None:
        self._write(self.canonical_root, "00_Inbox/SESSION-20260319.md", "canonical\n")
        staged = self._write(self.staging_root, "00_Inbox/SESSION-20260319.md", "staged\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        first = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune_staging=False,
            reset_staging_index=False,
        )
        second = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune_staging=False,
            reset_staging_index=False,
        )

        merged_content = (self.canonical_root / "00_Inbox/SESSION-20260319.md").read_text(encoding="utf-8")
        self.assertIn("canonical", merged_content)
        self.assertIn("staged", merged_content)
        self.assertEqual(first.merged, ["00_Inbox/SESSION-20260319.md"])
        self.assertIn("00_Inbox/SESSION-20260319.md", second.skipped)
        self.assertTrue(staged.exists())

    def test_merges_missing_index_rows_without_duplicates(self) -> None:
        canonical = """# Work Session Index

## Sessions
| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-03-18 | one | kept | log | none | path | tag |
"""
        staged = """# Work Session Index

## Sessions
| Date | Project | Session Summary | Work Log | Decisions | Code Links | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-03-18 | one | kept | log | none | path | tag |
| 2026-03-19 | two | added | log | none | path | tag |
"""
        self._write(self.canonical_root, "INDEX.md", canonical)
        self._write(self.staging_root, "INDEX.md", staged)

        sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune_staging=False,
            reset_staging_index=False,
        )
        sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
            prune_staging=False,
            reset_staging_index=False,
        )

        merged = (self.canonical_root / "INDEX.md").read_text(encoding="utf-8")
        self.assertEqual(merged.count("| 2026-03-19 | two | added | log | none | path | tag |"), 1)

    def test_raises_on_non_mergeable_conflict(self) -> None:
        self._write(self.canonical_root, "30_Projects/PROJ-1.md", "canonical\n")
        self._write(self.staging_root, "30_Projects/PROJ-1.md", "staged\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        with self.assertRaisesRegex(Exception, "conflicting staged files require manual review"):
            sync_vault_staging(
                staging_root=self.staging_root,
                canonical_root=self.canonical_root,
            )

    def test_merges_decision_note_by_frontmatter_and_sections(self) -> None:
        canonical = """---
type: decision
date: 2026-04-06
project: mission-control
status: accepted
tags: [decision, surprise-me, workflows, registry, staging, overlay]
---

# DEC-20260406-surprise-me-current-workflows-context-and-overlay-registry-fallback

## Context
- old context

## Decision
- keep existing rule

## Rationale
- old rationale

## Consequences
- old consequences
"""
        staged = """---
type: decision
date: 2026-04-06
updated: 2026-04-13
project: mission-control
status: accepted
tags: [decision, surprise-me, workflows, registry, staging, overlay]
---

# DEC-20260406-surprise-me-current-workflows-context-and-overlay-registry-fallback

## Context
- updated context

## Decision
- keep existing rule
- add staged-first precedence

## Rationale
- updated rationale

## Consequences
- updated consequences
"""
        rel_path = "10_Decisions/DEC-20260406-surprise-me-current-workflows-context-and-overlay-registry-fallback.md"
        self._write(self.canonical_root, rel_path, canonical)
        self._write(self.staging_root, rel_path, staged)
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(summary.merged, [rel_path])
        merged = (self.canonical_root / rel_path).read_text(encoding="utf-8")
        self.assertIn("updated: 2026-04-13", merged)
        self.assertIn("- updated context", merged)
        self.assertIn("- add staged-first precedence", merged)
        self.assertNotIn("- old rationale", merged)

    def test_merges_howto_note_and_inserts_staged_only_section_near_related_heading(self) -> None:
        canonical = """---
type: guide
date: 2026-04-06
project: mission-control
tags: [workflows, mission-control, operations]
---

# Current Workflows

## Owned Workflows

### Morning Digest
- Owner: `morning-triage-digest-nightly`
- Covers: overnight check summary
- Already covered examples: morning checklist for recurring manual review loops

## Maintenance Rule
- Add a short entry here.
"""
        staged = """---
type: guide
date: 2026-04-13
project: mission-control
tags: [workflows, mission-control, operations, surprise-me]
---

# Current Workflows

## Owned Workflows

### Morning Digest
- Owner: `morning-triage-digest-nightly`
- Applies to categories: `manual_loop_reduction`
- Covers: overnight check summary
- Already covered examples: recurring morning review helper suggestions

### Decision Follow-Through
- Owner: `current-workflows`
- Applies to categories: `deep_sleep_follow_through`
- Covers: update the authoritative workflow note

## Maintenance Rule
- Add a short entry here.
- Add or update `Applies to categories`.
"""
        rel_path = "60_How_To/HOWTO-current-workflows.md"
        self._write(self.canonical_root, rel_path, canonical)
        self._write(self.staging_root, rel_path, staged)
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(summary.merged, [rel_path])
        merged = (self.canonical_root / rel_path).read_text(encoding="utf-8")
        self.assertIn("date: 2026-04-13", merged)
        self.assertIn("surprise-me", merged)
        self.assertIn("### Decision Follow-Through", merged)
        self.assertLess(merged.index("### Morning Digest"), merged.index("### Decision Follow-Through"))
        self.assertLess(merged.index("### Decision Follow-Through"), merged.index("## Maintenance Rule"))

    def test_retries_permission_error_when_writing(self) -> None:
        target = self._write(self.canonical_root, "00_Inbox/SESSION-20260319.md", "canonical\n")
        self._write(self.staging_root, "00_Inbox/SESSION-20260319.md", "staged\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        real_write = Path.write_text
        call_count = {"count": 0}

        def flaky_write(path_obj: Path, content: str, encoding: str = "utf-8", **kwargs: object) -> int:
            if path_obj == target and call_count["count"] == 0:
                call_count["count"] += 1
                raise PermissionError("transient lock")
            return real_write(path_obj, content, encoding=encoding, **kwargs)

        with mock.patch.object(vault_staging_sync, "WRITE_RETRY_DELAY_SECONDS", 0.0):
            with mock.patch.object(Path, "write_text", autospec=True, side_effect=flaky_write):
                summary = sync_vault_staging(
                    staging_root=self.staging_root,
                    canonical_root=self.canonical_root,
                    prune_staging=False,
                    reset_staging_index=False,
                )

        self.assertEqual(summary.merged, ["00_Inbox/SESSION-20260319.md"])
        self.assertEqual(call_count["count"], 1)

    def test_merges_surprise_me_idea_registry_without_conflict(self) -> None:
        canonical = {
            "version": 1,
            "updated_at": "2026-03-30T01:00:00+00:00",
            "counts": {"total": 1, "unreviewed": 0, "approved": 1, "discarded": 0},
            "ideas": {
                "manual-loop-reduction-recurring-manual-review-loops": {
                    "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "category": "manual_loop_reduction",
                    "review_status": "approved",
                    "review_note": "accepted",
                    "reviewed_at": "2026-03-30T08:00:00+00:00",
                    "first_seen_at": "2026-03-29T01:00:00+00:00",
                    "last_seen_at": "2026-03-30T01:00:00+00:00",
                    "first_seen_run_id": "run-old",
                    "last_seen_run_id": "run-canonical",
                    "times_seen": 2,
                    "times_selected": 1,
                    "latest_idea_id": "manual-loops-1",
                    "seen_idea_ids": ["manual-loops-1"],
                    "source_notes": ["00_Inbox/SESSION-20260329.md"],
                    "latest_expected_benefit": "Save operator time.",
                    "latest_next_action": "proposal_only",
                    "latest_rationale": "Repeated manual checks.",
                }
            },
        }
        staged = {
            "version": 1,
            "updated_at": "2026-03-31T01:00:00+00:00",
            "counts": {"total": 2, "unreviewed": 2, "approved": 0, "discarded": 0},
            "ideas": {
                "manual-loop-reduction-recurring-manual-review-loops": {
                    "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "category": "manual_loop_reduction",
                    "review_status": "unreviewed",
                    "review_note": "",
                    "reviewed_at": "",
                    "first_seen_at": "2026-03-29T01:00:00+00:00",
                    "last_seen_at": "2026-03-31T01:00:00+00:00",
                    "first_seen_run_id": "run-old",
                    "last_seen_run_id": "run-staged",
                    "times_seen": 3,
                    "times_selected": 2,
                    "latest_idea_id": "manual-loops-2",
                    "seen_idea_ids": ["manual-loops-1", "manual-loops-2"],
                    "source_notes": ["00_Inbox/SESSION-20260330.md"],
                    "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                    "latest_next_action": "proposal_only",
                    "latest_rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                },
                "deep-sleep-follow-through": {
                    "idea_key": "deep-sleep-follow-through",
                    "title": "Turn recent deep-sleep review into one reusable checklist",
                    "category": "deep_sleep_follow_through",
                    "review_status": "unreviewed",
                    "review_note": "",
                    "reviewed_at": "",
                    "first_seen_at": "2026-03-31T01:00:00+00:00",
                    "last_seen_at": "2026-03-31T01:00:00+00:00",
                    "first_seen_run_id": "run-staged",
                    "last_seen_run_id": "run-staged",
                    "times_seen": 1,
                    "times_selected": 0,
                    "latest_idea_id": "deep-sleep-1",
                    "seen_idea_ids": ["deep-sleep-1"],
                    "source_notes": ["00_Inbox/SESSION-20260331.md"],
                    "latest_expected_benefit": "Reduce time spent interpreting repeated synthesis outputs.",
                    "latest_next_action": "ticket_recommendation",
                    "latest_rationale": "Deep-sleep surfaced a repeatable connection.",
                },
            },
        }
        self._write(self.canonical_root, "00_Inbox/surprise_me/idea_registry.json", json.dumps(canonical, indent=2, sort_keys=True) + "\n")
        self._write(self.staging_root, "00_Inbox/surprise_me/idea_registry.json", json.dumps(staged, indent=2, sort_keys=True) + "\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(summary.merged, ["00_Inbox/surprise_me/idea_registry.json"])
        merged = json.loads((self.canonical_root / "00_Inbox/surprise_me/idea_registry.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["counts"]["total"], 2)
        self.assertEqual(merged["counts"]["approved"], 1)
        self.assertEqual(merged["counts"]["unreviewed"], 1)
        manual = merged["ideas"]["manual-loop-reduction-recurring-manual-review-loops"]
        self.assertEqual(manual["review_status"], "approved")
        self.assertEqual(manual["times_seen"], 3)
        self.assertEqual(manual["times_selected"], 2)
        self.assertEqual(manual["last_seen_run_id"], "run-staged")
        self.assertIn("manual-loops-2", manual["seen_idea_ids"])
        self.assertFalse((self.staging_root / "00_Inbox/surprise_me/idea_registry.json").exists())

    def test_merges_surprise_me_idea_registry_overlay_without_conflict(self) -> None:
        canonical = {
            "version": 1,
            "updated_at": "2026-04-07T13:00:00+01:00",
            "counts": {"total": 2, "unreviewed": 1, "approved": 0, "discarded": 1},
            "ideas": {
                "manual-loop-reduction-recurring-manual-review-loops": {
                    "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "category": "manual_loop_reduction",
                    "review_status": "discarded",
                    "review_note": "Morning digest already covers this workflow.",
                    "reviewed_at": "2026-04-06T09:00:00+01:00",
                    "first_seen_at": "2026-03-29T01:00:00+00:00",
                    "last_seen_at": "2026-04-06T01:00:00+00:00",
                    "first_seen_run_id": "run-old",
                    "last_seen_run_id": "run-canonical",
                    "times_seen": 6,
                    "times_selected": 0,
                    "latest_idea_id": "manual-loops-6",
                    "seen_idea_ids": ["manual-loops-1", "manual-loops-6"],
                    "source_notes": ["00_Inbox/SESSION-20260406.md"],
                    "latest_expected_benefit": "Avoid duplicate review workflows.",
                    "latest_next_action": "proposal_only",
                    "latest_rationale": "The digest already owns the overnight review loop.",
                },
                "deep-sleep-follow-through": {
                    "idea_key": "deep-sleep-follow-through",
                    "title": "Turn recent deep-sleep review into one reusable checklist",
                    "category": "deep_sleep_follow_through",
                    "review_status": "approved",
                    "review_note": "Worth keeping in the backlog.",
                    "reviewed_at": "2026-04-06T09:30:00+01:00",
                    "first_seen_at": "2026-04-06T01:00:00+00:00",
                    "last_seen_at": "2026-04-06T01:00:00+00:00",
                    "first_seen_run_id": "run-canonical",
                    "last_seen_run_id": "run-canonical",
                    "times_seen": 1,
                    "times_selected": 0,
                    "latest_idea_id": "deep-sleep-1",
                    "seen_idea_ids": ["deep-sleep-1"],
                    "source_notes": ["00_Inbox/SESSION-20260406.md"],
                    "latest_expected_benefit": "Reduce synthesis follow-through friction.",
                    "latest_next_action": "ticket_recommendation",
                    "latest_rationale": "Deep-sleep surfaced a repeatable connection.",
                },
            },
        }
        staged = {
            "version": 1,
            "updated_at": "2026-04-07T14:25:00+01:00",
            "counts": {"total": 2, "unreviewed": 1, "approved": 0, "discarded": 1},
            "ideas": {
                "manual-loop-reduction-recurring-manual-review-loops": {
                    "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    "title": "Create a short morning checklist for recurring manual review loops",
                    "category": "manual_loop_reduction",
                    "review_status": "discarded",
                    "review_note": "Redundant with the existing morning digest workflow.",
                    "reviewed_at": "2026-04-07T14:25:00+01:00",
                    "first_seen_at": "2026-03-29T01:00:00+00:00",
                    "last_seen_at": "2026-04-07T01:00:05+00:00",
                    "first_seen_run_id": "run-old",
                    "last_seen_run_id": "run-staged",
                    "times_seen": 8,
                    "times_selected": 0,
                    "latest_idea_id": "manual-loops-8",
                    "seen_idea_ids": ["manual-loops-1", "manual-loops-6", "manual-loops-8"],
                    "source_notes": ["00_Inbox/SESSION-20260407.md"],
                    "latest_expected_benefit": "Avoid duplicate review workflows.",
                    "latest_next_action": "proposal_only",
                    "latest_rationale": "Morning digest already owns this workflow.",
                },
                "deep-sleep-follow-through": {
                    "idea_key": "deep-sleep-follow-through",
                    "title": "Turn recent deep-sleep review into one reusable checklist",
                    "category": "deep_sleep_follow_through",
                    "review_status": "unreviewed",
                    "review_note": "",
                    "reviewed_at": "",
                    "first_seen_at": "2026-04-06T01:00:00+00:00",
                    "last_seen_at": "2026-04-07T01:00:05+00:00",
                    "first_seen_run_id": "run-canonical",
                    "last_seen_run_id": "run-staged",
                    "times_seen": 3,
                    "times_selected": 1,
                    "latest_idea_id": "deep-sleep-2",
                    "seen_idea_ids": ["deep-sleep-1", "deep-sleep-2"],
                    "source_notes": ["00_Inbox/SESSION-20260407.md"],
                    "latest_expected_benefit": "Reduce synthesis follow-through friction.",
                    "latest_next_action": "ticket_recommendation",
                    "latest_rationale": "A second pass found the same repeatable connection.",
                },
                "vault-staging-sync-daily": {
                    "idea_key": "vault-staging-sync-daily",
                    "title": "Run the vault staging sync daily after morning triage",
                    "category": "workflow_hardening",
                    "review_status": "discarded",
                    "review_note": "Covered by the existing sync cron.",
                    "reviewed_at": "2026-04-07T14:25:00+01:00",
                    "first_seen_at": "2026-04-07T01:00:05+00:00",
                    "last_seen_at": "2026-04-07T01:00:05+00:00",
                    "first_seen_run_id": "run-staged",
                    "last_seen_run_id": "run-staged",
                    "times_seen": 1,
                    "times_selected": 0,
                    "latest_idea_id": "vault-sync-1",
                    "seen_idea_ids": ["vault-sync-1"],
                    "source_notes": ["00_Inbox/SESSION-20260407.md"],
                    "latest_expected_benefit": "Reduce stale staged content.",
                    "latest_next_action": "proposal_only",
                    "latest_rationale": "The sync job already exists, so this suggestion is redundant.",
                },
            },
        }
        rel_path = "00_Inbox/surprise_me/idea_registry.overlay.json"
        self._write(self.canonical_root, rel_path, json.dumps(canonical, indent=2, sort_keys=True) + "\n")
        self._write(self.staging_root, rel_path, json.dumps(staged, indent=2, sort_keys=True) + "\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(summary.merged, [rel_path])
        merged = json.loads((self.canonical_root / rel_path).read_text(encoding="utf-8"))
        self.assertEqual(merged["counts"]["total"], 3)
        self.assertEqual(merged["counts"]["approved"], 1)
        self.assertEqual(merged["counts"]["discarded"], 2)
        self.assertEqual(merged["counts"]["unreviewed"], 0)
        manual = merged["ideas"]["manual-loop-reduction-recurring-manual-review-loops"]
        self.assertEqual(manual["review_status"], "discarded")
        self.assertEqual(manual["review_note"], "Redundant with the existing morning digest workflow.")
        self.assertEqual(manual["times_seen"], 8)
        self.assertEqual(manual["last_seen_run_id"], "run-staged")
        self.assertIn("manual-loops-8", manual["seen_idea_ids"])
        deep_sleep = merged["ideas"]["deep-sleep-follow-through"]
        self.assertEqual(deep_sleep["review_status"], "approved")
        self.assertEqual(deep_sleep["review_note"], "Worth keeping in the backlog.")
        self.assertEqual(deep_sleep["reviewed_at"], "2026-04-06T09:30:00+01:00")
        self.assertEqual(deep_sleep["times_seen"], 3)
        self.assertEqual(deep_sleep["times_selected"], 1)
        self.assertEqual(deep_sleep["last_seen_run_id"], "run-staged")
        self.assertIn("deep-sleep-2", deep_sleep["seen_idea_ids"])
        self.assertIn("vault-staging-sync-daily", merged["ideas"])
        self.assertFalse((self.staging_root / rel_path).exists())

    def test_keeps_surprise_me_run_artifacts_in_staging(self) -> None:
        staged_run = self._write(
            self.staging_root,
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260331-010000.json",
            json.dumps({"run_id": "SURPRISE-ME-RUN-20260331-010000", "status": "succeeded"}, indent=2, sort_keys=True) + "\n",
        )
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertIn("00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260331-010000.json", summary.skipped)
        self.assertTrue(staged_run.exists())
        self.assertFalse((self.canonical_root / "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260331-010000.json").exists())

    def test_keeps_all_automated_run_artifacts_in_staging(self) -> None:
        run_paths = [
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260331-010000.md",
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260331-010000.json",
            "00_Inbox/morning_triage/runs/MORNING-TRIAGE-RUN-20260331-010000.md",
            "00_Inbox/morning_triage/runs/MORNING-TRIAGE-RUN-20260331-010000.json",
            "00_Inbox/workflow_experiments/runs/WORKFLOW-EXPERIMENT-RUN-20260331-010000.md",
            "00_Inbox/workflow_experiments/runs/WORKFLOW-EXPERIMENT-RUN-20260331-010000.json",
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260331-010000.md",
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260331-010000.json",
        ]
        for rel_path in run_paths:
            self._write(self.staging_root, rel_path, "automated run artifact\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(sorted(summary.skipped), sorted(run_paths))
        for rel_path in run_paths:
            self.assertTrue((self.staging_root / rel_path).exists())
            self.assertFalse((self.canonical_root / rel_path).exists())

    def test_pruning_retains_all_automated_run_artifacts(self) -> None:
        run_paths = [
            "00_Inbox/deep_sleep/runs/run.md",
            "00_Inbox/morning_triage/runs/run.json",
            "00_Inbox/workflow_experiments/runs/run.md",
            "00_Inbox/surprise_me/runs/run.json",
        ]
        for rel_path in run_paths:
            self._write(self.staging_root, rel_path, "automated run artifact\n")
        self._write(self.staging_root, "10_Decisions/DEC-1.md", "# decision\n")
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        summary = sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.canonical_root,
        )

        self.assertEqual(summary.pruned, ["10_Decisions/DEC-1.md"])
        for rel_path in run_paths:
            self.assertTrue((self.staging_root / rel_path).exists())
            self.assertFalse((self.canonical_root / rel_path).exists())


if __name__ == "__main__":
    unittest.main()
