from __future__ import annotations

import socket
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.youtube_transcript import (
    YouTubeTranscript,
    YouTubeTranscriptError,
    _classify_transcript_exception,
    export_youtube_transcript,
    export_youtube_transcript_with_metadata,
    extract_first_youtube_url,
    extract_video_id,
    fetch_full_youtube_transcript,
    fetch_youtube_transcript,
    maybe_enrich_message_with_youtube_transcript,
)


class TestYouTubeTranscriptHelpers(unittest.TestCase):
    def test_extract_first_youtube_url_detects_watch_and_short_links(self) -> None:
        self.assertEqual(
            extract_first_youtube_url("ideas https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_first_youtube_url("https://youtu.be/dQw4w9WgXcQ?t=43"),
            "https://youtu.be/dQw4w9WgXcQ?t=43",
        )

    def test_extract_video_id_supports_common_youtube_url_shapes(self) -> None:
        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=43"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_maybe_enrich_message_with_transcript_keeps_non_youtube_text_unchanged(self) -> None:
        message = "just a normal message"
        self.assertEqual(maybe_enrich_message_with_youtube_transcript(message), message)

    def test_maybe_enrich_message_with_transcript_formats_codex_prompt(self) -> None:
        transcript = YouTubeTranscript(
            source_url="https://youtu.be/dQw4w9WgXcQ",
            video_id="dQw4w9WgXcQ",
            transcript_text="idea one idea two",
            segment_count=2,
            truncated=False,
            language_code="en",
        )

        enriched = maybe_enrich_message_with_youtube_transcript(
            "Please review this https://youtu.be/dQw4w9WgXcQ",
            fetcher=lambda _: transcript,
        )

        self.assertIn("The user sent a YouTube video link through Telegram.", enriched)
        self.assertIn("Original message:", enriched)
        self.assertIn("Transcript language: en", enriched)
        self.assertIn("Transcript:\nidea one idea two", enriched)

    def test_fetch_youtube_transcript_reports_missing_dependency_cleanly(self) -> None:
        with mock.patch.dict(sys.modules, {"youtube_transcript_api": None}):
            with self.assertRaises(YouTubeTranscriptError) as ctx:
                fetch_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(ctx.exception.code, "youtube-transcript-dependency-missing")
        self.assertIn("Install `youtube-transcript-api`", ctx.exception.user_message)

    def test_fetch_youtube_transcript_keeps_default_prompt_cap(self) -> None:
        with mock.patch(
            "channel_runtime.youtube_transcript._load_transcript_api_class",
            return_value=_fake_transcript_api_class("x" * 12001),
        ):
            transcript = fetch_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(len(transcript.transcript_text), 12000)
        self.assertTrue(transcript.truncated)

    def test_fetch_full_youtube_transcript_disables_prompt_cap(self) -> None:
        with mock.patch(
            "channel_runtime.youtube_transcript._load_transcript_api_class",
            return_value=_fake_transcript_api_class("x" * 12001),
        ):
            transcript = fetch_full_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(len(transcript.transcript_text), 12001)
        self.assertFalse(transcript.truncated)

    def test_export_youtube_transcript_writes_full_markdown_file(self) -> None:
        transcript = YouTubeTranscript(
            source_url="https://youtu.be/dQw4w9WgXcQ",
            video_id="dQw4w9WgXcQ",
            transcript_text="full transcript text",
            segment_count=3,
            truncated=False,
            language_code="en",
        )
        exported_at = datetime(2026, 6, 19, 12, 34, 56, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = export_youtube_transcript(
                "https://youtu.be/dQw4w9WgXcQ",
                export_dir=tmpdir,
                fetcher=lambda _: transcript,
                now=exported_at,
            )

            self.assertEqual(path.name, "YOUTUBE-dQw4w9WgXcQ-20260619-123456.md")
            content = path.read_text(encoding="utf-8")

        self.assertIn("type: youtube_transcript", content)
        self.assertIn('source_url: "https://youtu.be/dQw4w9WgXcQ"', content)
        self.assertIn("segment_count: 3", content)
        self.assertIn("truncated: false", content)
        self.assertIn("## Transcript\n\nfull transcript text", content)

    def test_export_youtube_transcript_with_metadata_returns_path_and_transcript(self) -> None:
        transcript = YouTubeTranscript(
            source_url="https://youtu.be/dQw4w9WgXcQ",
            video_id="dQw4w9WgXcQ",
            transcript_text="full transcript text",
            segment_count=3,
            truncated=False,
            language_code="en",
        )
        exported_at = datetime(2026, 6, 19, 12, 34, 56, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_youtube_transcript_with_metadata(
                "https://youtu.be/dQw4w9WgXcQ",
                export_dir=tmpdir,
                fetcher=lambda _: transcript,
                now=exported_at,
            )

            self.assertEqual(result.path.name, "YOUTUBE-dQw4w9WgXcQ-20260619-123456.md")
            self.assertEqual(result.transcript, transcript)
            self.assertIn("full transcript text", result.path.read_text(encoding="utf-8"))

    def test_export_youtube_transcript_filename_preserves_leading_video_id_hyphen(self) -> None:
        transcript = YouTubeTranscript(
            source_url="https://youtu.be/-QFHIoCo-Ko",
            video_id="-QFHIoCo-Ko",
            transcript_text="full transcript text",
            segment_count=1,
            truncated=False,
            language_code="en",
        )
        exported_at = datetime(2026, 6, 19, 12, 34, 56, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = export_youtube_transcript(
                "https://youtu.be/-QFHIoCo-Ko",
                export_dir=tmpdir,
                fetcher=lambda _: transcript,
                now=exported_at,
            )

        self.assertEqual(path.name, "YOUTUBE--QFHIoCo-Ko-20260619-123456.md")

    def test_classify_transcript_exception_detects_dns_resolution_failure(self) -> None:
        root = socket.gaierror(-2, "Name or service not known")
        wrapped = ConnectionError("HTTPSConnectionPool(host='www.youtube.com', port=443)")
        wrapped.__cause__ = root

        result = _classify_transcript_exception(wrapped)

        self.assertEqual(result.code, "youtube-transcript-network-dns")
        self.assertIn("cannot currently resolve or reach YouTube", result.user_message)


def _fake_transcript_api_class(text: str):
    class _FakeTranscriptApi:
        def fetch(self, video_id: str):
            return _FakeFetchedTranscript(text)

    return _FakeTranscriptApi


class _FakeFetchedTranscript:
    language_code = "en"

    def __init__(self, text: str) -> None:
        self.snippets = [{"text": text}]
