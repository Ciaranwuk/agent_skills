from __future__ import annotations

from typing import Any

from channel_core.contracts import ChannelAdapterPort, ChannelRuntimeError, InboundMessage, OutboundMessage

from .api import TelegramApiClient, TelegramApiError
from .cursor_state import CursorStateError, DurableCursorStateStore
from .tts import TelegramTextToSpeechProcessor, TextToSpeechSynthesisError
from .update_parser import parse_update
from .voice_notes import TelegramVoiceNoteProcessor, VoiceNoteProcessingError


class TelegramChannelAdapter(ChannelAdapterPort):
    """Telegram adapter that tracks fetch/ack state and in-process dedupe."""

    def __init__(
        self,
        api_client: TelegramApiClient,
        *,
        cursor_state_store: DurableCursorStateStore | None = None,
        strict_state_io: bool = False,
        voice_note_processor: TelegramVoiceNoteProcessor | None = None,
        text_to_speech_processor: TelegramTextToSpeechProcessor | None = None,
    ) -> None:
        self._api = api_client
        self._cursor_state_store = cursor_state_store
        self._strict_state_io = bool(strict_state_io)
        self._voice_note_processor = voice_note_processor
        self._text_to_speech_processor = text_to_speech_processor
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

            if parsed.inbound is None:
                self._seen_update_ids.add(update_id)
                self._processed_ids.add(update_id)
                continue

            inbound = parsed.inbound
            if self._is_voice_note(inbound):
                resolved = self._resolve_voice_note(update_id=update_id, inbound=inbound)
                if resolved is None:
                    self._seen_update_ids.add(update_id)
                    self._processed_ids.add(update_id)
                    continue
                inbound = resolved

            self._seen_update_ids.add(update_id)
            self._pending_ack_ids.add(update_id)
            normalized.append(inbound)

        self._recompute_offset()
        return normalized

    def send_message(self, outbound: OutboundMessage) -> None:
        if self._should_send_voice(outbound):
            self._send_text_to_speech(outbound)
            return
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

    def _should_send_voice(self, outbound: OutboundMessage) -> bool:
        metadata = outbound.metadata if isinstance(outbound.metadata, dict) else {}
        tts = metadata.get("telegram_tts")
        return bool(isinstance(tts, dict) and tts.get("enabled"))

    def _send_text_to_speech(self, outbound: OutboundMessage) -> None:
        processor = self._text_to_speech_processor
        if processor is None:
            self._send_text_fallback(outbound)
            return
        try:
            synthesized = processor.synthesize(outbound)
        except TextToSpeechSynthesisError as exc:
            self._record_diagnostic(code=exc.code, message=exc.detail)
            self._send_text_fallback(outbound)
            return

        try:
            self._api.send_voice(
                chat_id=outbound.chat_id,
                voice_bytes=synthesized.audio_bytes,
                filename=synthesized.filename,
                reply_to_message_id=outbound.reply_to_message_id,
                caption=synthesized.caption,
            )
        except TelegramApiError as exc:
            if self._should_fallback_from_voice_send(exc):
                self._record_diagnostic(
                    code="tts-voice-send-fallback",
                    message=str(exc),
                )
                self._send_text_fallback(outbound)
                return
            raise ChannelRuntimeError(f"send_voice failed: {exc.to_dict()}") from exc

    def _send_text_fallback(self, outbound: OutboundMessage) -> None:
        fallback_text = self._resolve_tts_fallback_text(outbound)
        try:
            self._api.send_message(
                chat_id=outbound.chat_id,
                text=fallback_text,
                reply_to_message_id=outbound.reply_to_message_id,
            )
        except TelegramApiError as exc:
            raise ChannelRuntimeError(f"send_message failed: {exc.to_dict()}") from exc

    def _resolve_tts_fallback_text(self, outbound: OutboundMessage) -> str:
        metadata = outbound.metadata if isinstance(outbound.metadata, dict) else {}
        tts = metadata.get("telegram_tts")
        if isinstance(tts, dict):
            fallback_text = str(tts.get("fallback_text", "")).strip()
            if fallback_text:
                return fallback_text
        return outbound.text

    def _should_fallback_from_voice_send(self, exc: TelegramApiError) -> bool:
        if exc.retry_class in {"transient", "rate-limit"}:
            return False
        if exc.transient:
            return False
        return True

    def _is_voice_note(self, inbound: InboundMessage) -> bool:
        metadata = inbound.metadata if isinstance(inbound.metadata, dict) else {}
        return metadata.get("content_type") == "voice"

    def _resolve_voice_note(self, *, update_id: int, inbound: InboundMessage) -> InboundMessage | None:
        processor = self._voice_note_processor
        if processor is None or not processor.enabled:
            self._send_voice_note_reply(
                inbound,
                "I received your voice note, but voice-note handling is not enabled on this host yet. Please send text for now.",
            )
            self._record_diagnostic(
                code="voice-note-disabled",
                update_id=update_id,
                message="voice note received but no processor is configured",
            )
            return None

        try:
            enriched = processor.transcribe(inbound)
        except VoiceNoteProcessingError as exc:
            self._send_voice_note_reply(inbound, exc.user_message)
            self._record_diagnostic(code=exc.code, update_id=update_id, message=exc.detail)
            return None
        self._record_diagnostic(
            code="voice-note-transcribed",
            update_id=update_id,
            message="voice note transcribed into inbound text",
        )
        return enriched

    def _send_voice_note_reply(self, inbound: InboundMessage, text: str) -> None:
        try:
            self._api.send_message(
                chat_id=inbound.chat_id,
                text=text,
                reply_to_message_id=inbound.message_id,
            )
        except TelegramApiError as exc:
            self._record_diagnostic(
                code="voice-note-reply-failed",
                update_id=_to_int_update_id(inbound.update_id),
                message=str(exc),
            )

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
