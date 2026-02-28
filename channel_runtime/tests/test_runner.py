from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import ChannelRuntimeError, ConfigValidationError, InboundMessage, OutboundMessage
from channel_runtime.codex_orchestrator import CodexInvocationRequest
from channel_runtime import __main__ as runtime_main
from channel_runtime.config import RuntimeConfig, parse_runtime_config
from channel_runtime.runner import DefaultOrchestrator, HeartbeatEventEmitter, run_cycle, run_loop
from telegram_channel.adapter import TelegramChannelAdapter
from telegram_channel.api import TelegramApiError


class TestRuntimeConfig(unittest.TestCase):
    def test_parse_runtime_config_valid_env(self) -> None:
        env = {
            "CHANNEL_TOKEN": "token-123",
            "CHANNEL_MODE": "poll",
            "CHANNEL_ACK_POLICY": "on-success",
            "CHANNEL_ORCHESTRATOR_MODE": "codex",
            "CHANNEL_CODEX_TIMEOUT_S": "9.25",
            "CHANNEL_CODEX_SESSION_MAX": "64",
            "CHANNEL_CODEX_SESSION_IDLE_TTL_S": "1200",
            "CHANNEL_POLL_INTERVAL_S": "3.5",
            "CHANNEL_ALLOWED_CHAT_IDS": "100,200",
            "CHANNEL_CURSOR_STATE_PATH": "/tmp/tg-cursor.json",
            "CHANNEL_STRICT_CURSOR_STATE_IO": "true",
            "CHANNEL_LIVE_MODE": "true",
            "CHANNEL_ONCE": "true",
        }

        cfg = parse_runtime_config([], env=env)

        self.assertEqual(cfg.token, "token-123")
        self.assertEqual(cfg.mode, "poll")
        self.assertEqual(cfg.ack_policy, "on-success")
        self.assertEqual(cfg.orchestrator_mode, "codex")
        self.assertEqual(cfg.codex_timeout_s, 9.25)
        self.assertEqual(cfg.codex_session_max, 64)
        self.assertEqual(cfg.codex_session_idle_ttl_s, 1200.0)
        self.assertEqual(cfg.poll_interval_s, 3.5)
        self.assertEqual(cfg.allowed_chat_ids, ("100", "200"))
        self.assertEqual(cfg.cursor_state_path, "/tmp/tg-cursor.json")
        self.assertTrue(cfg.strict_cursor_state_io)
        self.assertTrue(cfg.live_mode)
        self.assertTrue(cfg.once)

    def test_cli_overrides_env(self) -> None:
        env = {
            "CHANNEL_TOKEN": "env-token",
            "CHANNEL_POLL_INTERVAL_S": "8",
            "CHANNEL_LIVE_MODE": "false",
        }

        cfg = parse_runtime_config(
            [
                "--token",
                "cli-token",
                "--poll-interval-s",
                "1.25",
                "--orchestrator-mode",
                "codex",
                "--ack-policy",
                "on-success",
                "--codex-timeout-s",
                "4.0",
                "--codex-session-max",
                "9",
                "--codex-session-idle-ttl-s",
                "22.5",
                "--allowed-chat-ids",
                "44,55",
                "--cursor-state-path",
                "",
                "--strict-cursor-state-io",
                "true",
                "--live-mode",
                "true",
                "--once",
            ],
            env=env,
        )

        self.assertEqual(cfg.token, "cli-token")
        self.assertEqual(cfg.poll_interval_s, 1.25)
        self.assertEqual(cfg.ack_policy, "on-success")
        self.assertEqual(cfg.orchestrator_mode, "codex")
        self.assertEqual(cfg.codex_timeout_s, 4.0)
        self.assertEqual(cfg.codex_session_max, 9)
        self.assertEqual(cfg.codex_session_idle_ttl_s, 22.5)
        self.assertEqual(cfg.allowed_chat_ids, ("44", "55"))
        self.assertEqual(cfg.cursor_state_path, "")
        self.assertTrue(cfg.strict_cursor_state_io)
        self.assertTrue(cfg.live_mode)
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

    def test_live_mode_allowlist_matrix(self) -> None:
        cfg = parse_runtime_config([], env={"CHANNEL_TOKEN": "x"})
        self.assertFalse(cfg.live_mode)
        self.assertEqual(cfg.allowed_chat_ids, ())

        cfg = parse_runtime_config(
            [],
            env={
                "CHANNEL_TOKEN": "x",
                "CHANNEL_LIVE_MODE": "false",
                "CHANNEL_ALLOWED_CHAT_IDS": "",
            },
        )
        self.assertFalse(cfg.live_mode)
        self.assertEqual(cfg.allowed_chat_ids, ())

        with self.assertRaisesRegex(
            ConfigValidationError,
            "allowed_chat_ids must be non-empty when live_mode is enabled",
        ):
            parse_runtime_config(
                [],
                env={
                    "CHANNEL_TOKEN": "x",
                    "CHANNEL_LIVE_MODE": "true",
                    "CHANNEL_ALLOWED_CHAT_IDS": "",
                },
            )

        cfg = parse_runtime_config(
            [],
            env={
                "CHANNEL_TOKEN": "x",
                "CHANNEL_LIVE_MODE": "true",
                "CHANNEL_ALLOWED_CHAT_IDS": "9001",
            },
        )
        self.assertTrue(cfg.live_mode)
        self.assertEqual(cfg.allowed_chat_ids, ("9001",))

    def test_invalid_live_mode_boolean_fails_fast(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "live_mode must be a boolean"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_LIVE_MODE": "sometimes"})

    def test_non_poll_mode_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "mode must be 'poll' for TG-P0"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_MODE": "webhook"})

    def test_invalid_ack_policy_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "ack_policy must be 'always' or 'on-success'"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_ACK_POLICY": "unsafe"})

    def test_invalid_orchestrator_mode_rejected(self) -> None:
        with self.assertRaisesRegex(
            ConfigValidationError, "orchestrator_mode must be 'default' or 'codex'"
        ):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_ORCHESTRATOR_MODE": "unknown"})

    def test_orchestrator_mode_whitespace_is_normalized(self) -> None:
        for raw_mode in ("codex ", " codex", "\tcodex\n"):
            with self.subTest(raw_mode=raw_mode):
                cfg = parse_runtime_config(
                    [],
                    env={"CHANNEL_TOKEN": "x", "CHANNEL_ORCHESTRATOR_MODE": raw_mode},
                )
                self.assertEqual(cfg.orchestrator_mode, "codex")

    def test_invalid_codex_timeout_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "codex_timeout_s must be a positive number"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_CODEX_TIMEOUT_S": "0"})

    def test_invalid_codex_session_policy_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "codex_session_max must be an integer >= 1"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_CODEX_SESSION_MAX": "0"})
        with self.assertRaisesRegex(ConfigValidationError, "codex_session_idle_ttl_s must be a positive number"):
            parse_runtime_config([], env={"CHANNEL_TOKEN": "x", "CHANNEL_CODEX_SESSION_IDLE_TTL_S": "0"})

    def test_unknown_cli_argument_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigValidationError, "unknown argument: --bad"):
            parse_runtime_config(["--bad"], env={"CHANNEL_TOKEN": "x"})


