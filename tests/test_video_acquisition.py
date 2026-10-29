import unittest

from channel_runtime.video_acquisition import (
    VideoResolutionError,
    resolve_public_video_page,
    resolve_vidalytics_settings,
)


class VideoAcquisitionTests(unittest.TestCase):
    def test_resolves_vidalytics_page_with_direct_media_fixture(self):
        html = '''
        <script>
          window.player = {"videoId":"aZ0GbEqBenzAGF5p",
            "media":"https://cdn.example.test/video/aZ0GbEqBenzAGF5p.mp4"};
          // https://fast.vidalytics.com/player.settings.json
        </script>
        '''
        candidate = resolve_public_video_page(
            source_url="https://go.example.test/join",
            html=html,
        )
        self.assertEqual(candidate.provider, "vidalytics")
        self.assertEqual(candidate.media_type, "mp4")
        self.assertEqual(candidate.video_id, "aZ0GbEqBenzAGF5p")

    def test_resolves_hls_from_settings(self):
        candidate = resolve_vidalytics_settings(
            source_url="https://go.example.test/join",
            settings={"videoId": "demo", "streams": {"hls": "https://cdn.example.test/demo.m3u8"}},
        )
        self.assertEqual(candidate.media_type, "hls")
        self.assertTrue(candidate.media_url.endswith("demo.m3u8"))

    def test_rejects_unknown_provider(self):
        with self.assertRaisesRegex(VideoResolutionError, "supported public video provider"):
            resolve_public_video_page(source_url="https://example.test", html="<video src='x.mp4'>")

    def test_requires_media_candidate(self):
        with self.assertRaisesRegex(VideoResolutionError, "direct MP4/HLS"):
            resolve_public_video_page(
                source_url="https://go.example.test/join",
                html='<script>var embed = "https://fast.vidalytics.com/player.js";</script>',
            )


if __name__ == "__main__":
    unittest.main()
