from __future__ import annotations

import json
import sys
from typing import Sequence

from channel_core.contracts import ConfigValidationError

from .config import parse_runtime_config
from .runner import run_loop


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        config = parse_runtime_config(args)
    except ConfigValidationError as exc:
        _emit_payload({"status": "failed", "reason": "invalid-config", "error": str(exc)})
        return 2

    try:
        if config.once:
            result = run_loop(config=config)
            _emit_payload(result)
            return _exit_code_for_result(result)

        run_loop(config=config, on_cycle=_emit_payload)
        return 0
    except KeyboardInterrupt:
        return 130


def _emit_payload(payload: dict[str, object]) -> None:
    print(json.dumps(dict(payload), sort_keys=True))


def _exit_code_for_result(result: dict[str, object]) -> int:
    status = str(result.get("status", "")).strip().lower()
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
