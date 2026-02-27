from __future__ import annotations

from typing import Any

from channel_core.contracts import ChannelAdapterPort, ChannelRuntimeError, InboundMessage, OutboundMessage

from .api import TelegramApiClient, TelegramApiError
from .update_parser import parse_update


class TelegramChannelAdapter(ChannelAdapterPort):
    """Telegram adapter that tracks fetch/ack state and in-process dedupe."""

    def __init__(self, api_client: TelegramApiClient) -> None:
        self._api = api_client
        self._seen_update_ids: set[int] = set()
        self._pending_ack_ids: set[int] = set()
        self._processed_ids: set[int] = set()
        self._next_offset: int | None = None

    def fetch_updates(self) -> list[InboundMessage]:
        try:
            raw_updates = self._api.get_updates(offset=self._next_offset)
        except TelegramApiError as exc:
            raise ChannelRuntimeError(f"fetch_updates failed: {exc.to_dict()}") from exc

        normalized: list[InboundMessage] = []
        for raw_update in raw_updates:
            parsed = parse_update(raw_update)
            if parsed.update_id is None:
                continue

            update_id = _to_int_update_id(parsed.update_id)
            if update_id is None:
                continue

            if update_id in self._seen_update_ids:
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

    def _recompute_offset(self) -> None:
        if self._pending_ack_ids:
            self._next_offset = min(self._pending_ack_ids)
            return
        if self._seen_update_ids:
            self._next_offset = max(self._seen_update_ids) + 1
            return
        self._next_offset = None


def _to_int_update_id(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
