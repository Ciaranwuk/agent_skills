#!/usr/bin/env python3
"""Run the morning-triage worker and optionally send Telegram notification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import morning_triage_digest, morning_triage_telegram_notifier  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import morning_triage_digest, morning_triage_telegram_notifier  # type: ignore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_morning_triage_digest")
    parser.add_argument("--skip-telegram-notify", action="store_true")
    parser.add_argument("--allow-notify-failure", action="store_true")
    parser.add_argument("--notify-chat-id", default="")
    parser.add_argument("--notify-chat-id-env-var", default=morning_triage_telegram_notifier.DEFAULT_CHAT_ID_ENV_VAR)
    parser.add_argument("--notify-token", default="")
    return parser


def _run_notifier(
    *,
    worker_payload: dict[str, object],
    wrapper_args: argparse.Namespace,
    worker_args: argparse.Namespace,
) -> dict[str, object]:
    if wrapper_args.skip_telegram_notify:
        return {"status": "skipped", "reason": "disabled"}

    output_file = str(worker_payload.get("output_file") or "").strip()
    if not output_file:
        return {"status": "skipped", "reason": "missing-output-file"}

    notifier_args = morning_triage_telegram_notifier.build_parser().parse_args(
        [
            "--staging-root",
            str(worker_args.staging_root),
            "--sidecar-path",
            str(Path(output_file).with_suffix(".json")),
            "--chat-id-env-var",
            str(wrapper_args.notify_chat_id_env_var),
        ]
    )
    notifier_args.chat_id = str(wrapper_args.notify_chat_id).strip()
    notifier_args.token = str(wrapper_args.notify_token).strip()

    try:
        return morning_triage_telegram_notifier.run(notifier_args)
    except morning_triage_telegram_notifier.MorningTriageNotifierError as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            "sidecar_path": str(Path(output_file).with_suffix(".json")),
        }
    except Exception as exc:  # pragma: no cover - defensive wrapper for unexpected notifier bugs
        return {
            "status": "failed",
            "reason": f"unexpected notifier error: {type(exc).__name__}: {exc}",
            "sidecar_path": str(Path(output_file).with_suffix(".json")),
        }


def main(argv: list[str] | None = None) -> int:
    wrapper_args, worker_argv = build_parser().parse_known_args(argv)
    worker_args = morning_triage_digest.build_parser().parse_args(worker_argv)

    try:
        worker_payload = morning_triage_digest.run(worker_args)
    except morning_triage_digest.MorningTriageError as exc:
        print(
            json.dumps(
                {
                    "worker": {
                        "run_id": "",
                        "status": "failed",
                        "jobs_considered": 0,
                        "urgent_items": 0,
                        "output_file": "",
                        "error": str(exc),
                    },
                    "notification": {"status": "skipped", "reason": "worker-failed"},
                },
                sort_keys=True,
            )
        )
        return 1

    notification = _run_notifier(
        worker_payload=worker_payload,
        wrapper_args=wrapper_args,
        worker_args=worker_args,
    )
    if notification.get("status") == "failed":
        print(f"morning-triage notifier failed: {notification.get('reason', 'unknown error')}", file=sys.stderr)
        if not wrapper_args.allow_notify_failure:
            print(json.dumps({"worker": worker_payload, "notification": notification}, sort_keys=True))
            return 1

    print(json.dumps({"worker": worker_payload, "notification": notification}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
