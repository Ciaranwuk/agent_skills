from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ChannelRuntimeError, ConfigValidationError, InboundMessage, OutboundMessage
from channel_runtime import __main__ as runtime_main
from channel_runtime.config import RuntimeConfig, parse_runtime_config
from channel_runtime.runner import DefaultOrchestrator, HeartbeatEventEmitter, run_cycle, run_loop


class TestRuntimeConfig(unittest.TestCase):
    def test_parse_runtime_config_valid_env(self) -> None:
        env = {
            "CHANNEL_TOKEN": "token-123",
            "CHANNEL_MODE": "poll",
            "CHANNEL_POLL_INTERVAL_S": "3.5",
            "CHANNEL_ALLOWED_CHAT_IDS": "100,200",
            "CHANNEL_ONCE": "true",
        }

        cfg = parse_runtime_config([], env=env)

        self.assertEqual(cfg.token, "token-123")
        self.assertEqual(cfg.mode, "poll")
        self.assertEqual(cfg.poll_interval_s, 3.5)
        self.assertEqual(cfg.allowed_chat_ids, ("100", "200"))
        self.assertTrue(cfg.once)

    def test_cli_overrides_env(self) -> None:
        env = {"CHANNEL_TOKEN": "env-token", "CHANNEL_POLL_INTERVAL_S": "8"}

        cfg = parse_runtime_config(
            [
                "--token",
                "cli-token",
                "--poll-interval-s",
                "1.25",
                "--allowed-chat-ids",
                "44,55",
                "--once",
            ],
            env=env,
        )

        self.assertEqual(cfg.token, "cli-token")
        self.assertEqual(cfg.poll_interval_s, 1.25)
        self.assertEqual(cfg.allowed_chat_ids, ("44", "55"))
        self.assertTrue(cfg.once)

    def test_invalid_token_fails_fast(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "token must be a non-empty string"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "   "})

    def test_invalid_poll_interval_fails_fast(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "poll_interval_s must be a positive number"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_POLL_INTERVAL_S": "zero"})

        with self.assertRaisesRegex(ConfigValidationError, "poll_interval_s must be a positive number"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_POLL_INTERVAL_S": "0"})

    def test_invalid_allowlist_fails_fast(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "allowed_chat_ids must not contain empty values"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_ALLOWED_CHAT_IDS": "1,,3"})

    def test_non_poll_mode_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "mode must be 'poll' for TG-P0"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_MODE": "webhook"})

    def test_unknown_cli_argument_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "unknown argument: --bad"):
            parse_runtime_config(["--bad"], env={"CHANNEL_TOKEN": "x"})


@dataclass
class _AdapterStub:
    updates: list[InboundMessage] = field(default_factory=list)
    fetch_exc: Exception | None = None
    sent: list[OutboundMessage] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    send_call_count: int = 0
    fail_send_calls: set[int] = field(default_factory=set)

    def fetch_updates(self) -> list[InboundMessage]:
        if self.fetch_exc is not None:
            raise self.fetch_exc
        return list(self.updates)

    def send_message(self, outbound: OutboundMessage) -> None:
        self.send_call_count += 1
        if self.send_call_count in self.fail_send_calls:
            raise ChannelRuntimeError("send failed")
        self.sent.append(outbound)

    def ack_update(self, update_id: str) -> None:
        self.acked.append(update_id)


class _RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        session_key: str,
        text: str,
        source: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("heartbeat unavailable")
        payload = {
            "session_key": session_key,
            "text": text,
            "source": source,
            "context": context,
        }
        self.calls.append(payload)
        return {"status": "accepted"}


def _inbound(update_id: str, *, text: str = "hello", chat_id: str = "100") -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id="u-1",
        text=text,
        message_id=f"m-{update_id}",
    )


class TestRuntimeRunnerP2(unittest.TestCase):
    def test_default_orchestrator_echo_wires_outbound_send(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="ping", chat_id="501")])

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
            enable_memory_hook=False,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "processed")
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(adapter.sent[0].chat_id, "501")
        self.assertEqual(adapter.sent[0].text, "echo: ping")
        self.assertEqual(adapter.sent[0].reply_to_message_id, "m-1")

    def test_memory_hook_failure_is_non_fatal_and_surfaces_error(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="ping")])

        def _failing_lookup(_: str) -> str | None:
            raise RuntimeError("memory hook down")

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            enable_memory_hook=True,
            memory_lookup=_failing_lookup,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, ["1"])
        self.assertIn("RuntimeError: memory hook down", result["errors"][0])

    def test_outbound_send_failure_isolated_per_update(self) -> None:
        adapter = _AdapterStub(
            updates=[_inbound("1", text="first"), _inbound("2", text="second")],
            fail_send_calls={1},
        )

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            orchestrator=DefaultOrchestrator(enable_memory_hook=False),
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["fetched_count"], 2)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["acked_count"], 2)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, ["1", "2"])
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0].text, "echo: second")
        self.assertIn("ChannelRuntimeError: send failed", result["errors"][0])

    def test_heartbeat_emit_failure_is_best_effort_and_non_fatal(self) -> None:
        adapter = _AdapterStub(fetch_exc=RuntimeError("network down"))
        publisher = _RecordingPublisher(fail=True)

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(publish_event=publisher),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "adapter-fetch-exception")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["heartbeat_emit_failures"], 1)

    def test_heartbeat_emits_when_failure_occurs(self) -> None:
        adapter = _AdapterStub(fetch_exc=RuntimeError("network down"))
        publisher = _RecordingPublisher(fail=False)

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(publish_event=publisher),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["heartbeat_emit_failures"], 0)
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(publisher.calls[0]["session_key"], "telegram:runtime")
        self.assertIn("cycle failure", publisher.calls[0]["text"])

    def test_allowlist_drops_before_orchestration_and_send(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="hidden", chat_id="999")])

        class _ExplodingOrchestrator:
            def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
                raise RuntimeError("orchestrator should not run for disallowed chats")

        result = run_cycle(
            config=RuntimeConfig(token="tkn", allowed_chat_ids=("100", "200")),
            adapter=adapter,
            orchestrator=_ExplodingOrchestrator(),
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "processed")
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["dropped_count"], 1)
        self.assertEqual(adapter.acked, ["1"])
        self.assertEqual(len(adapter.sent), 0)
        self.assertIn("not allowlisted", result["dropped_updates"][0]["reason"])

    def test_allowlist_normalizes_numeric_chat_ids(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="ok", chat_id="42")])

        result = run_cycle(
            config=RuntimeConfig(token="tkn", allowed_chat_ids=("0042",)),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["dropped_count"], 0)


