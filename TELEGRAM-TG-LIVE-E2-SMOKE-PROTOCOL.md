# TG-LIVE-E2 Live Smoke Protocol (No-Execution Artifact)

Date: 2026-02-28  
Owner lane: test + documentation  
Scope: scripted canary protocol for Telegram live path validation without executing live traffic in this artifact.

## 1) Purpose

Define a minimal, reproducible, low-blast-radius canary protocol for TG-LIVE-E2 with explicit expected outcomes for:
1. Happy path round trip (Telegram -> Codex -> Telegram)
2. Telegram transient failure behavior
3. Codex timeout/failure behavior
4. Allowlist drop behavior

This document is command-driven and evidence-first. It does not include live token values and does not execute the scenarios.

## 2) Preflight Checklist

Run from repo root:

```bash
cd /home/cwilson/projects/agent_skills
```

Environment and tooling:

```bash
python3 --version
python3 -c "import channel_runtime; print('channel_runtime import ok')"
command -v codex >/dev/null && echo "codex=present" || echo "codex=missing"
```

Secrets and canary scope:

```bash
test -n "${CHANNEL_TOKEN:-}" && echo "CHANNEL_TOKEN=set" || echo "CHANNEL_TOKEN=missing"
test -n "${CANARY_CHAT_ID:-}" && echo "CANARY_CHAT_ID=set" || echo "CANARY_CHAT_ID=missing"
echo "CHANNEL_TOKEN=${CHANNEL_TOKEN:+<REDACTED>}"
echo "CANARY_CHAT_ID=${CANARY_CHAT_ID:-<missing>}"
```

Safety guardrails:
- Use exactly one allowlisted canary chat for round-trip tests.
- Do not widen allowlist during this protocol.
- Keep `CHANNEL_ONCE=true` for single-cycle smoke checks.
- Store evidence under `artifacts/test-logs/tg-live-e2-20260228/`.

## 3) Canonical Runtime Env Template

Use this baseline for all scenarios (override per scenario as noted):

```bash
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
export CHANNEL_MODE="poll"
export CHANNEL_ACK_POLICY="always"
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CODEX_TIMEOUT_S="20.0"
export CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"
export CHANNEL_CODEX_SESSION_MAX="128"
export CHANNEL_CODEX_SESSION_IDLE_TTL_S="900.0"
export CHANNEL_POLL_INTERVAL_S="2.0"
export CHANNEL_ALLOWED_CHAT_IDS="${CANARY_CHAT_ID}"
export CHANNEL_CURSOR_STATE_PATH=".channel_runtime/telegram_cursor_state.json"
export CHANNEL_STRICT_CURSOR_STATE_IO="false"
export CHANNEL_LIVE_MODE="true"
export CHANNEL_ONCE="true"
```

Evidence capture wrapper:

```bash
RUN_ID="tg-live-e2-$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="artifacts/test-logs/tg-live-e2-20260228/${RUN_ID}"
mkdir -p "${LOG_DIR}"

python3 -m channel_runtime --once | tee "${LOG_DIR}/cycle.json"
```

## 4) Scenario Scripts and Expected Outcomes

## 4.1 Scenario 1: Happy Path Round Trip (Telegram -> Codex -> Telegram)

Operator setup:
1. Keep baseline env template.
2. Send one plain-text canary message from allowlisted chat before running the cycle.

Command template:

```bash
SCENARIO_ID="S1-happy-path"
python3 -m channel_runtime --once | tee "${LOG_DIR}/${SCENARIO_ID}.json"
```

Expected outcome:
- `status="ok"`
- `reason="processed"` (or `no-updates` if no inbound message was available at poll time)
- On true round trip: `fetched_count>=1`, `sent_count>=1`, `acked_count>=1`
- `error_count=0`
- `dropped_count=0`
- `telemetry.contract="tg-live.runtime.telemetry"`
- `telemetry.version="2.0"`
- `telemetry.counters.fetch_total == fetched_count`
- `telemetry.counters.send_total == sent_count`
- `telemetry.counters.drop_total == dropped_count`
- `error_details=[]`

