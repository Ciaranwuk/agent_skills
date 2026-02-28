from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CursorStateError(Exception):
    """Deterministic cursor state store error."""

    def __init__(self, *, kind: str, detail: str) -> None:
        self.kind = str(kind).strip() or "state-error"
        self.detail = str(detail).strip() or "unknown"
        super().__init__(f"{self.kind}: {self.detail}")


@dataclass(frozen=True)
class CursorStateSnapshot:
    committed_floor: int | None


class DurableCursorStateStore:
    """Minimal durable JSON state store for Telegram cursor floor."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> CursorStateSnapshot:
        if not self._path.exists():
            return CursorStateSnapshot(committed_floor=None)

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CursorStateError(kind="state-load-io", detail=str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise CursorStateError(kind="state-load-json", detail=str(exc)) from exc

        if not isinstance(payload, dict):
            raise CursorStateError(kind="state-load-shape", detail="state root must be an object")

        raw_floor = payload.get("committed_floor")
        if raw_floor is None:
            return CursorStateSnapshot(committed_floor=None)

        try:
            floor = int(str(raw_floor).strip())
        except (TypeError, ValueError) as exc:
            raise CursorStateError(kind="state-load-floor", detail="committed_floor must be an integer") from exc

        if floor < 0:
            raise CursorStateError(kind="state-load-floor", detail="committed_floor must be >= 0")

        return CursorStateSnapshot(committed_floor=floor)

    def save(self, *, committed_floor: int) -> None:
        try:
            floor = int(committed_floor)
        except (TypeError, ValueError) as exc:
            raise CursorStateError(kind="state-save-floor", detail="committed_floor must be an integer") from exc

        if floor < 0:
            raise CursorStateError(kind="state-save-floor", detail="committed_floor must be >= 0")

        payload: dict[str, Any] = {
            "version": 1,
            "committed_floor": floor,
        }

        parent = self._path.parent
        temp_path = self._path.with_name(f"{self._path.name}.tmp")

        try:
            parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temp_path.replace(self._path)
        except OSError as exc:
            raise CursorStateError(kind="state-save-io", detail=str(exc)) from exc
