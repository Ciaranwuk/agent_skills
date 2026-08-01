from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse


_MAX_TRANSCRIPT_CHARS = 12000


@dataclass(frozen=True)
class YouTubeTranscript:
    source_url: str
    video_id: str
    transcript_text: str
    segment_count: int
    truncated: bool
    language_code: str | None = None


@dataclass(frozen=True)
class YouTubeTranscriptExport:
    path: Path
    transcript: YouTubeTranscript


class YouTubeTranscriptError(RuntimeError):
    def __init__(self, *, code: str, user_message: str, detail: str) -> None:
        super().__init__(detail)
        self.code = str(code).strip() or "youtube-transcript-error"
        self.user_message = str(user_message).strip() or "I couldn't fetch that YouTube transcript."


def maybe_enrich_message_with_youtube_transcript(
    message_text: str,
    *,
    fetcher: Any = None,
) -> str:
    source_url = extract_first_youtube_url(message_text)
    if source_url is None:
        return str(message_text)

    transcript_fetcher = fetcher or fetch_youtube_transcript
    try:
        transcript = transcript_fetcher(source_url)
    except YouTubeTranscriptError:
        raise
    except Exception as exc:
        raise YouTubeTranscriptError(
            code="youtube-transcript-unavailable",
            user_message=(
                "I found the YouTube link, but I couldn't fetch a transcript for it. "
                "The video may not have usable captions."
            ),
            detail=f"failed to fetch transcript for url={source_url}: {type(exc).__name__}: {exc}",
        ) from exc
    return format_codex_prompt(message_text=message_text, transcript=transcript)


def extract_first_youtube_url(message_text: str) -> str | None:
    for raw_part in str(message_text).split():
        candidate = raw_part.strip().strip("()[]{}<>,.!?\"'")
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if _is_youtube_host(parsed.netloc):
            return candidate
    return None


def extract_video_id(source_url: str) -> str | None:
    parsed = urlparse(str(source_url).strip())
    host = parsed.netloc.strip().lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if host in {"youtu.be", "www.youtu.be"}:
        return _normalize_video_id(path_parts[0] if path_parts else None)
    if not _is_youtube_host(host):
        return None
    if parsed.path == "/watch":
        return _normalize_video_id((query.get("v") or [None])[0])
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return _normalize_video_id(path_parts[1])
    return None


def fetch_youtube_transcript(
    source_url: str,
    *,
    max_chars: int | None = _MAX_TRANSCRIPT_CHARS,
    proxy_config: Any = None,
    http_client: Any = None,
) -> YouTubeTranscript:
    video_id = extract_video_id(source_url)
    if video_id is None:
        raise YouTubeTranscriptError(
            code="youtube-video-id-invalid",
            user_message="I found a YouTube link, but I couldn't parse a valid video ID from it.",
            detail=f"invalid YouTube video URL: {source_url}",
        )

    api_cls = _load_transcript_api_class()
    try:
        api = _build_transcript_api(
            api_cls,
            proxy_config=proxy_config,
            http_client=http_client,
        )
        fetched = api.fetch(video_id) if hasattr(api, "fetch") else api_cls.get_transcript(video_id)
        transcript_text, segment_count = _coerce_transcript_text(fetched)
        if not transcript_text:
            raise ValueError("empty transcript")
    except YouTubeTranscriptError:
        raise
    except Exception as exc:
        error = _classify_transcript_exception(exc)
        raise YouTubeTranscriptError(
            code=error.code,
            user_message=error.user_message,
            detail=f"failed to fetch transcript for video_id={video_id}: {type(exc).__name__}: {exc}",
        ) from exc

    normalized_text = " ".join(transcript_text.split())
    truncated = False
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must be non-negative or None")
    if max_chars is not None and len(normalized_text) > max_chars:
        normalized_text = normalized_text[:max_chars].rstrip()
        truncated = True

    language_code = None
    raw_language = getattr(fetched, "language_code", None)
    if raw_language is not None:
        text = str(raw_language).strip()
        if text:
            language_code = text

    return YouTubeTranscript(
        source_url=str(source_url).strip(),
        video_id=video_id,
        transcript_text=normalized_text,
        segment_count=segment_count,
        truncated=truncated,
        language_code=language_code,
    )


def fetch_full_youtube_transcript(
    source_url: str,
    *,
    proxy_config: Any = None,
    http_client: Any = None,
) -> YouTubeTranscript:
    return fetch_youtube_transcript(
        source_url,
        max_chars=None,
        proxy_config=proxy_config,
        http_client=http_client,
    )


def export_youtube_transcript(
    source_url: str,
    *,
    export_dir: str | Path,
    fetcher: Any = None,
    now: datetime | None = None,
) -> Path:
    return export_youtube_transcript_with_metadata(
        source_url,
        export_dir=export_dir,
        fetcher=fetcher,
        now=now,
    ).path


