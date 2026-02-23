from __future__ import annotations

import argparse
import json

from .api import (
    HeartbeatUnavailableError,
    disable_heartbeat,
    enable_heartbeat,
    get_last_event,
    get_status,
    run_once,
    wake,
)
from .config import HeartbeatConfig

_OPERATOR_CONTRACT = "heartbeat.operator"
_OPERATOR_CONTRACT_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heartbeat_system")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run_once = sub.add_parser("run-once", help="Execute one heartbeat run")
    p_run_once.add_argument("--reason", default="manual")
    p_run_once.add_argument("--heartbeat-file", default="HEARTBEAT.md")
    p_run_once.add_argument("--ack-token", default="HEARTBEAT_OK")
    p_run_once.add_argument("--ack-max-chars", type=int, default=300)
    p_run_once.add_argument("--include-reasoning", action="store_true")
    p_run_once.add_argument(
        "--disabled",
        action="store_true",
        help="Run with heartbeat disabled in config (for boundary testing)",
    )

    sub.add_parser("status", help="Show heartbeat runtime status and counters")
    sub.add_parser("last-event", help="Show most recent heartbeat event")

    p_wake = sub.add_parser("wake", help="Request scheduler wake-up")
    p_wake.add_argument("--reason", default="manual")

    sub.add_parser("enable", help="Enable heartbeat execution")
    sub.add_parser("disable", help="Disable heartbeat execution")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run-once":
        config = HeartbeatConfig(
            enabled=not args.disabled,
            heartbeat_file=args.heartbeat_file,
            ack_token=args.ack_token,
            ack_max_chars=args.ack_max_chars,
            include_reasoning=args.include_reasoning,
        )
        try:
            payload = run_once(config=config, reason=args.reason)
            _emit_payload(payload)
            return 0
        except HeartbeatUnavailableError as exc:
            _emit_payload(
                {
                    "status": "failed",
                    "reason": "runner-unavailable",
                    "run_reason": args.reason,
                    "output_text": "",
                    "error": str(exc),
                    "error_code": "runner-unavailable",
                    "error_reason": "runner-unavailable",
                }
            )
            return 2
        except ValueError as exc:
            _emit_payload(
                {
                    "status": "failed",
                    "reason": "invalid-config",
                    "run_reason": args.reason,
                    "output_text": "",
                    "error": str(exc),
                    "error_code": "invalid-config",
                    "error_reason": "invalid-config",
                }
            )
            return 2

    if args.command == "status":
        _emit_payload(get_status())
        return 0

    if args.command == "last-event":
        _emit_payload(get_last_event())
        return 0

    if args.command == "wake":
        _emit_payload(wake(reason=args.reason))
        return 0

    if args.command == "enable":
        _emit_payload(enable_heartbeat())
        return 0

    if args.command == "disable":
        _emit_payload(disable_heartbeat())
        return 0

    parser.error("unknown command")
    return 2


def _emit_payload(payload: dict[str, object]) -> None:
    normalized = dict(payload)
    normalized["contract"] = _OPERATOR_CONTRACT
    normalized["contract_version"] = _OPERATOR_CONTRACT_VERSION
    normalized["contract_metadata"] = {
        "name": _OPERATOR_CONTRACT,
        "version": _OPERATOR_CONTRACT_VERSION,
    }
    normalized.setdefault("error_code", None)
    normalized.setdefault("error_reason", None)
    normalized.setdefault("ok", _infer_ok(normalized))
    print(json.dumps(normalized, sort_keys=True))


def _infer_ok(payload: dict[str, object]) -> bool:
    error_code = payload.get("error_code")
    if error_code not in (None, ""):
        return False
    status = str(payload.get("status", "")).strip().lower()
    if status in {"failed", "error", "not-running", "degraded"}:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
