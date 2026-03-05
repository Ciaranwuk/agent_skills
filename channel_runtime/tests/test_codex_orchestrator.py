from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ContractValidationError, InboundMessage
from channel_runtime.context.compaction import CompactionPolicy, CompactionService
from channel_runtime.context.contracts import ContextTurn
from channel_runtime.context.errors import ContextStoreError
from channel_runtime.context.store import ContextStore
from channel_runtime.codex_orchestrator import (
    CodexExecError,
    CodexInvocationRequest,
    CodexOrchestrator,
    CodexSessionManager,
    CodexSessionPolicy,
    _default_codex_invoke,
)


def _inbound(update_id: str, *, text: str = "hello", chat_id: str = "100") -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id="u-1",
        text=text,
        message_id=f"m-{update_id}",
    )


class TestCodexOrchestrator(unittest.TestCase):
    def test_operator_inspect_reports_session_state_without_invoking_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextStore(root_dir=Path(tmpdir) / ".channel_runtime" / "context", strict_io=False)
            store.append_turn(session_id="telegram:inspect", turn=ContextTurn(role="user", text="hello"))
            store.append_turn(session_id="telegram:inspect", turn=ContextTurn(role="assistant", text="world"))

            calls: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                calls.append(request)
                return "should-not-run"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                context_mode="durable",
                context_store=store,
                enable_context_operator_controls=True,
            )
            outbound = orchestrator.handle_message(_inbound("1", text="/ctx inspect"), session_id="telegram:inspect")

            self.assertIsNotNone(outbound)
            assert outbound is not None
            self.assertEqual(calls, [])
            self.assertIn("context inspect:", outbound.text)
            self.assertIn("session_id=telegram:inspect", outbound.text)
            self.assertIn("status=ok", outbound.text)
            self.assertIn("tokens_before=", outbound.text)
            self.assertIn("tokens_after=", outbound.text)
            self.assertEqual(outbound.metadata["operator_command"], "context-inspect")

    def test_operator_manual_compaction_missing_session_is_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextStore(root_dir=Path(tmpdir) / ".channel_runtime" / "context", strict_io=False)
            orchestrator = CodexOrchestrator(
                invoke_fn=lambda _: "unused",
                context_mode="durable",
                context_store=store,
                compaction_service=CompactionService(store=store),
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
                enable_context_operator_controls=True,
            )
            outbound = orchestrator.handle_message(_inbound("1", text="/ctx compact"), session_id="telegram:missing")

            self.assertIsNotNone(outbound)
            assert outbound is not None
            self.assertIn("context compact:", outbound.text)
            self.assertIn("status=skipped", outbound.text)
            self.assertIn("reason=session-missing", outbound.text)
            self.assertIn("tokens_before=0", outbound.text)
            self.assertIn("tokens_after=0", outbound.text)
            telemetry = orchestrator.drain_context_telemetry()
            self.assertEqual(telemetry["counters"]["compaction_attempted_total"], 0)
            self.assertEqual(telemetry["counters"]["compaction_reasons"]["manual_total"], 0)

    def test_operator_manual_compaction_reports_estimates_and_manual_reason_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextStore(root_dir=Path(tmpdir) / ".channel_runtime" / "context", strict_io=False)
            for index in range(4):
                store.append_turn(session_id="telegram:manual", turn=ContextTurn(role="user", text=f"user-{index} " + ("x" * 80)))
                store.append_turn(
                    session_id="telegram:manual",
                    turn=ContextTurn(role="assistant", text=f"assistant-{index} " + ("y" * 80)),
                )

            orchestrator = CodexOrchestrator(
                invoke_fn=lambda _: "unused",
                context_mode="durable",
                context_store=store,
                compaction_service=CompactionService(store=store),
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
                enable_context_operator_controls=True,
            )
            outbound = orchestrator.handle_message(_inbound("1", text="/context compact"), session_id="telegram:manual")

            self.assertIsNotNone(outbound)
            assert outbound is not None
            self.assertIn("context compact:", outbound.text)
            self.assertIn("status=compacted", outbound.text)
            self.assertIn("tokens_before=", outbound.text)
            self.assertIn("tokens_after=", outbound.text)
            telemetry = orchestrator.drain_context_telemetry()
            self.assertEqual(telemetry["counters"]["compaction_attempted_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_succeeded_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_reasons"]["manual_total"], 1)

    def test_durable_overflow_error_triggers_compaction_and_single_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            store = ContextStore(root_dir=root, strict_io=False)
            for index in range(4):
                store.append_turn(session_id="telegram:overflow", turn=ContextTurn(role="user", text=f"user-{index} " + ("x" * 80)))
                store.append_turn(
                    session_id="telegram:overflow",
                    turn=ContextTurn(role="assistant", text=f"assistant-{index} " + ("y" * 80)),
                )

            requests: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                requests.append(request)
                if len(requests) == 1:
                    raise CodexExecError("maximum context length exceeded")
                return "recovered"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                context_mode="durable",
                context_store=store,
                compaction_service=CompactionService(store=store),
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
            )
            outbound = orchestrator.handle_message(_inbound("1", text="latest"), session_id="telegram:overflow")

            self.assertIsNotNone(outbound)
            self.assertEqual(len(requests), 2)
            self.assertTrue(requests[1].conversation_history)
            self.assertEqual(requests[1].conversation_history[0]["user_text"], "[compaction-summary]")
            transcript = store.load_transcript(session_id="telegram:overflow")
            self.assertTrue(any(turn.metadata.get("source_type") == "compaction" for turn in transcript))

    def test_durable_overflow_retry_failure_records_deterministic_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            store = ContextStore(root_dir=root, strict_io=False)
            for index in range(4):
                store.append_turn(session_id="telegram:overflow-fail", turn=ContextTurn(role="user", text=f"user-{index} " + ("x" * 80)))
                store.append_turn(
                    session_id="telegram:overflow-fail",
                    turn=ContextTurn(role="assistant", text=f"assistant-{index} " + ("y" * 80)),
                )

            requests: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                requests.append(request)
                raise CodexExecError("maximum context length exceeded")

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                context_mode="durable",
                context_store=store,
                compaction_service=CompactionService(store=store),
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
            )
            outbound = orchestrator.handle_message(_inbound("1", text="latest"), session_id="telegram:overflow-fail")

            self.assertIsNone(outbound)
            self.assertEqual(len(requests), 2)
            diagnostics = orchestrator.drain_diagnostics()
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["code"], "codex-exec-failed")
            self.assertEqual(diagnostics[0]["layer"], "context")
            self.assertEqual(diagnostics[0]["operation"], "compact")
            self.assertIn("overflow_recovery", diagnostics[0])
            self.assertEqual(diagnostics[0]["overflow_recovery"]["attempted"], True)
            self.assertEqual(diagnostics[0]["overflow_recovery"]["retry_attempted"], True)
            self.assertEqual(diagnostics[0]["overflow_recovery"]["compaction_status"], "compacted")
            telemetry = orchestrator.drain_context_telemetry()
            self.assertEqual(telemetry["mode"], "durable")
            self.assertEqual(telemetry["counters"]["compaction_attempted_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_succeeded_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_failed_total"], 0)
            self.assertGreaterEqual(int(telemetry["counters"]["tokens_estimated_total"]), 1)

    def test_non_overflow_failure_does_not_trigger_compaction_recovery(self) -> None:
        class _NoCompactionExpected:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate_and_compact(self, *, session_id: str, policy: CompactionPolicy) -> object:
                self.calls += 1
                raise AssertionError(f"compaction should not run for non-overflow failure ({session_id}, {policy})")

        compaction_spy = _NoCompactionExpected()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextStore(root_dir=Path(tmpdir) / ".channel_runtime" / "context", strict_io=False)
            orchestrator = CodexOrchestrator(
                invoke_fn=lambda _: (_ for _ in ()).throw(CodexExecError("backend unavailable")),
                context_mode="durable",
                context_store=store,
                compaction_service=compaction_spy,  # type: ignore[arg-type]
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
            )

            outbound = orchestrator.handle_message(_inbound("1", text="latest"), session_id="telegram:no-overflow")
            self.assertIsNone(outbound)
            self.assertEqual(compaction_spy.calls, 0)
            diagnostics = orchestrator.drain_diagnostics()
            self.assertEqual(len(diagnostics), 1)
            self.assertNotIn("overflow_recovery", diagnostics[0])

    def test_non_strict_mode_continues_with_fallback_when_compaction_fails(self) -> None:
        class _FailedCompactionResult:
            status = "failed"
            reason = "compaction-error"
            conversation_history = ({"user_text": "fallback-user", "assistant_text": "fallback-assistant"},)

        class _AlwaysFailedCompactionService:
            def evaluate_and_compact(self, *, session_id: str, policy: CompactionPolicy) -> object:
                _ = (session_id, policy)
                return _FailedCompactionResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ContextStore(root_dir=Path(tmpdir) / ".channel_runtime" / "context", strict_io=False)
            requests: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                requests.append(request)
                if len(requests) == 1:
                    raise CodexExecError("maximum context length exceeded")
                return "recovered-via-fallback"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                context_mode="durable",
                context_store=store,
                compaction_service=_AlwaysFailedCompactionService(),  # type: ignore[arg-type]
                compaction_policy=CompactionPolicy(
                    context_window_tokens=90,
                    reserve_tokens=10,
                    keep_recent_tokens=45,
                    min_compaction_gain_tokens=0,
                    cooldown_window_s=0.0,
                ),
            )
            outbound = orchestrator.handle_message(_inbound("1", text="latest"), session_id="telegram:fallback")

            self.assertIsNotNone(outbound)
            self.assertEqual(len(requests), 2)
            self.assertEqual(
                requests[1].conversation_history,
                ({"user_text": "fallback-user", "assistant_text": "fallback-assistant"},),
            )
            diagnostics = orchestrator.drain_diagnostics()
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["code"], "context-compaction-fallback")
            self.assertEqual(diagnostics[0]["layer"], "context")
            self.assertEqual(diagnostics[0]["operation"], "compact")
            self.assertEqual(diagnostics[0]["overflow_recovery"]["compaction_status"], "failed")
            self.assertTrue(diagnostics[0]["overflow_recovery"]["fallback_applied"])
            telemetry = orchestrator.drain_context_telemetry()
            self.assertEqual(telemetry["counters"]["compaction_attempted_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_succeeded_total"], 0)
            self.assertEqual(telemetry["counters"]["compaction_failed_total"], 1)
            self.assertEqual(telemetry["counters"]["compaction_fallback_used_total"], 1)

    def test_legacy_mode_overflow_does_not_use_compaction_recovery(self) -> None:
        class _NoCompactionExpected:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate_and_compact(self, *, session_id: str, policy: CompactionPolicy) -> object:
                self.calls += 1
                raise AssertionError(f"compaction should never run in legacy mode ({session_id}, {policy})")

        compaction_spy = _NoCompactionExpected()
        orchestrator = CodexOrchestrator(
            invoke_fn=lambda _: (_ for _ in ()).throw(CodexExecError("maximum context length exceeded")),
            context_mode="legacy",
            compaction_service=compaction_spy,  # type: ignore[arg-type]
            compaction_policy=CompactionPolicy(
                context_window_tokens=90,
                reserve_tokens=10,
                keep_recent_tokens=45,
                min_compaction_gain_tokens=0,
                cooldown_window_s=0.0,
            ),
        )

        outbound = orchestrator.handle_message(_inbound("1", text="latest"), session_id="telegram:legacy-overflow")
        self.assertIsNone(outbound)
        self.assertEqual(compaction_spy.calls, 0)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertNotIn("overflow_recovery", diagnostics[0])

    def test_second_request_includes_prior_conversation_context(self) -> None:
        calls: list[CodexInvocationRequest] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            calls.append(request)
            return f"echo:{request.text}"

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)

        first = orchestrator.handle_message(_inbound("1", text="hello"), session_id="telegram:200")
        self.assertIsNotNone(first)

        second = orchestrator.handle_message(_inbound("2", text="follow up"), session_id="telegram:200")
        self.assertIsNotNone(second)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].conversation_history, ())
        self.assertEqual(
            calls[1].conversation_history,
            ({"user_text": "hello", "assistant_text": "echo:hello"},),
        )

    def test_conversation_history_is_session_isolated(self) -> None:
        calls_by_session: dict[str, list[CodexInvocationRequest]] = {}

        def _invoke(request: CodexInvocationRequest) -> str | None:
            calls_by_session.setdefault(request.session_id, []).append(request)
            return f"ok:{request.text}"

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)

        orchestrator.handle_message(_inbound("1", text="a1", chat_id="1"), session_id="telegram:1")
        orchestrator.handle_message(_inbound("2", text="b1", chat_id="2"), session_id="telegram:2")
        orchestrator.handle_message(_inbound("3", text="a2", chat_id="1"), session_id="telegram:1")
        orchestrator.handle_message(_inbound("4", text="b2", chat_id="2"), session_id="telegram:2")

        self.assertEqual(
            calls_by_session["telegram:1"][1].conversation_history,
            ({"user_text": "a1", "assistant_text": "ok:a1"},),
        )
        self.assertEqual(
            calls_by_session["telegram:2"][1].conversation_history,
            ({"user_text": "b1", "assistant_text": "ok:b1"},),
        )

    def test_conversation_history_is_bounded_by_policy(self) -> None:
        seen_history: list[tuple[dict[str, str | None], ...]] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            seen_history.append(request.conversation_history)
            return f"reply:{request.text}"

        manager = CodexSessionManager(
            policy=CodexSessionPolicy(max_sessions=5, idle_ttl_s=60.0, max_history_turns=2)
        )
        orchestrator = CodexOrchestrator(invoke_fn=_invoke, session_manager=manager)

        orchestrator.handle_message(_inbound("1", text="t1"), session_id="telegram:100")
        orchestrator.handle_message(_inbound("2", text="t2"), session_id="telegram:100")
        orchestrator.handle_message(_inbound("3", text="t3"), session_id="telegram:100")
        orchestrator.handle_message(_inbound("4", text="t4"), session_id="telegram:100")

        self.assertEqual(seen_history[0], ())
        self.assertEqual(
            seen_history[3],
            (
                {"user_text": "t2", "assistant_text": "reply:t2"},
                {"user_text": "t3", "assistant_text": "reply:t3"},
            ),
        )

    def test_durable_mode_reconstructs_history_from_persisted_transcript_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            store = ContextStore(root_dir=root, strict_io=False)

            orchestrator_one = CodexOrchestrator(
                invoke_fn=lambda req: f"echo:{req.text}",
                context_mode="durable",
                context_store=store,
            )
            first = orchestrator_one.handle_message(_inbound("1", text="hello"), session_id="telegram:200")
            self.assertIsNotNone(first)

            calls_after_restart: list[CodexInvocationRequest] = []

            def _invoke_after_restart(request: CodexInvocationRequest) -> str | None:
                calls_after_restart.append(request)
                return f"echo:{request.text}"

            orchestrator_two = CodexOrchestrator(
                invoke_fn=_invoke_after_restart,
                context_mode="durable",
                context_store=ContextStore(root_dir=root, strict_io=False),
            )
            second = orchestrator_two.handle_message(_inbound("2", text="follow up"), session_id="telegram:200")
            self.assertIsNotNone(second)

            self.assertEqual(len(calls_after_restart), 1)
            self.assertEqual(
                calls_after_restart[0].conversation_history,
                ({"user_text": "hello", "assistant_text": "echo:hello"},),
            )

    def test_durable_mode_history_is_session_isolated_by_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            store = ContextStore(root_dir=root, strict_io=False)
            store.append_turn(session_id="telegram:1", turn=ContextTurn(role="user", text="a1"))
            store.append_turn(session_id="telegram:1", turn=ContextTurn(role="assistant", text="ok:a1"))
            store.append_turn(session_id="telegram:2", turn=ContextTurn(role="user", text="b1"))
            store.append_turn(session_id="telegram:2", turn=ContextTurn(role="assistant", text="ok:b1"))

            calls_by_session: dict[str, list[CodexInvocationRequest]] = {}

            def _invoke(request: CodexInvocationRequest) -> str | None:
                calls_by_session.setdefault(request.session_id, []).append(request)
                return "ok"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                context_mode="durable",
                context_store=ContextStore(root_dir=root, strict_io=False),
            )

            orchestrator.handle_message(_inbound("1", text="a2", chat_id="1"), session_id="telegram:1")
            orchestrator.handle_message(_inbound("2", text="b2", chat_id="2"), session_id="telegram:2")

            self.assertEqual(
                calls_by_session["telegram:1"][0].conversation_history,
                ({"user_text": "a1", "assistant_text": "ok:a1"},),
            )
            self.assertEqual(
                calls_by_session["telegram:2"][0].conversation_history,
                ({"user_text": "b1", "assistant_text": "ok:b1"},),
            )

    def test_durable_mode_bridges_from_in_memory_history_when_transcript_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            session_manager = CodexSessionManager()
            session_manager.append_conversation_turn(
                "telegram:301",
                user_text="legacy hello",
                assistant_text="legacy reply",
            )
            seen_requests: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                seen_requests.append(request)
                return f"echo:{request.text}"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                session_manager=session_manager,
                context_mode="durable",
                context_store=ContextStore(root_dir=root, strict_io=False),
            )
            outbound = orchestrator.handle_message(_inbound("1", text="new message", chat_id="301"), session_id="telegram:301")

            self.assertIsNotNone(outbound)
            self.assertEqual(len(seen_requests), 1)
            self.assertEqual(
                seen_requests[0].conversation_history,
                ({"user_text": "legacy hello", "assistant_text": "legacy reply"},),
            )

            transcript = ContextStore(root_dir=root, strict_io=False).load_transcript(session_id="telegram:301")
            self.assertEqual([(turn.role, turn.text) for turn in transcript][:2], [("user", "legacy hello"), ("assistant", "legacy reply")])

    def test_durable_mode_bridge_never_overwrites_existing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".channel_runtime" / "context"
            store = ContextStore(root_dir=root, strict_io=False)
            store.append_turn(session_id="telegram:302", turn=ContextTurn(role="user", text="persisted user"))
            store.append_turn(session_id="telegram:302", turn=ContextTurn(role="assistant", text="persisted assistant"))

            session_manager = CodexSessionManager()
            session_manager.append_conversation_turn(
                "telegram:302",
                user_text="legacy user",
                assistant_text="legacy assistant",
            )

            seen_requests: list[CodexInvocationRequest] = []

            def _invoke(request: CodexInvocationRequest) -> str | None:
                seen_requests.append(request)
                return f"echo:{request.text}"

            orchestrator = CodexOrchestrator(
                invoke_fn=_invoke,
                session_manager=session_manager,
                context_mode="durable",
                context_store=store,
            )
            outbound = orchestrator.handle_message(_inbound("1", text="next", chat_id="302"), session_id="telegram:302")

            self.assertIsNotNone(outbound)
            self.assertEqual(len(seen_requests), 1)
            self.assertEqual(
                seen_requests[0].conversation_history,
                ({"user_text": "persisted user", "assistant_text": "persisted assistant"},),
            )

            transcript = ContextStore(root_dir=root, strict_io=False).load_transcript(session_id="telegram:302")
            self.assertEqual([(turn.role, turn.text) for turn in transcript][:2], [("user", "persisted user"), ("assistant", "persisted assistant")])

    def test_legacy_mode_never_uses_durable_store_path(self) -> None:
        class _ExplodingStore:
            def load_transcript(self, *, session_id: str) -> tuple[ContextTurn, ...]:
                raise AssertionError(f"load_transcript should not be called for legacy mode ({session_id})")

            def append_turn(self, *, session_id: str, turn: ContextTurn) -> None:
                raise AssertionError(f"append_turn should not be called for legacy mode ({session_id})")

            def replace_transcript(self, *, session_id: str, turns: tuple[ContextTurn, ...]) -> None:
                raise AssertionError(f"replace_transcript should not be called for legacy mode ({session_id})")

        calls: list[CodexInvocationRequest] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            calls.append(request)
            return f"ok:{request.text}"

        orchestrator = CodexOrchestrator(
            invoke_fn=_invoke,
            context_mode="legacy",
            context_store=_ExplodingStore(),
        )
        orchestrator.handle_message(_inbound("1", text="hello"), session_id="telegram:9")
        orchestrator.handle_message(_inbound("2", text="again"), session_id="telegram:9")

        self.assertEqual(calls[0].conversation_history, ())
        self.assertEqual(
            calls[1].conversation_history,
            ({"user_text": "hello", "assistant_text": "ok:hello"},),
        )

    def test_returns_one_outbound_from_invoke_response(self) -> None:
        calls: list[CodexInvocationRequest] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            calls.append(request)
            return "response from codex"

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)
        outbound = orchestrator.handle_message(_inbound("1", chat_id="200"), session_id="telegram:200")

        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.chat_id, "200")
        self.assertEqual(outbound.text, "response from codex")
        self.assertEqual(outbound.reply_to_message_id, "m-1")
        self.assertEqual(outbound.metadata["session_id"], "telegram:200")
        self.assertEqual(calls[0].update_id, "1")

    def test_empty_or_none_response_yields_no_outbound(self) -> None:
        orchestrator_none = CodexOrchestrator(invoke_fn=lambda _: None)
        outbound_none = orchestrator_none.handle_message(_inbound("1"), session_id="telegram:100")
        self.assertIsNone(outbound_none)

        orchestrator_empty = CodexOrchestrator(invoke_fn=lambda _: "  ")
        outbound_empty = orchestrator_empty.handle_message(_inbound("2"), session_id="telegram:100")
        self.assertIsNone(outbound_empty)

    def test_invoke_failure_returns_none_and_records_diagnostic(self) -> None:
        def _invoke(_: CodexInvocationRequest) -> str | None:
            raise RuntimeError("codex unavailable")

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)
        outbound = orchestrator.handle_message(_inbound("1"), session_id="telegram:100")

        self.assertIsNone(outbound)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "codex-exec-failed")
        self.assertTrue(diagnostics[0]["retryable"])
        self.assertIn("RuntimeError: codex unavailable", diagnostics[0]["message"])

    def test_context_subsystem_failure_records_context_layer_and_operation(self) -> None:
        def _invoke(_: CodexInvocationRequest) -> str | None:
            raise ContextStoreError(
                "context store load failed",
                code="context-store-load-error",
                operation="load_transcript",
            )

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)
        outbound = orchestrator.handle_message(_inbound("1"), session_id="telegram:100")

        self.assertIsNone(outbound)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "context-store-load-error")
        self.assertTrue(diagnostics[0]["retryable"])
        self.assertEqual(diagnostics[0]["layer"], "context")
        self.assertEqual(diagnostics[0]["operation"], "load_transcript")

    def test_invoke_failure_can_return_minimal_fallback_when_enabled(self) -> None:
        def _invoke(_: CodexInvocationRequest) -> str | None:
            raise RuntimeError("codex unavailable")

        orchestrator = CodexOrchestrator(invoke_fn=_invoke, notify_on_error=True)
        outbound = orchestrator.handle_message(_inbound("1"), session_id="telegram:100")

        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.text, "Sorry, something went wrong. Please try again.")
        self.assertEqual(outbound.reply_to_message_id, "m-1")
        self.assertEqual(outbound.metadata["orchestrator_mode"], "codex")
        self.assertEqual(outbound.metadata["error_code"], "codex-exec-failed")
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "codex-exec-failed")
        self.assertTrue(diagnostics[0]["retryable"])
        self.assertIn("RuntimeError: codex unavailable", diagnostics[0]["message"])

    def test_invalid_response_type_records_deterministic_diagnostic(self) -> None:
        orchestrator = CodexOrchestrator(invoke_fn=lambda _: {"text": "bad"})  # type: ignore[return-value]
        outbound = orchestrator.handle_message(_inbound("1"), session_id="telegram:100")

        self.assertIsNone(outbound)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "codex-invalid-response")
        self.assertFalse(diagnostics[0]["retryable"])
        self.assertIn("CodexInvalidResponseError: codex response must be a string or None", diagnostics[0]["message"])

    def test_contract_violation_maps_to_non_retryable_code(self) -> None:
        def _invoke(_: CodexInvocationRequest) -> str | None:
            raise ContractValidationError("broken contract")

        orchestrator = CodexOrchestrator(invoke_fn=_invoke)
        outbound = orchestrator.handle_message(_inbound("1"), session_id="telegram:100")

        self.assertIsNone(outbound)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "codex-contract-violation")
        self.assertFalse(diagnostics[0]["retryable"])
        self.assertIn("ContractValidationError: broken contract", diagnostics[0]["message"])

    def test_session_keyed_runtime_state_is_isolated(self) -> None:
        now = {"value": 100.0}

        def _clock() -> float:
            return now["value"]

        manager = CodexSessionManager(policy=CodexSessionPolicy(max_sessions=5, idle_ttl_s=60.0), clock=_clock)
        orchestrator = CodexOrchestrator(
            invoke_fn=lambda _: "ok",
            session_manager=manager,
        )

        orchestrator.handle_message(_inbound("1", chat_id="1"), session_id="telegram:1")
        now["value"] += 1
        orchestrator.handle_message(_inbound("2", chat_id="2"), session_id="telegram:2")
        now["value"] += 1
        orchestrator.handle_message(_inbound("3", chat_id="1"), session_id="telegram:1")

        session_one = manager.describe("telegram:1")
        session_two = manager.describe("telegram:2")
        assert session_one is not None
        assert session_two is not None
        self.assertEqual(session_one["invoke_count"], 2)
        self.assertEqual(session_two["invoke_count"], 1)
        self.assertEqual(set(manager.list_session_ids()), {"telegram:1", "telegram:2"})

    def test_idle_cleanup_and_capacity_eviction_are_deterministic(self) -> None:
        now = {"value": 0.0}

        def _clock() -> float:
            return now["value"]

        manager = CodexSessionManager(policy=CodexSessionPolicy(max_sessions=2, idle_ttl_s=5.0), clock=_clock)
        orchestrator = CodexOrchestrator(invoke_fn=lambda _: "ok", session_manager=manager)

        orchestrator.handle_message(_inbound("1", chat_id="10"), session_id="s-10")
        now["value"] += 1
        orchestrator.handle_message(_inbound("2", chat_id="20"), session_id="s-20")
        now["value"] += 5
        orchestrator.handle_message(_inbound("3", chat_id="20"), session_id="s-20")

        self.assertIsNone(manager.describe("s-10"))
        self.assertIsNotNone(manager.describe("s-20"))

        now["value"] += 1
        orchestrator.handle_message(_inbound("4", chat_id="30"), session_id="s-30")
        now["value"] += 1
        orchestrator.handle_message(_inbound("5", chat_id="40"), session_id="s-40")

        self.assertEqual(set(manager.list_session_ids()), {"s-30", "s-40"})
        self.assertIsNone(manager.describe("s-20"))

    def test_timeout_records_session_timeout_diagnostic_and_state(self) -> None:
        now = {"value": 50.0}

        def _clock() -> float:
            return now["value"]

        manager = CodexSessionManager(policy=CodexSessionPolicy(max_sessions=5, idle_ttl_s=30.0), clock=_clock)

        def _timeout(_: CodexInvocationRequest) -> str | None:
            raise TimeoutError("deadline exceeded")

        orchestrator = CodexOrchestrator(invoke_fn=_timeout, session_manager=manager)
        outbound = orchestrator.handle_message(_inbound("1", chat_id="500"), session_id="telegram:500")

        self.assertIsNone(outbound)
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(diagnostics[0]["code"], "codex-timeout")
        self.assertTrue(diagnostics[0]["retryable"])
        self.assertIn("TimeoutError: deadline exceeded", diagnostics[0]["message"])
        state = manager.describe("telegram:500")
        assert state is not None
        self.assertEqual(state["timeout_count"], 1)
        self.assertEqual(state["failure_count"], 1)
        self.assertEqual(state["invoke_count"], 0)

    def test_timeout_can_return_timeout_fallback_when_enabled(self) -> None:
        def _timeout(_: CodexInvocationRequest) -> str | None:
            raise TimeoutError("deadline exceeded")

        orchestrator = CodexOrchestrator(invoke_fn=_timeout, notify_on_error=True)
        outbound = orchestrator.handle_message(_inbound("1", chat_id="500"), session_id="telegram:500")

        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.chat_id, "500")
        self.assertEqual(outbound.text, "Sorry, the request timed out. Please try again.")
        self.assertEqual(outbound.metadata["error_code"], "codex-timeout")
        diagnostics = orchestrator.drain_diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "codex-timeout")
        self.assertTrue(diagnostics[0]["retryable"])
        self.assertIn("TimeoutError: deadline exceeded", diagnostics[0]["message"])


