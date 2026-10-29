import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from channel_runtime.video_acquisition import MediaCandidate
from channel_runtime.video_media import MediaConfig, VideoMediaError, download_media, extract_audio


class _Response:
    def __init__(self, body: bytes, content_length: str | None = None):
        self._body = body
        self.headers = {"Content-Length": content_length} if content_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int) -> bytes:
        body, self._body = self._body[:size], self._body[size:]
        return body


class _HLSResponse(_Response):
    pass


def _candidate(media_type="mp4"):
    suffix = ".m3u8" if media_type == "hls" else ".mp4"
    return MediaCandidate("https://example.test/page", f"https://cdn.test/video{suffix}", "vidalytics", media_type)


class VideoMediaTests(unittest.TestCase):
    def test_download_streams_to_destination_with_limit(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "media.mp4"
            result = download_media(
                _candidate(), destination, config=MediaConfig(max_download_bytes=5, chunk_size=2, allowed_media_hosts=("cdn.test",)),
                opener=lambda request, timeout: _Response(b"hello", "5"),
            )
            self.assertEqual(result.bytes_written, 5)
            self.assertEqual(destination.read_bytes(), b"hello")

    def test_download_rejects_content_length_before_writing(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(VideoMediaError, "Content-Length"):
                download_media(
                    _candidate(), Path(root) / "media.mp4", config=MediaConfig(max_download_bytes=4, allowed_media_hosts=("cdn.test",)),
                    opener=lambda request, timeout: _Response(b"hello", "5"),
                )

    def test_download_rejects_stream_that_grows_past_limit(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(VideoMediaError, "byte limit"):
                download_media(
                    _candidate(), Path(root) / "media.mp4", config=MediaConfig(max_download_bytes=4, allowed_media_hosts=("cdn.test",)),
                    opener=lambda request, timeout: _Response(b"hello"),
                )

    def test_extract_audio_uses_duration_limit_and_checks_output(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "audio.wav"

            def fake_runner(command, **kwargs):
                destination.write_bytes(b"RIFF")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            result = extract_audio(_candidate(), Path(root) / "media.mp4", destination,
                                   config=MediaConfig(max_duration_s=42, allowed_media_hosts=("cdn.test",)), runner=fake_runner,
                                   ffmpeg_binary="/usr/bin/ffmpeg")
            self.assertEqual(result.command[result.command.index("-t") + 1], "42")
            self.assertEqual(destination.read_bytes(), b"RIFF")

    def test_missing_ffmpeg_is_typed(self):
        with patch("channel_runtime.video_media.shutil.which", return_value=None):
            with self.assertRaisesRegex(VideoMediaError, "not found"):
                extract_audio(_candidate(), "media.mp4", "audio.wav", config=MediaConfig(allowed_media_hosts=("cdn.test",)))

    def test_host_policy_rejects_unapproved_media_host(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(VideoMediaError, "allowlist"):
                download_media(_candidate(), Path(root) / "media.mp4", opener=lambda request, timeout: _Response(b"x"))

    def test_hls_uses_on_demand_loopback_proxy_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "audio.wav"
            seen = {}

            resources = {
                "https://cdn.test/video.m3u8": b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n",
                "https://cdn.test/segment.ts": b"segment-bytes",
            }

            def fake_opener(request, timeout):
                return _HLSResponse(resources[request.full_url])

            def fake_runner(command, **kwargs):
                seen["input"] = command[command.index("-i") + 1]
                seen["whitelist"] = command[command.index("-protocol_whitelist") + 1]
                self.assertTrue(seen["input"].startswith("http://127.0.0.1:"))
                self.assertNotIn("mission-control-hls-", seen["input"])
                destination.write_bytes(b"RIFF")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            extract_audio(_candidate("hls"), _candidate("hls").media_url, destination,
                          config=MediaConfig(allowed_media_hosts=("cdn.test",)), runner=fake_runner,
                          ffmpeg_binary="/usr/bin/ffmpeg", opener=fake_opener)
            self.assertNotEqual(seen["input"], "https://cdn.test/video.m3u8")
            self.assertEqual(seen["whitelist"], "http,https,tcp,tls,crypto,data")

    def test_hls_proxy_rewrites_playlist_without_segment_files(self):
        with tempfile.TemporaryDirectory() as root:
            from channel_runtime.video_media import _HLSProxy

            resources = {
                "https://cdn.test/video.m3u8": b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n",
                "https://cdn.test/segment.ts": b"segment-bytes",
            }
            proxy = _HLSProxy(
                "https://cdn.test/video.m3u8",
                config=MediaConfig(allowed_media_hosts=("cdn.test",)),
                opener=lambda request, timeout: _HLSResponse(resources[request.full_url]),
            )
            proxy.start()
            try:
                import urllib.request

                manifest = urllib.request.urlopen(proxy.url, timeout=2).read().decode()
                self.assertIn("http://127.0.0.1:", manifest)
                self.assertNotIn("\nsegment.ts\n", manifest)
                segment_url = manifest.splitlines()[-1]
                self.assertEqual(urllib.request.urlopen(segment_url, timeout=2).read(), b"segment-bytes")
                self.assertEqual(list(Path(root).iterdir()), [])
            finally:
                proxy.close()


if __name__ == "__main__":
    unittest.main()