@dataclass
class _AdapterStub:
    updates: list[InboundMessage] = field(default_factory=list)
    fetch_exc: Exception | None = None
    sent: list[OutboundMessage] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    fail_ack_ids: set[str] = field(default_factory=set)
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
        if update_id in self.fail_ack_ids:
            raise ChannelRuntimeError("ack failed")
        self.acked.append(update_id)


@dataclass
class _DiagnosticAdapterStub:
    updates: list[InboundMessage] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    sent: list[OutboundMessage] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)

    def fetch_updates(self) -> list[InboundMessage]:
        return list(self.updates)

    def send_message(self, outbound: OutboundMessage) -> None:
        self.sent.append(outbound)

    def ack_update(self, update_id: str) -> None:
        self.acked.append(update_id)

    def drain_diagnostics(self) -> list[dict[str, str]]:
        drained = list(self.diagnostics)
        self.diagnostics.clear()
        return drained


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


class _PollingApiStub:
    def __init__(
        self,
        *,
        batches: list[list[dict[str, Any]]],
        fail_send_text_once: set[str] | None = None,
    ) -> None:
        self._batches = [list(batch) for batch in batches]
        self._fail_send_text_once = set(fail_send_text_once or set())
        self.offset_calls: list[int | None] = []
        self.sent_payloads: list[dict[str, Any]] = []

    def get_updates(self, *, offset=None, timeout_s=0, limit=100, allowed_updates=None):
        self.offset_calls.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, *, chat_id, text, reply_to_message_id=None):
        if text in self._fail_send_text_once:
            self._fail_send_text_once.remove(text)
            raise TelegramApiError(
                operation="sendMessage",
                kind="http-error",
                transient=True,
                description="temporary send failure",
                status_code=503,
                retry_class="transient",
            )
        self.sent_payloads.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return {"message_id": 999}


