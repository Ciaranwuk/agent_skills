# Telegram Channel Architecture and Flow

Date: 2026-02-28  
Scope: TG-LIVE behavior through D2/E2

## 1) What Fits Where

### `channel_core` (provider-agnostic core)
- `channel_core/contracts.py`
  - Defines `InboundMessage`, `OutboundMessage`, `ChannelAdapterPort`, `OrchestratorPort`.
- `channel_core/service.py`
  - Runs one deterministic cycle with `process_once(...)`.
  - Enforces ack policy: `always` or `on-success`.
  - Returns stable cycle shape (`status`, `reason`, counts, `errors`).

### `telegram_channel` (Telegram transport adapter)
- `telegram_channel/api.py`
  - Bot API client (`getUpdates`, `sendMessage`) with bounded retries.
  - Converts transport/API failures into structured `TelegramApiError`.
- `telegram_channel/update_parser.py`
  - Parses only `message.text` updates into `InboundMessage`.
  - Skips unsupported payloads deterministically.
- `telegram_channel/adapter.py`
  - Implements `ChannelAdapterPort` on top of Telegram API + parser.
  - Tracks seen/pending/processed IDs and next poll offset.
  - Emits diagnostics for stale drops and cursor-state I/O issues.
- `telegram_channel/cursor_state.py`
  - Durable JSON cursor floor store (`committed_floor`) for restart safety.

### `channel_runtime` (wiring, policy, and runtime outputs)
- `channel_runtime/config.py`
  - Parses env + CLI and validates runtime policy.
- `channel_runtime/runner.py`
  - Wires adapter + orchestrator + allowlist gate + heartbeat emitter.
  - Enriches service result with `dropped_updates`, `error_details`, `telemetry`.
- `channel_runtime/codex_orchestrator.py`
  - Codex-backed `OrchestratorPort` implementation.
  - Handles session lifecycle and classifies codex failures (`codex-timeout`, `codex-exec-failed`, etc).

### `heartbeat_system` interaction
- Runtime failure event path uses `heartbeat_system.api.publish_system_event(...)` from `runner.py` default emitter.
- Emission is best-effort; cycle payload always returns even if emit fails.
- Runtime payload includes `heartbeat_emit_failures` and `telemetry.heartbeat.emit_state`.

## 2) Runtime Config Contract (Current)

Config source precedence: CLI overrides env; env overrides defaults.

- Required:
  - `CHANNEL_TOKEN` / `--token`
- Core:
  - `CHANNEL_MODE` / `--mode` (`poll` only)
  - `CHANNEL_POLL_INTERVAL_S` / `--poll-interval-s` (>0)
  - `CHANNEL_ONCE` / `--once`
- Service policy:
  - `CHANNEL_ACK_POLICY` / `--ack-policy` (`always` or `on-success`)
- Orchestrator:
  - `CHANNEL_ORCHESTRATOR_MODE` / `--orchestrator-mode` (`default` or `codex`)
  - `CHANNEL_CODEX_TIMEOUT_S` / `--codex-timeout-s` (>0)
  - `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR` / `--notify-on-orchestrator-error` (boolean)
  - `CHANNEL_CODEX_SESSION_MAX` / `--codex-session-max` (int >= 1)
  - `CHANNEL_CODEX_SESSION_IDLE_TTL_S` / `--codex-session-idle-ttl-s` (>0)
- Live/allowlist:
  - `CHANNEL_ALLOWED_CHAT_IDS` / `--allowed-chat-ids` (CSV)
  - `CHANNEL_LIVE_MODE` / `--live-mode` (boolean; requires non-empty allowlist when true)
- Cursor state:
  - `CHANNEL_CURSOR_STATE_PATH` / `--cursor-state-path` (empty disables persistence store)
  - `CHANNEL_STRICT_CURSOR_STATE_IO` / `--strict-cursor-state-io` (boolean)

## 3) End-to-End Flow (Sequence Style)

### Success path

```text
Telegram getUpdates
  -> telegram_channel.api.TelegramApiClient.get_updates
  -> telegram_channel.adapter.TelegramChannelAdapter.fetch_updates
     -> telegram_channel.update_parser.parse_update
  -> channel_core.service.process_once
     -> session_id = "telegram:<chat_id>"
     -> channel_runtime runner orchestrator (default/codex) handle_message
     -> adapter.send_message (if outbound)
     -> adapter.ack_update (per ack_policy)
  -> channel_runtime.runner.run_cycle
     -> append drop/error diagnostics
     -> build error_details + telemetry
     -> stdout JSON payload
```

