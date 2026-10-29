import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from channel_runtime.video_acquisition import MediaCandidate
from channel_runtime.video_transcription import TranscriptionConfig, VideoTranscriptionError, transcribe_audio


def _candidate() -> MediaCandidate:
    return MediaCandidate("https://example.test/page", "https://cdn.test/video.mp4", "vidalytics", "mp4", "video-1")


class VideoTranscriptionTests(unittest.TestCase):
    def test_transcription_writes_provenance_json_and_text_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            audio = root_path / "audio.wav"
            audio.write_bytes(b"audio fixture")

            def fake_runner(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                (output_dir / "audio.txt").write_text("hello from video\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            result = transcribe_audio(
                _candidate(), audio, root_path / "artifacts", config=TranscriptionConfig(language="en"),
                runner=fake_runner, whisper_binary="/usr/bin/whisper",
            )
            payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "generic-video-transcript-v1")
            self.assertEqual(payload["source_url"], "https://example.test/page")
            self.assertEqual(payload["transcript_text"], "hello from video")
            self.assertEqual(payload["media_sha256"], hashlib.sha256(b"audio fixture").hexdigest())
            self.assertEqual(result.transcript_path.read_text(encoding="utf-8"), "hello from video\n")

    def test_missing_whisper_is_typed(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "audio.wav"
            audio.write_bytes(b"audio")
            with self.assertRaisesRegex(VideoTranscriptionError, "not found") as context:
                transcribe_audio(_candidate(), audio, Path(root) / "artifacts", whisper_binary="")
            self.assertEqual(context.exception.code, "whisper-missing")

    def test_timeout_and_empty_output_are_typed(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "audio.wav"
            audio.write_bytes(b"audio")

            def timed_out(*args, **kwargs):
                raise subprocess.TimeoutExpired(args[0], 1)

            with self.assertRaisesRegex(VideoTranscriptionError, "timed out") as context:
                transcribe_audio(_candidate(), audio, root, runner=timed_out, whisper_binary="whisper")
            self.assertEqual(context.exception.code, "transcription-timeout")

            def empty_output(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                (output_dir / "audio.txt").write_text("  ", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with self.assertRaisesRegex(VideoTranscriptionError, "empty transcript") as context:
                transcribe_audio(_candidate(), audio, root, runner=empty_output, whisper_binary="whisper")
            self.assertEqual(context.exception.code, "transcript-empty")

    def test_transcript_limit_rejects_oversized_output(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "audio.wav"
            audio.write_bytes(b"audio")

            def fake_runner(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                (output_dir / "audio.txt").write_text("too long", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with self.assertRaisesRegex(VideoTranscriptionError, "character limit"):
                transcribe_audio(
                    _candidate(), audio, root, config=TranscriptionConfig(max_transcript_chars=3),
                    runner=fake_runner, whisper_binary="whisper",
                )


if __name__ == "__main__":
    unittest.main()
