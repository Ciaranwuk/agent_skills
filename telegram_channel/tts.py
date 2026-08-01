from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from channel_core.contracts import OutboundMessage


@dataclass(frozen=True)
class TextToSpeechConfig:
    enabled: bool = False
    command: str = ""
    timeout_s: float = 30.0
    max_chars: int = 2000
    temp_dir: str = ".channel_runtime/tts"
    voice: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        if float(self.timeout_s) <= 0:
            raise ValueError("timeout_s must be > 0")
        if int(self.max_chars) < 1:
            raise ValueError("max_chars must be >= 1")


@dataclass(frozen=True)
class TextToSpeechSynthesisError(RuntimeError):
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class SynthesizedVoice:
    audio_bytes: bytes
    filename: str = "speech.ogg"
    caption: str | None = None


class TelegramTextToSpeechProcessor:
    """Render outbound text into Telegram voice bytes via a local helper CLI."""

    def __init__(self, *, config: TextToSpeechConfig | None = None) -> None:
        self._config = config or TextToSpeechConfig()

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    def supports(self, outbound: OutboundMessage) -> bool:
        metadata = outbound.metadata if isinstance(outbound.metadata, Mapping) else {}
        tts = metadata.get("telegram_tts")
        return bool(isinstance(tts, Mapping) and tts.get("enabled"))

    def synthesize(self, outbound: OutboundMessage) -> SynthesizedVoice:
        if not self.enabled:
            raise TextToSpeechSynthesisError(
                code="tts-disabled",
                detail="text-to-speech is disabled in runtime config",
            )

        base_command = shlex.split(str(self._config.command).strip())
        if not base_command:
            raise TextToSpeechSynthesisError(
                code="tts-command-missing",
                detail="text-to-speech command is not configured",
            )

        resolved_binary = shutil.which(base_command[0]) if len(base_command) == 1 else None
        if len(base_command) == 1 and resolved_binary is None:
            raise TextToSpeechSynthesisError(
                code="tts-command-missing",
                detail=f"text-to-speech command not found on PATH ({base_command[0]})",
            )
        if resolved_binary is not None:
            base_command[0] = resolved_binary

        text = _bound_text(_resolve_text(outbound), max_chars=self._config.max_chars)
        metadata = outbound.metadata if isinstance(outbound.metadata, Mapping) else {}
        tts_metadata = metadata.get("telegram_tts") if isinstance(metadata.get("telegram_tts"), Mapping) else {}
        voice = _coerce_optional_text(tts_metadata.get("voice")) or self._config.voice
        language = _coerce_optional_text(tts_metadata.get("language")) or self._config.language
        caption = _coerce_optional_text(tts_metadata.get("caption"))

        base_dir = Path(self._config.temp_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base_dir) as tmp_dir:
            temp_root = Path(tmp_dir)
            text_path = temp_root / "speech.txt"
            output_path = temp_root / "speech.ogg"
            text_path.write_text(text, encoding="utf-8")

            command = list(base_command)
            command.extend(["--text-file", str(text_path), "--output", str(output_path)])
            if voice:
                command.extend(["--voice", voice])
            if language:
                command.extend(["--language", language])

            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=float(self._config.timeout_s),
                )
            except FileNotFoundError as exc:
                raise TextToSpeechSynthesisError(
                    code="tts-command-missing",
                    detail=f"text-to-speech command not found ({base_command[0]})",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise TextToSpeechSynthesisError(
                    code="tts-timeout",
                    detail=f"text-to-speech command timed out after {self._config.timeout_s}s",
                ) from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                detail = stderr or (exc.stdout or "").strip() or f"text-to-speech command exited with code {exc.returncode}"
                raise TextToSpeechSynthesisError(
                    code="tts-command-failed",
                    detail=detail,
                ) from exc

            if not output_path.exists():
                raise TextToSpeechSynthesisError(
                    code="tts-output-missing",
                    detail="text-to-speech command did not produce speech.ogg",
                )

            audio_bytes = output_path.read_bytes()
            if not audio_bytes:
                raise TextToSpeechSynthesisError(
                    code="tts-output-empty",
                    detail="text-to-speech command produced an empty output file",
                )
            if not _looks_like_ogg(audio_bytes):
                raise TextToSpeechSynthesisError(
                    code="tts-output-invalid",
                    detail="text-to-speech command produced a non-OGG output file",
                )

            return SynthesizedVoice(audio_bytes=audio_bytes, filename=output_path.name, caption=caption)


def _resolve_text(outbound: OutboundMessage) -> str:
    metadata = outbound.metadata if isinstance(outbound.metadata, Mapping) else {}
    tts = metadata.get("telegram_tts") if isinstance(metadata.get("telegram_tts"), Mapping) else {}
    explicit_text = _coerce_optional_text(tts.get("text"))
    text = explicit_text or outbound.text
    normalized = " ".join(str(text).split()).strip()
    if not normalized:
        raise TextToSpeechSynthesisError(
            code="tts-text-missing",
            detail="no text was available for text-to-speech synthesis",
        )
    return normalized


def _bound_text(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _looks_like_ogg(audio_bytes: bytes) -> bool:
    return audio_bytes.startswith(b"OggS")