### Failure and control paths

- Adapter fetch failure:
  - `process_once` returns `status=failed`, `reason=adapter-fetch-exception`.
  - `run_cycle` maps `error_details` and attempts heartbeat failure event.
- Codex timeout/exec failure:
  - `CodexOrchestrator` records diagnostics (`codex-timeout` or `codex-exec-failed`).
  - If notify is disabled: no outbound fallback message.
  - If notify is enabled: sends minimal fallback text and still records diagnostics.
- Allowlist drop:
  - `AllowlistGateOrchestrator` drops before delegate orchestration.
  - Update can be acked; cycle remains `status=ok`; detail appears as `category=drop`.
- Ack policy `on-success`:
  - If processing/send fails for an update, ack is skipped (`ack_skipped_count` increments).
  - Intended for safer retry posture vs `always`.
- Cursor state I/O errors:
  - Always added as adapter diagnostics.
  - With `strict_cursor_state_io=true`, state load/save failure raises runtime error path.
  - With `false`, cycle continues and surfaces diagnostics only.

## 4) Payload Surfaces Used by Operators

### Service counters
- `fetched_count`, `sent_count`, `acked_count`, `ack_skipped_count`
- `dropped_count`, `dropped_updates`

### Structured diagnostics
- `errors[]` (legacy strings, still present)
- `error_details[]` (machine-routable):
  - Required top-level keys: `code`, `message`, `retryable`, `context`, `source`, `category`, `diagnostic_id`
  - `context` keys: `update_id`, `chat_id`, `session_id`, `layer`, `operation`

### Telemetry contract
- `telemetry.contract = "tg-live.runtime.telemetry"`
- `telemetry.version = "2.0"`
- `telemetry.counters`: `fetch_total`, `send_total`, `drop_total`, `heartbeat_emit_failures` (+ placeholder counters)
- `telemetry.timers_ms.cycle_total` (+ placeholder `fetch`/`send`)
- `telemetry.heartbeat.emit_state`: `disabled`, `emitted`, `emit-failed`

### Heartbeat failure event context (when emitted)
- `context.heartbeat.emit_state`
- `context.telemetry_digest.fetch_total`
- `context.telemetry_digest.send_total`
- `context.telemetry_digest.drop_total`
- `context.telemetry_digest.cycle_total_ms`

## 5) Where To Debug

### Layer 1: Config and startup errors
- Files:
  - `channel_runtime/config.py`
  - `channel_runtime/__main__.py`
- Check:
  - Invalid config payload (`reason=invalid-config`).
  - CLI flag names and booleans.
- Command:
  - `python3 -m channel_runtime --once`

### Layer 2: Telegram transport/parsing
- Files:
  - `telegram_channel/api.py`
  - `telegram_channel/update_parser.py`
  - `telegram_channel/adapter.py`
  - `telegram_channel/cursor_state.py`
- Check:
  - `adapter-fetch-exception`, send failures, `stale-drop`, cursor diagnostics.
  - Unsupported updates dropped before core processing.
- Commands:
  - `python3 -m unittest discover -s telegram_channel/tests -p 'test_*.py'`
  - `python3 -m unittest channel_runtime.tests.test_runner`

### Layer 3: Core processing and ack behavior
- Files:
  - `channel_core/service.py`
  - `channel_core/session_map.py`
- Check:
  - `ack_policy` behavior (`always` vs `on-success`).
  - `update-processing-exception` and `ack-update-failed`.
- Command:
  - `python3 -m unittest channel_core.tests.test_service`

### Layer 4: Runtime orchestration/telemetry/eventing
- Files:
  - `channel_runtime/runner.py`
  - `channel_runtime/codex_orchestrator.py`
  - `heartbeat_system/api.py`
- Check:
  - `error_details` mapping and dedupe.
  - `telemetry` contract values.
  - heartbeat emit state/failure counting.
  - codex diagnostic codes and fallback notify behavior.
- Commands:
  - `python3 -m unittest channel_runtime.tests.test_codex_orchestrator`
  - `python3 -m unittest channel_runtime.tests.test_runner`

## 6) Related Operator Docs

- Runbook: `TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md`
- Live plan: `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md`
- E2 smoke protocol: `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md`
