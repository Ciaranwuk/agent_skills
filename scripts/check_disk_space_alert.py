#!/usr/bin/env python3
"""Send a Telegram alert when the configured filesystem is nearly full."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from telegram_channel.api import TelegramApiClient


DEFAULT_THRESHOLD_BYTES = 10_000_000_000
# The operator's laptop data disk is exposed to this Linux environment at
# /mnt/c.  Keep the host root overrideable, but monitor the actual constrained
# filesystem by default.
DEFAULT_PATH = "/mnt/c"
DEFAULT_STATE_PATH = ".channel_runtime/disk_space_alert_state.json"
DEFAULT_CHAT_ID = "5946121750"


@dataclass(frozen=True)
class DiskStatus:
    path: str
    free_bytes: int
    total_bytes: int
    threshold_bytes: int

    @property
    def below_threshold(self) -> bool:
        return self.free_bytes < self.threshold_bytes


def inspect_disk(
    path: str = DEFAULT_PATH,
    *,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    usage_fn: Callable[[str], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> DiskStatus:
    usage = usage_fn(path)
    return DiskStatus(
        path=path,
        free_bytes=int(usage.free),
        total_bytes=int(usage.total),
        threshold_bytes=int(threshold_bytes),
    )


def format_gb(byte_count: int) -> str:
    return f"{byte_count / 1_000_000_000:.2f} GB"


def load_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def run_check(
    *,
    status: DiskStatus,
    state_path: Path,
    token: str,
    chat_id: str,
    api_factory: Callable[[str], TelegramApiClient] = TelegramApiClient,
) -> str:
    state = load_state(state_path)
    was_below = bool(state.get("alert_active", False))
    api = api_factory(token)

    if status.below_threshold and not was_below:
        text = (
            "⚠️ Low disk space alert\n"
            f"Filesystem: {status.path}\n"
            f"Available: {format_gb(status.free_bytes)}\n"
            f"Threshold: {format_gb(status.threshold_bytes)}"
        )
        api.send_message(chat_id=chat_id, text=text)
        save_state(state_path, {"alert_active": True, "last_free_bytes": status.free_bytes})
        return "alert-sent"

    if not status.below_threshold and was_below:
        text = (
            "✅ Disk space recovered\n"
            f"Filesystem: {status.path}\n"
            f"Available: {format_gb(status.free_bytes)}"
        )
        api.send_message(chat_id=chat_id, text=text)
        save_state(state_path, {"alert_active": False, "last_free_bytes": status.free_bytes})
        return "recovery-sent"

    save_state(state_path, {"alert_active": status.below_threshold, "last_free_bytes": status.free_bytes})
    return "unchanged"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=os.environ.get("DISK_ALERT_PATH", DEFAULT_PATH))
    parser.add_argument(
        "--threshold-gb",
        type=float,
        default=float(os.environ.get("DISK_ALERT_THRESHOLD_GB", "10")),
    )
    parser.add_argument(
        "--state-path",
        default=os.environ.get("DISK_ALERT_STATE_PATH", DEFAULT_STATE_PATH),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    token = os.environ.get("CHANNEL_TOKEN", "").strip()
    chat_id = os.environ.get("DISK_ALERT_CHAT_ID", DEFAULT_CHAT_ID).strip()
    if not token:
        print("disk-space-alert: CHANNEL_TOKEN is not set", file=sys.stderr)
        return 2
    if not chat_id:
        print("disk-space-alert: DISK_ALERT_CHAT_ID is empty", file=sys.stderr)
        return 2
    if args.threshold_gb <= 0:
        print("disk-space-alert: threshold must be positive", file=sys.stderr)
        return 2

    status = inspect_disk(
        args.path,
        threshold_bytes=int(args.threshold_gb * 1_000_000_000),
    )
    result = run_check(
        status=status,
        state_path=Path(args.state_path),
        token=token,
        chat_id=chat_id,
    )
    print(
        f"disk-space-alert: {result}; {args.path} has {format_gb(status.free_bytes)} available",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
