# Telegram Channel Operator Runbook

Date: 2026-03-05
Task Reference: TG-P3-3, TG-LIVE-D2, TG-LIVE-E2
Status: Finalized for current TG-P3 runtime behavior with additive TG-LIVE-D2 telemetry contract.
Architecture explainer: `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`

## 1) Purpose and Scope

### Purpose
Provide operator-ready commands and triage guidance for the implemented Telegram runtime.

### Scope
- Local runtime startup in one-cycle (`--once`) and continuous polling modes.
- Runtime config via environment variables and CLI overrides.
- Durable/legacy context mode controls and operator context commands.
- Allowlist enforcement behavior and explicit drop reporting.
- Additive runtime telemetry contract (`telemetry`) and heartbeat event digest cues.
- Runtime digest (`runtime_digest`) context counters for per-cycle P3 triage.
- TG-LIVE-E2 canary/smoke execution guidance for one-allowlisted-chat rollout.
- Troubleshooting for auth, timeout/network, parse/unsupported, outbound, and allowlist cases.

### Non-Goals
- No webhook, media, callback-query, or multi-channel operations.
- No production deployment guidance.

## 2) Runtime Entry Point and Configuration

### Entrypoint
Run the runtime with:

```bash
python3 -m channel_runtime
```

### Required Environment Variable

```bash
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
```

### Optional Environment Variables

```bash
# Must remain "poll" in current implementation.
export CHANNEL_MODE="poll"

# Ack behavior:
# - always: ack every fetched inbound update after processing attempt
# - on-success: skip ack when processing/send fails
export CHANNEL_ACK_POLICY="always"

# Seconds between cycles in continuous mode. Must be > 0.
export CHANNEL_POLL_INTERVAL_S="2.0"

# Comma-separated chat IDs. Empty/unset disables allowlist gating.
export CHANNEL_ALLOWED_CHAT_IDS="12345,-10098765"

# Require non-empty allowlist when true.
export CHANNEL_LIVE_MODE="false"

# "default" (echo) or "codex" (live Codex handoff path).
export CHANNEL_ORCHESTRATOR_MODE="default"

# Seconds for Codex handoff timeout when using codex mode.
export CHANNEL_CODEX_TIMEOUT_S="20.0"

# If true, codex orchestrator sends a minimal fallback user message on orchestrator errors/timeouts.
export CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"

# Codex session runtime lifecycle policy.
export CHANNEL_CODEX_SESSION_MAX="128"
export CHANNEL_CODEX_SESSION_IDLE_TTL_S="900.0"

# Durable cursor state (update floor) persistence path; set empty string to disable persistence.
export CHANNEL_CURSOR_STATE_PATH=".channel_runtime/telegram_cursor_state.json"

# If true, cursor state load/save I/O errors fail the cycle; otherwise surfaced as diagnostics.
export CHANNEL_STRICT_CURSOR_STATE_IO="false"

# Optional boolean (true/false/1/0/yes/no/on/off). Can also be set by --once.
export CHANNEL_ONCE="false"

# Context subsystem mode:
# - legacy (default): in-memory session history only
# - durable: JSONL transcript persistence + compaction/telemetry path
export CHANNEL_CONTEXT_MODE="legacy"

# Optional durable canary allowlist by chat ID:
# - empty/unset: durable mode applies to all chats when CHANNEL_CONTEXT_MODE=durable
# - non-empty: durable mode applies only to listed chat IDs; others stay on legacy baseline context behavior
export CHANNEL_CONTEXT_CANARY_CHAT_IDS=""

# Durable context policy inputs (parsed by runtime config).
export CHANNEL_CONTEXT_WINDOW_TOKENS="128000"
export CHANNEL_CONTEXT_RESERVE_TOKENS="16000"
export CHANNEL_CONTEXT_KEEP_RECENT_TOKENS="24000"
export CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS="1200"
export CHANNEL_CONTEXT_MIN_GAIN_TOKENS="800"
export CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S="300"

# Durable context I/O strictness:
# - false (default): malformed transcript lines are skipped and emitted as diagnostics
# - true: malformed transcript line fails load path with context-store-load-error
export CHANNEL_CONTEXT_STRICT_IO="false"

# Operator context controls:
# - false (default): /ctx inspect and /ctx compact are not intercepted
# - true: commands are handled when orchestrator=codex and context_mode=durable
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
```