class TestRuntimeRunnerP3Loop(unittest.TestCase):
    def test_run_loop_once_executes_single_cycle(self) -> None:
        calls: list[int] = []

        def _cycle(*, config: RuntimeConfig) -> dict[str, Any]:
            calls.append(1)
            return {"status": "ok", "reason": "processed"}

        result = run_loop(
            config=RuntimeConfig(token="tkn", once=True),
            run_cycle_fn=_cycle,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)

    def test_run_loop_continuous_continues_after_failed_cycle(self) -> None:
        statuses: list[str] = []
        sleep_calls: list[float] = []
        counter = {"value": 0}

        def _cycle(*, config: RuntimeConfig) -> dict[str, Any]:
            counter["value"] += 1
            if counter["value"] == 1:
                return {"status": "failed", "reason": "adapter-fetch-exception"}
            return {"status": "ok", "reason": "processed"}

        def _on_cycle(result: dict[str, Any]) -> None:
            statuses.append(str(result.get("status")))

        result = run_loop(
            config=RuntimeConfig(token="tkn", once=False, poll_interval_s=1.5),
            run_cycle_fn=_cycle,
            sleep_fn=sleep_calls.append,
            on_cycle=_on_cycle,
            max_cycles=2,
        )

        self.assertEqual(statuses, ["failed", "ok"])
        self.assertEqual(sleep_calls, [1.5])
        self.assertEqual(result["status"], "ok")

    def test_main_once_mode_exits_cleanly_after_one_cycle(self) -> None:
        calls: list[int] = []

        def _fake_run_loop(*, config: RuntimeConfig, **_: Any) -> dict[str, Any]:
            calls.append(1)
            return {"status": "ok", "reason": "processed", "fetched_count": 1}

        with patch.object(runtime_main, "run_loop", _fake_run_loop), patch(
            "sys.stdout.write"
        ) as stdout_write:
            code = runtime_main.main(["--token", "abc", "--once"])

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        written = "".join(str(call.args[0]) for call in stdout_write.call_args_list)
        self.assertIn('"status": "ok"', written)


if __name__ == "__main__":
    unittest.main()
