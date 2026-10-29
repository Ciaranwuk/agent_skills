# Phase 1 Memory System (Standalone)

## Overview
`memory_system` is a local, standalone memory index + retrieval package.

It provides:
- `rebuild`: build/sync a local SQLite FTS index from all markdown files (`*.md`) under one or more source roots
- `search`: run `memory_search` over indexed chunks
- `get`: run `memory_get` with safe path reads from configured source roots

Primary implementation files:
- `/home/cwilson/projects/agent_skills/memory_system/cli.py`
- `/home/cwilson/projects/agent_skills/memory_system/api.py`
- `/home/cwilson/projects/agent_skills/memory_system/index.py`
- `/home/cwilson/projects/agent_skills/memory_system/paths.py`

## Prerequisites
- Python 3 available as `python3`
- Run commands from `/home/cwilson/projects/agent_skills`
- Workspace and optional extra roots contain markdown docs to index (`*.md`)

## Quickstart (copy/paste)
```bash
cd /home/cwilson/projects/agent_skills

# 1) Create a demo workspace
WS="$(mktemp -d)"
mkdir -p "$WS/memory"
printf '# Root Memory\nalpha beta\n' > "$WS/MEMORY.md"
printf 'first line\nsecond line with token\nthird line\n' > "$WS/memory/notes.md"
printf 'outside memory folder token\n' > "$WS/notes.md"
VAULT="$(mktemp -d)"
printf 'vault decision token\n' > "$VAULT/decision.md"

# 2) Rebuild index
python3 -m memory_system --workspace "$WS" --source-root "$VAULT" --db "$WS/.memory_index.sqlite" rebuild --force

# 3) Search
python3 -m memory_system --workspace "$WS" --source-root "$VAULT" --db "$WS/.memory_index.sqlite" search token --max-results 10 --min-score 0

# 4) Get file content from workspace (single-root style path)
python3 -m memory_system --workspace "$WS" --source-root "$VAULT" get memory/notes.md --from 2 --lines 1

# 5) Get file content from secondary root (canonical key)
python3 -m memory_system --workspace "$WS" --source-root "$VAULT" get root1:decision.md
```

## CLI Usage
```bash
python3 -m memory_system [--workspace <path>] [--source-root <path> ...] [--db <path>] [--backend <builtin|qmd>] <command> ...
```

Commands:
- `rebuild [--force]`
- `search <query> [--max-results <1-50>] [--min-score <0..1>]`
- `get <path> [--from <line>=1+] [--lines <count>=1..2000]`

Notes:
- `--workspace` defaults to `.`
- `--source-root` is repeatable; when omitted, only `--workspace` is scanned
- `--db` defaults to `<workspace>/.memory_index.sqlite`
- `--backend` defaults to `builtin`
- `search` returns a deterministic unavailable JSON payload if the local index is missing/unavailable
- `search --backend qmd` is opt-in and now shells out to `qmd query --json`, normalizes results back to the builtin response shape, and falls back to builtin search when QMD is unavailable
- `rebuild --backend qmd` now runs a Python-owned lifecycle adapter around the verified QMD Phase 1 commands: `qmd collection add`, `qmd update`, `qmd embed`, and `qmd status`
- qmd subprocesses run with workspace-local `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR` rooted at `<workspace>/.qmd/`
- `get` remains builtin-only; selecting `--backend qmd` there exits with an explicit operator-facing error
- `get` accepts:
  - relative `*.md` paths (resolved against primary workspace root)
  - canonical multi-root keys (`rootN:relative/path.md`) for secondary roots
- indexer skips common noisy directories: `.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `__pycache__`
- planned QMD collections are named deterministically from the resolved source-root path (`memory-<slug>-<hash>`), so collection identity does not depend on source-root ordering
- QMD subprocesses now default their runtime home/config/cache/data under `<workspace>/.qmd/` so the adapter does not depend on writable `~/.config` or `~/.cache` paths
- when QMD prerequisites are missing, `rebuild --backend qmd` returns deterministic actionable JSON and exit code `1`
- when `search --backend qmd` falls back, the response stays builtin-shaped and includes a `fallback` object showing that QMD was requested but unavailable

## JSON Output Examples (current implementation)

### `memory_search` success
```json
{
  "citations": "off",
  "mode": "fts-only",
  "model": "",
  "provider": "builtin",
  "results": [
    {
      "endLine": 3,
      "path": "root1:decision.md",
      "score": 0.999999,
      "snippet": "vault decision token",
      "source": "memory",
      "startLine": 1
    }
  ]
}
```

### `memory_search` error/unavailable
```json
{
  "action": "Rebuild local memory index and retry memory_search.",
  "disabled": true,
  "error": "memory index unavailable: index file not found",
  "results": [],
  "unavailable": true,
  "warning": "Memory search is unavailable due to local index unavailability."
}
```

### `memory_search` with requested QMD fallback
```json
{
  "citations": "off",
  "fallback": {
    "details": [
      "qmd binary not found on PATH"
    ],
    "reason": "memory index unavailable: qmd binary not found on PATH",
    "requestedProvider": "qmd"
  },
  "mode": "fts-only",
  "model": "",
  "provider": "builtin",
  "results": [],
  "unavailable": true
}
```

### `memory_get` success
```json
{
  "path": "root1:decision.md",
  "text": "vault decision token\n"
}
```

### `memory_get` error (path denied)
```json
{
  "disabled": true,
  "error": "path not allowed",
  "path": "../etc/passwd",
  "text": ""
}
```
