#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${CHANNEL_RUNTIME_PYTHON_BIN:-python3}"
DEFAULT_ENV_FILE="$REPO_ROOT/.env.local"
ENV_FILE_PATH="${AGENT_SKILLS_ENV_FILE:-$DEFAULT_ENV_FILE}"
DNS_PREFLIGHT_ENABLED="${CHANNEL_RUNTIME_DNS_PREFLIGHT:-true}"
DNS_PREFLIGHT_HOSTS="${CHANNEL_RUNTIME_DNS_PREFLIGHT_HOSTS:-api.telegram.org,openai.com,www.youtube.com}"
DNS_PREFLIGHT_PORT="${CHANNEL_RUNTIME_DNS_PREFLIGHT_PORT:-443}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

prepend_pythonpath() {
  if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$REPO_ROOT:$PYTHONPATH"
    return 0
  fi
  export PYTHONPATH="$REPO_ROOT"
}

is_truthy() {
  local value
  value="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

strip_matching_quotes() {
  local value="$1"
  if [[ ${#value} -ge 2 ]]; then
    local first="${value:0:1}"
    local last="${value: -1}"
    if [[ "$first" == "$last" && ( "$first" == "'" || "$first" == "\"" ) ]]; then
      printf '%s' "${value:1:${#value}-2}"
      return 0
    fi
  fi
  printf '%s' "$value"
}

load_env_file_if_present() {
  local env_file="$1"
  if [[ ! -f "$env_file" ]]; then
    echo "channel_runtime: env file not found; continuing without file ($env_file)" >&2
    return 0
  fi
  echo "channel_runtime: loading env file $env_file" >&2

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    local line="$raw_line"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [[ -z "$line" || "${line:0:1}" == "#" ]]; then
      continue
    fi
    if [[ "$line" == export\ * ]]; then
      line="${line#export }"
      line="${line#"${line%%[![:space:]]*}"}"
    fi
    if [[ "$line" != *=* ]]; then
      continue
    fi

    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ -z "$key" ]]; then
      continue
    fi
    if [[ -n "${!key+x}" ]]; then
      continue
    fi
    value="$(strip_matching_quotes "$value")"
    export "$key=$value"
  done < "$env_file"
}

run_dns_preflight_if_enabled() {
  if ! is_truthy "$DNS_PREFLIGHT_ENABLED"; then
    echo "channel_runtime: DNS preflight disabled" >&2
    return 0
  fi
  echo "channel_runtime: running DNS preflight for $DNS_PREFLIGHT_HOSTS" >&2
  "$PYTHON_BIN" "$REPO_ROOT/scripts/check_channel_runtime_dns.py" \
    --hosts "$DNS_PREFLIGHT_HOSTS" \
    --port "$DNS_PREFLIGHT_PORT"
  echo "channel_runtime: DNS preflight passed" >&2
}

cd "$REPO_ROOT"
prepend_pythonpath
load_env_file_if_present "$ENV_FILE_PATH"
run_dns_preflight_if_enabled
echo "channel_runtime: starting poll loop with $PYTHON_BIN -m channel_runtime $*" >&2
exec "$PYTHON_BIN" -m channel_runtime "$@"
