from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

from channel_core.contracts import ConfigValidationError


@dataclass(frozen=True)
class RuntimeConfig:
    token: str
    mode: str = "poll"
    poll_interval_s: float = 2.0
    allowed_chat_ids: tuple[str, ...] = ()
    once: bool = False

    def __post_init__(self) -> None:
        token = str(self.token).strip()
        mode = str(self.mode).strip()

        if not token:
            raise ConfigValidationError("token must be a non-empty string")
        if mode != "poll":
            raise ConfigValidationError("mode must be 'poll' for TG-P0")
        if self.poll_interval_s <= 0:
            raise ConfigValidationError("poll_interval_s must be a positive number")
        for chat_id in self.allowed_chat_ids:
            if not str(chat_id).strip():
                raise ConfigValidationError("allowed_chat_ids must not contain empty values")


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
        "poll_interval_s": source_env.get("CHANNEL_POLL_INTERVAL_S", "2.0"),
        "allowed_chat_ids": source_env.get("CHANNEL_ALLOWED_CHAT_IDS", ""),
        "once": source_env.get("CHANNEL_ONCE", "false"),
    }

    _apply_cli_overrides(values, args)

    poll_interval_s = _parse_positive_float(values["poll_interval_s"], field_name="poll_interval_s")
    allowed_chat_ids = _parse_allowlist(values["allowed_chat_ids"])
    once = _parse_bool(values["once"], field_name="once")

    return RuntimeConfig(
        token=str(values["token"]),
        mode=str(values["mode"]),
        poll_interval_s=poll_interval_s,
        allowed_chat_ids=allowed_chat_ids,
        once=once,
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
            "--poll-interval-s",
            "--allowed-chat-ids",
        }:
            if i + 1 >= len(args):
                raise ConfigValidationError(f"missing value for {arg}")
            value = args[i + 1]
            if arg == "--token":
                values["token"] = value
            elif arg == "--mode":
                values["mode"] = value
            elif arg == "--poll-interval-s":
                values["poll_interval_s"] = value
            elif arg == "--allowed-chat-ids":
                values["allowed_chat_ids"] = value
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


def _parse_allowlist(raw: object) -> tuple[str, ...]:
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
            raise ConfigValidationError("allowed_chat_ids must be a string or list of strings") from exc

    if any(not value for value in values):
        raise ConfigValidationError("allowed_chat_ids must not contain empty values")
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