Effective defaults/behavior notes:
- `CHANNEL_CONTEXT_MODE=legacy` is default rollback-safe mode.
- `CHANNEL_CONTEXT_CANARY_CHAT_IDS` only affects runtime behavior when `CHANNEL_CONTEXT_MODE=durable`.
- `CHANNEL_CONTEXT_STRICT_IO` only affects durable mode context-store load behavior.
- `CHANNEL_CONTEXT_MANUAL_COMPACT=true` only takes effect in codex + durable mode.
- `CHANNEL_CONTEXT_MIN_GAIN_TOKENS`, `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S`, and `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` are parsed and validated but currently reserved for subsequent policy wiring; current codex durable runtime wiring uses `min_compaction_gain_tokens=0` and `cooldown_window_s=0.0`.

### CLI Overrides
Supported flags:
- `--token <value>`
- `--mode <value>` (current valid value: `poll`)
- `--ack-policy <value>` (`always` or `on-success`)
- `--poll-interval-s <value>`
- `--allowed-chat-ids <csv>`
- `--live-mode <boolean>`
- `--orchestrator-mode <value>` (`default` or `codex`)
- `--codex-timeout-s <value>`
- `--notify-on-orchestrator-error <boolean>`
- `--codex-session-max <int>=1`
- `--codex-session-idle-ttl-s <seconds>`
- `--cursor-state-path <path-or-empty>`
- `--strict-cursor-state-io <boolean>`
- `--context-mode <legacy|durable>`
- `--context-canary-chat-ids <csv>`
- `--context-window-tokens <int>=1`
- `--context-reserve-tokens <int>=0`
- `--context-keep-recent-tokens <int>=1`
- `--context-summary-max-tokens <int>=1`
- `--context-min-gain-tokens <int>=0`
- `--context-compaction-cooldown-s <seconds>=0`
- `--context-strict-io <boolean>`
- `--context-manual-compact <boolean>`
- `--once`

Unknown flags or missing values return an invalid-config failure payload and exit code `2`.

## 3) Preflight Checklist

```bash
cd /home/cwilson/projects/agent_skills
python3 --version

[ -n "${CHANNEL_TOKEN:-}" ] && echo "CHANNEL_TOKEN=set" || echo "CHANNEL_TOKEN=missing"
echo "CHANNEL_MODE=${CHANNEL_MODE:-poll}"
echo "CHANNEL_POLL_INTERVAL_S=${CHANNEL_POLL_INTERVAL_S:-2.0}"
echo "CHANNEL_ALLOWED_CHAT_IDS=${CHANNEL_ALLOWED_CHAT_IDS:-<empty>}"
echo "CHANNEL_LIVE_MODE=${CHANNEL_LIVE_MODE:-false}"
echo "CHANNEL_ACK_POLICY=${CHANNEL_ACK_POLICY:-always}"
echo "CHANNEL_ORCHESTRATOR_MODE=${CHANNEL_ORCHESTRATOR_MODE:-default}"
echo "CHANNEL_CODEX_TIMEOUT_S=${CHANNEL_CODEX_TIMEOUT_S:-20.0}"
echo "CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR=${CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR:-false}"
echo "CHANNEL_CODEX_SESSION_MAX=${CHANNEL_CODEX_SESSION_MAX:-128}"
echo "CHANNEL_CODEX_SESSION_IDLE_TTL_S=${CHANNEL_CODEX_SESSION_IDLE_TTL_S:-900.0}"
echo "CHANNEL_CURSOR_STATE_PATH=${CHANNEL_CURSOR_STATE_PATH:-.channel_runtime/telegram_cursor_state.json}"
echo "CHANNEL_STRICT_CURSOR_STATE_IO=${CHANNEL_STRICT_CURSOR_STATE_IO:-false}"
echo "CHANNEL_CONTEXT_MODE=${CHANNEL_CONTEXT_MODE:-legacy}"
echo "CHANNEL_CONTEXT_CANARY_CHAT_IDS=${CHANNEL_CONTEXT_CANARY_CHAT_IDS:-<empty>}"
echo "CHANNEL_CONTEXT_WINDOW_TOKENS=${CHANNEL_CONTEXT_WINDOW_TOKENS:-128000}"
echo "CHANNEL_CONTEXT_RESERVE_TOKENS=${CHANNEL_CONTEXT_RESERVE_TOKENS:-16000}"
echo "CHANNEL_CONTEXT_KEEP_RECENT_TOKENS=${CHANNEL_CONTEXT_KEEP_RECENT_TOKENS:-24000}"
echo "CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS=${CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS:-1200}"
echo "CHANNEL_CONTEXT_MIN_GAIN_TOKENS=${CHANNEL_CONTEXT_MIN_GAIN_TOKENS:-800}"
echo "CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S=${CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S:-300}"
echo "CHANNEL_CONTEXT_STRICT_IO=${CHANNEL_CONTEXT_STRICT_IO:-false}"
echo "CHANNEL_CONTEXT_MANUAL_COMPACT=${CHANNEL_CONTEXT_MANUAL_COMPACT:-false}"
```

