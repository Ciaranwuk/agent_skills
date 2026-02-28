from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ContractValidationError, InboundMessage
from channel_runtime.codex_orchestrator import (
    CodexInvocationRequest,
    CodexOrchestrator,
    CodexSessionManager,
    CodexSessionPolicy,
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


if __name__ == "__main__":
    unittest.main()
