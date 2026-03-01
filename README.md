# Agent Skills Repo

This repository contains a modular Telegram channel runtime and supporting systems.
It is organized so transport, core processing, runtime wiring, heartbeat eventing, and memory lookup can evolve independently.

## What This Repo Contains

Primary module set:
- `channel_core`: provider-agnostic contracts and single-cycle processing (`process_once`).
- `telegram_channel`: Telegram Bot API client, update parsing, transport adapter, and durable cursor state.
- `channel_runtime`: runtime config parsing, loop execution, orchestrator wiring (default and codex), allowlist gating, and payload shaping.
- `heartbeat_system`: system-event publication and scheduling utilities used for best-effort runtime failure notifications.
- `memory_system`: local memory index/search APIs used by optional runtime hooks.

Supporting paths:
- `scripts/`: helper scripts for repeated test/check workflows.
- `artifacts/`: generated logs and verification artifacts.

## Quick Start (Telegram Runtime)

Run from repo root:

```bash
cd /home/cwilson/projects/agent_skills
```

Set required env var:

```bash
export CHANNEL_TOKEN="<REDACTED_BOT_TOKEN>"
```

Recommended baseline env vars:

```bash
export CHANNEL_MODE="poll"
export CHANNEL_ACK_POLICY="always"
export CHANNEL_POLL_INTERVAL_S="2.0"
export CHANNEL_ALLOWED_CHAT_IDS=""
export CHANNEL_LIVE_MODE="false"
```

Run once:

```bash
python3 -m channel_runtime --once
```

Run continuously:

```bash
python3 -m channel_runtime
```

## Codex Mode Controls

Switch orchestrator mode:

```bash
export CHANNEL_ORCHESTRATOR_MODE="codex"
```

Codex timeout (seconds, must be `> 0`):

```bash
export CHANNEL_CODEX_TIMEOUT_S="20.0"
```

Fallback notification behavior on codex orchestrator errors:

```bash
export CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"
```

Behavior notes:
- `CHANNEL_ORCHESTRATOR_MODE="default"`: runtime replies with echo behavior.
- `CHANNEL_ORCHESTRATOR_MODE="codex"`: runtime calls `codex exec` through the codex orchestrator.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="true"`: sends a minimal fallback user-facing error message when codex invocation fails or times out.
- `CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR="false"`: suppresses fallback message; errors are still tracked in payload diagnostics.

## Testing

Run full module test suites (same coverage pattern used by `scripts/run_telegram_channel_checks.sh`):

```bash
python3 -m unittest discover -s channel_core/tests -p 'test_*.py'
python3 -m unittest discover -s telegram_channel/tests -p 'test_*.py'
python3 -m unittest discover -s channel_runtime/tests -p 'test_*.py'
python3 -m unittest discover -s heartbeat_system/tests -p 'test_*.py'
python3 -m unittest discover -s memory_system/tests -p 'test_*.py'
```

Or run the helper script:

```bash
bash scripts/run_telegram_channel_checks.sh
```

## Top-Level Docs

- [TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md](TELEGRAM-CHANNEL-ARCHITECTURE-AND-FLOW.md)
- [TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md](TELEGRAM-CHANNEL-OPERATOR-RUNBOOK.md)
- [TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md](TELEGRAM-TG-LIVE-E2-SMOKE-PROTOCOL.md)
- [TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md](TELEGRAM-CHANNEL-LIVE-CODEX-COMPLETION-PLAN.md)
