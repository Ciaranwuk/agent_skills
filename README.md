# Agent Skills Repo

This repo contains a modular Telegram runtime plus two supporting subsystems:
- `heartbeat_system`: best-effort runtime/system eventing and heartbeat execution.
- `memory_system`: local markdown indexing and retrieval (`search` and `get`).

Current default context mode is `legacy` (`CHANNEL_CONTEXT_MODE=legacy`). Durable context mode (`durable`) is available behind explicit runtime config.

## Repository Map

- `channel_core`: provider-agnostic contracts and single-cycle processing (`process_once`).
- `telegram_channel`: Telegram API client, update parsing, adapter, and durable cursor floor state.
- `channel_runtime`: config parsing, orchestration wiring, polling loop, telemetry payloads.
- `heartbeat_system`: heartbeat CLI/API, scheduler runtime, event dedupe/state store, system-event queue.
- `memory_system`: SQLite FTS index builder plus safe file retrieval from configured source roots.
- `scripts/`: helper scripts (for example: `scripts/run_telegram_channel_checks.sh`).
- `artifacts/`: generated logs and verification outputs.

## Quick Start (Runtime)

Run from repo root:

```bash
cd /home/cwilson/projects/agent_skills
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
export CHANNEL_MODE="poll"
export CHANNEL_ACK_POLICY="always"
export CHANNEL_POLL_INTERVAL_S="2.0"
export CHANNEL_ALLOWED_CHAT_IDS=""
export CHANNEL_LIVE_MODE="false"
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_STRICT_IO="false"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
```

Run one cycle:

```bash
python3 -m channel_runtime --once
```

Run continuously:

```bash
python3 -m channel_runtime
```

Apply a new Codex timeout in continuous mode:

```bash
export CHANNEL_CODEX_TIMEOUT_S="30.0"
bash scripts/restart_channel_runtime.sh
```

`scripts/restart_channel_runtime.sh` gracefully stops the existing runtime process tracked in
`.channel_runtime/channel_runtime.pid` and starts a new one with current environment values.

## Telegram Context Management (P3)

Context env vars and defaults:
- `CHANNEL_CONTEXT_MODE` (`legacy` default; `durable` enables persistent transcript + compaction path)
- `CHANNEL_CONTEXT_WINDOW_TOKENS` (`128000`)
- `CHANNEL_CONTEXT_RESERVE_TOKENS` (`16000`)
- `CHANNEL_CONTEXT_KEEP_RECENT_TOKENS` (`24000`)
- `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` (`1200`)
- `CHANNEL_CONTEXT_MIN_GAIN_TOKENS` (`800`)
- `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S` (`300`)
- `CHANNEL_CONTEXT_STRICT_IO` (`false`)
- `CHANNEL_CONTEXT_MANUAL_COMPACT` (`false`)

Effective behavior notes:
- Durable path is active only when `CHANNEL_CONTEXT_MODE=durable`.
- `CHANNEL_CONTEXT_STRICT_IO=true` makes malformed durable transcript lines fail load (`context-store-load-error`) instead of being skipped.
- `/ctx inspect` and `/ctx compact` are intercepted only when all are true:
  - `CHANNEL_ORCHESTRATOR_MODE=codex`
  - `CHANNEL_CONTEXT_MODE=durable`
  - `CHANNEL_CONTEXT_MANUAL_COMPACT=true`
- Parsed policy values `CHANNEL_CONTEXT_MIN_GAIN_TOKENS`, `CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S`, and `CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS` are reserved for subsequent wiring; current codex durable runtime uses fixed compaction policy inputs (`min_compaction_gain_tokens=0`, `cooldown_window_s=0.0`).

Operator command output fields (`/ctx inspect` and `/ctx compact`):
- `session_id`, `status`, `reason`, `tokens_before`, `tokens_after`, `gained_tokens`, `turns_before`, `turns_after`

Rollback to legacy context mode:

```bash
export CHANNEL_CONTEXT_MODE="legacy"
export CHANNEL_CONTEXT_MANUAL_COMPACT="false"
bash scripts/restart_channel_runtime.sh
```

Legacy-path deprecation contract (TG-CTX-P4-T03):
- `CHANNEL_CONTEXT_MODE=legacy` is the emergency kill-switch and remains supported for one release cycle after 2026-03-05.
- Removal gate for legacy mode:
  - durable-mode canary and full rollout stay stable for the full release cycle;
  - no unresolved sev-1/sev-2 incidents attributable to durable context;
  - operators validate rollback/runbook flows without requiring legacy mode.
- If durable behavior regresses during the retention window, operators should immediately roll back with the command block above.

P3 runtime payload additions for context triage:
- `telemetry.context.mode`
- `telemetry.context.compaction.*`
- `telemetry.context.tokens.*`
- `runtime_digest.context_mode`
- `runtime_digest.context_compaction.*`
- `runtime_digest.context_tokens.*`
- heartbeat event `context.telemetry_digest.context_*` fields

## heartbeat_system (Need To Know)

