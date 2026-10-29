from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_runtime.video_acquisition import resolve_public_video_page, resolve_vidalytics_settings  # noqa: E402
from channel_runtime.video_media import extract_audio  # noqa: E402
from channel_runtime.video_transcription import transcribe_audio  # noqa: E402


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "generic_video"


class GenericVideoPipelineTests(unittest.TestCase):
    def test_replayable_fixture_preserves_candidate_and_artifact_provenance(self) -> None:
        html = (FIXTURES / "vida_scalpius_landing.html").read_text(encoding="utf-8")
        settings = json.loads((FIXTURES / "vida_player_settings.json").read_text(encoding="utf-8"))
        candidate = resolve_public_video_page(source_url="https://go.scalpius.test/join", html=html)
        settings_candidate = resolve_vidalytics_settings(
            source_url="https://player.vidalytics.com/player.settings.json", settings=settings
        )
        self.assertEqual(candidate.video_id, settings_candidate.video_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "audio.wav"
            audio.write_bytes(b"fixture-audio")

            def fake_ffmpeg(command, **kwargs):
                del kwargs
                Path(command[-1]).write_bytes(b"wav")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            extracted = extract_audio(
                candidate,
                audio,
                root / "extracted.wav",
                runner=fake_ffmpeg,
                ffmpeg_binary="ffmpeg-fixture",
            )

            def fake_whisper(command, **kwargs):
                del kwargs
                output_dir = Path(command[command.index("--output_dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "extracted.txt").write_text("fixture transcript", encoding="utf-8")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            artifact = transcribe_audio(
                candidate,
                extracted.path,
                root / "artifacts",
                runner=fake_whisper,
                whisper_binary="whisper-fixture",
            )
            payload = json.loads(artifact.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "generic-video-transcript-v1")
            self.assertEqual(payload["source_url"], "https://go.scalpius.test/join")
            self.assertEqual(payload["video_id"], "aZ0GbEqBenzAGF5p")
            self.assertEqual(payload["transcript_text"], "fixture transcript")
            self.assertTrue(payload["media_sha256"])


if __name__ == "__main__":
    unittest.main()
