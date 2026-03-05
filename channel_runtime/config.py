from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

from channel_core.contracts import ConfigValidationError

LEGACY_CONTEXT_MODE = "legacy"
DURABLE_CONTEXT_MODE = "durable"
LEGACY_CONTEXT_EMERGENCY_TOGGLE = "CHANNEL_CONTEXT_MODE=legacy"
LEGACY_CONTEXT_RETENTION_RELEASES = 1


@dataclass(frozen=True)
class RuntimeConfig:
    token: str
    mode: str = "poll"
    ack_policy: str = "always"
    orchestrator_mode: str = "default"
    codex_timeout_s: float = 20.0
    notify_on_orchestrator_error: bool = False
    codex_session_max: int = 128
    codex_session_idle_ttl_s: float = 900.0
    poll_interval_s: float = 2.0
    allowed_chat_ids: tuple[str, ...] = ()
    cursor_state_path: str = ".channel_runtime/telegram_cursor_state.json"
    strict_cursor_state_io: bool = False
    live_mode: bool = False
    once: bool = False
    context_mode: str = LEGACY_CONTEXT_MODE
    context_canary_chat_ids: tuple[str, ...] = ()
    context_window_tokens: int = 128000
    context_reserve_tokens: int = 16000
    context_keep_recent_tokens: int = 24000
    context_summary_max_tokens: int = 1200
    context_min_gain_tokens: int = 800
    context_compaction_cooldown_s: float = 300.0
    context_strict_io: bool = False
    context_manual_compact: bool = False

    def __post_init__(self) -> None:
        token = str(self.token).strip()
        mode = str(self.mode).strip()
        ack_policy = str(self.ack_policy).strip().lower()
        orchestrator_mode = str(self.orchestrator_mode).strip()
        context_mode = str(self.context_mode).strip().lower()

        if not token:
            raise ConfigValidationError("token must be a non-empty string")
        if mode != "poll":
            raise ConfigValidationError("mode must be 'poll' for TG-P0")
        if ack_policy not in {"always", "on-success"}:
            raise ConfigValidationError("ack_policy must be 'always' or 'on-success'")
        if orchestrator_mode not in {"default", "codex"}:
            raise ConfigValidationError("orchestrator_mode must be 'default' or 'codex'")
        if context_mode not in {LEGACY_CONTEXT_MODE, DURABLE_CONTEXT_MODE}:
            raise ConfigValidationError(
                "context_mode must be 'legacy' or 'durable' "
                "(set CHANNEL_CONTEXT_MODE=legacy for emergency rollback)"
            )
        if self.codex_timeout_s <= 0:
            raise ConfigValidationError("codex_timeout_s must be a positive number")
        if int(self.codex_session_max) < 1:
            raise ConfigValidationError("codex_session_max must be an integer >= 1")
        if self.codex_session_idle_ttl_s <= 0:
            raise ConfigValidationError("codex_session_idle_ttl_s must be a positive number")
        if self.poll_interval_s <= 0:
            raise ConfigValidationError("poll_interval_s must be a positive number")
        if int(self.context_window_tokens) < 1:
            raise ConfigValidationError("context_window_tokens must be an integer >= 1")
        if int(self.context_reserve_tokens) < 0:
            raise ConfigValidationError("context_reserve_tokens must be an integer >= 0")
        if int(self.context_keep_recent_tokens) < 1:
            raise ConfigValidationError("context_keep_recent_tokens must be an integer >= 1")
        if int(self.context_summary_max_tokens) < 1:
            raise ConfigValidationError("context_summary_max_tokens must be an integer >= 1")
        if int(self.context_min_gain_tokens) < 0:
            raise ConfigValidationError("context_min_gain_tokens must be an integer >= 0")
        if float(self.context_compaction_cooldown_s) < 0:
            raise ConfigValidationError("context_compaction_cooldown_s must be a number >= 0")
        if int(self.context_window_tokens) <= int(self.context_reserve_tokens):
            raise ConfigValidationError("context_window_tokens must be greater than context_reserve_tokens")
        for chat_id in self.allowed_chat_ids:
            if not str(chat_id).strip():
                raise ConfigValidationError("allowed_chat_ids must not contain empty values")
        for chat_id in self.context_canary_chat_ids:
            if not str(chat_id).strip():
                raise ConfigValidationError("context_canary_chat_ids must not contain empty values")
        if self.live_mode and not self.allowed_chat_ids:
            raise ConfigValidationError("allowed_chat_ids must be non-empty when live_mode is enabled")

        object.__setattr__(self, "token", token)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "ack_policy", ack_policy)
        object.__setattr__(self, "orchestrator_mode", orchestrator_mode)
        object.__setattr__(self, "context_mode", context_mode)


