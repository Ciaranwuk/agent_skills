from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from telegram_channel.api import TelegramApiClient, TelegramApiError

try:
    from . import local_env, surprise_me_workflow_improvement  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import local_env, surprise_me_workflow_improvement  # type: ignore

RUNS_RELATIVE_DIR = Path("00_Inbox/surprise_me/runs")
DEFAULT_CHAT_ID_ENV_VAR = "SURPRISE_ME_TELEGRAM_CHAT_ID"
TOKEN_ENV_CANDIDATES = ("SURPRISE_ME_TELEGRAM_BOT_TOKEN", "CHANNEL_TOKEN")


class SurpriseMeNotifierError(RuntimeError):
    """Structured error for surprise-me Telegram notification flow."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def _ensure_under_root(staging_root: Path, sidecar_path: Path) -> Path:
    root = staging_root.resolve()
    candidate = sidecar_path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SurpriseMeNotifierError(f"path escapes staging root: {sidecar_path}") from exc
    return candidate


def select_latest_sidecar(staging_root: Path) -> Path:
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    candidates = sorted(runs_dir.glob("SURPRISE-ME-RUN-*.json"))
    if not candidates:
        raise SurpriseMeNotifierError(f"no surprise-me sidecars found under {runs_dir}")
    return candidates[-1].resolve()


def resolve_sidecar_path(staging_root: Path, raw_sidecar_path: str) -> Path:
    if not raw_sidecar_path.strip():
        return select_latest_sidecar(staging_root)
    candidate = Path(raw_sidecar_path).expanduser()
    if not candidate.is_absolute():
        candidate = staging_root / candidate
    safe_path = _ensure_under_root(staging_root, candidate)
    if not safe_path.is_file():
        raise SurpriseMeNotifierError(f"sidecar not found: {safe_path}")
    return safe_path


def load_sidecar(sidecar_path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SurpriseMeNotifierError(f"failed to read sidecar {sidecar_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SurpriseMeNotifierError(f"failed to parse sidecar {sidecar_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SurpriseMeNotifierError(f"sidecar {sidecar_path} must contain a JSON object")
    return payload


def resolve_report_path(payload: Mapping[str, Any], sidecar_path: Path) -> str:
    outputs = payload.get("outputs")
    if isinstance(outputs, Mapping):
        output_file = str(outputs.get("run_report") or "").strip()
        if output_file:
            return output_file

    artifacts = payload.get("artifacts_created")
    if isinstance(artifacts, list):
        for item in artifacts:
            value = str(item).strip()
            if value.endswith(".md"):
                return value
    return sidecar_path.with_suffix(".md").as_posix()


def format_message(payload: Mapping[str, Any], *, sidecar_path: Path) -> str:
    run_id = str(payload.get("run_id") or sidecar_path.stem)
    status = str(payload.get("status") or "unknown")
    output_file = resolve_report_path(payload, sidecar_path)

    if status == "succeeded":
        selected = payload.get("selected_idea")
        selected_mapping = selected if isinstance(selected, Mapping) else {}
        title = str(selected_mapping.get("title") or payload.get("summary") or "workflow improvement selected")
        benefit = str(selected_mapping.get("expected_benefit") or payload.get("expected_benefit") or "").strip()
        next_action = str(selected_mapping.get("next_action") or payload.get("action_taken") or "proposal_only")
        risk_level = str(selected_mapping.get("risk_level") or payload.get("risk_level") or "unknown")
        follow_up = str(payload.get("follow_up_recommendation") or "").strip()
        lines = [
            "Surprise-me nightly: succeeded",
            f"Run: {run_id}",
            f"Idea: {title}",
        ]
        if benefit:
            lines.append(f"Benefit: {benefit}")
        lines.append(f"Risk: {risk_level}")
        lines.append(f"Next: {next_action}")
        if follow_up:
            lines.append(f"Follow-up: {follow_up}")
        lines.append(f"Report: {output_file}")
        return "\n".join(lines)

    if status == "no_findings":
        lines = [
            "Surprise-me nightly: no high-confidence improvement tonight",
            f"Run: {run_id}",
            f"Notes scanned: {int(payload.get('notes_scanned') or 0)}",
            f"Summary: {str(payload.get('summary') or 'no high-confidence improvement tonight')}",
        ]
        follow_up = str(payload.get("follow_up_recommendation") or "").strip()
        if follow_up:
            lines.append(f"Follow-up: {follow_up}")
        lines.append(f"Report: {output_file}")
        return "\n".join(lines)

    lines = [
        "Surprise-me nightly: unexpected result",
        f"Run: {run_id}",
        f"Status: {status}",
        f"Report: {output_file}",
    ]
    return "\n".join(lines)


def _resolve_chat_id(args: argparse.Namespace) -> str:
    if str(args.chat_id).strip():
        return str(args.chat_id).strip()
    env_var = str(args.chat_id_env_var).strip()
    if env_var:
        chat_id = local_env.first_env_value(env_var)
        if chat_id:
            return chat_id
    raise SurpriseMeNotifierError("chat id is required via --chat-id or configured environment variable")


def _resolve_token(args: argparse.Namespace) -> str:
    if str(args.token).strip():
        return str(args.token).strip()
    token = local_env.first_env_value(*TOKEN_ENV_CANDIDATES)
    if token:
        return token
    raise SurpriseMeNotifierError(
        "Telegram bot token is required via --token or environment "
        f"({', '.join(TOKEN_ENV_CANDIDATES)})"
    )


def run(
    args: argparse.Namespace,
    *,
    api_client_factory=TelegramApiClient,
) -> dict[str, Any]:
    attempted_at = surprise_me_workflow_improvement.datetime.now(surprise_me_workflow_improvement.timezone.utc).isoformat()
    diagnostics: dict[str, Any] = {
        "attempted_at": attempted_at,
        "delivery_channel": "telegram",
        "failure_stage": "initializing",
        "sidecar_path": "",
        "report_path": "",
        "run_id": "",
        "chat_id": "",
        "message_length": 0,
    }

    staging_root = Path(args.staging_root).resolve()
    if not staging_root.exists():
        raise SurpriseMeNotifierError(f"staging root not found: {staging_root}", diagnostics=diagnostics)
    try:
        diagnostics["failure_stage"] = "resolving_sidecar"
        sidecar_path = resolve_sidecar_path(staging_root, str(args.sidecar_path))
        diagnostics["sidecar_path"] = sidecar_path.as_posix()

        diagnostics["failure_stage"] = "loading_sidecar"
        payload = load_sidecar(sidecar_path)
        diagnostics["run_id"] = str(payload.get("run_id") or sidecar_path.stem)
        diagnostics["report_path"] = resolve_report_path(payload, sidecar_path)

        diagnostics["failure_stage"] = "resolving_chat_id"
        chat_id = _resolve_chat_id(args)
        diagnostics["chat_id"] = chat_id

        diagnostics["failure_stage"] = "resolving_token"
        token = _resolve_token(args)

        diagnostics["failure_stage"] = "formatting_message"
        message = format_message(payload, sidecar_path=sidecar_path)
        diagnostics["message_length"] = len(message)

        diagnostics["failure_stage"] = "sending_message"
        client = api_client_factory(token)
        client.send_message(chat_id=chat_id, text=message)
    except TelegramApiError as exc:
        raise SurpriseMeNotifierError(
            f"failed to send Telegram notification: {exc}",
            diagnostics=diagnostics,
        ) from exc
    except SurpriseMeNotifierError as exc:
        merged_diagnostics = dict(diagnostics)
        merged_diagnostics.update(exc.diagnostics)
        enriched = SurpriseMeNotifierError(str(exc), diagnostics=merged_diagnostics)
        if exc.__cause__ is not None:
            raise enriched from exc.__cause__
        raise enriched
    return {
        "status": "sent",
        "attempted_at": attempted_at,
        "delivery_channel": "telegram",
        "failure_stage": "",
        "chat_id": chat_id,
        "run_id": diagnostics["run_id"],
        "sidecar_path": sidecar_path.as_posix(),
        "report_path": diagnostics["report_path"],
        "message_length": len(message),
        "message_text": message,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a surprise-me sidecar summary to Telegram.")
    parser.add_argument("--staging-root", default=str(surprise_me_workflow_improvement.DEFAULT_STAGING_ROOT))
    parser.add_argument("--sidecar-path", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--chat-id-env-var", default=DEFAULT_CHAT_ID_ENV_VAR)
    parser.add_argument("--token", default="")
    parser.add_argument("--allow-send-failure", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except SurpriseMeNotifierError as exc:
        diagnostics = dict(exc.diagnostics)
        status = "failed"
        if args.allow_send_failure and "failed to send Telegram notification:" in str(exc):
            status = "send_failed_nonfatal"
        print(
            json.dumps(
                {
                    "status": status,
                    "attempted_at": diagnostics.get("attempted_at", ""),
                    "delivery_channel": diagnostics.get("delivery_channel", "telegram"),
                    "failure_stage": diagnostics.get("failure_stage", ""),
                    "chat_id": str(diagnostics.get("chat_id", "")).strip(),
                    "run_id": str(diagnostics.get("run_id", "")).strip(),
                    "sidecar_path": str(diagnostics.get("sidecar_path", "")).strip(),
                    "report_path": str(diagnostics.get("report_path", "")).strip(),
                    "message_length": int(diagnostics.get("message_length", 0) or 0),
                    "message_text": "",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 0 if status == "send_failed_nonfatal" else 1

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
