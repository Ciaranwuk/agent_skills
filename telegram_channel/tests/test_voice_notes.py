from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import InboundMessage
from telegram_channel.voice_notes import TelegramVoiceNoteProcessor, VoiceNoteConfig, VoiceNoteProcessingError


class _ApiStub:
    def __init__(self) -> None:
        self.file_ids: list[str] = []
        self.file_paths: list[str] = []

    def get_file(self, *, file_id: str):
        self.file_ids.append(file_id)
        return {"file_path": "voice/file.ogg"}

    def download_file(self, *, file_path: str):
        self.file_paths.append(file_path)
        return b"audio-bytes"


class TestTelegramVoiceNoteProcessor(unittest.TestCase):
    def test_missing_whisper_binary_returns_explicit_error(self) -> None:
        processor = TelegramVoiceNoteProcessor(_ApiStub(), config=VoiceNoteConfig())
        inbound = InboundMessage(
            update_id="1",
            chat_id="100",
            user_id="200",
            text="[Voice note]",
            metadata={"content_type": "voice", "telegram_voice": {"file_id": "voice-1"}},
        )

        with patch.object(shutil, "which", return_value=None):
            with self.assertRaises(VoiceNoteProcessingError) as ctx:
                processor.transcribe(inbound)

        self.assertEqual(ctx.exception.code, "voice-note-transcriber-missing")
        self.assertIn("Install the `whisper` CLI", ctx.exception.user_message)

    def test_transcribe_downloads_audio_and_returns_bounded_inbound_text(self) -> None:
        api = _ApiStub()
        with tempfile.TemporaryDirectory() as tmp:
            processor = TelegramVoiceNoteProcessor(
                api,
                config=VoiceNoteConfig(
                    whisper_model="tiny",
                    max_chars=40,
                    temp_dir=tmp,
                ),
            )
            inbound = InboundMessage(
                update_id="2",
                chat_id="100",
                user_id="200",
                text="[Voice note]",
                message_id="7",
                metadata={
                    "content_type": "voice",
                    "telegram_voice": {
                        "file_id": "voice-2",
                        "file_unique_id": "uniq-2",
                        "duration_s": 12,
                        "mime_type": "audio/ogg",
                    },
                },
            )

            def fake_run(command, check, capture_output, text, timeout):
                transcript_dir = Path(command[command.index("--output_dir") + 1])
                audio_path = Path(command[1])
                (transcript_dir / f"{audio_path.stem}.txt").write_text(
                    "this transcript is longer than the configured maximum characters",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch.object(shutil, "which", return_value="/usr/bin/whisper"):
                with patch.object(subprocess, "run", side_effect=fake_run) as run_mock:
                    result = processor.transcribe(inbound)

        self.assertEqual(api.file_ids, ["voice-2"])
        self.assertEqual(api.file_paths, ["voice/file.ogg"])
        self.assertTrue(result.text.startswith("[Voice note transcript]\n"))
        self.assertTrue(result.text.endswith("[truncated]"))
        self.assertEqual(result.metadata["voice_note"]["transcript_source"], "whisper-cli")
        self.assertEqual(result.metadata["voice_note"]["whisper_model"], "tiny")
        self.assertTrue(result.metadata["voice_note"]["truncated"])
        self.assertEqual(run_mock.call_count, 1)

    def test_whisper_timeout_is_mapped_to_deterministic_user_message(self) -> None:
        processor = TelegramVoiceNoteProcessor(_ApiStub(), config=VoiceNoteConfig(transcribe_timeout_s=1.0))
        inbound = InboundMessage(
            update_id="3",
            chat_id="100",
            user_id="200",
            text="[Voice note]",
            metadata={"content_type": "voice", "telegram_voice": {"file_id": "voice-3"}},
        )

        with patch.object(shutil, "which", return_value="/usr/bin/whisper"):
            with patch.object(
                subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=["whisper"], timeout=1.0),
            ):
                with self.assertRaises(VoiceNoteProcessingError) as ctx:
                    processor.transcribe(inbound)

        self.assertEqual(ctx.exception.code, "voice-note-transcription-timeout")
        self.assertIn("timed out", ctx.exception.user_message)


if __name__ == "__main__":
    unittest.main()