## 4) Run Commands

### One-Cycle Mode (`--once`)

```bash
cd /home/cwilson/projects/agent_skills
python3 -m channel_runtime --once
```

Equivalent with explicit CLI values:

```bash
python3 -m channel_runtime \
  --once \
  --token "$CHANNEL_TOKEN" \
  --mode poll \
  --ack-policy "${CHANNEL_ACK_POLICY:-always}" \
  --poll-interval-s 2.0 \
  --allowed-chat-ids "${CHANNEL_ALLOWED_CHAT_IDS:-}" \
  --orchestrator-mode "${CHANNEL_ORCHESTRATOR_MODE:-default}" \
  --codex-timeout-s "${CHANNEL_CODEX_TIMEOUT_S:-20.0}" \
  --notify-on-orchestrator-error "${CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR:-false}" \
  --codex-session-max "${CHANNEL_CODEX_SESSION_MAX:-128}" \
  --codex-session-idle-ttl-s "${CHANNEL_CODEX_SESSION_IDLE_TTL_S:-900.0}" \
  --cursor-state-path "${CHANNEL_CURSOR_STATE_PATH:-.channel_runtime/telegram_cursor_state.json}" \
  --strict-cursor-state-io "${CHANNEL_STRICT_CURSOR_STATE_IO:-false}" \
  --context-mode "${CHANNEL_CONTEXT_MODE:-legacy}" \
  --context-canary-chat-ids "${CHANNEL_CONTEXT_CANARY_CHAT_IDS:-}" \
  --context-window-tokens "${CHANNEL_CONTEXT_WINDOW_TOKENS:-128000}" \
  --context-reserve-tokens "${CHANNEL_CONTEXT_RESERVE_TOKENS:-16000}" \
  --context-keep-recent-tokens "${CHANNEL_CONTEXT_KEEP_RECENT_TOKENS:-24000}" \
  --context-summary-max-tokens "${CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS:-1200}" \
  --context-min-gain-tokens "${CHANNEL_CONTEXT_MIN_GAIN_TOKENS:-800}" \
  --context-compaction-cooldown-s "${CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S:-300}" \
  --context-strict-io "${CHANNEL_CONTEXT_STRICT_IO:-false}" \
  --context-manual-compact "${CHANNEL_CONTEXT_MANUAL_COMPACT:-false}" \
  --live-mode "${CHANNEL_LIVE_MODE:-false}"
```

Behavior:
- Executes exactly one cycle.
- Emits one JSON payload to stdout.
- Exit code is `0` when payload status is not `failed`, else `1`.

### Continuous Mode

```bash
cd /home/cwilson/projects/agent_skills
python3 -m channel_runtime
```

Behavior:
- Repeats cycles indefinitely.
- Emits one JSON payload per cycle.
- Sleeps for `poll_interval_s` between cycles.
- Continues running even if a cycle returns an error payload.
- Stop with Ctrl+C (exit code `130`).

### Restart Helper (Apply Timeout Changes)

