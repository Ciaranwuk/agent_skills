#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from channel_runtime.untrusted_artifacts import (  # noqa: E402
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_UNTRUSTED_INBOX,
    UntrustedArtifactError,
    write_untrusted_artifact,
)
from channel_runtime.youtube_transcript import (  # noqa: E402
    YouTubeTranscriptError,
    fetch_full_youtube_transcript,
    transcript_to_dict,
)


FETCHER_NAME = "agent_skills.scripts.host_acquisition_fetcher.v1"
ALLOWED_JOB_TYPES = {"marketplace-search", "source-page"}
ALLOWED_JOB_TYPES.add("youtube-transcript")
ALLOWED_URL_SCHEMES = {"http", "https"}


class HostFetcherError(RuntimeError):
    def __init__(self, *, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = str(code).strip() or "host-fetcher-error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a URL into the untrusted artifact inbox.")
    parser.add_argument("--url", required=True, help="Source URL recorded in artifact metadata.")
    parser.add_argument("--inbox-root", default=DEFAULT_UNTRUSTED_INBOX)
    parser.add_argument("--job-type", default="source-page")
    parser.add_argument("--artifact-id", default="")
    parser.add_argument("--mime-type", default="")
    parser.add_argument("--marketplace", default="", help="Optional marketplace/source label for research artifacts.")
    parser.add_argument("--query", default="", help="Optional marketplace search query metadata.")
    parser.add_argument("--category", default="", help="Optional taxonomy/category metadata.")
    parser.add_argument("--from-file", default="", help="Read content from a local file instead of the network.")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES)
    args = parser.parse_args(argv)

    try:
        _validate_request(url=args.url, job_type=args.job_type)
        content, mime_type, status_code, extra_metadata = _read_content(
            job_type=args.job_type,
            url=args.url,
            from_file=args.from_file,
            mime_type=args.mime_type,
            marketplace=args.marketplace,
            query=args.query,
            category=args.category,
            timeout_seconds=args.timeout_seconds,
            max_bytes=args.max_bytes,
        )
        artifact_id = args.artifact_id.strip() or _default_artifact_id(args.url)
        metadata_path = write_untrusted_artifact(
            inbox_root=args.inbox_root,
            artifact_id=artifact_id,
            source_url=args.url,
            job_type=args.job_type,
            content=content,
            mime_type=mime_type,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            fetcher=FETCHER_NAME,
            status_code=status_code,
            extra_metadata=extra_metadata,
        )
    except (OSError, ValueError, UntrustedArtifactError, YouTubeTranscriptError, HostFetcherError) as exc:
        code = getattr(exc, "code", "host-fetcher-error")
        json.dump(
            {"ok": False, "code": code, "detail": f"{type(exc).__name__}: {exc}"},
            sys.stderr,
            sort_keys=True,
        )
        sys.stderr.write("\n")
        return 1

    json.dump({"ok": True, "metadata_path": str(metadata_path)}, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _validate_request(*, url: str, job_type: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '<empty>'}")
    if str(job_type).strip() not in ALLOWED_JOB_TYPES:
        raise ValueError(f"unsupported job type: {job_type}")


def _read_content(
    *,
    job_type: str,
    url: str,
    from_file: str,
    mime_type: str,
    marketplace: str = "",
    query: str = "",
    category: str = "",
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[bytes, str, int | None, dict[str, object] | None]:
    if max_bytes < 1:
        raise ValueError("--max-bytes must be at least 1")
    if from_file:
        content = Path(from_file).read_bytes()
        if len(content) > max_bytes:
            raise ValueError(f"artifact body exceeds max bytes: {len(content)} > {max_bytes}")
        return content, mime_type.strip() or "text/plain", None, _research_metadata(
            marketplace=marketplace,
            query=query,
            category=category,
        )

    if job_type == "youtube-transcript":
        transcript = fetch_full_youtube_transcript(url)
        content = (json.dumps(transcript_to_dict(transcript), sort_keys=True) + "\n").encode("utf-8")
        if len(content) > max_bytes:
            raise ValueError(f"artifact body exceeds max bytes: {len(content)} > {max_bytes}")
        return (
            content,
            mime_type.strip() or "application/json",
            None,
            {
                "video_id": transcript.video_id,
                "segment_count": transcript.segment_count,
                "language_code": transcript.language_code,
                "truncated": transcript.truncated,
            },
        )

    request = urllib.request.Request(url, headers={"User-Agent": "agent-skills-acquisition-bridge/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_type() or "application/octet-stream"
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError(f"artifact body exceeds max bytes: > {max_bytes}")
            return content, mime_type.strip() or content_type, getattr(response, "status", None), _research_metadata(
                marketplace=marketplace,
                query=query,
                category=category,
            )
    except urllib.error.HTTPError as exc:
        code = "source-page-request-blocked" if exc.code in {401, 403, 429} else "source-page-http-error"
        raise HostFetcherError(code=code, detail=f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        reason_text = f"{type(reason).__name__}: {reason}"
        if isinstance(reason, socket.gaierror) or "temporary failure in name resolution" in reason_text.lower():
            raise HostFetcherError(code="source-page-network-dns", detail=reason_text) from exc
        raise HostFetcherError(code="source-page-network-error", detail=reason_text) from exc


def _default_artifact_id(url: str) -> str:
    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{digest}"


def _research_metadata(*, marketplace: str, query: str, category: str) -> dict[str, object] | None:
    metadata = {
        key: value.strip()
        for key, value in {
            "marketplace": marketplace,
            "query": query,
            "category": category,
        }.items()
        if value.strip()
    }
    return metadata or None


if __name__ == "__main__":
    raise SystemExit(main())
