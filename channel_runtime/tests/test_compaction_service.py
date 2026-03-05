from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.context.assembler import ContextAssembler
from channel_runtime.context.compaction import CompactionPolicy, CompactionService
from channel_runtime.context.contracts import ContextTurn
from channel_runtime.context.errors import ContextStoreError
from channel_runtime.context.store import ContextStore
from channel_runtime.context.token_estimator import TokenEstimator, TokenEstimatorPolicy


def _dt_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


class TestCompactionService(unittest.TestCase):
    def test_threshold_crossing_compacts_and_persists_compaction_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            now_epoch = 1760000000.0
            store = ContextStore(root_dir=root, now_utc=lambda: _dt_from_epoch(now_epoch))
            estimator = TokenEstimator(
                policy=TokenEstimatorPolicy(
                    chars_per_token=4,
                    turn_overhead_tokens=6,
                    window_overhead_tokens=12,
                    safety_multiplier=1.0,
                )
            )
            assembler = ContextAssembler(token_estimator=estimator)
            service = CompactionService(
                store=store,
                assembler=assembler,
                estimator=estimator,
                now_s=lambda: now_epoch,
            )

            session_id = "telegram:901"
            payload = "x" * 50
            for index in range(3):
                store.append_turn(session_id=session_id, turn=ContextTurn(role="user", text=f"u{index}:{payload}"))
                store.append_turn(session_id=session_id, turn=ContextTurn(role="assistant", text=f"a{index}:{payload}"))

            result = service.evaluate_and_compact(
                session_id=session_id,
                policy=CompactionPolicy(
                    context_window_tokens=100,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=10,
                    cooldown_window_s=300,
                ),
            )

            self.assertEqual(result.status, "compacted")
            self.assertEqual(result.reason, "threshold")
            self.assertGreater(result.gained_tokens, 0)
            self.assertEqual(result.transcript_turns[0].metadata.get("source_type"), "compaction")
            self.assertEqual(
                result.conversation_history[0]["user_text"],
                "[compaction-summary]",
            )
            self.assertIn("Compaction Summary", str(result.conversation_history[0]["assistant_text"]))

            transcript_path = root / "transcripts" / "telegram%3A901.jsonl"
            records = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(records), 2)
            self.assertEqual(records[0]["type"], "compaction")
            self.assertIn("compaction_summary", records[0])

    def test_cooldown_suppresses_repeated_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            clock = {"now": 1760000000.0}
            store = ContextStore(root_dir=root, now_utc=lambda: _dt_from_epoch(clock["now"]))
            estimator = TokenEstimator(
                policy=TokenEstimatorPolicy(
                    chars_per_token=4,
                    turn_overhead_tokens=6,
                    window_overhead_tokens=12,
                    safety_multiplier=1.0,
                )
            )
            service = CompactionService(
                store=store,
                assembler=ContextAssembler(token_estimator=estimator),
                estimator=estimator,
                now_s=lambda: clock["now"],
            )

            session_id = "telegram:902"
            payload = "y" * 50
            for index in range(3):
                store.append_turn(session_id=session_id, turn=ContextTurn(role="user", text=f"u{index}:{payload}"))
                store.append_turn(session_id=session_id, turn=ContextTurn(role="assistant", text=f"a{index}:{payload}"))

            first = service.evaluate_and_compact(
                session_id=session_id,
                policy=CompactionPolicy(
                    context_window_tokens=100,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=10,
                    cooldown_window_s=300,
                ),
            )
            self.assertEqual(first.status, "compacted")

            for index in range(3, 5):
                store.append_turn(session_id=session_id, turn=ContextTurn(role="user", text=f"u{index}:{payload}"))
                store.append_turn(session_id=session_id, turn=ContextTurn(role="assistant", text=f"a{index}:{payload}"))

            clock["now"] += 60.0
            second = service.evaluate_and_compact(
                session_id=session_id,
                policy=CompactionPolicy(
                    context_window_tokens=100,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=10,
                    cooldown_window_s=300,
                ),
            )
            self.assertEqual(second.status, "skipped")
            self.assertEqual(second.reason, "cooldown-active")

            transcript_path = root / "transcripts" / "telegram%3A902.jsonl"
            records = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            compaction_count = sum(1 for record in records if record.get("type") == "compaction")
            self.assertEqual(compaction_count, 1)

    def test_min_gain_guard_skips_low_value_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            now_epoch = 1760000000.0
            store = ContextStore(root_dir=root, now_utc=lambda: _dt_from_epoch(now_epoch))
            estimator = TokenEstimator(
                policy=TokenEstimatorPolicy(
                    chars_per_token=4,
                    turn_overhead_tokens=6,
                    window_overhead_tokens=12,
                    safety_multiplier=1.0,
                )
            )
            service = CompactionService(
                store=store,
                assembler=ContextAssembler(token_estimator=estimator),
                estimator=estimator,
                now_s=lambda: now_epoch,
            )

            session_id = "telegram:903"
            payload = "z" * 50
            for index in range(3):
                store.append_turn(session_id=session_id, turn=ContextTurn(role="user", text=f"u{index}:{payload}"))
                store.append_turn(session_id=session_id, turn=ContextTurn(role="assistant", text=f"a{index}:{payload}"))

            result = service.evaluate_and_compact(
                session_id=session_id,
                policy=CompactionPolicy(
                    context_window_tokens=100,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=500,
                    cooldown_window_s=300,
                ),
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "min-gain")
            transcript_path = root / "transcripts" / "telegram%3A903.jsonl"
            records = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            compaction_count = sum(1 for record in records if record.get("type") == "compaction")
            self.assertEqual(compaction_count, 0)

    def test_compaction_failure_returns_failed_status_with_deterministic_fallback(self) -> None:
        class _FailingReplaceStore:
            def __init__(self, wrapped: ContextStore) -> None:
                self._wrapped = wrapped

            def load_transcript(self, *, session_id: str) -> tuple[ContextTurn, ...]:
                return self._wrapped.load_transcript(session_id=session_id)

            def append_turn(self, *, session_id: str, turn: ContextTurn) -> None:
                self._wrapped.append_turn(session_id=session_id, turn=turn)

            def replace_transcript(self, *, session_id: str, turns: tuple[ContextTurn, ...]) -> None:
                raise ContextStoreError(
                    "simulated replace failure",
                    code="context-store-save-error",
                    operation="replace_transcript",
                    retryable=True,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            now_epoch = 1760000000.0
            base_store = ContextStore(root_dir=root, now_utc=lambda: _dt_from_epoch(now_epoch))
            estimator = TokenEstimator(
                policy=TokenEstimatorPolicy(
                    chars_per_token=4,
                    turn_overhead_tokens=6,
                    window_overhead_tokens=12,
                    safety_multiplier=1.0,
                )
            )
            service = CompactionService(
                store=_FailingReplaceStore(base_store),
                assembler=ContextAssembler(token_estimator=estimator),
                estimator=estimator,
                now_s=lambda: now_epoch,
            )

            session_id = "telegram:904"
            payload = "q" * 70
            for index in range(4):
                base_store.append_turn(session_id=session_id, turn=ContextTurn(role="user", text=f"u{index}:{payload}"))
                base_store.append_turn(session_id=session_id, turn=ContextTurn(role="assistant", text=f"a{index}:{payload}"))

            result = service.evaluate_and_compact(
                session_id=session_id,
                policy=CompactionPolicy(
                    context_window_tokens=120,
                    reserve_tokens=10,
                    keep_recent_tokens=50,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "compaction-error")
            self.assertEqual(
                [(turn.role, turn.text) for turn in result.transcript_turns],
                [(f.role, f.text) for f in base_store.load_transcript(session_id=session_id)[-2:]],
            )
            self.assertEqual(
                result.conversation_history,
                ({"user_text": f"u3:{payload}", "assistant_text": f"a3:{payload}"},),
            )


if __name__ == "__main__":
    unittest.main()