Use the restart helper to apply updated environment settings (including `CHANNEL_CODEX_TIMEOUT_S`)
to a running continuous runtime.

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CODEX_TIMEOUT_S="30.0"
bash scripts/restart_channel_runtime.sh
```

Behavior:
- Reads the current shell environment and starts `python3 -m channel_runtime` with those values.
- Uses `.channel_runtime/channel_runtime.pid` to find and stop the previous runtime process.
- Stops with `SIGTERM` first; after a bounded wait it force-kills only if needed.
- Writes runtime logs to `artifacts/channel_runtime/channel_runtime.log`.

## 5) Payload and Exit Semantics

### Common Payload Fields
Typical payload includes:
- `status` (`ok` or `failed`)
- `reason`
- `fetched_count`
- `sent_count`
- `acked_count`
- `ack_skipped_count`
- `error_count`
- `errors`
- `error_details`
- `dropped_count`
- `dropped_updates`
- `heartbeat_emit_failures`
- `runtime_digest`
- `telemetry` (additive TG-LIVE-D2 block; legacy top-level counters remain)

### Operator Context Commands (`/ctx`)

Enable command interception:

```bash
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CONTEXT_MODE="durable"
export CHANNEL_CONTEXT_MANUAL_COMPACT="true"
```

Commands:
- `/ctx inspect` (alias: `/context inspect`)
- `/ctx compact` (alias: `/context compact`)

Expected outbound response format:
- Prefix: `context inspect:` or `context compact:`
- Fields: `session_id`, `status`, `reason`, `tokens_before`, `tokens_after`, `gained_tokens`, `turns_before`, `turns_after`

Typical status/reason combinations:
- Inspect durable existing session: `status=ok reason=session-found`
- Inspect legacy mode session: `status=ok reason=legacy-session`
- Inspect/compact missing session: `status=skipped reason=session-missing`
- Compact unavailable (legacy mode or controls disabled): `status=skipped reason=manual-compaction-unavailable`
- Internal command failure: `status=failed reason=internal-error`

### `error_details` Contract (TG-LIVE-D1)

`error_details` is additive and can include both `category="error"` and `category="drop"` items.
Each item includes:
- `code`
- `message`
- `retryable`
- `source` (for example `process_once`, `orchestrator.diagnostics`, `adapter.diagnostics`, `runtime-wrapper`)
- `category` (`error` or `drop`)
- `diagnostic_id` (stable per detail fingerprint within a cycle)
- `context` object with:
  - `update_id`
  - `chat_id`
  - `session_id`
  - `layer` (`service`, `orchestrator`, `adapter`, `gate`, `runtime-wrapper`)
  - `operation` (for example `fetch_updates`, `send_message`, `ack_update`, `handle_message`, `allowlist_check`, `stale_filter`, `cursor_state_load`, `cursor_state_save`)

### TG-LIVE-D2 Telemetry Contract (Additive)

`telemetry` is included in cycle payloads and does not replace existing top-level fields.

```json
{
  "telemetry": {
    "contract": "tg-live.runtime.telemetry",
    "version": "2.0",
    "counters": {
      "fetch_total": 0,
      "send_total": 0,
      "retry_total": null,
      "drop_total": 0,
      "queue_depth": null,
      "worker_restart_total": null,
      "heartbeat_emit_failures": 0
    },
    "timers_ms": {
      "cycle_total": 0,
      "fetch": null,
      "send": null
    },
    "context": {
      "mode": "legacy",
      "compaction": {
        "attempted_total": 0,
        "succeeded_total": 0,
        "failed_total": 0,
        "fallback_used_total": 0,
        "reasons": {
          "threshold_total": 0,
          "overflow_total": 0,
          "manual_total": 0
        }
      },
      "tokens": {
        "estimated_total": 0,
        "build_failures_total": 0,
        "current_estimate": null,
        "summary_estimate": null,
        "recent_estimate": null
      }
    },
    "heartbeat": {
      "emit_state": "disabled"
    },
    "placeholders": {
      "retry_total": "pending-provider-attempt-instrumentation",
      "queue_depth": "pending-runtime-queue-introspection",
      "worker_restart_total": "pending-supervisor-integration"
    }
  }
}
```

Operator interpretation:
- `telemetry.contract` / `telemetry.version`: contract identity (`tg-live.runtime.telemetry` / `2.0`).
- `telemetry.counters`: cycle counters; currently implemented counters are `fetch_total`, `send_total`, `drop_total`, `heartbeat_emit_failures`.
- `telemetry.timers_ms`: `cycle_total` is populated; `fetch`/`send` are currently `null` placeholders.
- `telemetry.heartbeat.emit_state`: heartbeat emission outcome summary for the cycle.
- `telemetry.context.mode`: runtime context mode (`legacy` or `durable`).
- `telemetry.context.compaction`: compaction counters and reason breakdown.
- `telemetry.context.tokens`: token estimate counters/gauges and estimator build failure counter.
- `telemetry.placeholders`: explicit strings documenting not-yet-instrumented fields.

### Runtime Digest (P3)

`runtime_digest` is included in each cycle payload for concise context-state triage:
- `runtime_digest.context_mode`
- `runtime_digest.context_compaction.attempted_total`
- `runtime_digest.context_compaction.succeeded_total`
- `runtime_digest.context_compaction.failed_total`
- `runtime_digest.context_compaction.fallback_used_total`
- `runtime_digest.context_tokens.estimated_total`
- `runtime_digest.context_tokens.build_failures_total`
- `runtime_digest.context_tokens.current_estimate`
- `runtime_digest.context_tokens.summary_estimate`
- `runtime_digest.context_tokens.recent_estimate`

### Heartbeat `emit_state` Meanings

- `disabled`: no successful heartbeat emit path was active for this cycle (emitter disabled and/or no emit-triggering failure in cycle flow).
- `emitted`: at least one heartbeat failure event emit was attempted and none failed.
- `emit-failed`: at least one heartbeat failure event emit attempt failed (best-effort path; runtime still returns cycle payload).

### Heartbeat Failure Event Context (Digest)

When a heartbeat failure event is emitted, event context includes a compact telemetry digest:
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
- `context.heartbeat.emit_state`

Use this digest for quick triage correlation when an emitted heartbeat event is available; otherwise rely on payload `telemetry`.

### Context Corruption/Error Handling and Diagnostics Taxonomy

Durable context store load path behavior:
- `CHANNEL_CONTEXT_STRICT_IO=false` (default):
  - malformed/invalid transcript lines are skipped.
  - non-fatal line diagnostics can include:
    - `context-store-malformed-line`
    - `context-store-invalid-record`
    - `context-store-session-mismatch`
- `CHANNEL_CONTEXT_STRICT_IO=true`:
  - malformed transcript line raises `context-store-load-error` (non-retryable) and fails context load.

Context subsystem diagnostic codes that can surface in `error_details[].code`:
- `context-store-load-error`
- `context-store-save-error`
- `context-assembler-error`
- `context-estimator-error`
- `context-compaction-error`
- `context-compaction-fallback`
- `context-operator-command-error`

Expected mapped context fields for taxonomy triage:
- `error_details[].context.layer`: usually `context` for subsystem events.
- `error_details[].context.operation`: one of `store_load`, `store_save`, `assemble`, `estimate`, `compact`, `inspect`.

### Expected Reasons
- `no-updates`
- `processed`
- `completed-with-errors`
- `adapter-fetch-exception`
- `runtime-process-once-exception`
- `runtime-loop-cycle-exception`
- `invalid-config` (from CLI/config parse failure path)

### Exit Codes
- `0`: successful CLI run (`--once` with non-failed status, or continuous mode normal operation)
- `1`: `--once` returned payload `status=failed`
- `2`: invalid config/arguments
- `130`: interrupted by Ctrl+C

## 6) Allowlist Behavior and Drop Semantics

Allowlist is enforced before orchestration.

When `CHANNEL_ALLOWED_CHAT_IDS` (or `--allowed-chat-ids`) is set:
- Messages from non-allowlisted `chat_id` are dropped.
- Dropped messages are not sent outbound.
- Dropped messages do not increase `error_count`.
- Drop details are surfaced in payload:
  - `dropped_count`
  - `dropped_updates[]` with `update_id`, `chat_id`, and `reason`
- Updates still proceed through service acknowledgement flow (no retry loop for dropped update IDs).

Matching notes:
- Chat IDs are normalized for numeric equivalence (for example, `42` and `0042` match).
- Empty allowlist means allowlist gate is effectively disabled.

Example drop excerpt:

```json
{
  "status": "ok",
  "reason": "processed",
  "dropped_count": 1,
  "dropped_updates": [
    {
      "update_id": "1001",
      "chat_id": "777777",
      "reason": "dropped update 1001: chat_id not allowlisted (777777)"
    }
  ]
}
```

## 7) Troubleshooting Matrix

| Category | Runtime Signature | Telemetry/Digest Cues | Likely Cause | Operator Checks | Immediate Action |
|---|---|---|---|---|---|
| auth | `errors` contains `api-error` or `http-error` during `getUpdates`/`sendMessage`; often `status=failed`, `reason=adapter-fetch-exception` | Payload: `telemetry.heartbeat.emit_state` often `emitted` or `emit-failed` on failed cycle. If heartbeat event exists, `context.telemetry_digest` shows cycle counts at failure point. | Invalid/revoked bot token | Verify `CHANNEL_TOKEN` is set and current | Update token in local env and rerun |
| timeout/network | `errors` contains `kind='timeout'` or `kind='network-error'`; `reason=adapter-fetch-exception` may recur | Payload: `telemetry.counters.fetch_total` often `0`; `telemetry.heartbeat.emit_state` may be `emitted`/`emit-failed` on repeated failures. Event digest `cycle_total_ms` helps identify stalled cycles. | Local network issue or Telegram API reachability problem | Check internet connectivity, DNS, proxy/firewall, and retry conditions | Retry; if persistent, capture sanitized payload and escalate |
| parse/unsupported | Raw Telegram updates arrive but cycle reason is `no-updates`, or fetched count lower than expected | Payload: `telemetry.counters.fetch_total` may be `0` or lower than inbound expectation; `telemetry.heartbeat.emit_state` typically `disabled` when no failure is emitted. | Non-`message.text` updates (or missing required IDs) are intentionally skipped | Confirm inbound data is plain text message updates | Send supported `message.text` input for smoke validation |
| outbound | `reason=completed-with-errors` and `errors` includes `send_message failed: ...` | Payload: `telemetry.counters.send_total < telemetry.counters.fetch_total`; `telemetry.heartbeat.emit_state` commonly `emitted`/`emit-failed`. Event digest confirms `send_total` and `drop_total` quickly. | Chat not reachable, API rejection, rate limit, or permissions | Verify destination chat and bot permissions | Retry once; record sanitized error dict (`operation`, `kind`, `status_code`, `error_code`) |
| allowlist | Payload shows `dropped_count > 0` and `dropped_updates[]` reason `chat_id not allowlisted` | Payload: `telemetry.counters.drop_total > 0`; `telemetry.heartbeat.emit_state` can remain `disabled` when only drops occur (drops are non-error). If another failure occurs, digest `drop_total` appears in heartbeat event context. | Chat ID not included or formatted incorrectly in allowlist | Compare payload `chat_id` with configured allowlist values | Add/fix ID in `CHANNEL_ALLOWED_CHAT_IDS` and rerun |
| context/corruption | `error_details[].code` is `context-store-load-error` / `context-store-save-error` / `context-compaction-error`; durable sessions may regress or skip | Check `runtime_digest.context_mode`, `runtime_digest.context_compaction.*`, and telemetry context counters. If heartbeat event exists, include `context.telemetry_digest.context_*` fields. | Durable transcript corruption, metadata mismatch, or compaction/store failure | Confirm `CHANNEL_CONTEXT_MODE`, `CHANNEL_CONTEXT_STRICT_IO`, and inspect `.channel_runtime/context` files for malformed JSONL lines | If canary impact persists, roll back to `CHANNEL_CONTEXT_MODE=legacy`; capture redacted payload + digest |

## 8) TG-LIVE-E2 Canary and Smoke Execution

Scenarios and rollback cues in this section align with:
- `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md` (`TG-LIVE-E2` and rollout strategy).
- `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md` (scenario script and evidence rubric).
- `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md` (layer boundaries, sequence flow, debug entry points).

### Stage Ramp Rollout (25% -> 50% -> 100%)

Use staged allowlist expansion for durable context rollout. Percentage values are based on the active rollout cohort size (operator-managed chat list).

#### Stage 0: Baseline and setup

Capture baseline from legacy mode before enabling durable mode:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_CANARY_CHAT_IDS=""
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Collect at least 30 minutes of baseline cycle payloads and record:
- Reply success rate baseline:
  - `success_rate = sent_count / max(fetched_count - dropped_count, 1)`
- Latency baseline:
  - `telemetry.timers_ms.cycle_total` p95 and p99
- Compaction baseline:
  - `runtime_digest.context_compaction.failed_total / max(runtime_digest.context_compaction.attempted_total, 1)` (expected near 0 in legacy mode)

#### Stage 1: 25% ramp

Entry checks:
- Baseline window captured and documented.
- `CHANNEL_CONTEXT_MODE=durable` set.
- `CHANNEL_CONTEXT_CANARY_CHAT_IDS` set to the 25% cohort list.
- Runtime restarted and first cycle confirms `runtime_digest.context_mode="durable"` for canary sessions.

Command template:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="durable"
export CHANNEL_CONTEXT_CANARY_CHAT_IDS="<CSV_CHAT_IDS_FOR_25_PERCENT>"
export CHANNEL_CONTEXT_MANUAL_COMPACT="true"
bash scripts/restart_channel_runtime.sh
```

