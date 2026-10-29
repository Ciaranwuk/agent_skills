#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE_PATH="${AGENT_SKILLS_ENV_FILE:-$REPO_ROOT/.env.local}"
PYTHON_BIN="${CHANNEL_RUNTIME_PYTHON_BIN:-python3}"

if [[ -f "$ENV_FILE_PATH" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    [[ -z "$line" || "${line:0:1}" == "#" || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
    [[ -n "${!key+x}" ]] || export "$key=$value"
  done < "$ENV_FILE_PATH"
fi

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" "$SCRIPT_DIR/check_disk_space_alert.py" "$@"
