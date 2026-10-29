from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:  # pragma: no cover - direct script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_channel.api import TelegramApiClient, TelegramApiError

try:
    from . import local_env, morning_triage_digest  # type: ignore
except ImportError:  # pragma: no cover - direct script execution path
    from scripts import local_env, morning_triage_digest  # type: ignore

RUNS_RELATIVE_DIR = Path("00_Inbox/morning_triage/runs")
DEFAULT_CHAT_ID_ENV_VAR = "MORNING_TRIAGE_TELEGRAM_CHAT_ID"
TOKEN_ENV_CANDIDATES = ("MORNING_TRIAGE_TELEGRAM_BOT_TOKEN", "CHANNEL_TOKEN")
MAX_URGENT_ITEMS = 5
MAX_FOLLOW_UPS = 3


class MorningTriageNotifierError(RuntimeError):
    """Structured error for morning-triage Telegram notification flow."""


def _count_items(value: object) -> int:
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MorningTriageNotifierError(f"invalid count value: {value!r}") from exc


def _ensure_under_root(staging_root: Path, sidecar_path: Path) -> Path:
    root = staging_root.resolve()
    candidate = sidecar_path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MorningTriageNotifierError(f"path escapes staging root: {sidecar_path}") from exc
    return candidate


def select_latest_sidecar(staging_root: Path) -> Path:
    runs_dir = staging_root / RUNS_RELATIVE_DIR
    candidates = sorted(runs_dir.glob("MORNING-TRIAGE-RUN-*.json"))
    if not candidates:
        raise MorningTriageNotifierError(f"no morning-triage sidecars found under {runs_dir}")
    return candidates[-1].resolve()


def resolve_sidecar_path(staging_root: Path, raw_sidecar_path: str) -> Path:
    if not raw_sidecar_path.strip():
        return select_latest_sidecar(staging_root)
    candidate = Path(raw_sidecar_path).expanduser()
    if not candidate.is_absolute():
        candidate = staging_root / candidate
    safe_path = _ensure_under_root(staging_root, candidate)
    if not safe_path.is_file():
        raise MorningTriageNotifierError(f"sidecar not found: {safe_path}")
    return safe_path


