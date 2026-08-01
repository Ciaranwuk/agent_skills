from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from channel_core.contracts import InboundMessage

from .api import TelegramApiClient, TelegramApiError

_VOICE_NOTE_PREFIX = "[Voice note transcript]"


@dataclass(frozen=True)
class VoiceNoteConfig:
    enabled: bool = True
    whisper_command: str = "whisper"
    whisper_model: str = "turbo"
    language: str | None = None
    transcribe_timeout_s: float = 120.0
    max_chars: int = 4000
    temp_dir: str = ".channel_runtime/voice_notes"

    def __post_init__(self) -> None:
        if float(self.transcribe_timeout_s) <= 0:
            raise ValueError("transcribe_timeout_s must be > 0")
        if int(self.max_chars) < 1:
            raise ValueError("max_chars must be >= 1")


@dataclass(frozen=True)
class VoiceNoteProcessingError(RuntimeError):
    code: str
    detail: str
    user_message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class TelegramVoiceNoteProcessor:
    """Download and transcribe Telegram voice notes into bounded inbound text."""

    def __init__(
        self,
        api_client: TelegramApiClient,
        *,
        config: VoiceNoteConfig | None = None,
    ) -> None:
        self._api = api_client
        self._config = config or VoiceNoteConfig()

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    def supports(self, inbound: InboundMessage) -> bool:
        metadata = inbound.metadata if isinstance(inbound.metadata, Mapping) else {}
        return metadata.get("content_type") == "voice"

    def transcribe(self, inbound: InboundMessage) -> InboundMessage:
        descriptor = _coerce_voice_descriptor(inbound.metadata)
        if descriptor is None:
            raise VoiceNoteProcessingError(
                code="voice-note-metadata-missing",
                detail="telegram voice metadata is missing or incomplete",
                user_message="I received your voice note, but its Telegram file details were incomplete. Please try again.",
            )

        whisper_binary = shutil.which(self._config.whisper_command)
        if whisper_binary is None:
            raise VoiceNoteProcessingError(
                code="voice-note-transcriber-missing",
                detail=f"whisper CLI not found on PATH ({self._config.whisper_command})",
                user_message=(
                    "I received your voice note, but local transcription is not installed on this host yet. "
                    "Install the `whisper` CLI and try again."
                ),
            )

        try:
            file_info = self._api.get_file(file_id=descriptor.file_id)
        except TelegramApiError as exc:
            raise VoiceNoteProcessingError(
                code="voice-note-file-lookup-failed",
                detail=str(exc),
                user_message=(
                    "I received your voice note, but I could not fetch its Telegram file details right now. "
                    "Please try again."
                ),
            ) from exc

        file_path = str(file_info.get("file_path", "")).strip()
        if not file_path:
            raise VoiceNoteProcessingError(
                code="voice-note-file-path-missing",
                detail="Telegram getFile response did not include file_path",
                user_message="I received your voice note, but Telegram did not return a downloadable file path. Please try again.",
            )

        try:
            audio_bytes = self._api.download_file(file_path=file_path)
        except TelegramApiError as exc:
            raise VoiceNoteProcessingError(
                code="voice-note-download-failed",
                detail=str(exc),
                user_message="I received your voice note, but I could not download it from Telegram right now. Please try again.",
            ) from exc

        transcript_text = self._run_whisper(whisper_binary=whisper_binary, audio_bytes=audio_bytes, file_path=file_path)
        bounded_text = _bound_transcript(transcript_text, max_chars=self._config.max_chars)
        metadata = dict(inbound.metadata)
        metadata["voice_note"] = {
            "file_id": descriptor.file_id,
            "file_unique_id": descriptor.file_unique_id,
            "duration_s": descriptor.duration_s,
            "mime_type": descriptor.mime_type,
            "transcript_source": "whisper-cli",
            "whisper_command": self._config.whisper_command,
            "whisper_model": self._config.whisper_model,
            "language": self._config.language,
            "truncated": bounded_text != transcript_text,
        }
        return InboundMessage(
            update_id=inbound.update_id,
            chat_id=inbound.chat_id,
            user_id=inbound.user_id,
            text=f"{_VOICE_NOTE_PREFIX}\n{bounded_text}",
            message_id=inbound.message_id,
            timestamp_s=inbound.timestamp_s,
            metadata=metadata,
        )

    def _run_whisper(self, *, whisper_binary: str, audio_bytes: bytes, file_path: str) -> str:
        suffix = Path(file_path).suffix or ".ogg"
        base_dir = Path(self._config.temp_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base_dir) as tmp_dir:
            temp_root = Path(tmp_dir)
            audio_path = temp_root / f"voice-note{suffix}"
            audio_path.write_bytes(audio_bytes)
            command = [
                whisper_binary,
                str(audio_path),
                "--model",
                self._config.whisper_model,
                "--output_format",
                "txt",
                "--output_dir",
                str(temp_root),
                "--verbose",
                "False",
            ]
            if self._config.language:
                command.extend(["--language", self._config.language])

            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=float(self._config.transcribe_timeout_s),
                )
            except FileNotFoundError as exc:
                raise VoiceNoteProcessingError(
                    code="voice-note-transcriber-missing",
                    detail=f"whisper CLI not found ({self._config.whisper_command})",
                    user_message=(
                        "I received your voice note, but local transcription is not installed on this host yet. "
                        "Install the `whisper` CLI and try again."
                    ),
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise VoiceNoteProcessingError(
                    code="voice-note-transcription-timeout",
                    detail=f"whisper CLI timed out after {self._config.transcribe_timeout_s}s",
                    user_message=(
                        "I received your voice note, but local transcription timed out on this host. "
                        "Please try a shorter note or send text."
                    ),
                ) from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                detail = stderr or (exc.stdout or "").strip() or f"whisper exited with code {exc.returncode}"
                raise VoiceNoteProcessingError(
                    code="voice-note-transcription-failed",
                    detail=detail,
                    user_message=(
                        "I received your voice note, but local transcription failed on this host. "
                        "Please try again or send text."
                    ),
                ) from exc

            transcript_path = temp_root / f"{audio_path.stem}.txt"
            if not transcript_path.exists():
                raise VoiceNoteProcessingError(
                    code="voice-note-transcript-missing",
                    detail=f"whisper did not produce expected transcript file {transcript_path.name}",
                    user_message=(
                        "I received your voice note, but local transcription did not produce a readable transcript. "
                        "Please try again or send text."
                    ),
                )

            transcript_text = transcript_path.read_text(encoding="utf-8").strip()
            if not transcript_text:
                raise VoiceNoteProcessingError(
                    code="voice-note-transcript-empty",
                    detail="whisper produced an empty transcript",
                    user_message=(
                        "I received your voice note, but the transcript came back empty. "
                        "Please try again or send text."
                    ),
                )
            return transcript_text


@dataclass(frozen=True)
class _VoiceDescriptor:
    file_id: str
    file_unique_id: str | None
    duration_s: int | None
    mime_type: str | None


def _coerce_voice_descriptor(metadata: Mapping[str, Any]) -> _VoiceDescriptor | None:
    voice = metadata.get("telegram_voice")
    if not isinstance(voice, Mapping):
        return None
    file_id = str(voice.get("file_id", "")).strip()
    if not file_id:
        return None
    file_unique_id = str(voice.get("file_unique_id", "")).strip() or None
    duration_s = _coerce_optional_int(voice.get("duration_s"))
    mime_type = str(voice.get("mime_type", "")).strip() or None
    return _VoiceDescriptor(
        file_id=file_id,
        file_unique_id=file_unique_id,
        duration_s=duration_s,
        mime_type=mime_type,
    )


def _coerce_optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _bound_transcript(text: str, *, max_chars: int) -> str:
    normalized = " ".join(str(text).split()).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 14].rstrip() + " [truncated]"
