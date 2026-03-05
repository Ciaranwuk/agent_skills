# Telegram Channel Architecture and Flow

Date: 2026-03-05  
Scope: TG-LIVE behavior through D2/E2 and TG-CTX-P3 context management updates

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
- `channel_runtime/context/` (durable context subsystem in TG-CTX-P3)
  - `store.py`: JSONL transcript persistence + metadata with strict/non-strict corruption handling.
  - `assembler.py`: deterministic durable transcript to conversation-history assembly.
  - `compaction.py`: threshold/manual compaction planning and transcript rewrite path.
  - `token_estimator.py`: token estimates for assembled windows and telemetry gauges.
  - `errors.py`: context error taxonomy and mapping to runtime-compatible `error_details`.
  - `contracts.py` / `types.py`: subsystem ports + context dataclasses + context-store diagnostics.

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
- Context subsystem:
  - `CHANNEL_CONTEXT_MODE` / `--context-mode` (`legacy` default, or `durable`)
  - `CHANNEL_CONTEXT_WINDOW_TOKENS` / `--context-window-tokens` (default `128000`)
  - `CHANNEL_CONTEXT_RESERVE_TOKENS` / `--context-reserve-tokens` (default `16000`)
  - `CHANNEL_CONTEXT_KEEP_RECENT_TOKENS` / `--context-keep-recent-tokens` (default `24000`)
  - `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` / `--context-summary-max-tokens` (default `1200`)
  - `CHANNEL_CONTEXT_MIN_GAIN_TOKENS` / `--context-min-gain-tokens` (default `800`)
  - `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S` / `--context-compaction-cooldown-s` (default `300`)
  - `CHANNEL_CONTEXT_STRICT_IO` / `--context-strict-io` (default `false`)
  - `CHANNEL_CONTEXT_MANUAL_COMPACT` / `--context-manual-compact` (default `false`)

Context defaults and current effective behavior:
- Default mode remains `legacy`; durable store/compaction paths are disabled unless `CHANNEL_CONTEXT_MODE=durable`.
- `CHANNEL_CONTEXT_STRICT_IO` is effective in durable mode (corruption/load behavior).
- `CHANNEL_CONTEXT_MANUAL_COMPACT=true` enables `/ctx inspect` and `/ctx compact` interception in codex mode.
- Current runtime wiring uses fixed compaction policy values (`min_compaction_gain_tokens=0`, `cooldown_window_s=0.0`) when constructing the codex durable orchestrator; parsed values for `CHANNEL_CONTEXT_MIN_GAIN_TOKENS`, `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S`, and `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` are reserved for subsequent policy wiring.
- Legacy emergency path retention contract (TG-CTX-P4-T03):
  - `CHANNEL_CONTEXT_MODE=legacy` is the explicit kill-switch for emergency rollback.
  - Retention window is one release cycle after 2026-03-05.
  - Legacy mode removal requires all conditions:
    - durable canary and full rollout complete that release cycle without unresolved stability regressions;
    - no unresolved sev-1/sev-2 incidents attributable to durable context behavior;
    - rollback/runbook validation completes without requiring legacy mode.

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
- Context transcript corruption and strict I/O:
  - `ContextStore.load_transcript(...)` validates each JSONL line.
  - With `CHANNEL_CONTEXT_STRICT_IO=false` (default), malformed lines are skipped and recorded as context-store diagnostics.
  - With `CHANNEL_CONTEXT_STRICT_IO=true`, malformed transcript lines raise `context-store-load-error` (non-retryable) and fail the durable path.
- Manual operator context commands:
  - Enabled only when all are true: `CHANNEL_ORCHESTRATOR_MODE=codex`, `CHANNEL_CONTEXT_MODE=durable`, `CHANNEL_CONTEXT_MANUAL_COMPACT=true`.
  - Commands: `/ctx inspect` (`/context inspect`) and `/ctx compact` (`/context compact`).
  - Report format fields: `session_id`, `status`, `reason`, `tokens_before`, `tokens_after`, `gained_tokens`, `turns_before`, `turns_after`.

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
- `telemetry.context.mode`: `legacy` or `durable`
- `telemetry.context.compaction`:
  - `attempted_total`, `succeeded_total`, `failed_total`, `fallback_used_total`
  - `reasons.threshold_total`, `reasons.overflow_total`, `reasons.manual_total`
- `telemetry.context.tokens`:
  - `estimated_total`, `build_failures_total`
  - `current_estimate`, `summary_estimate`, `recent_estimate`

### Runtime digest
- `runtime_digest.context_mode`
- `runtime_digest.context_compaction`:
  - `attempted_total`, `succeeded_total`, `failed_total`, `fallback_used_total`
- `runtime_digest.context_tokens`:
  - `estimated_total`, `build_failures_total`, `current_estimate`, `summary_estimate`, `recent_estimate`

### Heartbeat failure event context (when emitted)
- `context.heartbeat.emit_state`
- `context.telemetry_digest.fetch_total`
- `context.telemetry_digest.send_total`
- `context.telemetry_digest.drop_total`
- `context.telemetry_digest.cycle_total_ms`
- `context.telemetry_digest.context_mode`
- `context.telemetry_digest.context_compaction_attempted_total`
- `context.telemetry_digest.context_compaction_succeeded_total`
- `context.telemetry_digest.context_compaction_failed_total`
- `context.telemetry_digest.context_compaction_fallback_used_total`
- `context.telemetry_digest.context_compaction_reason_threshold_total`
- `context.telemetry_digest.context_compaction_reason_overflow_total`
- `context.telemetry_digest.context_compaction_reason_manual_total`
- `context.telemetry_digest.context_tokens_estimated_total`
- `context.telemetry_digest.context_tokens_build_failures_total`
- `context.telemetry_digest.context_current_tokens_estimate`
- `context.telemetry_digest.context_summary_tokens_estimate`
- `context.telemetry_digest.context_recent_tokens_estimate`

### Context diagnostics taxonomy
- Context subsystem core codes:
  - `context-store-load-error` (`operation=store_load`, retryable depends on failure type)
  - `context-store-save-error` (`operation=store_save`, retryable depends on failure type)
  - `context-assembler-error` (`operation=assemble`, non-retryable)
  - `context-estimator-error` (`operation=estimate`, non-retryable)
  - `context-compaction-error` (`operation=compact`, retryable)
- Context store non-strict parse diagnostics (line-level skip diagnostics):
  - `context-store-malformed-line`
  - `context-store-invalid-record`
  - `context-store-session-mismatch`
- Operator control diagnostics:
  - `context-operator-command-error` when inspect/compact command handling fails.

## 5) Rollback to Legacy Context Mode

Use this rollback whenever durable-context behavior regresses canary/runtime stability:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Expected rollback effect:
- Durable context store and compaction paths are bypassed.
- `/ctx inspect` and `/ctx compact` are not intercepted by orchestrator control path.
- Payload continues to include `runtime_digest` and `telemetry`; context fields reflect `context_mode=legacy` with zeroed compaction counters.
- Rollback expectation for operators: keep this kill-switch available for the one-release retention window; invoke immediately on durable regressions and restart runtime with updated env.

## 6) Where To Debug

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

## 7) Related Operator Docs

- Runbook: `TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md`
- Live plan: `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md`
- E2 smoke protocol: `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md`