Required telemetry observations (minimum 30 minutes):
- Reply success rate degradation versus baseline <= 2 percentage points.
- `telemetry.timers_ms.cycle_total` p95 <= baseline p95 + 250 ms and p99 <= baseline p99 + 500 ms.
- Compaction failure ratio <= 1%:
  - `failed_total / max(attempted_total, 1) <= 0.01`

Exit checks:
- No stop condition triggered.
- No sustained `context-store-*` or `context-compaction-*` error burst.
- Evidence captured in rollout artifact.

#### Stage 2: 50% ramp

Entry checks:
- Stage 1 exit checks completed.
- `CHANNEL_CONTEXT_CANARY_CHAT_IDS` expanded to 50% cohort and restart completed.
- First cycles show expected mixed behavior (durable for allowlisted cohort, legacy for others).

Command template:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="durable"
export CHANNEL_CONTEXT_CANARY_CHAT_IDS="<CSV_CHAT_IDS_FOR_50_PERCENT>"
export CHANNEL_CONTEXT_MANUAL_COMPACT="true"
bash scripts/restart_channel_runtime.sh
```

Required telemetry observations (minimum 60 minutes):
- Reply success rate degradation versus baseline <= 2 percentage points.
- `telemetry.timers_ms.cycle_total` p95 <= baseline p95 + 250 ms and p99 <= baseline p99 + 500 ms.
- Compaction failure ratio <= 1%.

Exit checks:
- No stop condition triggered.
- No monotonic increase in `runtime_digest.context_compaction.failed_total` without corresponding recovery.
- Evidence captured and reviewed by on-call operator.

#### Stage 3: 100% ramp

Entry checks:
- Stage 2 exit checks completed.
- `CHANNEL_CONTEXT_CANARY_CHAT_IDS` expanded to all rollout cohort chats (100%).
- Restart completed and durable context active for entire intended cohort.

Command template:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="durable"
export CHANNEL_CONTEXT_CANARY_CHAT_IDS="<CSV_CHAT_IDS_FOR_100_PERCENT>"
export CHANNEL_CONTEXT_MANUAL_COMPACT="true"
bash scripts/restart_channel_runtime.sh
```