class TestDefaultCodexInvoke(unittest.TestCase):
    def test_invokes_codex_with_workspace_write_and_repo_parent_cd(self) -> None:
        request = CodexInvocationRequest(
            session_id="telegram:100",
            chat_id="100",
            user_id="u-1",
            text="hello",
            update_id="1",
            message_id="m-1",
            conversation_history=(),
        )

        with mock.patch("channel_runtime.codex_orchestrator.subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            output = _default_codex_invoke(request, timeout_s=3.0)

        self.assertEqual(output, "ok")
        run_mock.assert_called_once()
        args = run_mock.call_args.args[0]
        self.assertEqual(args[0:2], ["codex", "exec"])
        self.assertEqual(args[2:4], ["--sandbox", "workspace-write"])
        self.assertEqual(args[4], "--skip-git-repo-check")
        self.assertEqual(args[5], "--cd")
        self.assertEqual(args[6], "/home/cwilson/projects")

    def test_invokes_subprocess_with_provided_timeout(self) -> None:
        request = CodexInvocationRequest(
            session_id="telegram:100",
            chat_id="100",
            user_id="u-1",
            text="hello",
            update_id="1",
            message_id="m-1",
            conversation_history=(),
        )

        with mock.patch("channel_runtime.codex_orchestrator.subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            _default_codex_invoke(request, timeout_s=12.5)

        self.assertEqual(run_mock.call_args.kwargs["timeout"], 12.5)


if __name__ == "__main__":
    unittest.main()