Pass rule:
- PASS only if a cycle captured the inbound canary and returned `sent_count>=1` with zero errors and zero drops.

## 4.2 Scenario 2: Telegram Transient Failure Behavior

Injection method:
- Simulate transient network failure by forcing an invalid HTTPS proxy for the runtime process.

Command template:

```bash
SCENARIO_ID="S2-telegram-transient-fetch"
HTTPS_PROXY="http://127.0.0.1:9" \
python3 -m channel_runtime --once | tee "${LOG_DIR}/${SCENARIO_ID}.json"
```

Expected outcome:
- `status="failed"`
- `reason="adapter-fetch-exception"`
- `error_count>=1`
- `errors[0]` contains `fetch_updates failed`
- `error_details[0].code="adapter-fetch-exception"`
- `error_details[0].retryable=true`
- `error_details[0].context.operation="fetch_updates"`
- `telemetry.counters.fetch_total=0`
- `telemetry.counters.send_total=0`
- `telemetry.counters.drop_total=0`

Pass rule:
- PASS only if failure is surfaced as retryable adapter fetch exception with no process crash.

## 4.3 Scenario 3: Codex Timeout/Failure Behavior

Run one or both injections:

Timeout-focused injection (preferred):

```bash
SCENARIO_ID="S3-codex-timeout"
CHANNEL_ORCHESTRATOR_MODE="codex" \
CHANNEL_CODEX_TIMEOUT_S="0.001" \
python3 -m channel_runtime --once | tee "${LOG_DIR}/${SCENARIO_ID}.json"
```

Exec-failure injection (deterministic fallback):

```bash
SCENARIO_ID="S3-codex-exec-fail"
PATH="/nonexistent" \
CHANNEL_ORCHESTRATOR_MODE="codex" \
python3 -m channel_runtime --once | tee "${LOG_DIR}/${SCENARIO_ID}.json"
```

Expected outcome (on cycle with inbound message):
- `status="ok"` (service continues)
- `reason="completed-with-errors"`
- `error_count>=1`
- `sent_count=0` for failed handoff cycle
- `error_details` contains one of:
  - `code="codex-timeout"` with `retryable=true`
  - `code="codex-exec-failed"` with `retryable=true`
- For either code:
  - `source="orchestrator.diagnostics"`
  - `category="error"`
  - `context.layer="orchestrator"`
  - `context.operation="handle_message"`
- `telemetry.counters.send_total=0` for the failed cycle

Pass rule:
- PASS only if Codex handoff failure is contained in diagnostics/error_details and runtime remains alive with structured output.

## 4.4 Scenario 4: Allowlist Drop Behavior

Injection method:
- Configure allowlist to exclude the chat that sends the canary message.

Command template:

```bash
SCENARIO_ID="S4-allowlist-drop"
CHANNEL_ALLOWED_CHAT_IDS="999999999" \
python3 -m channel_runtime --once | tee "${LOG_DIR}/${SCENARIO_ID}.json"
```

Expected outcome (on cycle with inbound message from non-allowlisted chat):
- `status="ok"`
- `reason="processed"`
- `dropped_count>=1`
- `dropped_updates[0].reason` contains `chat_id not allowlisted`
- `sent_count=0`
- `error_count=0` (drop is non-error)
- `error_details` contains `code="allowlist-drop"` with:
  - `category="drop"`
  - `retryable=false`
  - `context.operation="allowlist_check"`
- `telemetry.counters.drop_total == dropped_count`

Pass rule:
- PASS only if update is dropped before orchestration/send and represented as drop telemetry + drop detail (not crash/error path).

## 5) Telemetry and `error_details` Validation Checklist

For each scenario log:

```bash
jq '{status,reason,fetched_count,sent_count,acked_count,error_count,dropped_count,errors,error_details,telemetry}' \
  "${LOG_DIR}/<scenario>.json"
```

Validate mandatory keys in each `error_details[]` item:
- `code`
- `message`
- `retryable`
- `context.update_id`
- `context.chat_id`
- `context.session_id`
- `context.layer`
- `context.operation`
- `source`
- `category`
- `diagnostic_id`

