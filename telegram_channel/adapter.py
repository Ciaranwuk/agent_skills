from __future__ import annotations

from typing import Any

from channel_core.contracts import ChannelAdapterPort, ChannelRuntimeError, InboundMessage, OutboundMessage

from .api import TelegramApiClient, TelegramApiError
from .cursor_state import CursorStateError, DurableCursorStateStore
from .update_parser import parse_update


class TelegramChannelAdapter(ChannelAdapterPort):
    """Telegram adapter that tracks fetch/ack state and in-process dedupe."""

    def __init__(
        self,
        api_client: TelegramApiClient,
        *,
        cursor_state_store: DurableCursorStateStore | None = None,
        strict_state_io: bool = False,
    ) -> None:
        self._api = api_client
        self._cursor_state_store = cursor_state_store
        self._strict_state_io = bool(strict_state_io)
        self._diagnostics: list[dict[str, str]] = []
        self._seen_update_ids: set[int] = set()
        self._pending_ack_ids: set[int] = set()
        self._processed_ids: set[int] = set()
        self._committed_floor: int | None = self._load_committed_floor()
        self._next_offset: int | None = self._committed_floor

    def fetch_updates(self) -> list[InboundMessage]:
        try:
            raw_updates = self._api.get_updates(offset=self._next_offset)
        except TelegramApiError as exc:
            raise ChannelRuntimeError(f"fetch_updates failed: {exc.to_dict()}") from exc

        normalized: list[InboundMessage] = []
        seen_in_batch: set[int] = set()
        for raw_update in raw_updates:
            parsed = parse_update(raw_update)
            if parsed.update_id is None:
                continue

            update_id = _to_int_update_id(parsed.update_id)
            if update_id is None:
                continue

            if update_id in seen_in_batch:
                self._processed_ids.add(update_id)
                continue
            seen_in_batch.add(update_id)

            if self._committed_floor is not None and update_id < self._committed_floor:
                self._processed_ids.add(update_id)
                self._record_diagnostic(
                    code="stale-drop",
                    update_id=update_id,
                    message=f"dropped stale update {update_id} below committed floor {self._committed_floor}",
                )
                continue

            if update_id in self._seen_update_ids and update_id not in self._pending_ack_ids:
                self._processed_ids.add(update_id)
                continue

            self._seen_update_ids.add(update_id)
            if parsed.inbound is None:
                self._processed_ids.add(update_id)
                continue

            self._pending_ack_ids.add(update_id)
            normalized.append(parsed.inbound)

        self._recompute_offset()
        return normalized

    def send_message(self, outbound: OutboundMessage) -> None:
        try:
            self._api.send_message(
                chat_id=outbound.chat_id,
                text=outbound.text,
                reply_to_message_id=outbound.reply_to_message_id,
            )
        except TelegramApiError as exc:
            raise ChannelRuntimeError(f"send_message failed: {exc.to_dict()}") from exc

    def ack_update(self, update_id: str) -> None:
        numeric_id = _to_int_update_id(update_id)
        if numeric_id is None:
            raise ChannelRuntimeError("ack_update requires a numeric update_id")

        self._seen_update_ids.add(numeric_id)
        self._pending_ack_ids.discard(numeric_id)
        self._processed_ids.add(numeric_id)
        self._recompute_offset()

    def drain_diagnostics(self) -> list[dict[str, str]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics

    def _recompute_offset(self) -> None:
        if self._pending_ack_ids:
            candidate_floor = min(self._pending_ack_ids)
        elif self._seen_update_ids:
            candidate_floor = max(self._seen_update_ids) + 1
        elif self._committed_floor is not None:
            candidate_floor = self._committed_floor
        else:
            candidate_floor = None

        if candidate_floor is not None and self._committed_floor is not None:
            candidate_floor = max(candidate_floor, self._committed_floor)

        self._next_offset = candidate_floor

        if candidate_floor is not None and (self._committed_floor is None or candidate_floor > self._committed_floor):
            self._committed_floor = candidate_floor
            self._persist_committed_floor(candidate_floor)

    def _load_committed_floor(self) -> int | None:
        if self._cursor_state_store is None:
            return None
        try:
            snapshot = self._cursor_state_store.load()
            return snapshot.committed_floor
        except CursorStateError as exc:
            self._handle_state_error(operation="load", exc=exc)
            return None

    def _persist_committed_floor(self, floor: int) -> None:
        if self._cursor_state_store is None:
            return
        try:
            self._cursor_state_store.save(committed_floor=floor)
        except CursorStateError as exc:
            self._handle_state_error(operation="save", exc=exc)

    def _handle_state_error(self, *, operation: str, exc: CursorStateError) -> None:
        message = f"cursor_state_{operation} failed: {exc}"
        self._record_diagnostic(
            code=f"cursor-state-{operation}-error",
            message=message,
        )
        if self._strict_state_io:
            raise ChannelRuntimeError(message) from exc

    def _record_diagnostic(
        self,
        *,
        code: str,
        message: str,
        update_id: int | None = None,
    ) -> None:
        payload = {"code": str(code), "message": str(message)}
        if update_id is not None:
            payload["update_id"] = str(update_id)
        self._diagnostics.append(payload)


def _to_int_update_id(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