def _inbound(update_id: str, *, text: str = "hello", chat_id: str = "100") -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id="u-1",
        text=text,
        message_id=f"m-{update_id}",
    )


class TestRuntimeRunnerP2(unittest.TestCase):
    def test_integration_multicycle_ack_send_drop_invariants(self) -> None:
        api = _PollingApiStub(
            batches=[
                [
                    {
                        "update_id": 1,
                        "message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": 200}, "text": "first"},
                    },
                    {
                        "update_id": 2,
                        "message": {"message_id": 2, "chat": {"id": 999}, "from": {"id": 200}, "text": "drop-me"},
                    },
                ],
                [
                    {
                        "update_id": 1,
                        "message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": 200}, "text": "first"},
                    },
                    {
                        "update_id": 3,
                        "message": {"message_id": 3, "chat": {"id": 100}, "from": {"id": 200}, "text": "retry-me"},
                    },
                ],
                [
                    {
                        "update_id": 3,
                        "message": {"message_id": 3, "chat": {"id": 100}, "from": {"id": 200}, "text": "retry-me"},
                    },
                    {
                        "update_id": 4,
                        "message": {"message_id": 4, "chat": {"id": 999}, "from": {"id": 200}, "text": "drop-me"},
                    },
                ],
            ],
            fail_send_text_once={"echo: retry-me"},
        )
        adapter = TelegramChannelAdapter(api)
        config = RuntimeConfig(token="tkn", ack_policy="on-success", allowed_chat_ids=("100",))

        first = run_cycle(config=config, adapter=adapter, heartbeat_emitter=HeartbeatEventEmitter(enabled=False))
        second = run_cycle(config=config, adapter=adapter, heartbeat_emitter=HeartbeatEventEmitter(enabled=False))
        third = run_cycle(config=config, adapter=adapter, heartbeat_emitter=HeartbeatEventEmitter(enabled=False))

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["sent_count"], 1)
        self.assertEqual(first["acked_count"], 2)
        self.assertEqual(first["ack_skipped_count"], 0)
        self.assertEqual(first["dropped_count"], 1)

        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["sent_count"], 0)
        self.assertEqual(second["acked_count"], 0)
        self.assertEqual(second["ack_skipped_count"], 1)
        self.assertEqual(second["dropped_count"], 1)
        self.assertEqual(second["reason"], "completed-with-errors")

        self.assertEqual(third["status"], "ok")
        self.assertEqual(third["sent_count"], 1)
        self.assertEqual(third["acked_count"], 2)
        self.assertEqual(third["ack_skipped_count"], 0)
        self.assertEqual(third["dropped_count"], 1)
        self.assertEqual(api.offset_calls, [None, 3, 3])
        self.assertEqual([payload["text"] for payload in api.sent_payloads], ["echo: first", "echo: retry-me"])

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
        self.assertEqual(result["ack_skipped_count"], 0)
        self.assertEqual(result["error_details"], [])
        self.assertEqual(adapter.sent[0].chat_id, "501")
        self.assertEqual(adapter.sent[0].text, "echo: ping")
        self.assertEqual(adapter.sent[0].reply_to_message_id, "m-1")
        self.assertIn("telemetry", result)
        self.assertEqual(result["telemetry"]["contract"], "tg-live.runtime.telemetry")
        self.assertEqual(result["telemetry"]["version"], "2.0")
        self.assertEqual(result["telemetry"]["heartbeat"]["emit_state"], "disabled")
        self.assertEqual(result["telemetry"]["counters"]["fetch_total"], result["fetched_count"])
        self.assertEqual(result["telemetry"]["counters"]["send_total"], result["sent_count"])
        self.assertEqual(result["telemetry"]["counters"]["drop_total"], result["dropped_count"])
        self.assertEqual(
            result["telemetry"]["counters"]["heartbeat_emit_failures"],
            result["heartbeat_emit_failures"],
        )
        self.assertIsNone(result["telemetry"]["counters"]["retry_total"])
        self.assertIsNone(result["telemetry"]["counters"]["queue_depth"])
        self.assertIsNone(result["telemetry"]["counters"]["worker_restart_total"])
        self.assertIsNone(result["telemetry"]["timers_ms"]["fetch"])
        self.assertIsNone(result["telemetry"]["timers_ms"]["send"])
        self.assertGreaterEqual(result["telemetry"]["timers_ms"]["cycle_total"], 0)
        self.assertEqual(
            result["telemetry"]["placeholders"]["retry_total"],
            "pending-provider-attempt-instrumentation",
        )
        self.assertEqual(
            result["telemetry"]["placeholders"]["queue_depth"],
            "pending-runtime-queue-introspection",
        )
        self.assertEqual(
            result["telemetry"]["placeholders"]["worker_restart_total"],
            "pending-supervisor-integration",
        )

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
        self.assertEqual(result["ack_skipped_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, ["1"])
        self.assertIn("RuntimeError: memory hook down", result["errors"][0])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "orchestrator-error")
        self.assertFalse(detail["retryable"])
        self.assertEqual(detail["source"], "orchestrator.diagnostics")
        self.assertEqual(detail["category"], "error")
        self.assertEqual(detail["context"]["layer"], "orchestrator")
        self.assertEqual(detail["context"]["operation"], "handle_message")
        self.assertEqual(detail["context"]["update_id"], "1")
        self.assertEqual(detail["context"]["chat_id"], "")
        self.assertEqual(detail["context"]["session_id"], "")
        self.assertTrue(detail["diagnostic_id"])

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
        self.assertEqual(result["ack_skipped_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, ["1", "2"])
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0].text, "echo: second")
        self.assertIn("ChannelRuntimeError: send failed", result["errors"][0])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "update-processing-exception")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "process_once")
        self.assertEqual(detail["context"]["layer"], "service")
        self.assertEqual(detail["context"]["operation"], "send_message")
        self.assertEqual(detail["context"]["update_id"], "1")
        self.assertEqual(detail["category"], "error")

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
        self.assertEqual(result["telemetry"]["heartbeat"]["emit_state"], "emit-failed")
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "adapter-fetch-exception")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "process_once")
        self.assertEqual(detail["context"]["layer"], "service")
        self.assertEqual(detail["context"]["operation"], "fetch_updates")

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
        self.assertEqual(result["telemetry"]["heartbeat"]["emit_state"], "emitted")
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(publisher.calls[0]["session_key"], "telegram:runtime")
        self.assertIn("cycle failure", publisher.calls[0]["text"])
        context = publisher.calls[0]["context"]
        self.assertIn("heartbeat", context)
        self.assertEqual(context["heartbeat"]["emit_state"], "emitted")
        self.assertIn("telemetry_digest", context)
        self.assertIn("fetch_total", context["telemetry_digest"])
        self.assertIn("send_total", context["telemetry_digest"])
        self.assertIn("drop_total", context["telemetry_digest"])
        self.assertIn("cycle_total_ms", context["telemetry_digest"])
        self.assertNotIn("telemetry", context)

    def test_heartbeat_emit_state_disabled_when_emitter_disabled_on_failure(self) -> None:
        adapter = _AdapterStub(fetch_exc=RuntimeError("network down"))
        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["heartbeat_emit_failures"], 0)
        self.assertEqual(result["telemetry"]["heartbeat"]["emit_state"], "disabled")

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
        self.assertEqual(result["ack_skipped_count"], 0)
        self.assertEqual(adapter.acked, ["1"])
        self.assertEqual(len(adapter.sent), 0)
        self.assertIn("not allowlisted", result["dropped_updates"][0]["reason"])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "allowlist-drop")
        self.assertEqual(detail["category"], "drop")
        self.assertFalse(detail["retryable"])
        self.assertEqual(detail["context"]["layer"], "gate")
        self.assertEqual(detail["context"]["operation"], "allowlist_check")

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

    def test_codex_mode_uses_injected_invoke_fn(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="solve", chat_id="77")])
        requests: list[CodexInvocationRequest] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            requests.append(request)
            return "codex: done"

        result = run_cycle(
            config=RuntimeConfig(token="tkn", orchestrator_mode="codex"),
            adapter=adapter,
            codex_invoke=_invoke,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "processed")
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(adapter.sent[0].text, "codex: done")
        self.assertEqual(adapter.sent[0].metadata.get("orchestrator_mode"), "codex")
        self.assertEqual(requests[0].session_id, "telegram:77")
        self.assertEqual(requests[0].text, "solve")

    def test_codex_mode_with_whitespace_is_resolved_deterministically(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="solve", chat_id="77")])
        requests: list[CodexInvocationRequest] = []

        def _invoke(request: CodexInvocationRequest) -> str | None:
            requests.append(request)
            return "codex: done"

        result = run_cycle(
            config=RuntimeConfig(token="tkn", orchestrator_mode=" codex "),
            adapter=adapter,
            codex_invoke=_invoke,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(adapter.sent[0].text, "codex: done")
        self.assertEqual(requests[0].session_id, "telegram:77")

    def test_codex_mode_timeout_is_non_fatal_and_deterministic(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="slow", chat_id="42")])

        def _timeout(_: CodexInvocationRequest) -> str | None:
            raise TimeoutError("codex timed out")

        result = run_cycle(
            config=RuntimeConfig(token="tkn", orchestrator_mode="codex"),
            adapter=adapter,
            codex_invoke=_timeout,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(result["ack_skipped_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, ["1"])
        self.assertIn("TimeoutError: codex timed out", result["errors"][0])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "codex-timeout")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "orchestrator.diagnostics")
        self.assertEqual(detail["context"]["layer"], "orchestrator")
        self.assertEqual(detail["context"]["operation"], "handle_message")
        self.assertEqual(detail["context"]["session_id"], "telegram:42")

    def test_on_success_ack_policy_skips_ack_when_send_fails(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="first")], fail_send_calls={1})

        result = run_cycle(
            config=RuntimeConfig(token="tkn", ack_policy="on-success"),
            adapter=adapter,
            orchestrator=DefaultOrchestrator(enable_memory_hook=False),
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["acked_count"], 0)
        self.assertEqual(result["ack_skipped_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(adapter.acked, [])

    def test_ack_failure_maps_to_structured_detail(self) -> None:
        adapter = _AdapterStub(updates=[_inbound("1", text="first")], fail_ack_ids={"1"})

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            orchestrator=DefaultOrchestrator(enable_memory_hook=False),
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "ack-update-failed")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "process_once")
        self.assertEqual(detail["context"]["layer"], "service")
        self.assertEqual(detail["context"]["operation"], "ack_update")
        self.assertEqual(detail["context"]["update_id"], "1")
        self.assertEqual(detail["category"], "error")

    def test_adapter_cursor_state_diagnostics_are_surfaced(self) -> None:
        adapter = _DiagnosticAdapterStub(
            diagnostics=[
                {"code": "cursor-state-load-error", "message": "cursor_state_load failed: read failed"},
                {"code": "cursor-state-save-error", "message": "cursor_state_save failed: write failed"},
            ]
        )
        publisher = _RecordingPublisher(fail=False)

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(publish_event=publisher),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "completed-with-errors")
        self.assertEqual(result["error_count"], 2)
        self.assertIn("cursor_state_load failed: read failed", result["errors"])
        self.assertIn("cursor_state_save failed: write failed", result["errors"])
        self.assertEqual(len(result["error_details"]), 2)
        self.assertEqual(result["error_details"][0]["code"], "cursor-state-load-error")
        self.assertEqual(result["error_details"][0]["context"]["operation"], "cursor_state_load")
        self.assertTrue(result["error_details"][0]["retryable"])
        self.assertEqual(result["error_details"][1]["code"], "cursor-state-save-error")
        self.assertEqual(result["error_details"][1]["context"]["operation"], "cursor_state_save")
        self.assertTrue(result["error_details"][1]["retryable"])
        self.assertEqual(result["heartbeat_emit_failures"], 0)
        self.assertEqual(len(publisher.calls), 2)
        self.assertEqual(publisher.calls[0]["text"], "adapter failure: cursor_state_load failed: read failed")
        self.assertEqual(publisher.calls[1]["text"], "adapter failure: cursor_state_save failed: write failed")

    def test_adapter_stale_drop_is_visible_in_dropped_updates(self) -> None:
        adapter = _DiagnosticAdapterStub(
            diagnostics=[
                {
                    "code": "stale-drop",
                    "update_id": "49",
                    "message": "dropped stale update 49 below committed floor 50",
                }
            ]
        )

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "no-updates")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["dropped_count"], 1)
        self.assertEqual(result["dropped_updates"][0]["update_id"], "49")
        self.assertIn("stale update 49", result["dropped_updates"][0]["reason"])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "stale-drop")
        self.assertEqual(detail["category"], "drop")
        self.assertEqual(detail["context"]["layer"], "adapter")
        self.assertEqual(detail["context"]["operation"], "stale_filter")

    def test_adapter_diagnostics_are_not_duplicated_across_cycles(self) -> None:
        adapter = _DiagnosticAdapterStub(
            diagnostics=[{"code": "cursor-state-load-error", "message": "cursor_state_load failed: read failed"}]
        )

        first = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )
        second = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        self.assertEqual(first["error_count"], 1)
        self.assertEqual(first["reason"], "completed-with-errors")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["reason"], "no-updates")

    def test_error_details_order_and_in_cycle_exact_dedup(self) -> None:
        adapter = _DiagnosticAdapterStub(
            updates=[_inbound("1", text="first"), _inbound("2", text="second")],
            diagnostics=[
                {"code": "cursor-state-load-error", "message": "cursor_state_load failed: read failed"},
                {"code": "cursor-state-load-error", "message": "cursor_state_load failed: read failed"},
                {
                    "code": "stale-drop",
                    "update_id": "10",
                    "message": "dropped stale update 10 below committed floor 11",
                },
            ],
        )

        class _FailingOrchestrator:
            def __init__(self) -> None:
                self._diagnostics = [
                    {"code": "orchestrator-error", "update_id": "2", "message": "boom"},
                    {"code": "orchestrator-error", "update_id": "2", "message": "boom"},
                ]

            def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
                if inbound.update_id == "1":
                    raise RuntimeError("send path failure trigger")
                return OutboundMessage(chat_id=inbound.chat_id, text="ok")

            def drain_diagnostics(self) -> list[dict[str, str]]:
                drained = list(self._diagnostics)
                self._diagnostics.clear()
                return drained

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            orchestrator=_FailingOrchestrator(),
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        codes = [detail["code"] for detail in result["error_details"]]
        self.assertEqual(
            codes,
            [
                "update-processing-exception",
                "orchestrator-error",
                "cursor-state-load-error",
                "stale-drop",
            ],
        )
        self.assertEqual(len(result["error_details"]), 4)
        self.assertEqual(result["error_count"], 5)
        self.assertEqual(len(result["errors"]), 5)
        self.assertEqual(result["dropped_count"], 1)

    def test_runtime_process_once_exception_maps_structured_detail(self) -> None:
        with patch("channel_runtime.runner.process_once", side_effect=RuntimeError("hard crash")):
            result = run_cycle(
                config=RuntimeConfig(token="tkn"),
                heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "runtime-process-once-exception")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "runtime-process-once-exception")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "runtime-wrapper")
        self.assertEqual(detail["context"]["layer"], "runtime-wrapper")
        self.assertEqual(detail["context"]["operation"], "run_cycle")
        self.assertEqual(detail["context"]["update_id"], "")
        self.assertEqual(detail["context"]["chat_id"], "")
        self.assertEqual(detail["context"]["session_id"], "")
        self.assertIn("telemetry", result)
        self.assertEqual(result["telemetry"]["contract"], "tg-live.runtime.telemetry")
        self.assertEqual(result["telemetry"]["heartbeat"]["emit_state"], "disabled")

    def test_error_details_always_include_mandatory_keys(self) -> None:
        adapter = _DiagnosticAdapterStub(
            diagnostics=[{"code": "cursor-state-load-error", "message": "cursor_state_load failed: read failed"}]
        )

        result = run_cycle(
            config=RuntimeConfig(token="tkn"),
            adapter=adapter,
            heartbeat_emitter=HeartbeatEventEmitter(enabled=False),
        )

        detail = result["error_details"][0]
        self.assertEqual(
            set(detail.keys()),
            {"code", "message", "retryable", "context", "source", "category", "diagnostic_id"},
        )
        self.assertEqual(
            set(detail["context"].keys()),
            {"update_id", "chat_id", "session_id", "layer", "operation"},
        )


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

    def test_run_loop_returns_last_cycle_telemetry_unchanged(self) -> None:
        telemetry_one = {
            "contract": "tg-live.runtime.telemetry",
            "version": "2.0",
            "counters": {
                "fetch_total": 1,
                "send_total": 1,
                "retry_total": None,
                "drop_total": 0,
                "queue_depth": None,
                "worker_restart_total": None,
                "heartbeat_emit_failures": 0,
            },
            "timers_ms": {"cycle_total": 11, "fetch": None, "send": None},
            "heartbeat": {"emit_state": "disabled"},
            "placeholders": {},
        }
        telemetry_two = {
            "contract": "tg-live.runtime.telemetry",
            "version": "2.0",
            "counters": {
                "fetch_total": 2,
                "send_total": 0,
                "retry_total": None,
                "drop_total": 1,
                "queue_depth": None,
                "worker_restart_total": None,
                "heartbeat_emit_failures": 0,
            },
            "timers_ms": {"cycle_total": 7, "fetch": None, "send": None},
            "heartbeat": {"emit_state": "disabled"},
            "placeholders": {},
        }
        payloads = [
            {"status": "failed", "reason": "adapter-fetch-exception", "telemetry": telemetry_one},
            {"status": "ok", "reason": "processed", "telemetry": telemetry_two},
        ]
        counter = {"value": 0}

        def _cycle(*, config: RuntimeConfig) -> dict[str, Any]:
            idx = counter["value"]
            counter["value"] += 1
            return payloads[idx]

        result = run_loop(
            config=RuntimeConfig(token="tkn", once=False, poll_interval_s=0.1),
            run_cycle_fn=_cycle,
            sleep_fn=lambda _: None,
            max_cycles=2,
        )

        self.assertIs(result["telemetry"], telemetry_two)

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

    def test_run_loop_cycle_exception_includes_structured_error_detail(self) -> None:
        def _cycle(*, config: RuntimeConfig) -> dict[str, Any]:
            raise RuntimeError("loop crash")

        result = run_loop(
            config=RuntimeConfig(token="tkn", once=True),
            run_cycle_fn=_cycle,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "runtime-loop-cycle-exception")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["code"], "runtime-loop-cycle-exception")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["source"], "runtime-wrapper")
        self.assertEqual(detail["context"]["layer"], "runtime-wrapper")
        self.assertEqual(detail["context"]["operation"], "run_loop")

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

    def test_main_once_mode_failed_result_exits_one(self) -> None:
        def _fake_run_loop(*, config: RuntimeConfig, **_: Any) -> dict[str, Any]:
            return {"status": "failed", "reason": "adapter-fetch-exception"}

        with patch.object(runtime_main, "run_loop", _fake_run_loop):
            code = runtime_main.main(["--token", "abc", "--once"])

        self.assertEqual(code, 1)

    def test_main_invalid_config_exits_two_and_emits_payload(self) -> None:
        with patch("sys.stdout.write") as stdout_write:
            code = runtime_main.main(["--token", " "])

        self.assertEqual(code, 2)
        written = "".join(str(call.args[0]) for call in stdout_write.call_args_list)
        self.assertIn('"status": "failed"', written)
        self.assertIn('"reason": "invalid-config"', written)

    def test_main_keyboard_interrupt_exits_130(self) -> None:
        def _interrupt(*, config: RuntimeConfig, **_: Any) -> dict[str, Any]:
            raise KeyboardInterrupt()

        with patch.object(runtime_main, "run_loop", _interrupt):
            code = runtime_main.main(["--token", "abc", "--once"])

        self.assertEqual(code, 130)

    def test_main_continuous_mode_emits_on_cycle_and_returns_zero(self) -> None:
        def _fake_run_loop(*, config: RuntimeConfig, on_cycle: Callable[[dict[str, Any]], None], **_: Any) -> dict[str, Any]:
            on_cycle({"status": "failed", "reason": "adapter-fetch-exception"})
            on_cycle({"status": "ok", "reason": "processed"})
            return {"status": "ok", "reason": "processed"}

        with patch.object(runtime_main, "run_loop", _fake_run_loop), patch(
            "sys.stdout.write"
        ) as stdout_write:
            code = runtime_main.main(["--token", "abc"])

        self.assertEqual(code, 0)
        written = "".join(str(call.args[0]) for call in stdout_write.call_args_list)
        self.assertIn('"reason": "adapter-fetch-exception"', written)
        self.assertIn('"reason": "processed"', written)


if __name__ == "__main__":
    unittest.main()
