import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import deep_sleep_link_synthesis


class FrozenDateTime(datetime):
    current = datetime(2026, 3, 25, 1, 2, 3, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value


class DeepSleepLinkSynthesisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.vault_root = self.root / "vault"
        self.staging_root = self.root / "_vault_staging"
        self.vault_root.mkdir()
        self.staging_root.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write(self, rel_path: str, content: str) -> Path:
        path = self.vault_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _args(self, *extra: str):
        return deep_sleep_link_synthesis.build_parser().parse_args(
            [
                "--vault-root",
                str(self.vault_root),
                "--staging-root",
                str(self.staging_root),
                *extra,
            ]
        )

    def _run(self, *extra: str, now: datetime | None = None) -> dict[str, object]:
        FrozenDateTime.current = now or datetime(2026, 3, 25, 1, 2, 3, tzinfo=timezone.utc)
        args = self._args(*extra)
        with mock.patch.object(deep_sleep_link_synthesis, "datetime", FrozenDateTime):
            return deep_sleep_link_synthesis.run(args)

    def _build_connections(
        self,
        *selected_paths: str,
        include_decisions: bool = False,
    ) -> list[deep_sleep_link_synthesis.CandidateConnection]:
        paths = [self.vault_root / rel_path for rel_path in selected_paths]
        notes, parse_issues = deep_sleep_link_synthesis._load_notes(
            self.vault_root,
            paths,
            include_decisions=include_decisions,
        )
        self.assertEqual(parse_issues, [])
        return deep_sleep_link_synthesis._build_connections(notes)

    def test_iter_allowed_paths_respects_lookback_window(self) -> None:
        self._write("00_Inbox/SESSION-20260325.md", "# SESSION-20260325\n")
        self._write("00_Inbox/SESSION-20260324.md", "# SESSION-20260324\n")
        self._write("00_Inbox/SESSION-20260320.md", "# SESSION-20260320\n")
        self._write("20_Work_Logs/LOG-20260325.md", "# LOG-20260325\n")

        args = self._args("--lookback-days", "2", "--include-sessions", "--context-file", "20_Work_Logs/LOG-20260325.md")
        with mock.patch.object(deep_sleep_link_synthesis, "datetime", FrozenDateTime):
            paths = deep_sleep_link_synthesis._iter_allowed_paths(self.vault_root, args)

        rel_paths = [path.relative_to(self.vault_root).as_posix() for path in paths]
        self.assertEqual(
            rel_paths,
            [
                "00_Inbox/SESSION-20260324.md",
                "00_Inbox/SESSION-20260325.md",
                "20_Work_Logs/LOG-20260325.md",
            ],
        )

    def test_load_notes_includes_one_hop_linked_decision_outside_lookback(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Continuing [[10_Decisions/DEC-20260301-legacy-choice.md]].",
                ]
            )
            + "\n",
        )
        self._write(
            "10_Decisions/DEC-20260301-legacy-choice.md",
            "# DEC-20260301-legacy-choice\n\nAccepted rollout boundary.\n",
        )

        args = self._args("--lookback-days", "3", "--include-sessions", "--include-decisions")
        with mock.patch.object(deep_sleep_link_synthesis, "datetime", FrozenDateTime):
            selected = deep_sleep_link_synthesis._iter_allowed_paths(self.vault_root, args)
            notes, parse_issues = deep_sleep_link_synthesis._load_notes(
                self.vault_root,
                selected,
                include_decisions=True,
            )

        rel_paths = {note.rel_path for note in notes}
        self.assertEqual(parse_issues, [])
        self.assertIn("00_Inbox/SESSION-20260325.md", rel_paths)
        self.assertIn("10_Decisions/DEC-20260301-legacy-choice.md", rel_paths)

    def test_run_creates_append_only_report_and_json_sidecar(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Tracking [[10_Decisions/DEC-20260325-boundary.md]].",
                ]
            )
            + "\n",
        )
        self._write(
            "10_Decisions/DEC-20260325-boundary.md",
            "# DEC-20260325-boundary\n\nBoundary is staging-only output.\n",
        )

        first = self._run(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-decisions",
            now=datetime(2026, 3, 25, 1, 2, 3, tzinfo=timezone.utc),
        )
        second = self._run(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-decisions",
            now=datetime(2026, 3, 25, 1, 2, 4, tzinfo=timezone.utc),
        )

        runs_dir = self.staging_root / "00_Inbox/deep_sleep/runs"
        markdown_files = sorted(path.name for path in runs_dir.glob("DEEP-SLEEP-RUN-*.md"))
        json_files = sorted(path.name for path in runs_dir.glob("DEEP-SLEEP-RUN-*.json"))

        self.assertEqual(
            markdown_files,
            [
                "DEEP-SLEEP-RUN-20260325-010203.md",
                "DEEP-SLEEP-RUN-20260325-010204.md",
            ],
        )
        self.assertEqual(
            json_files,
            [
                "DEEP-SLEEP-RUN-20260325-010203.json",
                "DEEP-SLEEP-RUN-20260325-010204.json",
            ],
        )
        self.assertEqual(first["run_id"], "DEEP-SLEEP-RUN-20260325-010203")
        self.assertEqual(second["run_id"], "DEEP-SLEEP-RUN-20260325-010204")

    def test_run_writes_connection_payload_to_json_sidecar(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Continuing [[10_Decisions/DEC-20260325-boundary.md]].",
                ]
            )
            + "\n",
        )
        self._write(
            "10_Decisions/DEC-20260325-boundary.md",
            "\n".join(
                [
                    "# DEC-20260325-boundary",
                    "",
                    "## Decision",
                    "Keep deep sleep output in staging.",
                ]
            )
            + "\n",
        )

        result = self._run(
            "--lookback-days",
            "2",
            "--include-sessions",
            "--include-decisions",
            now=datetime(2026, 3, 25, 1, 2, 3, tzinfo=timezone.utc),
        )

        json_path = Path(result["output_file"]).with_suffix(".json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["run_id"], "DEEP-SLEEP-RUN-20260325-010203")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["candidate_connections"], 1)
        self.assertEqual(payload["clusters_found"], 0)
        self.assertEqual(
            [note["path"] for note in payload["notes"]],
            [
                "00_Inbox/SESSION-20260325.md",
                "10_Decisions/DEC-20260325-boundary.md",
            ],
        )
        self.assertEqual(
            [item["relation_type"] for item in payload["connections"]],
            ["decision_affects_log"],
        )
        self.assertEqual(payload["outputs"]["json_sidecar"], json_path.as_posix())

    def test_routine_decision_reference_does_not_emit_noisy_decision_affects_log(self) -> None:
        self._write(
            "20_Work_Logs/LOG-20260325.md",
            "\n".join(
                [
                    "# LOG-20260325",
                    "",
                    "Completed the routine checklist and noted [[10_Decisions/DEC-20260325-housekeeping.md]].",
                    "No rollout, follow-up, or active impact depended on that reference.",
                ]
            )
            + "\n",
        )
        self._write(
            "10_Decisions/DEC-20260325-housekeeping.md",
            "\n".join(
                [
                    "# DEC-20260325-housekeeping",
                    "",
                    "## Decision",
                    "Keep routine housekeeping notes in the standard template.",
                ]
            )
            + "\n",
        )

        connections = self._build_connections(
            "20_Work_Logs/LOG-20260325.md",
            "10_Decisions/DEC-20260325-housekeeping.md",
            include_decisions=True,
        )

        self.assertNotIn(
            "decision_affects_log",
            [connection.relation_type for connection in connections],
        )

    def test_marginal_shared_file_and_ticket_overlap_does_not_emit_session_continues_plan(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Closed a brief review of `scripts/deep_sleep_link_synthesis.py` while checking MC-0085.",
                    "No plan or follow-up carried forward from the touchpoint.",
                ]
            )
            + "\n",
        )
        self._write(
            "20_Work_Logs/LOG-20260325.md",
            "\n".join(
                [
                    "# LOG-20260325",
                    "",
                    "Mentioned `scripts/deep_sleep_link_synthesis.py` again after finishing MC-0085 cleanup.",
                    "The overlap was incidental rather than a continuing work thread.",
                ]
            )
            + "\n",
        )

        connections = self._build_connections(
            "00_Inbox/SESSION-20260325.md",
            "20_Work_Logs/LOG-20260325.md",
        )

        self.assertNotIn(
            "session_continues_plan",
            [connection.relation_type for connection in connections],
        )

    def test_shared_file_with_active_follow_through_emits_session_continues_plan(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Need to tighten deterministic heuristics in scripts/deep_sleep_link_synthesis.py to reduce noisy findings.",
                ]
            )
            + "\n",
        )
        self._write(
            "20_Work_Logs/LOG-20260325.md",
            "\n".join(
                [
                    "# LOG-20260325",
                    "",
                    "Implemented deterministic heuristic updates in scripts/deep_sleep_link_synthesis.py to reduce noisy findings and preserve evidence-backed links.",
                ]
            )
            + "\n",
        )

        connections = self._build_connections(
            "00_Inbox/SESSION-20260325.md",
            "20_Work_Logs/LOG-20260325.md",
        )

        self.assertIn(
            "session_continues_plan",
            [connection.relation_type for connection in connections],
        )

    def test_generic_path_stem_overlap_does_not_emit_session_continues_plan(self) -> None:
        self._write(
            "00_Inbox/SESSION-20260325.md",
            "\n".join(
                [
                    "# SESSION-20260325",
                    "",
                    "Need to verify the real CLI behavior before rollout.",
                    "The relevant file is `/home/cwilson/projects/mission_control/mission_control/cli.py`.",
                ]
            )
            + "\n",
        )
        self._write(
            "20_Work_Logs/LOG-20260325.md",
            "\n".join(
                [
                    "# LOG-20260325",
                    "",
                    "Documented the release review after the scheduler work settled.",
                    "This note mentions the cli in prose but does not continue that file-specific plan.",
                ]
            )
            + "\n",
        )

        connections = self._build_connections(
            "00_Inbox/SESSION-20260325.md",
            "20_Work_Logs/LOG-20260325.md",
        )

        self.assertNotIn(
            "session_continues_plan",
            [connection.relation_type for connection in connections],
        )


if __name__ == "__main__":
    unittest.main()
