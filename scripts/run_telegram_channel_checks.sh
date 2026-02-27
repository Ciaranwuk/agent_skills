#!/usr/bin/env bash
set -euo pipefail

# TG-QA-1 + TG-QA-2 runner scaffold.
# This template does not imply implementation completeness.

REPO_ROOT="/home/cwilson/projects/agent_skills"
LOG_DIR="$REPO_ROOT/artifacts/test-logs/telegram-channel-20260223"

mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

run_cmd() {
  local pass_id="$1"
  local suite_id="$2"
  local cmd="$3"
  local safe_suite_id log_file
  local start_utc end_utc exit_code

  # Ensure suite identifiers are safe for filenames.
  safe_suite_id="$(printf '%s' "$suite_id" | tr -c 'A-Za-z0-9._-' '_')"
  log_file="$LOG_DIR/${pass_id}-${safe_suite_id}.log"
  mkdir -p "$(dirname "$log_file")"

  start_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  set +e
  bash -lc "$cmd" 2>&1 | tee "$log_file"
  exit_code=${PIPESTATUS[0]}
  set -e
  end_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  printf "%s\t%s\t%s\t%s\t%s\t%d\n" \
    "$pass_id" "$suite_id" "$start_utc" "$end_utc" "$cmd" "$exit_code"

  return "$exit_code"
}

printf "pass_id\tsuite_id\tstart_utc\tend_utc\tcommand\texit_code\n"

CMDS=(
  "TG-QA-1/channel_core::python3 -m unittest discover -s channel_core/tests -p 'test_*.py'"
  "TG-QA-1/telegram_channel::python3 -m unittest discover -s telegram_channel/tests -p 'test_*.py'"
  "TG-QA-1/channel_runtime::python3 -m unittest discover -s channel_runtime/tests -p 'test_*.py'"
  "TG-QA-2/heartbeat_system::python3 -m unittest discover -s heartbeat_system/tests -p 'test_*.py'"
  "TG-QA-2/memory_system::python3 -m unittest discover -s memory_system/tests -p 'test_*.py'"
)

for pass_id in 1 2; do
  for entry in "${CMDS[@]}"; do
    suite_id="${entry%%::*}"
    cmd="${entry##*::}"
    run_cmd "$pass_id" "$suite_id" "$cmd"
  done
done