Required telemetry observations (minimum 120 minutes):
- Reply success rate degradation versus baseline <= 2 percentage points.
- `telemetry.timers_ms.cycle_total` p95 <= baseline p95 + 250 ms and p99 <= baseline p99 + 500 ms.
- Compaction failure ratio <= 1%.

Exit checks:
- No stop condition triggered during the full observation window.
- No unresolved context subsystem failures in `error_details`.
- Final evidence bundle completed.

### Stop Conditions and Immediate Rollback Triggers

Stop ramp and execute rollback immediately if any condition occurs:
- Reply success rate degradation > 3 percentage points for 10 consecutive minutes.
- Latency budget breach:
  - `cycle_total` p95 > baseline p95 + 500 ms, or
  - `cycle_total` p99 > baseline p99 + 1000 ms
  - sustained for 10 consecutive minutes.
- Compaction failure ratio > 2% for 10 consecutive minutes.
- Any sustained context diagnostic burst:
  - repeated `context-store-load-error`, `context-store-save-error`, or `context-compaction-error`.

Immediate rollback command:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_CANARY_CHAT_IDS=""
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Post-rollback validation:
- `runtime_digest.context_mode` returns to `legacy`.
- `telemetry.context.mode` returns to `legacy`.
- Compaction counters stop increasing in legacy mode.

