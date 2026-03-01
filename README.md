# Agent Skills Repo

This repo contains a modular Telegram runtime plus two supporting subsystems:
- `heartbeat_system`: best-effort runtime/system eventing and heartbeat execution.
- `memory_system`: local markdown indexing and retrieval (`search` and `get`).

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
```

Run one cycle:

```bash
python3 -m channel_runtime --once
```

Run continuously:

```bash
python3 -m channel_runtime
```

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
