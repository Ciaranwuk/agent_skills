"""Provider-neutral discovery of public video media candidates.

This module deliberately stops at resolution.  Downloading media and invoking
transcription belong to later, separately bounded stages.  Keeping discovery
pure makes landing-page parsing testable without network access and prevents a
page's arbitrary URLs from becoming executable acquisition targets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse


class VideoResolutionError(ValueError):
    """A public landing page could not be resolved to an allowed media URL."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MediaCandidate:
    source_url: str
    media_url: str
    provider: str
    media_type: str
    video_id: str | None = None


_VIDALYTICS_HOST = "vidalytics.com"
_MEDIA_EXTENSIONS = (".mp4", ".m3u8")
_URL_RE = re.compile(r"https?://[^\"'<>\s]+", re.IGNORECASE)
_VIDALYTICS_EMBED_RE = re.compile(
    r"(?i)(https?://[^\"'<>\s]+/embeds/[^\"'<>\s]+/[^\"'<>\s]+/)"
)


def detect_provider(page_url: str, html: str) -> str | None:
    """Return a supported provider name when the page exposes one."""

    haystack = f"{page_url}\n{html}".lower()
    if "vidalytics" in haystack:
        return "vidalytics"
    return None


def resolve_public_video_page(*, source_url: str, html: str) -> MediaCandidate:
    """Resolve a supported public landing page without performing I/O."""

    source_url = _require_http_url(source_url, code="invalid-source-url")
    provider = detect_provider(source_url, html)
    if provider == "vidalytics":
        return resolve_vidalytics(source_url=source_url, html=html)
    raise VideoResolutionError(
        "unsupported-provider",
        "the landing page does not expose a supported public video provider",
    )


def resolve_vidalytics(*, source_url: str, html: str) -> MediaCandidate:
    """Resolve a Vidalytics embed and its publicly exposed media settings."""

    source_url = _require_http_url(source_url, code="invalid-source-url")
    if "vidalytics" not in html.lower() and "vidalytics" not in source_url.lower():
        raise VideoResolutionError("vidalytics-embed-not-found", "no Vidalytics embed was found")

    video_id = _first_match(
        html,
        (
            r'(?i)["\']video(?:Id|ID)["\']\s*[:=]\s*["\']([^"\']+)',
            r"(?i)vidalytics[^\n]{0,300}?video(?:Id|ID)[^A-Za-z0-9]+([A-Za-z0-9_-]{8,})",
        ),
    )
    settings_url = find_vidalytics_settings_url(source_url=source_url, html=html)
    if settings_url:
        settings_url = urljoin(source_url, settings_url)
        parsed = urlparse(settings_url)
        if parsed.hostname and not _is_allowed_host(parsed.hostname):
            raise VideoResolutionError("settings-host-not-allowed", "Vidalytics settings URL has an unapproved host")

    media_url = _find_media_url(html)
    if not media_url:
        raise VideoResolutionError(
            "media-settings-required",
            "the page identifies Vidalytics but exposes no direct MP4/HLS media candidate; fetch player settings first",
        )
    media_url = urljoin(source_url, media_url)
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"}:
        raise VideoResolutionError("media-url-not-http", "resolved media URL is not HTTP(S)")
    suffix = parsed.path.lower()
    media_type = "hls" if suffix.endswith(".m3u8") else "mp4" if suffix.endswith(".mp4") else "unknown"
    if media_type == "unknown":
        raise VideoResolutionError("unsupported-media-type", "resolved media is not MP4 or HLS")
    return MediaCandidate(source_url, media_url, "vidalytics", media_type, video_id)


def find_vidalytics_settings_url(*, source_url: str, html: str) -> str | None:
    """Find the public Vidalytics player settings endpoint exposed by an embed."""

    source_url = _require_http_url(source_url, code="invalid-source-url")
    settings_url = _first_match(
        html,
        (
            r'(?i)["\'](?:settings|settingsUrl|playerSettings)["\']\s*[:=]\s*["\']([^"\']+)',
            r'(?i)https?://[^"\']*vidalytics[^"\']*player\.settings\.json[^"\']*',
        ),
    )
    if not settings_url:
        embed_match = _VIDALYTICS_EMBED_RE.search(html)
        if embed_match:
            settings_url = urljoin(embed_match.group(1), "player.settings.json")
    if not settings_url:
        return None
    settings_url = urljoin(source_url, settings_url)
    parsed = urlparse(settings_url)
    if parsed.scheme not in {"http", "https"}:
        raise VideoResolutionError("settings-url-not-http", "Vidalytics settings URL is not HTTP(S)")
    if not parsed.hostname or not _is_allowed_host(parsed.hostname):
        raise VideoResolutionError("settings-host-not-allowed", "Vidalytics settings URL has an unapproved host")
    return settings_url


def resolve_vidalytics_settings(*, source_url: str, settings: Mapping[str, Any]) -> MediaCandidate:
    """Resolve a candidate from parsed Vidalytics ``player.settings.json``."""

    source_url = _require_http_url(source_url, code="invalid-source-url")
    media_url = _find_media_url(json.dumps(settings, sort_keys=True))
    if not media_url:
        raise VideoResolutionError("media-url-missing", "Vidalytics settings contain no MP4/HLS URL")
    media_url = urljoin(source_url, media_url)
    suffix = urlparse(media_url).path.lower()
    media_type = "hls" if suffix.endswith(".m3u8") else "mp4" if suffix.endswith(".mp4") else "unknown"
    if media_type == "unknown":
        raise VideoResolutionError("unsupported-media-type", "Vidalytics settings contain no supported media URL")
    video_id = _first_string(settings, ("videoId", "video_id", "id"))
    if video_id is None:
        video_match = re.search(r"/video/[^/]+/([A-Za-z0-9_-]+)/", media_url)
        if video_match:
            video_id = video_match.group(1)
    return MediaCandidate(source_url, media_url, "vidalytics", media_type, video_id)


def _find_media_url(value: str) -> str | None:
    for candidate in _URL_RE.findall(value):
        candidate = candidate.rstrip(",]}")
        if urlparse(candidate).path.lower().endswith(_MEDIA_EXTENSIONS):
            return candidate
    return None


def _first_match(value: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def _first_string(value: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _require_http_url(value: str, *, code: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VideoResolutionError(code, "source URL must be an absolute HTTP(S) URL")
    return str(value).strip()


def _is_allowed_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == _VIDALYTICS_HOST or host.endswith("." + _VIDALYTICS_HOST)


class _UnusedEmbedParser(HTMLParser):
    """Reserved for the network-facing stage; HTML parsing stays dependency-free."""