def parse_runtime_config(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Parse config from env plus minimal CLI overrides with deterministic errors."""
    args = list(argv or ())
    source_env = dict(env or os.environ)

    values: dict[str, object] = {
        "token": source_env.get("CHANNEL_TOKEN", ""),
        "mode": source_env.get("CHANNEL_MODE", "poll"),
        "ack_policy": source_env.get("CHANNEL_ACK_POLICY", "always"),
        "orchestrator_mode": source_env.get("CHANNEL_ORCHESTRATOR_MODE", "default"),
        "codex_timeout_s": source_env.get("CHANNEL_CODEX_TIMEOUT_S", "20.0"),
        "notify_on_orchestrator_error": source_env.get("CHANNEL_NOTIFY_ON_ORCHESTRATOR_ERROR", "false"),
        "codex_session_max": source_env.get("CHANNEL_CODEX_SESSION_MAX", "128"),
        "codex_session_idle_ttl_s": source_env.get("CHANNEL_CODEX_SESSION_IDLE_TTL_S", "900.0"),
        "poll_interval_s": source_env.get("CHANNEL_POLL_INTERVAL_S", "2.0"),
        "allowed_chat_ids": source_env.get("CHANNEL_ALLOWED_CHAT_IDS", ""),
        "cursor_state_path": source_env.get("CHANNEL_CURSOR_STATE_PATH", ".channel_runtime/telegram_cursor_state.json"),
        "strict_cursor_state_io": source_env.get("CHANNEL_STRICT_CURSOR_STATE_IO", "false"),
        "live_mode": source_env.get("CHANNEL_LIVE_MODE", "false"),
        "once": source_env.get("CHANNEL_ONCE", "false"),
        "context_mode": source_env.get("CHANNEL_CONTEXT_MODE", LEGACY_CONTEXT_MODE),
        "context_canary_chat_ids": source_env.get(
            "CHANNEL_CONTEXT_CANARY_ALLOWLIST_CHAT_IDS",
            source_env.get("CHANNEL_CONTEXT_CANARY_CHAT_IDS", ""),
        ),
        "context_window_tokens": source_env.get("CHANNEL_CONTEXT_WINDOW_TOKENS", "128000"),
        "context_reserve_tokens": source_env.get("CHANNEL_CONTEXT_RESERVE_TOKENS", "16000"),
        "context_keep_recent_tokens": source_env.get("CHANNEL_CONTEXT_KEEP_RECENT_TOKENS", "24000"),
        "context_summary_max_tokens": source_env.get("CHANNEL_CONTEXT_SUMMARY_MAX_TOKENS", "1200"),
        "context_min_gain_tokens": source_env.get("CHANNEL_CONTEXT_MIN_GAIN_TOKENS", "800"),
        "context_compaction_cooldown_s": source_env.get("CHANNEL_CONTEXT_COMPACTION_COOLDOWN_S", "300"),
        "context_strict_io": source_env.get("CHANNEL_CONTEXT_STRICT_IO", "false"),
        "context_manual_compact": source_env.get("CHANNEL_CONTEXT_MANUAL_COMPACT", "false"),
    }

    _apply_cli_overrides(values, args)

    codex_timeout_s = _parse_positive_float(values["codex_timeout_s"], field_name="codex_timeout_s")
    notify_on_orchestrator_error = _parse_bool(
        values["notify_on_orchestrator_error"],
        field_name="notify_on_orchestrator_error",
    )
    codex_session_max = _parse_positive_int(values["codex_session_max"], field_name="codex_session_max")
    codex_session_idle_ttl_s = _parse_positive_float(
        values["codex_session_idle_ttl_s"],
        field_name="codex_session_idle_ttl_s",
    )
    poll_interval_s = _parse_positive_float(values["poll_interval_s"], field_name="poll_interval_s")
    allowed_chat_ids = _parse_allowlist(values["allowed_chat_ids"], field_name="allowed_chat_ids")
    cursor_state_path = str(values["cursor_state_path"]).strip()
    strict_cursor_state_io = _parse_bool(values["strict_cursor_state_io"], field_name="strict_cursor_state_io")
    live_mode = _parse_bool(values["live_mode"], field_name="live_mode")
    once = _parse_bool(values["once"], field_name="once")
    context_canary_chat_ids = _parse_allowlist(
        values["context_canary_chat_ids"],
        field_name="context_canary_chat_ids",
    )
    context_window_tokens = _parse_positive_int(
        values["context_window_tokens"],
        field_name="context_window_tokens",
    )
    context_reserve_tokens = _parse_nonnegative_int(
        values["context_reserve_tokens"],
        field_name="context_reserve_tokens",
    )
    context_keep_recent_tokens = _parse_positive_int(
        values["context_keep_recent_tokens"],
        field_name="context_keep_recent_tokens",
    )
    context_summary_max_tokens = _parse_positive_int(
        values["context_summary_max_tokens"],
        field_name="context_summary_max_tokens",
    )
    context_min_gain_tokens = _parse_nonnegative_int(
        values["context_min_gain_tokens"],
        field_name="context_min_gain_tokens",
    )
    context_compaction_cooldown_s = _parse_nonnegative_float(
        values["context_compaction_cooldown_s"],
        field_name="context_compaction_cooldown_s",
    )
    context_strict_io = _parse_bool(values["context_strict_io"], field_name="context_strict_io")
    context_manual_compact = _parse_bool(values["context_manual_compact"], field_name="context_manual_compact")

    return RuntimeConfig(
        token=str(values["token"]),
        mode=str(values["mode"]),
        ack_policy=str(values["ack_policy"]),
        orchestrator_mode=str(values["orchestrator_mode"]),
        codex_timeout_s=codex_timeout_s,
        notify_on_orchestrator_error=notify_on_orchestrator_error,
        codex_session_max=codex_session_max,
        codex_session_idle_ttl_s=codex_session_idle_ttl_s,
        poll_interval_s=poll_interval_s,
        allowed_chat_ids=allowed_chat_ids,
        cursor_state_path=cursor_state_path,
        strict_cursor_state_io=strict_cursor_state_io,
        live_mode=live_mode,
        once=once,
        context_mode=str(values["context_mode"]),
        context_canary_chat_ids=context_canary_chat_ids,
        context_window_tokens=context_window_tokens,
        context_reserve_tokens=context_reserve_tokens,
        context_keep_recent_tokens=context_keep_recent_tokens,
        context_summary_max_tokens=context_summary_max_tokens,
        context_min_gain_tokens=context_min_gain_tokens,
        context_compaction_cooldown_s=context_compaction_cooldown_s,
        context_strict_io=context_strict_io,
        context_manual_compact=context_manual_compact,
    )


def _apply_cli_overrides(values: dict[str, object], args: list[str]) -> None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--once":
            values["once"] = True
            i += 1
            continue

        if arg in {
            "--token",
            "--mode",
            "--ack-policy",
            "--orchestrator-mode",
            "--codex-timeout-s",
            "--notify-on-orchestrator-error",
            "--codex-session-max",
            "--codex-session-idle-ttl-s",
            "--poll-interval-s",
            "--allowed-chat-ids",
            "--cursor-state-path",
            "--strict-cursor-state-io",
            "--live-mode",
            "--context-mode",
            "--context-canary-chat-ids",
            "--context-canary-allowlist-chat-ids",
            "--context-window-tokens",
            "--context-reserve-tokens",
            "--context-keep-recent-tokens",
            "--context-summary-max-tokens",
            "--context-min-gain-tokens",
            "--context-compaction-cooldown-s",
            "--context-strict-io",
            "--context-manual-compact",
        }:
            if i + 1 >= len(args):
                raise ConfigValidationError(f"missing value for {arg}")
            value = args[i + 1]
            if arg == "--token":
                values["token"] = value
            elif arg == "--mode":
                values["mode"] = value
            elif arg == "--ack-policy":
                values["ack_policy"] = value
            elif arg == "--orchestrator-mode":
                values["orchestrator_mode"] = value
            elif arg == "--codex-timeout-s":
                values["codex_timeout_s"] = value
            elif arg == "--notify-on-orchestrator-error":
                values["notify_on_orchestrator_error"] = value
            elif arg == "--codex-session-max":
                values["codex_session_max"] = value
            elif arg == "--codex-session-idle-ttl-s":
                values["codex_session_idle_ttl_s"] = value
            elif arg == "--poll-interval-s":
                values["poll_interval_s"] = value
            elif arg == "--allowed-chat-ids":
                values["allowed_chat_ids"] = value
            elif arg == "--cursor-state-path":
                values["cursor_state_path"] = value
            elif arg == "--strict-cursor-state-io":
                values["strict_cursor_state_io"] = value
            elif arg == "--live-mode":
                values["live_mode"] = value
            elif arg == "--context-mode":
                values["context_mode"] = value
            elif arg == "--context-canary-chat-ids":
                values["context_canary_chat_ids"] = value
            elif arg == "--context-canary-allowlist-chat-ids":
                values["context_canary_chat_ids"] = value
            elif arg == "--context-window-tokens":
                values["context_window_tokens"] = value
            elif arg == "--context-reserve-tokens":
                values["context_reserve_tokens"] = value
            elif arg == "--context-keep-recent-tokens":
                values["context_keep_recent_tokens"] = value
            elif arg == "--context-summary-max-tokens":
                values["context_summary_max_tokens"] = value
            elif arg == "--context-min-gain-tokens":
                values["context_min_gain_tokens"] = value
            elif arg == "--context-compaction-cooldown-s":
                values["context_compaction_cooldown_s"] = value
            elif arg == "--context-strict-io":
                values["context_strict_io"] = value
            elif arg == "--context-manual-compact":
                values["context_manual_compact"] = value
            i += 2
            continue

        raise ConfigValidationError(f"unknown argument: {arg}")


def _parse_positive_float(raw: object, *, field_name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{field_name} must be a positive number") from exc
    if value <= 0:
        raise ConfigValidationError(f"{field_name} must be a positive number")
    return value


def _parse_positive_int(raw: object, *, field_name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{field_name} must be an integer >= 1") from exc
    if value < 1:
        raise ConfigValidationError(f"{field_name} must be an integer >= 1")
    return value


def _parse_nonnegative_int(raw: object, *, field_name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{field_name} must be an integer >= 0") from exc
    if value < 0:
        raise ConfigValidationError(f"{field_name} must be an integer >= 0")
    return value


def _parse_nonnegative_float(raw: object, *, field_name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{field_name} must be a number >= 0") from exc
    if value < 0:
        raise ConfigValidationError(f"{field_name} must be a number >= 0")
    return value


def _parse_allowlist(raw: object, *, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        values = [part.strip() for part in text.split(",")]
    else:
        try:
            values = [str(part).strip() for part in raw]  # type: ignore[arg-type]
        except TypeError as exc:
            raise ConfigValidationError(f"{field_name} must be a string or list of strings") from exc

    if any(not value for value in values):
        raise ConfigValidationError(f"{field_name} must not contain empty values")
    return tuple(values)


def _parse_bool(raw: object, *, field_name: str) -> bool:
    if isinstance(raw, bool):
        return raw

    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigValidationError(f"{field_name} must be a boolean")


def legacy_context_retention_plan() -> dict[str, object]:
    """One-release retention contract for the legacy context emergency path."""
    return {
        "mode": LEGACY_CONTEXT_MODE,
        "emergency_toggle": LEGACY_CONTEXT_EMERGENCY_TOGGLE,
        "retention_releases": LEGACY_CONTEXT_RETENTION_RELEASES,
        "removal_criteria": (
            "durable-mode canary and full rollout remain stable for one release cycle",
            "no unresolved sev-1/sev-2 durable context incidents",
            "operator rollback runbook validated without legacy mode dependency",
        ),
    }
