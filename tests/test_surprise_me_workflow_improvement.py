import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from memory_system.index import MemoryIndex
from scripts import surprise_me_idea_registry, surprise_me_workflow_improvement
from scripts.vault_staging_sync import sync_vault_staging


class FrozenDateTime(datetime):
    current = datetime(2026, 3, 26, 1, 2, 3, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value


class SurpriseMeWorkflowImprovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.vault_root = self.root / "vault"
        self.staging_root = self.root / "_vault_staging"
        self.vault_root.mkdir()
        self.staging_root.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write(self, root: Path, rel_path: str, content: str) -> Path:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_vault(self, rel_path: str, content: str) -> Path:
        return self._write(self.vault_root, rel_path, content)

    def _args(self, *extra: str):
        return surprise_me_workflow_improvement.build_parser().parse_args(
            [
                "--vault-root",
                str(self.vault_root),
                "--staging-root",
                str(self.staging_root),
                *extra,
            ]
        )

    def _run(self, *extra: str, now: datetime | None = None) -> dict[str, object]:
        FrozenDateTime.current = now or datetime(2026, 3, 26, 1, 2, 3, tzinfo=timezone.utc)
        args = self._args(*extra)
        with mock.patch.object(surprise_me_workflow_improvement, "datetime", FrozenDateTime):
            return surprise_me_workflow_improvement.run(args)

    def test_iter_allowed_paths_respects_lookback_and_context_files(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n")
        self._write_vault("00_Inbox/SESSION-20260323.md", "# SESSION-20260323\n")
        self._write_vault("20_Work_Logs/LOG-20260325.md", "# LOG-20260325\n")
        self._write_vault("30_Projects/PROFILE-ciaran.md", "# profile\n")

        args = self._args(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-work-logs",
            "--context-file",
            "30_Projects/PROFILE-ciaran.md",
        )
        with mock.patch.object(surprise_me_workflow_improvement, "datetime", FrozenDateTime):
            paths = surprise_me_workflow_improvement._iter_allowed_paths(
                self.vault_root,
                self.staging_root,
                args,
            )

        rel_paths = [path.relative_to(self.vault_root).as_posix() for path in paths]
        self.assertEqual(
            rel_paths,
            [
                "00_Inbox/SESSION-20260326.md",
                "20_Work_Logs/LOG-20260325.md",
                "30_Projects/PROFILE-ciaran.md",
            ],
        )

    def test_iter_allowed_paths_accepts_staged_overlay_context_file(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n")
        self._write(
            self.staging_root,
            "60_How_To/HOWTO-current-workflows.md",
            "# Current Workflows\n\n- Morning digest owns the recurring overnight review loop.\n",
        )

        args = self._args(
            "--lookback-days",
            "1",
            "--include-sessions",
        )
        with mock.patch.object(surprise_me_workflow_improvement, "datetime", FrozenDateTime):
            paths = surprise_me_workflow_improvement._iter_allowed_paths(
                self.vault_root,
                self.staging_root,
                args,
            )

        rel_paths = [
            surprise_me_workflow_improvement._relative_note_path(path, self.vault_root, self.staging_root)
            for path in paths
        ]
        self.assertEqual(
            rel_paths,
            [
                "60_How_To/HOWTO-current-workflows.md",
                "00_Inbox/SESSION-20260326.md",
            ],
        )

    def test_iter_allowed_paths_prefers_staged_current_workflows_overlay_when_both_exist(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n")
        self._write_vault(
            "60_How_To/HOWTO-current-workflows.md",
            "# Current Workflows\n\n- Canonical placeholder.\n",
        )
        self._write(
            self.staging_root,
            "60_How_To/HOWTO-current-workflows.md",
            "# Current Workflows\n\n- Staged overlay should take precedence.\n",
        )

        args = self._args(
            "--lookback-days",
            "1",
            "--include-sessions",
        )
        with mock.patch.object(surprise_me_workflow_improvement, "datetime", FrozenDateTime):
            paths = surprise_me_workflow_improvement._iter_allowed_paths(
                self.vault_root,
                self.staging_root,
                args,
            )

        self.assertEqual(paths[0], (self.staging_root / "60_How_To/HOWTO-current-workflows.md").resolve())

    def test_run_suppresses_manual_loop_when_current_workflows_note_marks_it_covered(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Reviewed the overnight queue manually again before the morning pass.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Verified the recurring checks manually again during the same morning review loop.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260325.md",
            "\n".join(
                [
                    "# LOG-20260325",
                    "",
                    "Checked the recurring manual review steps again during the morning pass.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "60_How_To/HOWTO-current-workflows.md",
            "\n".join(
                [
                    "# Current Workflows",
                    "",
                    "The existing morning checklist already covers the recurring manual review loops and repeated checks.",
                ]
            )
            + "\n",
        )

        result = self._run("--lookback-days", "2", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(payload["candidate_ideas"][0]["category"], "manual_loop_reduction")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertIn("60_How_To/HOWTO-current-workflows.md", payload["inputs"]["context_files"])
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})
        self.assertIsNone(payload["workflow_suggestion"])

    def test_run_accepts_staging_only_current_workflows_note_with_memory_retrieval_enabled(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nReviewed the overnight queue manually again before the morning pass.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "# LOG-20260326\n\nVerified the recurring checks manually again during the same morning review loop.\n",
        )
        self._write(
            self.staging_root,
            "60_How_To/HOWTO-current-workflows.md",
            "# Current Workflows\n\nThe existing morning digest already covers the recurring manual review loops and repeated checks.\n",
        )
        db_path = self.root / ".memory_index.sqlite"
        MemoryIndex(workspace=self.root, db_path=db_path, source_roots=[self.vault_root]).sync(force=True)

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
            "--memory-db-path",
            str(db_path),
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertIn("60_How_To/HOWTO-current-workflows.md", payload["inputs"]["context_files"])
        self.assertIn(
            payload["inputs"]["memory_retrieval"]["status"],
            {"used", "no_results", "unavailable"},
        )

    def test_run_can_select_pre_surprise_me_workflow_experiment_sidecar(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")
        runs_dir = self.staging_root / "00_Inbox/workflow_experiments/runs"
        runs_dir.mkdir(parents=True)
        sidecar = runs_dir / "WORKFLOW-EXPERIMENT-RUN-20260326-014500.json"
        sidecar.write_text(
            json.dumps(
                {
                    "run_id": "WORKFLOW-EXPERIMENT-RUN-20260326-014500",
                    "status": "succeeded",
                    "selected_experiment": {
                        "title": "Run one read-only workflow experiment for telegram restart checks",
                        "hypothesis": "A small approved experiment can reduce repeated restart checks.",
                        "metric": "One fewer manual restart verification in morning triage.",
                        "next_action": "Review and approve one bounded experiment.",
                        "source_notes": ["00_Inbox/SESSION-20260326.md"],
                        "signals": [
                            {
                                "note_path": "00_Inbox/SESSION-20260326.md",
                                "line": 3,
                                "snippet": "Reviewed the Telegram restart check again.",
                                "score": 2,
                            }
                        ],
                    },
                    "outputs": {
                        "run_report": str(runs_dir / "WORKFLOW-EXPERIMENT-RUN-20260326-014500.md"),
                        "json_sidecar": str(sidecar),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--disable-memory-retrieval",
            "--disable-web-research",
            "--include-workflow-experiment-runs",
            "--workflow-experiment-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["category"], "workflow_experiment_follow_through")
        self.assertEqual(payload["workflow_suggestion"]["contract"], "workflow_suggestion")
        self.assertEqual(payload["workflow_suggestion"]["lane"], "agent_workflow_architecture")
        self.assertEqual(
            payload["workflow_suggestion"]["diagnostics"]["code"],
            "category_hint_matched",
        )
        self.assertEqual(
            payload["workflow_suggestion_diagnostics"],
            {"eligible": True, "code": "local_note_contract_emitted"},
        )
        self.assertEqual(
            payload["inputs"]["workflow_experiment_inputs"],
            ["00_Inbox/workflow_experiments/runs/WORKFLOW-EXPERIMENT-RUN-20260326-014500.json"],
        )

    def test_find_context_coverage_match_uses_structured_current_workflow_rule_categories(self) -> None:
        note_path = self._write(
            self.staging_root,
            "60_How_To/HOWTO-current-workflows.md",
            "\n".join(
                [
                    "---",
                    "type: guide",
                    "date: 2026-04-13",
                    "---",
                    "",
                    "# Current Workflows",
                    "",
                    "## Workflow Ownership",
                    "",
                    "### Decision Follow-Through",
                    "- Owner: `current-workflows`",
                    "- Applies to categories: `deep_sleep_follow_through`",
                    "- Covers: update the canonical workflow note when a decision changes day-to-day operating behavior",
                ]
            )
            + "\n",
        )
        note = surprise_me_workflow_improvement._parse_note(
            self.vault_root,
            note_path,
            staging_root=self.staging_root,
        )
        idea = surprise_me_workflow_improvement.CandidateIdea(
            idea_id="deep-sleep-000015521",
            idea_key="deep-sleep-follow-through-decision-affects-log",
            title="Turn recent deep-sleep decision review into one reusable checklist",
            category="deep_sleep_follow_through",
            score=72,
            rationale="Deep-sleep already surfaced a repeatable connection.",
            expected_benefit="Reduce time spent interpreting repeated synthesis outputs.",
            risk_level="low",
            next_action="ticket_recommendation",
            evidence=(),
            source_notes=("00_Inbox/SESSION-20260326.md",),
        )

        match = surprise_me_workflow_improvement._find_context_coverage_match(idea, [note])

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.rel_path, "60_How_To/HOWTO-current-workflows.md")

    def test_run_suppresses_deep_sleep_checklist_when_canonical_current_workflows_uses_headings_and_examples(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write_vault(
            "60_How_To/HOWTO-current-workflows.md",
            "\n".join(
                [
                    "---",
                    "type: guide",
                    "date: 2026-04-06",
                    "project: mission-control",
                    "tags: [workflows, mission-control, operations]",
                    "---",
                    "",
                    "# Current Workflows",
                    "",
                    "## Workflow Ownership",
                    "",
                    "### Morning Digest",
                    "- Owner: `morning-triage-digest-nightly`",
                    "- Covers: overnight check summary, recurring follow-up review, deep-sleep connection preview, surprise-me suggestion triage",
                    "- Already covered examples: morning checklist for recurring manual review loops, deep-sleep review checklist for morning follow-through",
                    "- Notes: `surprise-me` should treat checklist-style proposals for this lane as redundant unless the digest stops covering them or the scope changes materially.",
                ]
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(payload["candidate_ideas"][0]["category"], "deep_sleep_follow_through")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertIn("60_How_To/HOWTO-current-workflows.md", payload["inputs"]["context_files"])
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_run_discards_repeated_manual_loop_when_current_workflows_note_owns_it(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nReviewed the overnight queue manually again before the morning pass.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "# LOG-20260326\n\nVerified the recurring checks manually again during the same morning review loop.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260325.md",
            "# LOG-20260325\n\nChecked the recurring manual review steps again during the morning pass.\n",
        )
        self._write_vault(
            "60_How_To/HOWTO-current-workflows.md",
            "\n".join(
                [
                    "# Current Workflows",
                    "",
                    "## Workflow Ownership",
                    "",
                    "### Morning Digest",
                    "- Owner: `morning-triage-digest-nightly`",
                    "- Covers: overnight check summary, recurring follow-up review, deep-sleep connection preview, surprise-me suggestion triage",
                    "- Already covered examples: morning checklist for recurring manual review loops, deep-sleep review checklist for morning follow-through",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 2,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run("--lookback-days", "2", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["category"], "manual_loop_reduction")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["candidate_ideas"][0]["times_seen"], 2)
        self.assertEqual(payload["candidate_ideas"][0]["times_selected"], 1)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_run_discards_repeated_deep_sleep_follow_through_when_current_workflows_note_owns_it(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write_vault(
            "60_How_To/HOWTO-current-workflows.md",
            "\n".join(
                [
                    "# Current Workflows",
                    "",
                    "## Workflow Ownership",
                    "",
                    "### Morning Digest",
                    "- Owner: `morning-triage-digest-nightly`",
                    "- Covers: overnight check summary, recurring follow-up review, deep-sleep connection preview, surprise-me suggestion triage",
                    "- Already covered examples: morning checklist for recurring manual review loops, deep-sleep review checklist for morning follow-through",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md": {
                            "idea_key": "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md",
                            "category": "deep_sleep_follow_through",
                            "title": "Turn recent deep-sleep decision affects log review into one reusable checklist",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 3,
                            "times_selected": 2,
                            "latest_idea_id": "deep-sleep-000000001",
                            "seen_idea_ids": ["deep-sleep-000000001"],
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "latest_expected_benefit": "Reduce time spent interpreting repeated synthesis outputs and make follow-through more consistent.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Deep-sleep already surfaced a repeatable connection, so formalizing the morning review step is likely the fastest workflow gain.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["category"], "deep_sleep_follow_through")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["candidate_ideas"][0]["times_seen"], 3)
        self.assertEqual(payload["candidate_ideas"][0]["times_selected"], 2)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_run_creates_single_selected_idea_and_sidecar(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["candidate_connections"], 1)
        self.assertGreaterEqual(int(result["candidate_ideas"]), 1)
        report_path = Path(result["output_file"])
        json_path = report_path.with_suffix(".json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["run_id"], "SURPRISE-ME-RUN-20260326-010203")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["category"], "anchor_review_helper")
        self.assertEqual(payload["selected_idea"]["review_status"], "unreviewed")
        self.assertTrue(payload["selected_idea"]["idea_key"])
        self.assertNotIn("MC-", payload["selected_idea"]["title"])
        self.assertNotIn("sqlite", payload["selected_idea"]["title"].lower())
        self.assertEqual(payload["idea_registry"]["counts_before_selection"]["total"], 0)
        self.assertEqual(payload["idea_registry"]["counts_after_run"]["total"], 1)
        self.assertEqual(payload["selection_diagnostics"]["selected_rank_among_eligible"], 1)
        self.assertIn("ranked first", payload["selection_diagnostics"]["summary"])
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_seen_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_selected_count"], 0)
        self.assertEqual(len(payload["artifacts_created"]), 2)
        registry_path = Path(payload["idea_registry"]["path"])
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry_payload["counts"]["unreviewed"], 1)
        self.assertIn(payload["selected_idea"]["idea_key"], registry_payload["ideas"])
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn("scripts/deep_sleep_link_synthesis.py", report_text)
        self.assertIn("## Selection Diagnostics", report_text)
        self.assertIn("Idea registry before selection: `0` tracked", report_text)

    def test_run_checks_unreviewed_previous_idea_before_generating_new_ideas(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "previous-workflow-idea": {
                            "idea_key": "previous-workflow-idea",
                            "category": "workflow_experiment_follow_through",
                            "title": "Review the earlier workflow experiment proposal",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 0,
                            "latest_idea_id": "previous-idea-000000001",
                            "seen_idea_ids": ["previous-idea-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Close an existing workflow-improvement loop before adding another.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "A previous surprise-me proposal remains unreviewed.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
            "--disable-memory-retrieval",
            "--disable-web-research",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "previous_idea")
        self.assertEqual(payload["selected_idea"]["idea_key"], "previous-workflow-idea")
        self.assertEqual(payload["selected_idea"]["idea_id"], "previous-idea-000000001")
        self.assertEqual(payload["selection_diagnostics"]["selected_previously_seen"], True)
        self.assertEqual(len(payload["candidate_ideas"]), 1)
        self.assertEqual({idea["category"] for idea in payload["candidate_ideas"]}, {"workflow_experiment_follow_through"})
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("Selection mode: `previous_idea`", report_text)

    def test_run_ignores_discarded_previous_idea_before_generating_new_ideas(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "previous-workflow-idea": {
                            "idea_key": "previous-workflow-idea",
                            "category": "workflow_experiment_follow_through",
                            "title": "Review the earlier workflow experiment proposal",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 0,
                            "latest_idea_id": "previous-idea-000000001",
                            "seen_idea_ids": ["previous-idea-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Close an existing workflow-improvement loop before adding another.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "A previous surprise-me proposal remains unreviewed.",
                            "review_note": "Not worth doing.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
            "--disable-memory-retrieval",
            "--disable-web-research",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "evidence")
        self.assertEqual(payload["selected_idea"]["category"], "anchor_review_helper")
        self.assertNotEqual(payload["selected_idea"]["idea_key"], "previous-workflow-idea")

    def test_run_can_add_memory_retrieved_context_notes(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260320.md",
            "\n".join(
                [
                    "# SESSION-20260320",
                    "",
                    "Rebuilt onboarding handoff context after repeated friction in the morning review loop.",
                    "Need to consolidate onboarding handoff notes so future follow-up work is easier to resume.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Investigated onboarding handoff friction again during the morning review.",
                    "Need to reconcile the onboarding handoff context before the next follow-up.",
                ]
            )
            + "\n",
        )
        db_path = self.root / ".memory_index.sqlite"
        MemoryIndex(workspace=self.root, db_path=db_path, source_roots=[self.vault_root]).sync(force=True)

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--enable-memory-retrieval",
            "--memory-workspace",
            str(self.root),
            "--memory-db-path",
            str(db_path),
            "--memory-note-limit",
            "2",
            "--memory-context-lines",
            "4",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["notes_scanned"], 2)
        self.assertEqual(payload["inputs"]["memory_retrieval"]["status"], "used")
        self.assertEqual(payload["inputs"]["memory_retrieval"]["backend"], "builtin")
        self.assertIn("00_Inbox/SESSION-20260320.md", payload["inputs"]["memory_retrieval"]["added_paths"])
        self.assertEqual(len(payload["inputs"]["memory_retrieval"]["results"]), 1)
        self.assertEqual(payload["inputs"]["memory_retrieval"]["results"][0]["path"], "00_Inbox/SESSION-20260320.md")
        self.assertIn("onboarding handoff", payload["inputs"]["memory_retrieval"]["results"][0]["excerpt"])
        self.assertIn("00_Inbox/SESSION-20260320.md", [item["path"] for item in payload["notes"]])
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("## Retrieved Context Pack", report_text)
        self.assertIn("00_Inbox/SESSION-20260320.md", report_text)

    def test_run_marks_memory_retrieval_unavailable_when_index_roots_do_not_match(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Investigated onboarding handoff friction again during the morning review.",
                    "Need to reconcile the onboarding handoff context before the next follow-up.",
                ]
            )
            + "\n",
        )
        other_vault = self.root / "other_vault"
        other_vault.mkdir()
        (other_vault / "SESSION-20260320.md").write_text("# SESSION-20260320\n\nDifferent memory root.\n", encoding="utf-8")
        db_path = self.root / ".memory_index.sqlite"
        MemoryIndex(workspace=self.root, db_path=db_path, source_roots=[other_vault]).sync(force=True)

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--enable-memory-retrieval",
            "--memory-workspace",
            str(self.root),
            "--memory-db-path",
            str(db_path),
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn(result["status"], {"succeeded", "no_findings"})
        self.assertEqual(payload["inputs"]["memory_retrieval"]["status"], "unavailable")
        self.assertIn("rebuild required", payload["inputs"]["memory_retrieval"]["warning"])
        self.assertEqual(payload["inputs"]["memory_retrieval"]["added_paths"], [])
        self.assertIn("Rebuild local memory index", payload["inputs"]["memory_retrieval"]["action"])

    def test_run_can_use_recent_deep_sleep_sidecars_as_optional_context(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["category"], "deep_sleep_follow_through")
        self.assertEqual(
            payload["inputs"]["deep_sleep_inputs"],
            ["00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json"],
        )

    def test_run_suppresses_checklist_candidate_when_review_note_says_morning_digest_already_covers_it(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                            "review_note": "Redundant with the existing morning digest workflow. Morning triage already consolidates these recurring overnight checks and follow-ups, so a separate checklist proposal would duplicate an existing cron-owned workflow.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["category"], "deep_sleep_follow_through")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["selection_diagnostics"]["eligible_unreviewed_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["excluded_reviewed_count"], 1)
        self.assertIn("excluded by registry review state", payload["selection_diagnostics"]["summary"])

    def test_run_writes_no_findings_report_when_no_signal_is_present(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        result = self._run("--lookback-days", "1", "--include-sessions", "--disable-web-research")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(result["candidate_connections"], 0)
        self.assertEqual(payload["selected_idea"], None)

    def test_run_uses_web_research_when_no_local_proposal_and_no_roadmap_topic(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            return_value=(
                "https://duckduckgo.com/html/?q=workflow+automation",
                [
                    surprise_me_workflow_improvement.WebResearchHit(
                        rank=1,
                        title="Workflow automation retrospectives",
                        url="https://example.com/workflow-automation-retrospectives",
                        snippet="Teams use recurring retrospectives to turn workflow friction into one small automation proposal.",
                    )
                ],
            ),
        ) as search_mock, mock.patch.object(
            surprise_me_workflow_improvement,
            "_fetch_text_url",
            return_value="<html><title>Workflow automation retrospectives</title><body>Turn recurring workflow friction into one small automation proposal.</body></html>",
        ):
            result = self._run("--lookback-days", "1", "--include-sessions", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "web_research_fallback")
        self.assertEqual(payload["selected_idea"]["category"], "workflow_web_research_follow_through")
        self.assertIsNone(payload["workflow_suggestion"])
        self.assertEqual(
            payload["workflow_suggestion_diagnostics"],
            {"eligible": False, "code": "web_evidence_excluded"},
        )
        self.assertIn("Workflow automation retrospectives", payload["selected_idea"]["title"])
        self.assertEqual(
            payload["inputs"]["roadmap_fallback"]["topic_slug"],
            surprise_me_workflow_improvement.DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG,
        )
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["status"], "web_only")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "used")
        self.assertIn("workflow", search_mock.call_args.kwargs.get("query", "") or search_mock.call_args.args[0])
        self.assertIn("bounded web research surfaced", payload["selected_idea"]["rationale"])

    def test_run_uses_workflow_web_research_when_no_local_proposal_even_with_roadmap_topic(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Media & Audience",
                    "Capture lessons learned from distribution experiments.",
                ]
            )
            + "\n",
        )
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            return_value=(
                "https://duckduckgo.com/html/?q=workflow+automation",
                [
                    surprise_me_workflow_improvement.WebResearchHit(
                        rank=1,
                        title="Workflow automation retrospectives",
                        url="https://example.com/workflow-automation-retrospectives",
                        snippet="Teams use recurring retrospectives to turn workflow friction into one small automation proposal.",
                    )
                ],
            ),
        ) as search_mock, mock.patch.object(
            surprise_me_workflow_improvement,
            "_fetch_text_url",
            return_value="<html><title>Workflow automation retrospectives</title><body>Turn recurring workflow friction into one small automation proposal.</body></html>",
        ):
            result = self._run("--lookback-days", "1", "--include-sessions", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        search_mock.assert_called_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "web_research_fallback")
        self.assertEqual(payload["selected_idea"]["category"], "workflow_web_research_follow_through")
        self.assertEqual(
            payload["inputs"]["roadmap_fallback"]["topic_slug"],
            surprise_me_workflow_improvement.DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG,
        )
        self.assertIn("workflow", search_mock.call_args.args[0])

    def test_run_attempts_web_research_when_no_local_proposal_and_no_roadmap_support(self) -> None:
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            return_value=("https://duckduckgo.com/html/?q=workflow+automation", []),
        ) as search_mock, mock.patch.object(
            surprise_me_workflow_improvement,
            "_fetch_text_url",
            side_effect=AssertionError("no result pages should be fetched without search hits"),
        ):
            result = self._run("--lookback-days", "1", "--include-sessions", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        search_mock.assert_called_once()
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(result["selection_mode"], "none")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(
            payload["inputs"]["roadmap_fallback"]["topic_slug"],
            surprise_me_workflow_improvement.DEFAULT_WORKFLOW_WEB_FALLBACK_TOPIC_SLUG,
        )
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["status"], "no_supporting_notes")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "no_results")
        self.assertTrue(payload["inputs"]["roadmap_fallback"]["web_research"]["enabled"])
        self.assertIn("workflow", search_mock.call_args.args[0])

    def test_run_uses_roadmap_fallback_when_normal_candidates_are_reviewed_out(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Produce Trading Income",
                    "[[Trading Stream Plan]]",
                    "Build a repeatable trading pipeline.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "Trading Stream Plan.md",
            "\n".join(
                [
                    "# Trading Stream Plan",
                    "",
                    "## Open Questions",
                    "- Which operator-ready strategy spec is still missing for the current backtest lane?",
                    "",
                    "- [ ] Create one operator-facing strategy spec template for the active backtest loop.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nChecked the recurring manual review steps again during the morning pass.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "# LOG-20260326\n\nVerified the recurring checks manually again during the same morning review loop.\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260325.md",
            "# SESSION-20260325\n\nReviewed the overnight queue manually again before the morning pass.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Prior run.",
                            "review_note": "Redundant with the morning digest.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-work-logs",
            "--disable-memory-retrieval",
            "--disable-web-research",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "roadmap_research_fallback")
        self.assertEqual(payload["selection_mode"], "roadmap_research_fallback")
        self.assertEqual(payload["selected_idea"]["category"], "roadmap_research_follow_through")
        self.assertIn("Produce Trading Income", payload["selected_idea"]["title"])
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["topic_title"], "1) Produce Trading Income")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["status"], "used")
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("## Roadmap Fallback Research", report_text)
        self.assertIn("roadmap_research_fallback", report_text)

    def test_run_keeps_no_findings_when_roadmap_fallback_has_no_actionable_signal(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Media & Audience",
                    "Capture lessons learned.",
                ]
            )
            + "\n",
        )
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--disable-memory-retrieval",
            "--disable-web-research",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(result["selection_mode"], "none")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["topic_title"], "1) Media & Audience")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["status"], "no_supporting_notes")
        self.assertIn("Gather more roadmap-linked research evidence", payload["follow_up_recommendation"])

    def test_run_uses_web_research_when_roadmap_fallback_has_no_local_actionable_signal(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Media & Audience",
                    "Capture lessons learned from distribution experiments.",
                ]
            )
            + "\n",
        )
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            side_effect=[
                ("https://duckduckgo.com/html/?q=workflow+automation", []),
                (
                    "https://duckduckgo.com/html/?q=media+audience",
                    [
                        surprise_me_workflow_improvement.WebResearchHit(
                            rank=1,
                            title="Agent workflow postmortems for recurring audience experiments",
                            url="https://example.com/agent-workflow-postmortems",
                            snippet="Postmortems show how to turn repeated experiment learnings into one reusable review step.",
                        )
                    ],
                ),
            ],
        ) as search_mock, mock.patch.object(
            surprise_me_workflow_improvement,
            "_fetch_text_url",
            return_value="<html><title>Agent workflow postmortems</title><body>Document recurring experiment learnings and turn them into one operating checklist.</body></html>",
        ):
            result = self._run("--lookback-days", "1", "--include-sessions", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["selection_mode"], "roadmap_research_fallback")
        self.assertEqual(payload["selected_idea"]["category"], "roadmap_research_follow_through")
        self.assertIn("Agent workflow postmortems", payload["selected_idea"]["title"])
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["status"], "web_only")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "used")
        self.assertEqual(
            payload["inputs"]["roadmap_fallback"]["web_research"]["fetched_urls"],
            ["https://example.com/agent-workflow-postmortems"],
        )
        self.assertEqual(
            payload["selected_idea"]["evidence"][0]["note_path"],
            "https://example.com/agent-workflow-postmortems",
        )
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("Web research status", report_text)
        self.assertIn("https://example.com/agent-workflow-postmortems", report_text)

    def test_run_records_web_research_warning_when_search_fails(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Media & Audience",
                    "Capture lessons learned.",
                ]
            )
            + "\n",
        )
        self._write_vault("00_Inbox/SESSION-20260326.md", "# SESSION-20260326\n\nQuiet note.\n")

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            side_effect=OSError("network down"),
        ):
            result = self._run("--lookback-days", "1", "--include-sessions", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "unavailable")
        self.assertIn("network down", payload["inputs"]["roadmap_fallback"]["web_research"]["warning"])

    def test_web_research_flag_defaults_enabled_and_disable_switch_is_honored(self) -> None:
        default_args = self._args("--lookback-days", "1", "--include-sessions")
        disabled_args = self._args("--lookback-days", "1", "--include-sessions", "--disable-web-research")
        enabled_args = self._args("--lookback-days", "1", "--include-sessions", "--disable-web-research", "--enable-web-research")

        self.assertTrue(surprise_me_workflow_improvement._web_research_enabled(default_args))
        self.assertFalse(surprise_me_workflow_improvement._web_research_enabled(disabled_args))
        self.assertTrue(surprise_me_workflow_improvement._web_research_enabled(enabled_args))

    def test_roadmap_fallback_records_web_research_failure_without_failing_run(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Produce Trading Income",
                    "[[Trading Stream Plan]]",
                    "Build a repeatable trading pipeline.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "Trading Stream Plan.md",
            "\n".join(
                [
                    "# Trading Stream Plan",
                    "",
                    "## Open Questions",
                    "- Which operator-ready strategy spec is still missing for the current backtest lane?",
                    "",
                    "- [ ] Create one operator-facing strategy spec template for the active backtest loop.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nChecked the recurring manual review steps again during the morning pass.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "# LOG-20260326\n\nVerified the recurring checks manually again during the same morning review loop.\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260325.md",
            "# SESSION-20260325\n\nReviewed the overnight queue manually again before the morning pass.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Prior run.",
                            "review_note": "Redundant with the morning digest.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            side_effect=surprise_me_workflow_improvement.urllib_error.URLError("offline"),
        ):
            result = self._run("--lookback-days", "2", "--include-sessions", "--include-work-logs", "--disable-memory-retrieval")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertEqual(payload["selection_mode"], "roadmap_research_fallback")
        self.assertIn("roadmap_fallback", payload["inputs"])
        self.assertIn("web_research", payload["inputs"]["roadmap_fallback"])
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "unavailable")
        self.assertEqual(payload["inputs"]["roadmap_fallback"]["web_research"]["provider"], "duckduckgo")
        self.assertIn("offline", payload["inputs"]["roadmap_fallback"]["web_research"]["error"])
        self.assertIn("Check network access for roadmap fallback web research", payload["inputs"]["roadmap_fallback"]["web_research"]["action"])
        self.assertIn("Web research status: `unavailable`", report_text)
        self.assertIn("Web research warning: web research unavailable: <urlopen error offline>", report_text)

    def test_roadmap_fallback_selection_is_stable_when_web_research_flag_is_disabled(self) -> None:
        self._write_vault(
            "Technology_Roadmap.md",
            "\n".join(
                [
                    "# Technology Roadmap",
                    "",
                    "### 1) Produce Trading Income",
                    "[[Trading Stream Plan]]",
                    "Build a repeatable trading pipeline.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "Trading Stream Plan.md",
            "\n".join(
                [
                    "# Trading Stream Plan",
                    "",
                    "## Open Questions",
                    "- Which operator-ready strategy spec is still missing for the current backtest lane?",
                    "",
                    "- [ ] Create one operator-facing strategy spec template for the active backtest loop.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nChecked the recurring manual review steps again during the morning pass.\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "# LOG-20260326\n\nVerified the recurring checks manually again during the same morning review loop.\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260325.md",
            "# SESSION-20260325\n\nReviewed the overnight queue manually again before the morning pass.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Prior run.",
                            "review_note": "Redundant with the morning digest.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        with mock.patch.object(
            surprise_me_workflow_improvement,
            "_search_duckduckgo_html",
            return_value=("https://duckduckgo.com/html/?q=stub", ()),
        ):
            default_result = self._run("--lookback-days", "2", "--include-sessions", "--include-work-logs", "--disable-memory-retrieval")
        disabled_result = self._run(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-work-logs",
            "--disable-memory-retrieval",
            "--disable-web-research",
        )

        default_payload = json.loads(Path(default_result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        disabled_payload = json.loads(Path(disabled_result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(default_result["status"], "succeeded")
        self.assertEqual(disabled_result["status"], "succeeded")
        self.assertEqual(default_result["selection_mode"], "roadmap_research_fallback")
        self.assertEqual(disabled_result["selection_mode"], "roadmap_research_fallback")
        self.assertEqual(default_payload["selected_idea"]["idea_key"], disabled_payload["selected_idea"]["idea_key"])
        self.assertEqual(
            default_payload["inputs"]["roadmap_fallback"]["topic_title"],
            disabled_payload["inputs"]["roadmap_fallback"]["topic_title"],
        )
        self.assertEqual(default_payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "no_results")
        self.assertEqual(disabled_payload["inputs"]["roadmap_fallback"]["web_research"]["status"], "disabled")
        self.assertFalse(disabled_payload["inputs"]["roadmap_fallback"]["web_research"]["enabled"])

    def test_run_ignores_ticket_and_sqlite_anchor_helpers(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Checked `MC-0092` again and verified `/home/cwilson/projects/mission_control/data/mission_control.sqlite` manually.",
                    "Need to review `MC-0092` again before tomorrow morning.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Confirmed `MC-0092` after re-reading `/home/cwilson/projects/mission_control/data/mission_control.sqlite`.",
                    "Next action: review the repeated manual checks and capture one checklist.",
                ]
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "no_findings")
        self.assertIsNone(payload["selected_idea"])

    def test_run_prefers_code_anchor_over_ticket_and_sqlite_noise(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Checked `MC-0092` in `/home/cwilson/projects/mission_control/data/mission_control.sqlite` while re-reading `scripts/surprise_me_workflow_improvement.py`.",
                    "Need to tighten follow-up handling in `scripts/surprise_me_workflow_improvement.py` before the next review of `MC-0092`.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Verified `scripts/surprise_me_workflow_improvement.py` again after confirming `MC-0092` in `/home/cwilson/projects/mission_control/data/mission_control.sqlite`.",
                    "Next action: document one deterministic review path for `scripts/surprise_me_workflow_improvement.py` so `MC-0092` checks stop repeating.",
                ]
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-work-logs",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(payload["selected_idea"]["category"], "anchor_review_helper")
        self.assertIn("scripts/surprise_me_workflow_improvement.py", payload["selected_idea"]["title"])
        self.assertNotIn("MC-0092", payload["selected_idea"]["title"])
        self.assertNotIn("mission_control.sqlite", payload["selected_idea"]["title"])

    def test_run_filters_ticket_and_note_path_anchor_helpers_from_candidates(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260324.md",
            "\n".join(
                [
                    "# SESSION-20260324",
                    "",
                    "Checked `MC-0079` again after re-reading `/home/cwilson/projects/agent_skills/README.md` manually.",
                    "Reviewed the same manual verification steps again before handing off the work.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Confirmed `MC-0079` again and re-opened `/home/cwilson/projects/_vault_staging/00_Inbox/NOTE-20260323-memory-system-qmd-phase2-follow-up-tickets.md`.",
                    "Reviewed the same manual verification steps again before handing off the work.",
                    "Need to keep the repeated manual checks in one deterministic checklist.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Verified `MC-0079` after reviewing `/home/cwilson/projects/agent_skills/README.md` one more time.",
                    "Next action: review the repeated manual checks and capture one checklist.",
                ]
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "3",
            "--include-sessions",
            "--include-work-logs",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(payload["selected_idea"]["category"], "manual_loop_reduction")
        self.assertEqual(
            [idea["category"] for idea in payload["candidate_ideas"]],
            ["manual_loop_reduction"],
        )

    def test_run_does_not_reselect_discarded_or_approved_ideas(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                            "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000001",
                            "seen_idea_ids": ["anchor-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Prior run.",
                            "review_note": "Discarded as too narrow.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["selection_diagnostics"]["eligible_unreviewed_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["excluded_reviewed_count"], 1)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})
        self.assertIn("excluded by registry review state", payload["selection_diagnostics"]["summary"])
        self.assertIn("already reviewed", payload["follow_up_recommendation"])
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("excluded by review state: `1`", report_text)
        self.assertIn("All candidates were excluded by registry review state before selection (discarded `1`).", report_text)

    def test_run_suppresses_manual_checklist_when_digest_entry_review_note_claims_ownership(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Reviewed the overnight queue manually again before the morning pass.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Checked the recurring manual review steps again during the morning pass.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Verified the recurring checks manually again during the same morning review loop.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "anchor-review-helper-scripts-morning-triage-digest-py": {
                            "idea_key": "anchor-review-helper-scripts-morning-triage-digest-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/morning_triage_digest.py`",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-020304",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-020304",
                            "first_seen_at": "2026-03-25T02:03:04+00:00",
                            "last_seen_at": "2026-03-25T02:03:04+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000002",
                            "seen_idea_ids": ["anchor-000000002"],
                            "source_notes": ["20_Work_Logs/LOG-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Prior run.",
                            "review_note": "Redundant because the existing morning digest already owns these recurring manual checks and follow-up review steps.",
                            "reviewed_at": "2026-03-25T03:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run("--lookback-days", "2", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertIsNone(payload["selected_idea"])
        self.assertEqual(payload["candidate_ideas"][0]["category"], "manual_loop_reduction")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["selection_diagnostics"]["eligible_unreviewed_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_run_overrides_stale_unreviewed_checklist_with_reviewed_digest_coverage(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000000001",
                            "seen_idea_ids": ["manual-loops-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                            "review_note": "Redundant with the existing morning digest workflow. Morning triage already consolidates these recurring overnight checks and follow-ups, so a separate checklist proposal would duplicate an existing cron-owned workflow.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        },
                        "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md": {
                            "idea_key": "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md",
                            "category": "deep_sleep_follow_through",
                            "title": "Turn recent deep-sleep decision affects log review into one reusable checklist",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 2,
                            "times_selected": 2,
                            "latest_idea_id": "deep-sleep-000000001",
                            "seen_idea_ids": ["deep-sleep-000000001"],
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "latest_expected_benefit": "Reduce time spent interpreting repeated synthesis outputs and make follow-through more consistent.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Deep-sleep already surfaced a repeatable connection, so formalizing the morning review step is likely the fastest workflow gain.",
                            "review_note": "",
                            "reviewed_at": "",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["category"], "deep_sleep_follow_through")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["candidate_ideas"][0]["times_selected"], 2)
        self.assertEqual(payload["selection_diagnostics"]["eligible_unreviewed_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_run_prefers_new_unreviewed_idea_over_previously_selected_one(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                    "Reviewed scripts/morning_triage_digest.py again and need to tighten the follow-up wording.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                    "Verified scripts/morning_triage_digest.py again after reviewing the latest digest output.",
                    "Next action: document a repeatable review path for scripts/morning_triage_digest.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                            "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000001",
                            "seen_idea_ids": ["anchor-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Prior run.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["title"], "Draft a deterministic review helper for `scripts/morning_triage_digest.py`")
        prior_candidate = next(
            item for item in payload["candidate_ideas"] if item["title"] == "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`"
        )
        self.assertEqual(prior_candidate["times_selected"], 1)
        self.assertEqual(payload["selection_diagnostics"]["selected_rank_among_eligible"], 1)
        self.assertIn("ranked first", payload["selection_diagnostics"]["summary"])
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_seen_count"], 1)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_selected_count"], 1)
        self.assertEqual(
            payload["selection_diagnostics"]["selected_history"],
            {"times_seen": 0, "times_selected": 0, "review_status": "unreviewed"},
        )
        self.assertEqual(payload["selection_diagnostics"]["eligible_candidates"][0]["title"], payload["selected_idea"]["title"])
        self.assertEqual(payload["selection_diagnostics"]["eligible_candidates"][1]["title"], prior_candidate["title"])

    def test_run_explains_when_repeated_selection_is_forced_by_registry_history(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                    "Reviewed scripts/deep_sleep_link_synthesis.py one more time to confirm the latest deterministic output.",
                    "Reviewed scripts/morning_triage_digest.py again and need to tighten the follow-up wording.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                    "Verified scripts/morning_triage_digest.py again after reviewing the latest digest output.",
                    "Next action: document a repeatable review path for scripts/morning_triage_digest.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                            "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000001",
                            "seen_idea_ids": ["anchor-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Prior run.",
                            "review_note": "",
                            "reviewed_at": "",
                        },
                        "anchor-review-helper-scripts-morning-triage-digest-py": {
                            "idea_key": "anchor-review-helper-scripts-morning-triage-digest-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/morning_triage_digest.py`",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-020304",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-020304",
                            "first_seen_at": "2026-03-25T02:03:04+00:00",
                            "last_seen_at": "2026-03-25T02:03:04+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000002",
                            "seen_idea_ids": ["anchor-000000002"],
                            "source_notes": ["20_Work_Logs/LOG-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Prior run.",
                            "review_note": "",
                            "reviewed_at": "",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["title"], "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`")
        self.assertTrue(payload["selection_diagnostics"]["selected_previously_seen"])
        self.assertTrue(payload["selection_diagnostics"]["selected_previously_selected"])
        self.assertEqual(payload["selection_diagnostics"]["selected_rank_among_eligible"], 1)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_seen_count"], 2)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_selected_count"], 2)
        self.assertEqual(
            payload["selection_diagnostics"]["selected_history"],
            {"times_seen": 1, "times_selected": 1, "review_status": "unreviewed"},
        )
        self.assertIn("all `2` eligible candidates had already been surfaced", payload["selection_diagnostics"]["summary"])
        self.assertEqual(payload["selection_diagnostics"]["eligible_candidates"][0]["times_selected"], 1)
        registry_payload = json.loads((self.staging_root / "00_Inbox/surprise_me/idea_registry.json").read_text(encoding="utf-8"))
        self.assertEqual(
            registry_payload["ideas"]["anchor-review-helper-scripts-deep-sleep-link-synthesis-py"]["times_selected"],
            2,
        )
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("all `2` eligible candidates had already been surfaced", report_text)
        self.assertIn("Eligible candidates with prior registry history: seen before `2` / selected before `2`", report_text)
        self.assertIn("seen `1` selected `1`", report_text)

    def test_select_idea_prefers_less_frequently_selected_fallback(self) -> None:
        candidate_ideas = [
            surprise_me_workflow_improvement.CandidateIdea(
                idea_id="manual-loops-000152551",
                idea_key="manual-loop-reduction-recurring-manual-review-loops",
                title="Create a short morning checklist for recurring manual review loops",
                category="manual_loop_reduction",
                score=185,
                rationale="High-signal repeated manual work.",
                expected_benefit="Save operator time on repeated low-risk review steps.",
                risk_level="low",
                next_action="proposal_only",
                evidence=(),
                source_notes=("00_Inbox/SESSION-20260329.md",),
                times_seen=3,
                times_selected=2,
            ),
            surprise_me_workflow_improvement.CandidateIdea(
                idea_id="deep-sleep-000015521",
                idea_key="deep-sleep-follow-through-decision-affects-log",
                title="Turn recent deep-sleep decision review into one reusable checklist",
                category="deep_sleep_follow_through",
                score=72,
                rationale="Deep-sleep already surfaced a repeatable connection.",
                expected_benefit="Reduce time spent interpreting repeated synthesis outputs.",
                risk_level="low",
                next_action="ticket_recommendation",
                evidence=(),
                source_notes=("00_Inbox/SESSION-20260326.md",),
                times_seen=2,
                times_selected=1,
            ),
        ]

        selected = surprise_me_workflow_improvement._select_idea(candidate_ideas)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.idea_key, "deep-sleep-follow-through-decision-affects-log")

    def test_run_backfills_registry_history_from_canonical_sidecars_when_registry_is_missing(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                    "Reviewed scripts/morning_triage_digest.py again and need to tighten the follow-up wording.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                    "Verified scripts/morning_triage_digest.py again after reviewing the latest digest output.",
                    "Next action: document a repeatable review path for scripts/morning_triage_digest.py.",
                ]
            )
            + "\n",
        )
        prior_sidecar = {
            "run_id": "SURPRISE-ME-RUN-20260325-010203",
            "status": "succeeded",
            "candidate_ideas": [
                {
                    "idea_id": "anchor-000000001",
                    "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                    "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                    "category": "anchor_review_helper",
                    "score": 72,
                    "rationale": "Prior run.",
                    "expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                    "risk_level": "low",
                    "next_action": "ticket_recommendation",
                    "source_notes": ["00_Inbox/SESSION-20260325.md"],
                    "evidence": [],
                    "review_status": "unreviewed",
                    "times_seen": 0,
                    "times_selected": 0,
                }
            ],
            "selected_idea": {
                "idea_id": "anchor-000000001",
                "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                "category": "anchor_review_helper",
                "score": 72,
                "rationale": "Prior run.",
                "expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                "risk_level": "low",
                "next_action": "ticket_recommendation",
                "source_notes": ["00_Inbox/SESSION-20260325.md"],
                "evidence": [],
                "review_status": "unreviewed",
                "times_seen": 0,
                "times_selected": 0,
            },
        }
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260325-010203.json",
            json.dumps(prior_sidecar, indent=2, sort_keys=True) + "\n",
        )

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["selected_idea"]["title"], "Draft a deterministic review helper for `scripts/morning_triage_digest.py`")
        prior_candidate = next(
            item for item in payload["candidate_ideas"] if item["idea_key"] == "anchor-review-helper-scripts-deep-sleep-link-synthesis-py"
        )
        self.assertEqual(prior_candidate["times_seen"], 1)
        self.assertEqual(prior_candidate["times_selected"], 1)
        self.assertEqual(payload["idea_registry"]["counts_before_selection"]["total"], 1)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_seen_count"], 1)
        self.assertEqual(payload["selection_diagnostics"]["eligible_previously_selected_count"], 1)
        report_text = Path(result["output_file"]).read_text(encoding="utf-8")
        self.assertIn("Idea registry before selection: `1` tracked", report_text)

    def test_run_prefers_canonical_registry_over_legacy_staged_overlay(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )
        canonical_registry = {
            "version": 1,
            "ideas": {
                "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                    "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                    "category": "anchor_review_helper",
                    "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                    "review_status": "unreviewed",
                    "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                    "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                    "first_seen_at": "2026-03-25T01:02:03+00:00",
                    "last_seen_at": "2026-03-25T01:02:03+00:00",
                    "times_seen": 1,
                    "times_selected": 0,
                    "latest_idea_id": "anchor-000000001",
                    "seen_idea_ids": ["anchor-000000001"],
                    "source_notes": ["00_Inbox/SESSION-20260325.md"],
                    "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                    "latest_next_action": "ticket_recommendation",
                    "latest_rationale": "Prior run.",
                    "review_note": "",
                    "reviewed_at": "",
                }
            },
        }
        staged_registry = {
            "version": 1,
            "ideas": {
                "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                    "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                    "category": "anchor_review_helper",
                    "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                    "review_status": "discarded",
                    "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                    "last_seen_run_id": "SURPRISE-ME-RUN-20260326-000000",
                    "first_seen_at": "2026-03-25T01:02:03+00:00",
                    "last_seen_at": "2026-03-26T00:00:00+00:00",
                    "times_seen": 1,
                    "times_selected": 0,
                    "latest_idea_id": "anchor-000000001",
                    "seen_idea_ids": ["anchor-000000001"],
                    "source_notes": ["00_Inbox/SESSION-20260325.md"],
                    "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                    "latest_next_action": "ticket_recommendation",
                    "latest_rationale": "Discard pending promotion.",
                    "review_note": "Discard before next sync.",
                    "reviewed_at": "2026-03-26T00:00:00+00:00",
                }
            },
        }
        self._write_vault(
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(canonical_registry, indent=2, sort_keys=True) + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(staged_registry, indent=2, sort_keys=True) + "\n",
        )

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "unreviewed")
        self.assertEqual(payload["idea_registry"]["counts_before_selection"]["unreviewed"], 1)

    def test_run_consults_canonical_registry_after_sync_while_surprise_me_runs_stay_staged(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "\n".join(
                [
                    "# SESSION-20260326",
                    "",
                    "Re-opened scripts/deep_sleep_link_synthesis.py to verify deterministic output again.",
                    "Need to tighten follow-up handling in scripts/deep_sleep_link_synthesis.py before the next review.",
                ]
            )
            + "\n",
        )
        self._write_vault(
            "20_Work_Logs/LOG-20260326.md",
            "\n".join(
                [
                    "# LOG-20260326",
                    "",
                    "Reviewed scripts/deep_sleep_link_synthesis.py again after confirming the latest deterministic run.",
                    "Next action: document a repeatable review path for scripts/deep_sleep_link_synthesis.py.",
                ]
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "anchor-review-helper-scripts-deep-sleep-link-synthesis-py": {
                            "idea_key": "anchor-review-helper-scripts-deep-sleep-link-synthesis-py",
                            "category": "anchor_review_helper",
                            "title": "Draft a deterministic review helper for `scripts/deep_sleep_link_synthesis.py`",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "anchor-000000001",
                            "seen_idea_ids": ["anchor-000000001"],
                            "source_notes": ["00_Inbox/SESSION-20260325.md"],
                            "latest_expected_benefit": "Reduce repeated verification and re-reading around one concrete work artifact.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Legacy staged memory.",
                            "review_note": "Discard before sync.",
                            "reviewed_at": "2026-03-25T02:00:00+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        staged_sidecar = self._write(
            self.staging_root,
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260325-010203.json",
            json.dumps({"run_id": "SURPRISE-ME-RUN-20260325-010203", "status": "succeeded"}, indent=2, sort_keys=True) + "\n",
        )
        self._write(self.staging_root, "INDEX.md", "# Work Session Index\n")

        sync_vault_staging(
            staging_root=self.staging_root,
            canonical_root=self.vault_root,
        )

        self.assertTrue(staged_sidecar.exists())
        self.assertFalse((self.vault_root / "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260325-010203.json").exists())

        result = self._run("--lookback-days", "1", "--include-sessions", "--include-work-logs")

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["idea_registry"]["counts_before_selection"]["discarded"], 1)
        self.assertEqual(Path(payload["idea_registry"]["path"]), self.staging_root / "00_Inbox/surprise_me/idea_registry.json")

    def test_load_idea_memory_does_not_fabricate_overlay_review_state_from_staging_history(self) -> None:
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "first_seen_at": "2026-03-27T02:00:02+00:00",
                            "last_seen_at": "2026-03-27T02:00:02+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000119508",
                            "seen_idea_ids": ["manual-loops-000119508"],
                            "source_notes": ["00_Inbox/SESSION-20260327.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks.",
                            "review_note": "Covered by the existing morning digest workflow.",
                            "reviewed_at": "2026-04-07T14:25:00+01:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/surprise_me/runs/SURPRISE-ME-RUN-20260410-010004.json",
            json.dumps(
                {
                    "run_id": "SURPRISE-ME-RUN-20260410-010004",
                    "selected_idea": {
                        "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                    },
                    "candidate_ideas": [
                        {
                            "idea_id": "manual-loops-000185070",
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "score": 145,
                            "source_notes": ["00_Inbox/SESSION-20260404.md"],
                            "rationale": "Recent notes spend repeated effort on manual checks and re-reading.",
                            "expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "next_action": "proposal_only",
                            "risk_level": "low",
                            "review_status": "unreviewed",
                            "times_seen": 12,
                            "times_selected": 3,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        registry = surprise_me_workflow_improvement.load_idea_memory(self.vault_root, self.staging_root)

        entry = registry.entries["manual-loop-reduction-recurring-manual-review-loops"]
        self.assertEqual(entry.review_status, "discarded")
        self.assertEqual(entry.reviewed_at, "2026-04-07T14:25:00+01:00")
        self.assertEqual(entry.review_note, "Covered by the existing morning digest workflow.")

    def test_run_merges_canonical_overlay_review_note_for_deep_sleep_repeat_suppression(self) -> None:
        self._write_vault(
            "00_Inbox/SESSION-20260326.md",
            "# SESSION-20260326\n\nMinimal fresh note.\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md": {
                            "idea_key": "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md",
                            "category": "deep_sleep_follow_through",
                            "title": "Turn recent deep-sleep decision affects log review into one reusable checklist",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 2,
                            "times_selected": 2,
                            "latest_idea_id": "deep-sleep-000000001",
                            "seen_idea_ids": ["deep-sleep-000000001"],
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "latest_expected_benefit": "Reduce time spent interpreting repeated synthesis outputs and make follow-through more consistent.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Deep-sleep already surfaced a repeatable connection, so formalizing the morning review step is likely the fastest workflow gain.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.overlay.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md": {
                            "idea_key": "deep-sleep-follow-through-decision-affects-log-00-inbox-session-20260325-md-10-decisions-dec-20260325-surprise-me-md",
                            "category": "deep_sleep_follow_through",
                            "title": "Turn recent deep-sleep decision affects log review into one reusable checklist",
                            "review_status": "discarded",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260325-010203",
                            "first_seen_at": "2026-03-25T01:02:03+00:00",
                            "last_seen_at": "2026-03-25T01:02:03+00:00",
                            "times_seen": 2,
                            "times_selected": 2,
                            "latest_idea_id": "deep-sleep-000000001",
                            "seen_idea_ids": ["deep-sleep-000000001"],
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "latest_expected_benefit": "Reduce time spent interpreting repeated synthesis outputs and make follow-through more consistent.",
                            "latest_next_action": "ticket_recommendation",
                            "latest_rationale": "Deep-sleep already surfaced a repeatable connection, so formalizing the morning review step is likely the fastest workflow gain.",
                            "review_note": "Redundant with the existing morning digest workflow. Morning triage already previews deep-sleep connections and owns the recurring morning follow-through review, so a separate checklist idea would duplicate an existing cron-owned workflow.",
                            "reviewed_at": "2026-04-06T10:02:19.596722+00:00",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        self._write(
            self.staging_root,
            "00_Inbox/deep_sleep/runs/DEEP-SLEEP-RUN-20260326-003000.json",
            json.dumps(
                {
                    "run_id": "DEEP-SLEEP-RUN-20260326-003000",
                    "connections": [
                        {
                            "relation_type": "decision_affects_log",
                            "confidence": "high",
                            "suggested_action": "Review whether one authoritative recent note should summarize the impact.",
                            "source_notes": [
                                "00_Inbox/SESSION-20260325.md",
                                "10_Decisions/DEC-20260325-surprise-me.md",
                            ],
                            "evidence": [
                                {
                                    "note_path": "00_Inbox/SESSION-20260325.md",
                                    "line": 10,
                                    "snippet": "Recorded the accepted rollout boundary again.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "1",
            "--include-sessions",
            "--include-deep-sleep-runs",
            "--deep-sleep-limit",
            "1",
        )

        payload = json.loads(Path(result["output_file"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "no_findings")
        self.assertEqual(payload["candidate_ideas"][0]["category"], "deep_sleep_follow_through")
        self.assertEqual(payload["candidate_ideas"][0]["review_status"], "discarded")
        self.assertEqual(payload["selection_diagnostics"]["eligible_unreviewed_count"], 0)
        self.assertEqual(payload["selection_diagnostics"]["excluded_review_state_counts"], {"discarded": 1})

    def test_registry_cli_can_update_review_status_by_idea_key(self) -> None:
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "first_seen_at": "2026-03-27T02:00:02+00:00",
                            "last_seen_at": "2026-03-27T02:00:02+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000119508",
                            "seen_idea_ids": ["manual-loops-000119508"],
                            "source_notes": ["00_Inbox/SESSION-20260327.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        args = surprise_me_idea_registry.build_parser().parse_args(
            [
                "--vault-root",
                str(self.vault_root),
                "--staging-root",
                str(self.staging_root),
                "set-status",
                "--idea-key",
                "manual-loop-reduction-recurring-manual-review-loops",
                "--status",
                "approved",
                "--note",
                "Approved for future rollout discussion.",
            ]
        )
        payload = surprise_me_idea_registry.run(args)

        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["idea"]["review_status"], "approved")
        self.assertEqual(payload["idea"]["review_note"], "Approved for future rollout discussion.")
        registry_payload = json.loads((self.staging_root / "00_Inbox/surprise_me/idea_registry.json").read_text(encoding="utf-8"))
        self.assertEqual(
            registry_payload["ideas"]["manual-loop-reduction-recurring-manual-review-loops"]["review_status"],
            "approved",
        )
        self.assertEqual(
            Path(payload["registry_path"]),
            self.staging_root / "00_Inbox/surprise_me/idea_registry.json",
        )
        canonical_payload = json.loads((self.vault_root / "00_Inbox/surprise_me/idea_registry.json").read_text(encoding="utf-8"))
        self.assertEqual(
            canonical_payload["ideas"]["manual-loop-reduction-recurring-manual-review-loops"]["review_status"],
            "unreviewed",
        )

    def test_registry_cli_falls_back_to_overlay_when_primary_staged_registry_is_not_writable(self) -> None:
        self._write(
            self.vault_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps(
                {
                    "version": 1,
                    "ideas": {
                        "manual-loop-reduction-recurring-manual-review-loops": {
                            "idea_key": "manual-loop-reduction-recurring-manual-review-loops",
                            "category": "manual_loop_reduction",
                            "title": "Create a short morning checklist for recurring manual review loops",
                            "review_status": "unreviewed",
                            "first_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "last_seen_run_id": "SURPRISE-ME-RUN-20260327-020002",
                            "first_seen_at": "2026-03-27T02:00:02+00:00",
                            "last_seen_at": "2026-03-27T02:00:02+00:00",
                            "times_seen": 1,
                            "times_selected": 1,
                            "latest_idea_id": "manual-loops-000119508",
                            "seen_idea_ids": ["manual-loops-000119508"],
                            "source_notes": ["00_Inbox/SESSION-20260327.md"],
                            "latest_expected_benefit": "Save operator time on repeated low-risk review steps.",
                            "latest_next_action": "proposal_only",
                            "latest_rationale": "Recent notes spend repeated effort on manual checks.",
                            "review_note": "",
                            "reviewed_at": "",
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        staged_registry_path = self._write(
            self.staging_root,
            "00_Inbox/surprise_me/idea_registry.json",
            json.dumps({"version": 1, "ideas": {}}, indent=2, sort_keys=True) + "\n",
        )
        staged_registry_path.chmod(0o444)
        self.addCleanup(lambda: staged_registry_path.exists() and staged_registry_path.chmod(0o644))

        args = surprise_me_idea_registry.build_parser().parse_args(
            [
                "--vault-root",
                str(self.vault_root),
                "--staging-root",
                str(self.staging_root),
                "set-status",
                "--idea-key",
                "manual-loop-reduction-recurring-manual-review-loops",
                "--status",
                "discarded",
                "--note",
                "Covered elsewhere.",
            ]
        )
        payload = surprise_me_idea_registry.run(args)

        overlay_path = self.staging_root / "00_Inbox/surprise_me/idea_registry.overlay.json"
        overlay_payload = json.loads(overlay_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(
            overlay_payload["ideas"]["manual-loop-reduction-recurring-manual-review-loops"]["review_status"],
            "discarded",
        )

    def test_main_emits_json_summary_and_failure_exit_code(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.argv", ["surprise_me_workflow_improvement", "--vault-root", str(self.vault_root), "--staging-root", str(self.staging_root)]):
            with redirect_stdout(stdout):
                exit_code = surprise_me_workflow_improvement.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("at least one include flag is required", payload["error"])


if __name__ == "__main__":
    unittest.main()