### One-Allowlisted-Chat Canary Bootstrap (Optional)

Before Stage 1, you can run a single-chat bootstrap cycle:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
export CHANNEL_LIVE_MODE="true"
export CHANNEL_ALLOWED_CHAT_IDS="<CANARY_CHAT_ID>"
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CODEX_TIMEOUT_S="20.0"
python3 -m channel_runtime --once
```

Continuous canary loop:

```bash
python3 -m channel_runtime \
  --live-mode true \
  --allowed-chat-ids "<CANARY_CHAT_ID>" \
  --orchestrator-mode codex
```

Operator notes:
- Use this only as a pre-ramp smoke check; do not treat it as a substitute for 25%/50%/100% staged observations.
- If `--live-mode true` is set with an empty allowlist, startup fails with `reason=invalid-config`.

### TG-LIVE-E2 Smoke Scenarios and Rollback Triggers

| Scenario | Expected signal | Rollback trigger |
|---|---|---|
| Happy path round-trip (Telegram -> Codex -> Telegram in canary chat) | Payload shows `status=ok`; `sent_count >= 1`; no unexpected `errors[]` for scenario input | Roll back if canary cannot complete round-trip reliably |
| Telegram transient failure (fetch/send timeout or network error) | Failure is surfaced in `errors[]`/`error_details[]`; runtime remains running in continuous mode; telemetry reflects failure cycle | Roll back on sustained adapter fetch/send failure rate above release threshold |
| Codex timeout/failure in `codex` mode | Diagnostic code such as `codex-timeout` or `codex-exec-failed` appears; cycle surfaces error context and telemetry state | Roll back on repeated Codex handoff timeout/failure beyond release threshold |
| Allowlist drop (non-allowlisted chat) | `dropped_count > 0`, `dropped_updates[].reason` includes `chat_id not allowlisted`, and `error_count` does not increment only for drop | Roll back on unexpected duplicate/drop behavior in canary evidence |

Additional rollout-level rollback triggers:
- Queue saturation or worker restart churn in canary telemetry/evidence.
- Sustained durable context failures (`context-store-*`, `context-compaction-*`) above release threshold.
- Any stop condition from the staged ramp process above.

### Explicit Rollback Path: Legacy Context Mode

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Post-rollback expectations:
- Runtime remains in codex/default orchestrator mode as configured, but context handling uses legacy path.
- `runtime_digest.context_mode` reports `legacy`.
- `telemetry.context.mode` reports `legacy`.
- `telemetry.context.compaction.*` and `runtime_digest.context_compaction.*` remain zero unless/until durable mode is re-enabled.

### Required Evidence Capture (Redacted)

Capture and store a minimal redacted evidence bundle per smoke scenario:
- Command used (with token redacted).
- One cycle payload snippet showing: `status`, `reason`, `error_count`, `dropped_count`, and `telemetry.heartbeat.emit_state`.
- One telemetry counter snippet showing: `telemetry.counters.fetch_total`, `send_total`, `drop_total`, `heartbeat_emit_failures`.
- If a heartbeat event exists, capture `context.telemetry_digest` plus `context.heartbeat.emit_state`.
- Rollback decision note: `continue canary` or `rollback`, with one-line reason.

Sanitized payload snippet template:

```json
{
  "status": "ok",
  "reason": "processed",
  "error_count": 0,
  "dropped_count": 0,
  "telemetry": {
    "counters": {
      "fetch_total": 1,
      "send_total": 1,
      "drop_total": 0,
      "heartbeat_emit_failures": 0
    },
    "heartbeat": {
      "emit_state": "disabled"
    }
  }
}
```

Redaction minimums:
- Replace token values and full chat/user IDs (`<REDACTED_TOKEN>`, `<REDACTED_CHAT_ID>`).
- Keep only fields needed to prove scenario outcome and rollback decision.

## 9) Security Notes

- Do not put bot tokens or other secrets in notes, tickets, screenshots, or logs.
- Redact token values from shared command examples.
- Keep secrets in local shell env only (or gitignored local env files).
- Keep captured evidence minimal and sanitized.

## 10) Source References

- `/home/cwilson/projects/agent_skills/channel_runtime/__main__.py`
- `/home/cwilson/projects/agent_skills/channel_runtime/config.py`
- `/home/cwilson/projects/agent_skills/channel_runtime/runner.py`
- `/home/cwilson/projects/agent_skills/channel_runtime/codex_orchestrator.py`
- `/home/cwilson/projects/agent_skills/channel_runtime/context/store.py`
- `/home/cwilson/projects/agent_skills/channel_runtime/context/errors.py`
- `/home/cwilson/projects/agent_skills/channel_core/service.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/adapter.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/update_parser.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/api.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/cursor_state.py`
- `/home/cwilson/projects/agent_skills/artifacts/TELEGRAM-TG-P3-IMPLEMENTATION-NOTE-20260224.md`
- `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md`
- `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md`
- `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`
