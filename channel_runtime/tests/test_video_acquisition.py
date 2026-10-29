from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.video_acquisition import (  # noqa: E402
    VideoResolutionError,
    resolve_public_video_page,
    find_vidalytics_settings_url,
    resolve_vidalytics_settings,
)


class VideoAcquisitionTests(unittest.TestCase):
    def test_derives_settings_url_from_embed_loader(self) -> None:
        settings_url = find_vidalytics_settings_url(
            source_url="https://go.example.test/join",
            html=(
                "<script>run('https://quick.vidalytics.com/embeds/account/video-token/');</script>"
            ),
        )
        self.assertEqual(
            settings_url,
            "https://quick.vidalytics.com/embeds/account/video-token/player.settings.json",
        )

    def test_resolves_vidalytics_mp4_from_page_fixture(self) -> None:
        candidate = resolve_public_video_page(
            source_url="https://go.example.test/join",
            html=(
                '<div data-provider="Vidalytics" '
                'data-videoId="aZ0GbEqBenzAGF5p" '
                'data-media="https://cdn.vidalytics.com/video.mp4"></div>'
            ),
        )
        self.assertEqual(candidate.provider, "vidalytics")
        self.assertEqual(candidate.video_id, "aZ0GbEqBenzAGF5p")
        self.assertEqual(candidate.media_type, "mp4")
        self.assertEqual(candidate.media_url, "https://cdn.vidalytics.com/video.mp4")

    def test_resolves_hls_from_settings_payload(self) -> None:
        candidate = resolve_vidalytics_settings(
            source_url="https://player.vidalytics.com/player.settings.json",
            settings={
                "videoId": "video-123",
                "sources": [{"url": "https://cdn.vidalytics.com/video.m3u8"}],
            },
        )
        self.assertEqual(candidate.media_type, "hls")
        self.assertEqual(candidate.video_id, "video-123")

    def test_rejects_unsupported_provider(self) -> None:
        with self.assertRaisesRegex(VideoResolutionError, "supported public video provider"):
            resolve_public_video_page(
                source_url="https://example.test/video",
                html='<video src="https://cdn.example.test/video.mp4"></video>',
            )

    def test_rejects_malformed_or_unsupported_media(self) -> None:
        with self.assertRaisesRegex(VideoResolutionError, "no direct MP4/HLS"):
            resolve_public_video_page(
                source_url="https://example.test/video",
                html='<div data-provider="Vidalytics">videoId=abc123</div>',
            )

        with self.assertRaisesRegex(VideoResolutionError, "no MP4/HLS URL"):
            resolve_vidalytics_settings(
                source_url="https://player.vidalytics.com/settings.json",
                settings={"videoId": "video-123", "url": "https://cdn.vidalytics.com/video.webm"},
            )


if __name__ == "__main__":
    unittest.main()
