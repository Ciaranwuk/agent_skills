from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .contracts import ContextStorePort, ContextTurn
from .errors import ContextStoreError
from .types import ContextSessionMetadata, ContextStoreDiagnostic, DiagnosticCode


_SCHEMA_VERSION = 1
_ALLOWED_TYPES = frozenset({"user", "assistant", "system", "compaction"})


class ContextStore(ContextStorePort):
    """Durable transcript persistence with strict/non-strict corruption handling."""

    def __init__(
        self,
        *,
        root_dir: str | Path = ".channel_runtime/context",
        strict_io: bool = False,
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        self._root_dir = Path(root_dir)
        self._strict_io = bool(strict_io)
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self._diagnostics: list[ContextStoreDiagnostic] = []

    def load_transcript(self, *, session_id: str) -> tuple[ContextTurn, ...]:
        normalized_session = _normalize_session_id(session_id)
        transcript_path = self._transcript_path(normalized_session)
        if not transcript_path.exists():
            return ()

        turns: list[ContextTurn] = []
        try:
            with transcript_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        self._handle_load_issue(
                            session_id=normalized_session,
                            line_number=line_number,
                            code="context-store-invalid-record",
                            message="empty transcript line",
                        )
                        continue

                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError as exc:
                        self._handle_load_issue(
                            session_id=normalized_session,
                            line_number=line_number,
                            code="context-store-malformed-line",
                            message=f"malformed json: {exc.msg}",
                        )
                        continue

                    try:
                        turns.append(_record_to_turn(payload=payload, session_id=normalized_session))
                    except ValueError as exc:
                        self._handle_load_issue(
                            session_id=normalized_session,
                            line_number=line_number,
                            code=_diagnostic_code_for_error(exc),
                            message=str(exc),
                        )
                        continue
        except OSError as exc:
            raise ContextStoreError(
                f"failed to load transcript for session_id={normalized_session}: {exc}",
                code="context-store-load-error",
                operation="load_transcript",
                retryable=True,
            ) from exc

        return tuple(turns)

    def append_turn(self, *, session_id: str, turn: ContextTurn) -> None:
        normalized_session = _normalize_session_id(session_id)
        prepared_turn = _normalize_turn(turn)

        try:
            self._ensure_dirs()
            metadata_map = self._load_metadata_map()
            session_metadata = _metadata_from_map(metadata_map.get(normalized_session))
            now_iso = _datetime_to_rfc3339(self._now_utc())
            next_index = (session_metadata.turn_count if session_metadata is not None else 0) + 1
            entry_id = prepared_turn.turn_id or f"{next_index:08d}"
            timestamp_iso = _timestamp_for_turn(prepared_turn=prepared_turn, now_utc=self._now_utc)
            transcript_relpath = self._transcript_relpath(normalized_session)

            record = _turn_to_record(
                turn=prepared_turn,
                session_id=normalized_session,
                timestamp_iso=timestamp_iso,
                entry_id=entry_id,
            )
            transcript_path = self._root_dir / transcript_relpath
            with transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
                if self._strict_io:
                    handle.flush()
                    os.fsync(handle.fileno())

            created_at = session_metadata.created_at if session_metadata is not None else now_iso
            chat_id = session_metadata.chat_id if session_metadata is not None else _chat_id_for_session(normalized_session)
            updated_metadata = ContextSessionMetadata(
                schema_version=_SCHEMA_VERSION,
                session_id=normalized_session,
                transcript_path=transcript_relpath,
                created_at=created_at,
                updated_at=now_iso,
                turn_count=next_index,
                last_entry_id=entry_id,
                chat_id=chat_id,
            )
            metadata_map[normalized_session] = _metadata_to_map(updated_metadata)
            self._write_metadata_map(metadata_map)
        except ContextStoreError:
            raise
        except OSError as exc:
            raise ContextStoreError(
                f"failed to append transcript turn for session_id={normalized_session}: {exc}",
                code="context-store-save-error",
                operation="append_turn",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise ContextStoreError(
                f"failed to append transcript turn for session_id={normalized_session}: {exc}",
                code="context-store-save-error",
                operation="append_turn",
                retryable=False,
            ) from exc

    def replace_transcript(self, *, session_id: str, turns: tuple[ContextTurn, ...]) -> None:
        normalized_session = _normalize_session_id(session_id)
        try:
            self._ensure_dirs()
            transcript_relpath = self._transcript_relpath(normalized_session)
            transcript_path = self._root_dir / transcript_relpath
            now_iso = _datetime_to_rfc3339(self._now_utc())

            with transcript_path.open("w", encoding="utf-8") as handle:
                for index, raw_turn in enumerate(turns, start=1):
                    turn = _normalize_turn(raw_turn)
                    entry_id = turn.turn_id or f"{index:08d}"
                    timestamp_iso = _timestamp_for_turn(prepared_turn=turn, now_utc=self._now_utc)
                    record = _turn_to_record(
                        turn=turn,
                        session_id=normalized_session,
                        timestamp_iso=timestamp_iso,
                        entry_id=entry_id,
                    )
                    handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
                if self._strict_io:
                    handle.flush()
                    os.fsync(handle.fileno())

            metadata_map = self._load_metadata_map()
            existing = _metadata_from_map(metadata_map.get(normalized_session))
            created_at = existing.created_at if existing is not None else now_iso
            chat_id = existing.chat_id if existing is not None else _chat_id_for_session(normalized_session)
            last_entry_id = ""
            if turns:
                last_entry_id = turns[-1].turn_id or f"{len(turns):08d}"
            metadata_map[normalized_session] = _metadata_to_map(
                ContextSessionMetadata(
                    schema_version=_SCHEMA_VERSION,
                    session_id=normalized_session,
                    transcript_path=transcript_relpath,
                    created_at=created_at,
                    updated_at=now_iso,
                    turn_count=len(turns),
                    last_entry_id=last_entry_id,
                    chat_id=chat_id,
                )
            )
            self._write_metadata_map(metadata_map)
        except ContextStoreError:
            raise
        except OSError as exc:
            raise ContextStoreError(
                f"failed to replace transcript for session_id={normalized_session}: {exc}",
                code="context-store-save-error",
                operation="replace_transcript",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise ContextStoreError(
                f"failed to replace transcript for session_id={normalized_session}: {exc}",
                code="context-store-save-error",
                operation="replace_transcript",
                retryable=False,
            ) from exc

    def load_session_metadata(self, *, session_id: str) -> ContextSessionMetadata | None:
        normalized_session = _normalize_session_id(session_id)
        try:
            metadata_map = self._load_metadata_map()
        except OSError as exc:
            raise ContextStoreError(
                f"failed to load metadata for session_id={normalized_session}: {exc}",
                code="context-store-load-error",
                operation="load_metadata",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise ContextStoreError(
                f"failed to load metadata for session_id={normalized_session}: {exc}",
                code="context-store-load-error",
                operation="load_metadata",
                retryable=False,
            ) from exc
        return _metadata_from_map(metadata_map.get(normalized_session))

    def diagnostics(self) -> tuple[ContextStoreDiagnostic, ...]:
        return tuple(self._diagnostics)

    def clear_diagnostics(self) -> None:
        self._diagnostics.clear()

    @property
    def strict_io(self) -> bool:
        return self._strict_io

    def _handle_load_issue(
        self,
        *,
        session_id: str,
        line_number: int,
        code: DiagnosticCode,
        message: str,
    ) -> None:
        if self._strict_io:
            raise ContextStoreError(
                f"malformed transcript for session_id={session_id} at line={line_number}: {message}",
                code="context-store-load-error",
                operation="load_transcript",
                retryable=False,
            )
        self._diagnostics.append(
            ContextStoreDiagnostic(
                code=code,
                session_id=session_id,
                line_number=int(line_number),
                message=str(message),
            )
        )

    def _ensure_dirs(self) -> None:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        (self._root_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    def _metadata_path(self) -> Path:
        return self._root_dir / "sessions.json"

    def _transcript_relpath(self, session_id: str) -> str:
        return f"transcripts/{_session_key_to_filename(session_id)}.jsonl"

    def _transcript_path(self, session_id: str) -> Path:
        return self._root_dir / self._transcript_relpath(session_id)

    def _load_metadata_map(self) -> dict[str, dict[str, Any]]:
        metadata_path = self._metadata_path()
        if not metadata_path.exists():
            return {}
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("metadata file must contain an object")
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, dict):
                normalized[key] = value
        return normalized

    def _write_metadata_map(self, metadata_map: dict[str, dict[str, Any]]) -> None:
        self._ensure_dirs()
        metadata_path = self._metadata_path()
        tmp_path = metadata_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata_map, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            if self._strict_io:
                handle.flush()
                os.fsync(handle.fileno())
        tmp_path.replace(metadata_path)


def _normalize_session_id(session_id: str) -> str:
    normalized = str(session_id).strip()
    if not normalized:
        raise ValueError("session_id must be a non-empty string")
    return normalized


def _normalize_turn(turn: ContextTurn) -> ContextTurn:
    role = str(turn.role or "").strip().lower()
    if role not in _ALLOWED_TYPES:
        raise ValueError(f"unsupported turn role: {role}")
    text = str(turn.text)
    if role in {"user", "assistant", "system"} and not text:
        raise ValueError("turn text must be non-empty for user/assistant/system roles")
    metadata = {str(k): str(v) for k, v in dict(turn.metadata or {}).items()}
    created_at = None if turn.created_at_s is None else float(turn.created_at_s)
    return ContextTurn(role=role, text=text, turn_id=str(turn.turn_id), created_at_s=created_at, metadata=metadata)


def _session_key_to_filename(session_id: str) -> str:
    return quote(session_id, safe="")


def _turn_to_record(
    *,
    turn: ContextTurn,
    session_id: str,
    timestamp_iso: str,
    entry_id: str,
) -> dict[str, Any]:
    if not str(entry_id).strip():
        raise ValueError("entry_id must be non-empty")
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "entry_id": str(entry_id),
        "session_id": session_id,
        "timestamp": timestamp_iso,
        "type": turn.role,
    }
    if turn.role == "compaction":
        payload["compaction_summary"] = str(turn.text)
    else:
        payload["text"] = str(turn.text)
    if turn.metadata:
        payload["meta"] = {str(k): str(v) for k, v in dict(turn.metadata).items()}
    return payload


def _record_to_turn(*, payload: Any, session_id: str) -> ContextTurn:
    if not isinstance(payload, dict):
        raise ValueError("record must be an object")

    schema_version = payload.get("schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {schema_version}")

    record_session_id = str(payload.get("session_id", "")).strip()
    if record_session_id != session_id:
        raise ValueError(f"record session_id mismatch: {record_session_id}")

    entry_type = str(payload.get("type", "")).strip().lower()
    if entry_type not in _ALLOWED_TYPES:
        raise ValueError(f"unsupported type: {entry_type}")

    entry_id = str(payload.get("entry_id", "")).strip()
    if not entry_id:
        raise ValueError("record entry_id must be non-empty")

    timestamp = str(payload.get("timestamp", "")).strip()
    created_at_s = _parse_rfc3339_to_epoch(timestamp)

    if entry_type == "compaction":
        text = str(payload.get("compaction_summary", "")).strip()
    else:
        text = str(payload.get("text", "")).strip()
    if not text:
        raise ValueError("record text must be non-empty")

    meta_raw = payload.get("meta", {})
    meta_map: dict[str, str] = {}
    if isinstance(meta_raw, dict):
        meta_map = {str(k): str(v) for k, v in meta_raw.items()}
    if entry_type == "compaction":
        meta_map.setdefault("source_type", "compaction")
        role = "system"
    else:
        role = entry_type

    return ContextTurn(
        role=role,
        text=text,
        turn_id=entry_id,
        created_at_s=created_at_s,
        metadata=meta_map,
    )


def _metadata_to_map(metadata: ContextSessionMetadata) -> dict[str, Any]:
    return {
        "schema_version": metadata.schema_version,
        "session_id": metadata.session_id,
        "transcript_path": metadata.transcript_path,
        "created_at": metadata.created_at,
        "updated_at": metadata.updated_at,
        "turn_count": metadata.turn_count,
        "last_entry_id": metadata.last_entry_id,
        "chat_id": metadata.chat_id,
    }


def _metadata_from_map(raw: Any) -> ContextSessionMetadata | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("metadata entry must be an object")
    schema_version = int(raw.get("schema_version", _SCHEMA_VERSION))
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(f"unsupported metadata schema_version: {schema_version}")

    session_id = _normalize_session_id(str(raw.get("session_id", "")))
    transcript_path = str(raw.get("transcript_path", "")).strip()
    if not transcript_path:
        raise ValueError("metadata transcript_path must be non-empty")

    created_at = str(raw.get("created_at", "")).strip()
    updated_at = str(raw.get("updated_at", "")).strip()
    _parse_rfc3339_to_epoch(created_at)
    _parse_rfc3339_to_epoch(updated_at)

    turn_count = int(raw.get("turn_count", 0))
    if turn_count < 0:
        raise ValueError("metadata turn_count must be >= 0")

    return ContextSessionMetadata(
        schema_version=schema_version,
        session_id=session_id,
        transcript_path=transcript_path,
        created_at=created_at,
        updated_at=updated_at,
        turn_count=turn_count,
        last_entry_id=str(raw.get("last_entry_id", "")).strip(),
        chat_id=str(raw.get("chat_id", "")).strip(),
    )


def _timestamp_for_turn(*, prepared_turn: ContextTurn, now_utc: Callable[[], datetime]) -> str:
    if prepared_turn.created_at_s is not None:
        dt = datetime.fromtimestamp(float(prepared_turn.created_at_s), tz=timezone.utc)
    else:
        dt = now_utc()
    return _datetime_to_rfc3339(dt)


def _datetime_to_rfc3339(dt: datetime) -> str:
    value = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def _parse_rfc3339_to_epoch(value: str) -> float:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("record timestamp must be non-empty")
    iso_value = normalized
    if normalized.endswith("Z"):
        iso_value = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {normalized}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {normalized}")
    return dt.timestamp()


def _chat_id_for_session(session_id: str) -> str:
    if session_id.startswith("telegram:"):
        return session_id.split(":", 1)[1]
    return ""


def _diagnostic_code_for_error(exc: Exception) -> DiagnosticCode:
    text = str(exc)
    if "session_id mismatch" in text:
        return "context-store-session-mismatch"
    return "context-store-invalid-record"


__all__ = ["ContextStore", "ContextSessionMetadata", "ContextStoreDiagnostic"]