def export_youtube_transcript_with_metadata(
    source_url: str,
    *,
    export_dir: str | Path,
    fetcher: Any = None,
    now: datetime | None = None,
) -> YouTubeTranscriptExport:
    transcript_fetcher = fetcher or fetch_full_youtube_transcript
    transcript = transcript_fetcher(source_url)
    destination_dir = Path(export_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    filename = f"YOUTUBE-{_safe_filename_part(transcript.video_id)}-{timestamp:%Y%m%d-%H%M%S}.md"
    destination = destination_dir / filename
    destination.write_text(
        _format_transcript_export(transcript=transcript, exported_at=timestamp),
        encoding="utf-8",
    )
    return YouTubeTranscriptExport(path=destination, transcript=transcript)


def transcript_to_dict(transcript: YouTubeTranscript) -> dict[str, Any]:
    return asdict(transcript)


def transcript_from_mapping(payload: Mapping[str, Any]) -> YouTubeTranscript:
    return YouTubeTranscript(
        source_url=str(payload.get("source_url", "")).strip(),
        video_id=str(payload.get("video_id", "")).strip(),
        transcript_text=str(payload.get("transcript_text", "")).strip(),
        segment_count=int(payload.get("segment_count", 0)),
        truncated=bool(payload.get("truncated", False)),
        language_code=(str(payload.get("language_code", "")).strip() or None),
    )


def _load_transcript_api_class() -> Any:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise YouTubeTranscriptError(
            code="youtube-transcript-dependency-missing",
            user_message=(
                "I found the YouTube link, but transcript support is not installed on this host yet. "
                "Install `youtube-transcript-api` and try again."
            ),
            detail="youtube_transcript_api import failed",
        ) from exc
    return YouTubeTranscriptApi


def _build_transcript_api(
    api_cls: Any,
    *,
    proxy_config: Any = None,
    http_client: Any = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if proxy_config is not None:
        kwargs["proxy_config"] = proxy_config
    if http_client is not None:
        kwargs["http_client"] = http_client
    if not kwargs:
        return api_cls()
    return api_cls(**kwargs)


@dataclass(frozen=True)
class _TranscriptFetchErrorClassification:
    code: str
    user_message: str


def _classify_transcript_exception(exc: Exception) -> _TranscriptFetchErrorClassification:
    root = exc
    while getattr(root, "__cause__", None) is not None:
        next_exc = getattr(root, "__cause__")
        if not isinstance(next_exc, Exception):
            break
        root = next_exc

    error_text = f"{type(exc).__name__}: {exc} {type(root).__name__}: {root}".lower()
    if isinstance(root, socket.gaierror) or "temporary failure in name resolution" in error_text or "name or service not known" in error_text:
        return _TranscriptFetchErrorClassification(
            code="youtube-transcript-network-dns",
            user_message=(
                "I found the YouTube link, but this host cannot currently resolve or reach YouTube. "
                "The transcript workflow is blocked by host DNS/network connectivity, not by the video itself."
            ),
        )

    return _TranscriptFetchErrorClassification(
        code="youtube-transcript-unavailable",
        user_message=(
            "I found the YouTube link, but I couldn't fetch a transcript for it. "
            "The video may not have usable captions."
        ),
    )


def _coerce_transcript_text(fetched: Any) -> tuple[str, int]:
    snippets = getattr(fetched, "snippets", fetched)
    if not isinstance(snippets, Iterable):
        raise ValueError("transcript payload is not iterable")

    parts: list[str] = []
    segment_count = 0
    for snippet in snippets:
        text = None
        if isinstance(snippet, dict):
            text = snippet.get("text")
        else:
            text = getattr(snippet, "text", None)
        normalized = str(text or "").strip()
        if not normalized:
            continue
        parts.append(normalized)
        segment_count += 1

    return (" ".join(parts).strip(), segment_count)


def format_codex_prompt(*, message_text: str, transcript: YouTubeTranscript) -> str:
    transcript_note = "yes" if transcript.truncated else "no"
    language_code = transcript.language_code or "unknown"
    return (
        "The user sent a YouTube video link through Telegram.\n\n"
        f"Original message:\n{str(message_text).strip()}\n\n"
        "Use the transcript below as the primary source. Discuss the strongest ideas from the video, "
        "which ideas or workflows are reusable, what actions are worth testing, and where the operator "
        "should be skeptical or validate further.\n\n"
        f"Video URL: {transcript.source_url}\n"
        f"Video ID: {transcript.video_id}\n"
        f"Transcript language: {language_code}\n"
        f"Transcript segments: {transcript.segment_count}\n"
        f"Transcript truncated: {transcript_note}\n\n"
        "Transcript:\n"
        f"{transcript.transcript_text}"
    )


def _format_transcript_export(*, transcript: YouTubeTranscript, exported_at: datetime) -> str:
    language_code = transcript.language_code or ""
    return (
        "---\n"
        "type: youtube_transcript\n"
        f"exported_at: {json.dumps(exported_at.isoformat())}\n"
        f"source_url: {json.dumps(transcript.source_url)}\n"
        f"video_id: {json.dumps(transcript.video_id)}\n"
        f"language_code: {json.dumps(language_code)}\n"
        f"segment_count: {transcript.segment_count}\n"
        f"truncated: {str(transcript.truncated).lower()}\n"
        "tags: [youtube, transcript]\n"
        "---\n\n"
        f"# YouTube Transcript - {transcript.video_id}\n\n"
        f"- Source URL: {transcript.source_url}\n"
        f"- Language: {language_code or 'unknown'}\n"
        f"- Segments: {transcript.segment_count}\n"
        f"- Truncated: {'yes' if transcript.truncated else 'no'}\n\n"
        "## Transcript\n\n"
        f"{transcript.transcript_text}\n"
    )


def _safe_filename_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value.strip())
    return safe.strip("_") or "youtube-video"


def _is_youtube_host(host: str) -> bool:
    normalized = str(host).strip().lower()
    return normalized in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }


def _normalize_video_id(raw_value: Any) -> str | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    normalized = "".join(ch for ch in value if ch in allowed)
    if len(normalized) < 6:
        return None
    return normalized