Validate telemetry contract:
- `telemetry.contract == "tg-live.runtime.telemetry"`
- `telemetry.version == "2.0"`
- `telemetry.counters.heartbeat_emit_failures` present
- `telemetry.timers_ms.cycle_total` is integer >= 0
- placeholder fields exist: `retry_total`, `queue_depth`, `worker_restart_total`

## 6) Redaction Requirements

Before sharing any evidence outside local operator context:
- Replace all token-like values with `<REDACTED>`.
- Redact chat IDs and user IDs unless explicitly approved:
  - `chat_id` -> `<REDACTED_CHAT_ID>`
  - `user_id` -> `<REDACTED_USER_ID>`
- Redact message text if it contains user content.
- Keep only fields needed to prove outcome.

Redaction command template:

```bash
sed -E \
  -e 's/[0-9]{8,}/<REDACTED_NUMERIC_ID>/g' \
  -e 's/(bot)[A-Za-z0-9:_-]+/\1<REDACTED>/g' \
  "${LOG_DIR}/<scenario>.json" > "${LOG_DIR}/<scenario>.redacted.json"
```

## 7) Rollback Criteria (Go/No-Go)

Immediate NO-GO and rollback to non-live/default mode if any occurs during canary:
1. Happy path cannot produce a successful round trip after controlled retries.
2. Telegram transient failure is not classified as structured retryable adapter fetch failure.
3. Codex timeout/failure causes unstructured crash or missing diagnostics.
4. Allowlist drop sends outbound message or increments error_count unexpectedly.
5. Telemetry contract missing/invalid (`contract`, `version`, counters/timers block).

Rollback command template:

```bash
export CHANNEL_LIVE_MODE="false"
export CHANNEL_ORCHESTRATOR_MODE="default"
export CHANNEL_ALLOWED_CHAT_IDS=""
python3 -m channel_runtime --once
```

## 8) Pass/Fail Rubric

Scenario-level:
- PASS: observed payload matches all required expected signals.
- FAIL: any required signal missing or contradictory.
- UNTESTED: scenario not executed or no inbound update available for that run.

Protocol-level:
- GO: S1/S2/S3/S4 all PASS with redacted evidence captured.
- NO-GO: any FAIL.
- CONDITIONAL: only UNTESTED gaps remain; rerun required before rollout decision.

## 9) Evidence Capture Template

Use this table in release notes or test report:

| Scenario | Command | Start UTC | End UTC | Status | Reason | Key signals observed | Redacted evidence path | Decision |
|---|---|---|---|---|---|---|---|---|
| S1 happy path | `python3 -m channel_runtime --once` | `<ts>` | `<ts>` | `<ok/failed>` | `<reason>` | `sent_count>=1,error_count=0,dropped_count=0` | `<path>` | `<PASS/FAIL/UNTESTED>` |
| S2 telegram transient | `HTTPS_PROXY=http://127.0.0.1:9 ...` | `<ts>` | `<ts>` | `<ok/failed>` | `<reason>` | `adapter-fetch-exception,retryable=true` | `<path>` | `<PASS/FAIL/UNTESTED>` |
| S3 codex timeout/failure | `CHANNEL_CODEX_TIMEOUT_S=0.001 ...` | `<ts>` | `<ts>` | `<ok/failed>` | `<reason>` | `codex-timeout|codex-exec-failed,send_count=0` | `<path>` | `<PASS/FAIL/UNTESTED>` |
| S4 allowlist drop | `CHANNEL_ALLOWED_CHAT_IDS=999999999 ...` | `<ts>` | `<ts>` | `<ok/failed>` | `<reason>` | `dropped_count>=1,allowlist-drop,category=drop` | `<path>` | `<PASS/FAIL/UNTESTED>` |

## 10) Source Alignment

Protocol expectations are aligned to:
- `channel_runtime/config.py`
- `channel_runtime/runner.py`
- `channel_runtime/codex_orchestrator.py`
- `channel_core/service.py`
- `telegram_channel/adapter.py`
- `telegram_channel/api.py`
- `telegram_channel/cursor_state.py`
- `TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md`
