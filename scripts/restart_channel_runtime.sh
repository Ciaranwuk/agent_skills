#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="${CHANNEL_RUNTIME_PID_FILE:-$REPO_ROOT/.channel_runtime/channel_runtime.pid}"
LOG_FILE="${CHANNEL_RUNTIME_LOG_FILE:-$REPO_ROOT/artifacts/channel_runtime/channel_runtime.log}"
RUNTIME_CMD="${CHANNEL_RUNTIME_CMD:-python3 -m channel_runtime}"
PROCESS_MATCH="${CHANNEL_RUNTIME_PROCESS_MATCH:-channel_runtime}"
STOP_WAIT_S="${CHANNEL_RUNTIME_STOP_WAIT_S:-15}"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

is_running() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

cmdline_for_pid() {
  local pid="$1"
  ps -p "$pid" -o args= 2>/dev/null || true
}

stop_existing() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "Invalid PID file at $PID_FILE; removing stale file" >&2
    rm -f "$PID_FILE"
    return 0
  fi

  if ! is_running "$pid"; then
    echo "Stale PID file found ($pid); removing" >&2
    rm -f "$PID_FILE"
    return 0
  fi

  local cmdline
  cmdline="$(cmdline_for_pid "$pid")"
  if [[ -z "$cmdline" || "$cmdline" != *"$PROCESS_MATCH"* ]]; then
    echo "Refusing to stop PID $pid; process does not match '$PROCESS_MATCH'" >&2
    exit 1
  fi

  echo "Stopping existing channel_runtime process PID=$pid"
  kill -TERM "$pid"

  local elapsed=0
  while is_running "$pid"; do
    if (( elapsed >= STOP_WAIT_S )); then
      echo "Process PID=$pid did not stop in ${STOP_WAIT_S}s; sending SIGKILL" >&2
      kill -KILL "$pid" || true
      break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  rm -f "$PID_FILE"
}

start_runtime() {
  echo "Starting channel_runtime with CHANNEL_CODEX_TIMEOUT_S=${CHANNEL_CODEX_TIMEOUT_S:-20.0}"
  nohup bash -lc "$RUNTIME_CMD" >>"$LOG_FILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"
  echo "channel_runtime started PID=$new_pid"
  echo "PID file: $PID_FILE"
  echo "Log file: $LOG_FILE"
}

cd "$REPO_ROOT"
stop_existing
start_runtime
