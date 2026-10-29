"""Bounded media acquisition for the generic video workflow.

Resolution and execution are intentionally separate.  This module accepts only
the typed :class:`MediaCandidate` produced by ``video_acquisition`` and keeps
network and ffmpeg work bounded and injectable for tests.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.parse import urljoin, urlparse
from urllib.parse import parse_qs, quote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .video_acquisition import MediaCandidate


class VideoMediaError(RuntimeError):
    """A bounded download or audio extraction operation failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MediaConfig:
    max_download_bytes: int = 512 * 1024 * 1024
    download_timeout_s: float = 120.0
    extraction_timeout_s: float = 300.0
    max_duration_s: int = 7200
    chunk_size: int = 1024 * 1024
    max_redirects: int = 3
    # Maximum number of HLS resources the on-demand proxy may serve.  Resources
    # are streamed and are not represented by one local file per segment.
    max_hls_files: int = 100_000
    ffmpeg_command: str = "ffmpeg"
    allowed_media_hosts: tuple[str, ...] = ("vidalytics.com", "*.vidalytics.com")

    def __post_init__(self) -> None:
        if self.max_download_bytes < 1:
            raise ValueError("max_download_bytes must be positive")
        if self.download_timeout_s <= 0 or self.extraction_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        if self.max_duration_s < 1 or self.chunk_size < 1:
            raise ValueError("max_duration_s and chunk_size must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if self.max_hls_files < 1:
            raise ValueError("max_hls_files must be positive")
        if not all(str(host).strip() for host in self.allowed_media_hosts):
            raise ValueError("allowed_media_hosts must contain non-empty host patterns")


@dataclass(frozen=True)
class DownloadedMedia:
    candidate: MediaCandidate
    path: Path
    bytes_written: int


@dataclass(frozen=True)
class ExtractedAudio:
    candidate: MediaCandidate
    path: Path
    command: tuple[str, ...]


def download_media(
    candidate: MediaCandidate,
    destination: str | Path,
    *,
    config: MediaConfig | None = None,
    opener: Callable[..., Any] | None = None,
) -> DownloadedMedia:
    """Download a direct media candidate without exceeding its byte budget."""

    config = config or MediaConfig()
    _validate_candidate(candidate, config)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(candidate.media_url, headers={"User-Agent": "mission-control-video/1"})
    open_media = opener or _build_bounded_opener(config.max_redirects, config.allowed_media_hosts).open
    temporary_path: Path | None = None
    bytes_written = 0
    try:
        with open_media(request, timeout=config.download_timeout_s) as response:
            content_length = _content_length(response)
            if content_length is not None and content_length > config.max_download_bytes:
                raise VideoMediaError("download-too-large", "media Content-Length exceeds the configured limit")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".part", dir=destination.parent, delete=False
            ) as output:
                temporary_path = Path(output.name)
                while True:
                    chunk = response.read(config.chunk_size)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > config.max_download_bytes:
                        raise VideoMediaError("download-too-large", "media exceeded the configured byte limit")
                    output.write(chunk)
        temporary_path.replace(destination)
        return DownloadedMedia(candidate=candidate, path=destination, bytes_written=bytes_written)
    except VideoMediaError:
        raise
    except (OSError, URLError, TimeoutError) as exc:
        raise VideoMediaError("download-failed", f"media download failed: {exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def extract_audio(
    candidate: MediaCandidate,
    media_input: str | Path,
    destination: str | Path,
    *,
    config: MediaConfig | None = None,
    runner: Callable[..., Any] = subprocess.run,
    ffmpeg_binary: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> ExtractedAudio:
    """Extract bounded mono 16 kHz WAV audio using ffmpeg.

    ``media_input`` may be a downloaded file or a resolved HLS URL.  HLS is
    limited by ``-t`` and the subprocess timeout; callers should still prefer
    a downloaded file when the provider exposes MP4.
    """

    config = config or MediaConfig()
    _validate_candidate(candidate, config)
    binary = ffmpeg_binary or shutil.which(config.ffmpeg_command)
    if not binary:
        raise VideoMediaError("ffmpeg-missing", f"ffmpeg binary not found: {config.ffmpeg_command}")
    hls_proxy: Any | None = None
    if candidate.media_type == "hls" and str(media_input).lower().startswith("https://"):
        try:
            hls_proxy = _HLSProxy(str(media_input), config=config, opener=opener)
            hls_proxy.start()
            media_input = hls_proxy.url
        except VideoMediaError:
            raise
    input_value = str(media_input)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = (
        binary,
        "-nostdin",
        "-y",
        *(("-protocol_whitelist", "http,https,tcp,tls,crypto,data") if candidate.media_type == "hls" else ()),
        "-i",
        input_value,
        "-t",
        str(config.max_duration_s),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(destination),
    )
    try:
        try:
            completed = runner(list(command), check=False, capture_output=True, text=True, timeout=config.extraction_timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            code = "extraction-timeout" if isinstance(exc, subprocess.TimeoutExpired) else "extraction-failed"
            raise VideoMediaError(code, f"ffmpeg audio extraction failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "ffmpeg returned a non-zero exit code").strip()
            raise VideoMediaError("extraction-failed", detail)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise VideoMediaError("audio-output-missing", "ffmpeg did not produce a non-empty audio file")
        return ExtractedAudio(candidate=candidate, path=destination, command=command)
    finally:
        if hls_proxy is not None:
            hls_proxy.close()


def _validate_candidate(candidate: MediaCandidate, config: MediaConfig) -> None:
    if candidate.media_type not in {"mp4", "hls"}:
        raise VideoMediaError("unsupported-media-type", "only MP4 and HLS candidates can be acquired")
    if not candidate.media_url.lower().startswith("https://"):
        raise VideoMediaError("media-url-not-https", "media URL must use HTTPS")
    hostname = urlparse(candidate.media_url).hostname
    if not hostname or not _host_allowed(hostname, config.allowed_media_hosts):
        raise VideoMediaError("media-host-not-allowed", "media URL host is not on the configured allowlist")


class _BoundedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects: int, allowed_hosts: tuple[str, ...]) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._redirect_count = 0
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirect_count += 1
        if self._redirect_count > self._max_redirects:
            raise VideoMediaError("too-many-redirects", "media URL exceeded the redirect limit")
        if not str(newurl).lower().startswith("https://"):
            raise VideoMediaError("redirect-not-https", "media redirect target must use HTTPS")
        hostname = urlparse(newurl).hostname
        if not hostname or not _host_allowed(hostname, self._allowed_hosts):
            raise VideoMediaError("redirect-host-not-allowed", "media redirect target is not on the configured allowlist")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_bounded_opener(max_redirects: int, allowed_hosts: tuple[str, ...]):
    return build_opener(_BoundedRedirectHandler(max_redirects, allowed_hosts))


_HLS_URI_ATTRIBUTE = re.compile(r'(?P<prefix>URI=)(?P<quote>["\'])(?P<uri>.*?)(?P=quote)')


class _HLSProxy:
    """On-demand, policy-enforcing HLS proxy for ffmpeg.

    The proxy keeps playlists in memory and streams media resources directly
    from the bounded opener.  This avoids both a file per segment and giving
    ffmpeg an unrestricted path to remote redirects.
    """

    def __init__(self, manifest_url: str, *, config: MediaConfig, opener: Callable[..., Any] | None) -> None:
        self.manifest_url = manifest_url
        self.config = config
        self.open_resource = opener or _build_bounded_opener(config.max_redirects, config.allowed_media_hosts).open
        self.resources = 0
        self.bytes = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self.server is None:
            raise RuntimeError("HLS proxy has not started")
        return f"http://127.0.0.1:{self.server.server_port}/resource?url={quote(self.manifest_url, safe='')}"

    def start(self) -> None:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                try:
                    values = parse_qs(urlsplit(self.path).query).get("url", [])
                    if len(values) != 1:
                        raise VideoMediaError("hls-resource-not-allowed", "HLS proxy request is missing its resource URL")
                    proxy.serve(self, values[0])
                except VideoMediaError as exc:
                    self.send_error(502, exc.code)
                except Exception:
                    self.send_error(502, "hls-fetch-failed")

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            raise VideoMediaError("hls-proxy-failed", f"could not start HLS proxy: {exc}") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def serve(self, request_handler: BaseHTTPRequestHandler, resource_url: str) -> None:
        parsed = urlparse(resource_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname or not _host_allowed(parsed.hostname, self.config.allowed_media_hosts):
            raise VideoMediaError("hls-resource-not-allowed", "HLS resource is not on the configured HTTPS allowlist")
        self.resources += 1
        if self.resources > self.config.max_hls_files:
            raise VideoMediaError("hls-too-many-files", "HLS resource limit exceeded")
        response = self.open_resource(Request(resource_url, headers={"User-Agent": "mission-control-video/1"}), timeout=self.config.download_timeout_s)
        with response:
            first = response.read(self.config.chunk_size)
            self.bytes += len(first)
            if self.bytes > self.config.max_download_bytes:
                raise VideoMediaError("hls-download-too-large", "HLS resources exceeded the configured byte limit")
            is_playlist = first.lstrip().startswith(b"#EXTM3U") or parsed.path.lower().endswith((".m3u8", ".m3u"))
            if is_playlist:
                body = first + self._read_remaining(response)
                rewritten = self._rewrite_playlist(resource_url, body)
                payload = rewritten.encode("utf-8")
                request_handler.send_response(200)
                request_handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
                request_handler.send_header("Content-Length", str(len(payload)))
                request_handler.end_headers()
                request_handler.wfile.write(payload)
                return
            request_handler.send_response(200)
            request_handler.send_header("Content-Type", "application/octet-stream")
            request_handler.end_headers()
            request_handler.wfile.write(first)
            while True:
                chunk = response.read(self.config.chunk_size)
                if not chunk:
                    break
                self.bytes += len(chunk)
                if self.bytes > self.config.max_download_bytes:
                    raise VideoMediaError("hls-download-too-large", "HLS resources exceeded the configured byte limit")
                request_handler.wfile.write(chunk)

    def _read_remaining(self, response: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = response.read(self.config.chunk_size)
            if not chunk:
                return b"".join(chunks)
            self.bytes += len(chunk)
            if self.bytes > self.config.max_download_bytes:
                raise VideoMediaError("hls-download-too-large", "HLS resources exceeded the configured byte limit")
            chunks.append(chunk)

    def _rewrite_playlist(self, base_url: str, body: bytes) -> str:
        lines = body.decode("utf-8", errors="strict").splitlines()
        rewritten: list[str] = []
        for line in lines:
            if "URI=" in line:
                line = _HLS_URI_ATTRIBUTE.sub(
                    lambda match: f'{match.group("prefix")}{match.group("quote")}{self._local_url(urljoin(base_url, match.group("uri")))}{match.group("quote")}',
                    line,
                )
            elif line and not line.startswith("#"):
                line = self._local_url(urljoin(base_url, line.strip()))
            rewritten.append(line)
        return "\n".join(rewritten) + "\n"

    def _local_url(self, resource_url: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/resource?url={quote(resource_url, safe='')}"

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def _host_allowed(hostname: str, patterns: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for pattern in patterns:
        normalized = pattern.lower().strip().rstrip(".")
        if normalized.startswith("*."):
            if host.endswith(normalized[1:]) and host != normalized[2:]:
                return True
        elif host == normalized:
            return True
    return False


def _content_length(response: Any) -> int | None:
    try:
        value = response.headers.get("Content-Length")
        return int(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None