Purpose:
- Provides heartbeat run control (`run-once`, scheduler start/stop APIs) and a session-scoped system-event bus.
- Used by `channel_runtime` for best-effort failure publication (it does not gate runtime success).

Key entrypoints:
- CLI: `python3 -m heartbeat_system ...`
- API: `heartbeat_system.api.run_once`, `get_status`, `get_last_event`, `wake`, `enable_heartbeat`, `disable_heartbeat`, `publish_system_event`

Typical usage patterns:

```bash
# One heartbeat run (manual reason)
python3 -m heartbeat_system run-once --reason manual

# Runtime/heartbeat visibility
python3 -m heartbeat_system status
python3 -m heartbeat_system last-event

# Wake request for active scheduler
python3 -m heartbeat_system wake --reason manual
```

Where runtime uses it:
- `channel_runtime.runner.HeartbeatEventEmitter` publishes runtime failure diagnostics through `heartbeat_system.api.publish_system_event`.
- Emission is attempted for cycle failures, adapter/orchestrator diagnostics, and process-once exceptions.

Failure semantics (best-effort, non-fatal):
- Heartbeat emission failures are swallowed and tracked as `heartbeat_emit_failures` telemetry.
- Runtime cycle completion does not depend on heartbeat publish success.
- If heartbeat runtime/scheduler is not running, `wake` returns a structured `not-running` payload instead of crashing.

## memory_system (Need To Know)

Purpose:
- Local markdown memory service for indexing and retrieval.
- Supports three-step workflow: `rebuild` index -> `search` snippets -> `get` safe file content.

Index/search/get workflow:
1. Build or sync SQLite FTS index from allowed markdown roots.
2. Query index with `search` (returns ranked snippets and line spans).
3. Read source content with `get` (full file or line window).

Key commands:

```bash
cd /home/cwilson/projects/agent_skills

# Rebuild default workspace index
python3 -m memory_system rebuild --force

# Search local memory
python3 -m memory_system search "telegram timeout" --max-results 5 --min-score 0.2

# Read a file from workspace root
python3 -m memory_system get README.md --from 1 --lines 40

# Include an extra source root and use canonical key for secondary root files
python3 -m memory_system --source-root /tmp/extra_docs search "heartbeat"
python3 -m memory_system --source-root /tmp/extra_docs get root1:notes.md
```

DB path pattern:
- Default: `<workspace>/.memory_index.sqlite`
- Override with `--db <path>` on any command.

Safety notes:
- Source roots are explicit: primary `--workspace` plus optional repeated `--source-root`.
- Index scan excludes noisy directories (`.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `__pycache__`).
- `get` is constrained to allowed roots and markdown files only:
  - denies absolute paths
  - denies `..` traversal
  - denies symlink path components
  - denies non-`.md` targets
- When search index is missing/unavailable, `search` returns a deterministic unavailable payload (does not throw by default).

Runtime usage note:
- In default orchestrator mode, enabling `CHANNEL_ENABLE_MEMORY_HOOK=true` triggers a best-effort `memory_search(..., maxResults=1)` lookup and appends `memory: <snippet>` only when a result exists.

## How Components Fit Together

- `telegram_channel` fetches updates, parses inbound messages, sends replies, and manages cursor acks.
- `channel_core` executes a single deterministic `fetch -> orchestrate -> send -> ack` cycle.
- `channel_runtime` composes adapter + orchestrator + policies, runs one cycle or loop, and emits telemetry.
- `memory_system` is an optional lookup dependency used by runtime default orchestrator when memory hook is enabled.
- `heartbeat_system` is a side-channel for runtime/system events and heartbeat operations; runtime treats it as best-effort and non-blocking.

## Codex Mode Controls

```bash
export CHANNEL_ORCHESTRATOR_MODE="codex"
export CHANNEL_CODEX_TIMEOUT_S="20.0"
export CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"
```

Behavior:
- `CHANNEL_ORCHESTRATOR_MODE="default"`: echo-style response path (optionally memory-enriched).
- `CHANNEL_ORCHESTRATOR_MODE="codex"`: invokes `codex exec` via `channel_runtime.codex_orchestrator`.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="true"`: sends minimal user-facing fallback text on codex failures/timeouts.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"`: suppresses fallback text; diagnostics still recorded.

## Testing

```bash
python3 -m unittest discover -s channel_core/tests -p 'test_*.py'
python3 -m unittest discover -s telegram_channel/tests -p 'test_*.py'
python3 -m unittest discover -s channel_runtime/tests -p 'test_*.py'
python3 -m unittest discover -s heartbeat_system/tests -p 'test_*.py'
python3 -m unittest discover -s memory_system/tests -p 'test_*.py'
```

Or:

```bash
bash scripts/run_telegram_channel_checks.sh
```

## Additional Docs

- [TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md](TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md)
- [TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md](TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md)
- [TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md](TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md)
- [TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md](TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md)

## Context Schema Docs

- Durable session metadata contract (`schema_version = 1`):
  - `artifacts/schemas/context_session.schema.json`
- Durable transcript entry contract (`schema_version = 1`):
  - `artifacts/schemas/context_transcript_entry.schema.json`
