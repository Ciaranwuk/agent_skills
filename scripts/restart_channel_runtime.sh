#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="${CHANNEL_RUNTIME_PID_FILE:-$REPO_ROOT/.channel_runtime/channel_runtime.pid}"
LOG_FILE="${CHANNEL_RUNTIME_LOG_FILE:-$REPO_ROOT/artifacts/channel_runtime/channel_runtime.log}"
RUNTIME_CMD="${CHANNEL_RUNTIME_CMD:-bash $REPO_ROOT/scripts/run_channel_runtime_foreground.sh}"
PROCESS_MATCH="${CHANNEL_RUNTIME_PROCESS_MATCH:-channel_runtime}"
STOP_WAIT_S="${CHANNEL_RUNTIME_STOP_WAIT_S:-15}"
STARTUP_WAIT_S="${CHANNEL_RUNTIME_STARTUP_WAIT_S:-3}"
LOG_MAX_BYTES="${CHANNEL_RUNTIME_LOG_MAX_BYTES:-104857600}"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

process_state() {
  local pid="$1"
  ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]'
}

is_live_pid() {
  local pid="$1"
  local state
  state="$(process_state "$pid")"
  [[ -n "$state" && "$state" != Z* ]]
}

cmdline_for_pid() {
  local pid="$1"
  ps -p "$pid" -o args= 2>/dev/null || true
}

rotate_log_if_needed() {
  if [[ ! -f "$LOG_FILE" ]]; then
    return 0
  fi
  if [[ ! "$LOG_MAX_BYTES" =~ ^[0-9]+$ || "$LOG_MAX_BYTES" == "0" ]]; then
    return 0
  fi

  local size
  size="$(wc -c < "$LOG_FILE" | tr -d '[:space:]')"
  if [[ -z "$size" || "$size" -le "$LOG_MAX_BYTES" ]]; then
    return 0
  fi

  local stamp rotated
  stamp="$(date +%Y%m%d%H%M%S)"
  rotated="${LOG_FILE}.${stamp}"
  mv "$LOG_FILE" "$rotated"
  echo "Rotated oversized channel_runtime log to $rotated (${size} bytes)"
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

  if ! is_live_pid "$pid"; then
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
  while is_live_pid "$pid"; do
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
  # Preserve the caller's exported environment exactly; a login shell can
  # rehydrate profile state that diverges from the shell where the operator
  # validated helper/network behavior.
  nohup bash -c "$RUNTIME_CMD" >>"$LOG_FILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"

  local elapsed=0
  while (( elapsed < STARTUP_WAIT_S )); do
    if ! is_live_pid "$new_pid"; then
      local exit_code=1
      if wait "$new_pid"; then
        exit_code=0
      else
        exit_code=$?
      fi
      echo "channel_runtime failed during startup (exit=$exit_code); see $LOG_FILE" >&2
      rm -f "$PID_FILE"
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  if ! is_live_pid "$new_pid"; then
    echo "channel_runtime failed during startup; see $LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi

  echo "channel_runtime started PID=$new_pid"
  echo "PID file: $PID_FILE"
  echo "Log file: $LOG_FILE"
}

cd "$REPO_ROOT"
stop_existing
rotate_log_if_needed
start_runtime