def load_sidecar(sidecar_path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MorningTriageNotifierError(f"failed to read sidecar {sidecar_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MorningTriageNotifierError(f"failed to parse sidecar {sidecar_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MorningTriageNotifierError(f"sidecar {sidecar_path} must contain a JSON object")
    return payload


def resolve_report_path(payload: Mapping[str, Any], sidecar_path: Path) -> str:
    outputs = payload.get("outputs")
    if isinstance(outputs, Mapping):
        output_file = str(outputs.get("run_report") or "").strip()
        if output_file:
            return output_file
    return sidecar_path.with_suffix(".md").as_posix()


def _render_ticket_context(ticket_context: Mapping[str, Any]) -> list[str]:
    total_open = int(ticket_context.get("total_open") or 0)
    lines = [f"Open tickets: {total_open}"]
    lane_counts = ticket_context.get("lane_counts")
    if isinstance(lane_counts, Mapping):
        for lane in ("In Progress", "Blocked", "Ready", "Backlog", "Review"):
            count = int(lane_counts.get(lane) or 0)
            if count:
                lines.append(f"{lane}: {count}")
    spotlight = ticket_context.get("spotlight_tickets")
    if isinstance(spotlight, list) and spotlight:
        keys = []
        for ticket in spotlight[:5]:
            if isinstance(ticket, Mapping):
                key = str(ticket.get("ticket_key") or "").strip()
                if key:
                    keys.append(key)
        if keys:
            lines.append(f"Spotlight: {', '.join(keys)}")
    return lines


def _format_ranked_items(
    items: object,
    *,
    limit: int,
) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items[:limit]:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip() or "Untitled item"
        detail = str(item.get("detail") or "").strip()
        line = f"- {title}"
        if detail:
            line = f"{line}: {detail}"
        lines.append(line)
    return lines


def format_message(payload: Mapping[str, Any], *, sidecar_path: Path) -> str:
    run_id = str(payload.get("run_id") or sidecar_path.stem)
    status = str(payload.get("status") or "unknown")
    jobs_considered = _count_items(payload.get("jobs_considered"))
    urgent_items = payload.get("urgent_items")
    urgent_count = len(urgent_items) if isinstance(urgent_items, list) else 0
    report_path = resolve_report_path(payload, sidecar_path)

    if status == "nothing_urgent":
        lines = ["Morning triage: nothing urgent overnight."]
    elif status == "attention_required":
        lines = ["Morning triage: attention required."]
    else:
        lines = ["Morning triage: unexpected result."]

    lines.extend(
        [
            "",
            f"Run: {run_id}",
            f"Jobs checked: {jobs_considered}",
            f"Urgent items: {urgent_count}",
        ]
    )

    ticket_context = payload.get("ticket_context")
    if isinstance(ticket_context, Mapping):
        lines.append("")
        lines.append("Mission Control:")
        lines.extend(f"- {line}" for line in _render_ticket_context(ticket_context))

    urgent_lines = _format_ranked_items(urgent_items, limit=MAX_URGENT_ITEMS)
    if urgent_lines:
        lines.append("")
        lines.append("Urgent:")
        lines.extend(urgent_lines)

    follow_up_lines = _format_ranked_items(payload.get("follow_ups"), limit=MAX_FOLLOW_UPS)
    if follow_up_lines:
        lines.append("")
        lines.append("Follow-ups:")
        for index, line in enumerate(follow_up_lines):
            if index:
                lines.append("")
            lines.append(line)

    lines.extend(["", f"Report: {report_path}"])
    return "\n".join(lines)


def _resolve_chat_id(args: argparse.Namespace) -> str:
    if str(args.chat_id).strip():
        return str(args.chat_id).strip()
    env_var = str(args.chat_id_env_var).strip()
    if env_var:
        chat_id = local_env.first_env_value(env_var)
        if chat_id:
            return chat_id
    raise MorningTriageNotifierError("chat id is required via --chat-id or configured environment variable")


def _resolve_token(args: argparse.Namespace) -> str:
    if str(args.token).strip():
        return str(args.token).strip()
    token = local_env.first_env_value(*TOKEN_ENV_CANDIDATES)
    if token:
        return token
    raise MorningTriageNotifierError(
        "Telegram bot token is required via --token or environment "
        f"({', '.join(TOKEN_ENV_CANDIDATES)})"
    )


def run(
    args: argparse.Namespace,
    *,
    api_client_factory=TelegramApiClient,
) -> dict[str, Any]:
    staging_root = Path(args.staging_root).resolve()
    if not staging_root.exists():
        raise MorningTriageNotifierError(f"staging root not found: {staging_root}")

    sidecar_path = resolve_sidecar_path(staging_root, str(args.sidecar_path))
    payload = load_sidecar(sidecar_path)
    chat_id = _resolve_chat_id(args)
    token = _resolve_token(args)
    message = format_message(payload, sidecar_path=sidecar_path)
    client = api_client_factory(token)
    try:
        client.send_message(chat_id=chat_id, text=message)
    except TelegramApiError as exc:
        raise MorningTriageNotifierError(f"failed to send Telegram notification: {exc}") from exc
    return {
        "status": "sent",
        "chat_id": chat_id,
        "run_id": str(payload.get("run_id") or sidecar_path.stem),
        "sidecar_path": sidecar_path.as_posix(),
        "report_path": resolve_report_path(payload, sidecar_path),
        "message_text": message,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a morning-triage sidecar summary to Telegram.")
    parser.add_argument("--staging-root", default=str(morning_triage_digest.DEFAULT_STAGING_ROOT))
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
    except MorningTriageNotifierError as exc:
        status = "failed"
        if args.allow_send_failure and "failed to send Telegram notification:" in str(exc):
            status = "send_failed_nonfatal"
        print(
            json.dumps(
                {
                    "status": status,
                    "chat_id": str(args.chat_id).strip(),
                    "run_id": "",
                    "sidecar_path": str(args.sidecar_path).strip(),
                    "report_path": "",
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
