from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ContractValidationError, InboundMessage, OutboundMessage
from channel_core.service import process_once


@dataclass
class _AdapterStub:
    updates: list[InboundMessage] = field(default_factory=list)
    fetch_exc: Exception | None = None
    send_exc: Exception | None = None
    ack_exc_ids: set[str] = field(default_factory=set)
    sent: list[OutboundMessage] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)

    def fetch_updates(self) -> list[InboundMessage]:
        if self.fetch_exc is not None:
            raise self.fetch_exc
        return list(self.updates)

    def send_message(self, outbound: OutboundMessage) -> None:
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append(outbound)

    def ack_update(self, update_id: str) -> None:
        if update_id in self.ack_exc_ids:
            raise RuntimeError("ack failed")
        self.acked.append(update_id)


@dataclass
class _OrchestratorStub:
    responses: dict[str, Any]
    sessions: list[str] = field(default_factory=list)

    def handle_message(self, inbound: InboundMessage, *, session_id: str):
        self.sessions.append(session_id)
        return self.responses.get(inbound.update_id)


def _inbound(update_id: str, chat_id: str = "42", text: str = "hello") -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id="u-1",
        text=text,
        message_id=f"m-{update_id}",
    )


class TestContractsAndService(unittest.TestCase):
    def test_contract_validates_required_fields(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "update_id must be a non-empty string"):
            InboundMessage(update_id="", chat_id="1", user_id="2", text="x")

        with self.assertRaisesRegex(ContractValidationError, "text must be a non-empty string"):
            OutboundMessage(chat_id="1", text=" ")

    def test_process_once_empty_batch(self) -> None:
        adapter = _AdapterStub(updates=[])
        orchestrator = _OrchestratorStub(responses={})

        result = process_once(adapter, orchestrator)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "no-updates")
        self.assertEqual(result["fetched_count"], 0)
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["acked_count"], 0)
        self.assertEqual(result["error_count"], 0)

    def test_process_once_one_inbound_one_outbound(self) -> None:
        inbound = _inbound("1", chat_id="1001")
        outbound = OutboundMessage(chat_id="1001", text="ack")
        adapter = _AdapterStub(updates=[inbound])
        orchestrator = _OrchestratorStub(responses={"1": outbound})

        result = process_once(adapter, orchestrator)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "processed")
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(adapter.sent[0].text, "ack")
        self.assertEqual(adapter.acked, ["1"])
        self.assertEqual(orchestrator.sessions, ["telegram:1001"])

    def test_process_once_fetch_exception_returns_failed(self) -> None:
        adapter = _AdapterStub(fetch_exc=RuntimeError("network down"))
        orchestrator = _OrchestratorStub(responses={})

        result = process_once(adapter, orchestrator)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "adapter-fetch-exception")
        self.assertEqual(result["error_count"], 1)
        self.assertIn("RuntimeError: network down", result["errors"][0])

    def test_process_once_unsupported_orchestrator_output_continues(self) -> None:
        first = _inbound("1", chat_id="200")
        second = _inbound("2", chat_id="200")
        valid = OutboundMessage(chat_id="200", text="ok")
        adapter = _AdapterStub(updates=[first, second])
        orchestrator = _OrchestratorStub(responses={"1": {"unexpected": True}, "2": valid})

        result = process_once(adapter, orchestrator)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["fetched_count"], 2)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["acked_count"], 2)
        self.assertEqual(result["error_count"], 1)
        self.assertIn("unsupported output type", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
