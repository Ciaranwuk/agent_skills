# Telegram Channel Operator Runbook

Date: 2026-02-28
Task Reference: TG-P3-3, TG-LIVE-D2, TG-LIVE-E2
Status: Finalized for current TG-P3 runtime behavior with additive TG-LIVE-D2 telemetry contract.
Architecture explainer: `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`

## 1) Purpose and Scope

### Purpose
Provide operator-ready commands and triage guidance for the implemented Telegram runtime.

### Scope
- Local runtime startup in one-cycle (`--once`) and continuous polling modes.
- Runtime config via environment variables and CLI overrides.
- Allowlist enforcement behavior and explicit drop reporting.
- Additive runtime telemetry contract (`telemetry`) and heartbeat event digest cues.
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
```

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
- `telemetry` (additive TG-LIVE-D2 block; legacy top-level counters remain)

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
- `telemetry.placeholders`: explicit strings documenting not-yet-instrumented fields.

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
- `context.heartbeat.emit_state`

Use this digest for quick triage correlation when an emitted heartbeat event is available; otherwise rely on payload `telemetry`.

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

## 8) TG-LIVE-E2 Canary and Smoke Execution

Scenarios and rollback cues in this section align with:
- `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md` (`TG-LIVE-E2` and rollout strategy).
- `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md` (scenario script and evidence rubric).
- `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md` (layer boundaries, sequence flow, debug entry points).

### One-Allowlisted-Chat Canary Rollout

Use exactly one canary chat ID during initial live rollout:

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
- Keep allowlist restricted to one chat until smoke outcomes are recorded and reviewed.
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
- `/home/cwilson/projects/agent_skills/channel_core/service.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/adapter.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/update_parser.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/api.py`
- `/home/cwilson/projects/agent_skills/telegram_channel/cursor_state.py`
- `/home/cwilson/projects/agent_skills/artifacts/TELEGRAM-TG-P3-IMPLEMENTATION-NOTE-20260224.md`
- `TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md`
- `TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md`
- `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`
