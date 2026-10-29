"""Bounded Whisper transcription and provenance-rich video artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .video_acquisition import MediaCandidate


_OFFLOAD_ROOT = Path("/mnt/d/agent_skills-offload")
_MODEL_DIR_ENV = "CHANNEL_WHISPER_MODEL_DIR"
_TRANSCRIPTION_TEMP_DIR_ENV = "CHANNEL_VIDEO_TRANSCRIPTION_TEMP_DIR"


def _default_model_dir() -> str:
    configured = os.environ.get(_MODEL_DIR_ENV, "").strip()
    if configured:
        return configured
    offload_dir = _OFFLOAD_ROOT / "whisper-models"
    if offload_dir.is_dir() or _OFFLOAD_ROOT.is_dir():
        return str(offload_dir)
    return str(Path(__file__).resolve().parents[1] / ".runtime" / "whisper-models")


def _default_temp_dir() -> str:
    configured = os.environ.get(_TRANSCRIPTION_TEMP_DIR_ENV, "").strip()
    if configured:
        return configured
    if _OFFLOAD_ROOT.is_dir():
        return str(_OFFLOAD_ROOT / "video-transcription")
    return ".channel_runtime/video_transcription"


class VideoTranscriptionError(RuntimeError):
    """A transcription operation failed in a typed, operator-safe way."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TranscriptionConfig:
    whisper_command: str = "whisper"
    whisper_model: str = "turbo"
    language: str | None = None
    timeout_s: float = 600.0
    max_transcript_chars: int = 500_000
    max_transcript_bytes: int = 2_000_000
    temp_dir: str = field(default_factory=_default_temp_dir)
    model_dir: str = field(default_factory=_default_model_dir)

    def __post_init__(self) -> None:
        if not str(self.whisper_command).strip():
            raise ValueError("whisper_command must not be empty")
        if not str(self.whisper_model).strip():
            raise ValueError("whisper_model must not be empty")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_transcript_chars < 1 or self.max_transcript_bytes < 1:
            raise ValueError("transcript limits must be positive")


@dataclass(frozen=True)
class TranscriptArtifact:
    candidate: MediaCandidate
    artifact_path: Path
    transcript_path: Path
    transcript_text: str
    media_sha256: str
    language: str | None
    model: str


def transcribe_audio(
    candidate: MediaCandidate,
    audio_path: str | Path,
    artifact_dir: str | Path,
    *,
    config: TranscriptionConfig | None = None,
    runner: Callable[..., Any] = subprocess.run,
    whisper_binary: str | None = None,
) -> TranscriptArtifact:
    """Transcribe an extracted audio file and persist JSON plus plain text artifacts."""

    config = config or TranscriptionConfig()
    audio_path = Path(audio_path)
    _validate_input(candidate, audio_path)
    binary = whisper_binary or shutil.which(config.whisper_command)
    if not binary:
        raise VideoTranscriptionError("whisper-missing", f"Whisper binary not found: {config.whisper_command}")

    media_sha256 = _sha256_file(audio_path)
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = _safe_stem(candidate.video_id or media_sha256[:16])
    temp_parent = Path(config.temp_dir)
    try:
        temp_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        temp_parent = None
    with tempfile.TemporaryDirectory(dir=str(temp_parent) if temp_parent is not None else None) as tmp:
        output_dir = Path(tmp)
        command = [
            binary,
            str(audio_path),
            "--model",
            config.whisper_model,
            "--model_dir",
            config.model_dir,
            "--output_format",
            "txt",
            "--output_dir",
            str(output_dir),
            "--verbose",
            "False",
        ]
        if config.language:
            command.extend(["--language", config.language])
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.timeout_s,
            )
        except FileNotFoundError as exc:
            raise VideoTranscriptionError("whisper-missing", f"Whisper binary not found: {config.whisper_command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoTranscriptionError("transcription-timeout", f"Whisper timed out after {config.timeout_s}s") from exc
        except OSError as exc:
            raise VideoTranscriptionError("transcription-failed", f"Whisper could not start: {type(exc).__name__}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Whisper returned a non-zero exit code").strip()
            raise VideoTranscriptionError("transcription-failed", _bounded_detail(detail))
        transcript_file = output_dir / f"{audio_path.stem}.txt"
        if not transcript_file.is_file():
            raise VideoTranscriptionError("transcript-missing", "Whisper did not produce the expected text output")
        try:
            raw = transcript_file.read_bytes()
        except OSError as exc:
            raise VideoTranscriptionError("transcript-read-failed", "Whisper output could not be read") from exc
    if len(raw) > config.max_transcript_bytes:
        raise VideoTranscriptionError("transcript-too-large", "Whisper output exceeds the configured byte limit")
    try:
        transcript_text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise VideoTranscriptionError("transcript-invalid", "Whisper output is not valid UTF-8") from exc
    if not transcript_text:
        raise VideoTranscriptionError("transcript-empty", "Whisper produced an empty transcript")
    if len(transcript_text) > config.max_transcript_chars:
        raise VideoTranscriptionError("transcript-too-large", "Whisper output exceeds the configured character limit")

    transcript_path = artifact_dir / f"{safe_stem}-{media_sha256[:16]}.txt"
    artifact_path = artifact_dir / f"{safe_stem}-{media_sha256[:16]}.json"
    transcript_path.write_text(transcript_text + "\n", encoding="utf-8")
    payload = {
        "schema": "generic-video-transcript-v1",
        "job_type": "generic-video-transcript",
        "source_url": candidate.source_url,
        "provider": candidate.provider,
        "media_url": candidate.media_url,
        "media_type": candidate.media_type,
        "video_id": candidate.video_id,
        "media_sha256": media_sha256,
        "audio_path": str(audio_path),
        "transcript_path": str(transcript_path),
        "transcript_text": transcript_text,
        "transcript_chars": len(transcript_text),
        "language": config.language,
        "model": config.whisper_model,
        "transcriber": "whisper-cli",
    }
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return TranscriptArtifact(
        candidate=candidate,
        artifact_path=artifact_path,
        transcript_path=transcript_path,
        transcript_text=transcript_text,
        media_sha256=media_sha256,
        language=config.language,
        model=config.whisper_model,
    )


def _validate_input(candidate: MediaCandidate, audio_path: Path) -> None:
    if candidate.media_type not in {"mp4", "hls"}:
        raise VideoTranscriptionError("unsupported-media-type", "only MP4 and HLS candidates can be transcribed")
    if not audio_path.is_file():
        raise VideoTranscriptionError("audio-missing", "extracted audio file does not exist")
    if audio_path.stat().st_size == 0:
        raise VideoTranscriptionError("audio-empty", "extracted audio file is empty")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_stem(value: str) -> str:
    result = "".join(char if char.isalnum() or char in "-_" else "-" for char in value).strip("-")
    return result or "video"


def _bounded_detail(value: str, limit: int = 500) -> str:
    return " ".join(value.split())[:limit] or "Whisper returned a non-zero exit code"
