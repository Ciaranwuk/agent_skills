#!/usr/bin/env python3
"""Run the surprise-me worker and optionally send Telegram notification."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import surprise_me_telegram_notifier, surprise_me_workflow_improvement  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import surprise_me_telegram_notifier, surprise_me_workflow_improvement  # type: ignore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_surprise_me_nightly")
    parser.add_argument("--telegram-notify", action="store_true")
    parser.add_argument("--notify-chat-id", default="")
    parser.add_argument("--notify-chat-id-env-var", default=surprise_me_telegram_notifier.DEFAULT_CHAT_ID_ENV_VAR)
    parser.add_argument("--notify-token", default="")
    return parser


def _run_notifier(
    *,
    worker_payload: dict[str, object],
    wrapper_args: argparse.Namespace,
    worker_args: argparse.Namespace,
) -> dict[str, object]:
    attempted_at = datetime.now(timezone.utc).isoformat()
    if not wrapper_args.telegram_notify:
        return {
            "status": "skipped",
            "reason": "morning-digest-owned",
            "attempted_at": attempted_at,
            "delivery_channel": "telegram",
            "failure_stage": "disabled",
        }

    output_file = str(worker_payload.get("output_file") or "").strip()
    if not output_file:
        return {
            "status": "skipped",
            "reason": "missing-output-file",
            "attempted_at": attempted_at,
            "delivery_channel": "telegram",
            "failure_stage": "missing-output-file",
        }

    notifier_args = surprise_me_telegram_notifier.build_parser().parse_args(
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
        notifier_payload = surprise_me_telegram_notifier.run(notifier_args)
        return {
            "status": str(notifier_payload.get("status") or "sent"),
            "attempted_at": notifier_payload.get("attempted_at", attempted_at),
            "delivery_channel": notifier_payload.get("delivery_channel", "telegram"),
            "failure_stage": notifier_payload.get("failure_stage", ""),
            "chat_id": notifier_payload.get("chat_id", notifier_args.chat_id),
            "run_id": notifier_payload.get("run_id", worker_payload.get("run_id", "")),
            "sidecar_path": notifier_payload.get("sidecar_path", str(Path(output_file).with_suffix(".json"))),
            "report_path": notifier_payload.get("report_path", output_file),
            "message_length": len(str(notifier_payload.get("message_text") or "")),
        }
    except surprise_me_telegram_notifier.SurpriseMeNotifierError as exc:
        diagnostics = dict(exc.diagnostics)
        failure: dict[str, Any] = {
            "status": "failed",
            "attempted_at": diagnostics.get("attempted_at", attempted_at),
            "delivery_channel": diagnostics.get("delivery_channel", "telegram"),
            "failure_stage": diagnostics.get("failure_stage", ""),
            "reason": str(exc),
            "chat_id": diagnostics.get("chat_id", ""),
            "run_id": diagnostics.get("run_id", worker_payload.get("run_id", "")),
            "sidecar_path": diagnostics.get("sidecar_path", str(Path(output_file).with_suffix(".json"))),
            "report_path": diagnostics.get("report_path", output_file),
            "message_length": diagnostics.get("message_length", 0),
        }
        cause = exc.__cause__
        if isinstance(cause, surprise_me_telegram_notifier.TelegramApiError):
            failure["telegram_api_error"] = cause.to_dict()
        return failure


def _persist_notification_result(worker_payload: dict[str, object], notification: dict[str, object]) -> None:
    output_file = Path(str(worker_payload.get("output_file") or "").strip())
    if not output_file:
        return
    sidecar_path = output_file.with_suffix(".json")
    report_path = output_file
    if sidecar_path.is_file():
        try:
            sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sidecar_payload = {}
        if isinstance(sidecar_payload, dict):
            sidecar_payload["notification"] = notification
            sidecar_path.write_text(json.dumps(sidecar_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8").rstrip()
        marker = "\n## Notification\n"
        if marker in report_text:
            report_text = report_text.split(marker, 1)[0].rstrip()
        lines = [
            report_text,
            "",
            "## Notification",
            f"- Status: `{notification.get('status', 'unknown')}`",
            f"- Attempted at: `{notification.get('attempted_at', '')}`",
            f"- Delivery channel: `{notification.get('delivery_channel', 'telegram')}`",
        ]
        chat_id = str(notification.get("chat_id") or "").strip()
        if chat_id:
            lines.append(f"- Chat ID: `{chat_id}`")
        run_id = str(notification.get("run_id") or "").strip()
        if run_id:
            lines.append(f"- Run ID: `{run_id}`")
        sidecar_path = str(notification.get("sidecar_path") or "").strip()
        if sidecar_path:
            lines.append(f"- Sidecar: `{sidecar_path}`")
        report_output = str(notification.get("report_path") or "").strip()
        if report_output:
            lines.append(f"- Report: `{report_output}`")
        message_length = notification.get("message_length")
        if isinstance(message_length, int):
            lines.append(f"- Message length: `{message_length}`")
        failure_stage = str(notification.get("failure_stage") or "").strip()
        if failure_stage:
            lines.append(f"- Failure stage: `{failure_stage}`")
        reason = str(notification.get("reason") or "").strip()
        if reason:
            lines.append(f"- Reason: `{reason}`")
        telegram_api_error = notification.get("telegram_api_error")
        if isinstance(telegram_api_error, dict):
            lines.append(
                "- Telegram API error: "
                f"kind `{telegram_api_error.get('kind', '')}` / transient `{telegram_api_error.get('transient', False)}` / "
                f"description `{telegram_api_error.get('description', '')}`"
            )
            status_code = telegram_api_error.get("status_code")
            error_code = telegram_api_error.get("error_code")
            retry_after = telegram_api_error.get("retry_after_seconds")
            if status_code is not None:
                lines.append(f"- Telegram status code: `{status_code}`")
            if error_code is not None:
                lines.append(f"- Telegram error code: `{error_code}`")
            if retry_after is not None:
                lines.append(f"- Retry after seconds: `{retry_after}`")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    wrapper_args, worker_argv = build_parser().parse_known_args(argv)
    worker_args = surprise_me_workflow_improvement.build_parser().parse_args(worker_argv)

    try:
        worker_payload = surprise_me_workflow_improvement.run(worker_args)
    except surprise_me_workflow_improvement.SurpriseMeError as exc:
        print(
            json.dumps(
                {
                    "worker": {
                        "run_id": "",
                        "status": "failed",
                        "notes_scanned": 0,
                        "candidate_connections": 0,
                        "candidate_ideas": 0,
                        "selected_idea_id": "",
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
    _persist_notification_result(worker_payload, notification)
    if notification.get("status") == "failed":
        print(f"surprise-me notifier failed: {notification.get('reason', 'unknown error')}", file=sys.stderr)

    print(json.dumps({"worker": worker_payload, "notification": notification}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
