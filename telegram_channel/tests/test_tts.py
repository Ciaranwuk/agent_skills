from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from channel_core.contracts import OutboundMessage
from telegram_channel.tts import TelegramTextToSpeechProcessor, TextToSpeechConfig, TextToSpeechSynthesisError


class TestTelegramTextToSpeechProcessor(unittest.TestCase):
    def test_supports_reads_metadata_flag(self) -> None:
        processor = TelegramTextToSpeechProcessor(config=TextToSpeechConfig())
        outbound = OutboundMessage(chat_id="1", text="hello", metadata={"telegram_tts": {"enabled": True}})
        self.assertTrue(processor.supports(outbound))

    def test_synthesize_requires_command(self) -> None:
        processor = TelegramTextToSpeechProcessor(config=TextToSpeechConfig(enabled=True, command=""))
        outbound = OutboundMessage(chat_id="1", text="hello", metadata={"telegram_tts": {"enabled": True}})
        with self.assertRaises(TextToSpeechSynthesisError) as ctx:
            processor.synthesize(outbound)
        self.assertEqual(ctx.exception.code, "tts-command-missing")

    def test_synthesize_runs_helper_and_reads_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            helper = Path(tmpdir) / "helper.py"
            helper.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "import argparse",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--text-file', required=True)",
                        "parser.add_argument('--output', required=True)",
                        "parser.add_argument('--voice', default='')",
                        "parser.add_argument('--language', default='')",
                        "args = parser.parse_args()",
                        "text = Path(args.text_file).read_text(encoding='utf-8')",
                        "Path(args.output).write_bytes(b'OggS' + text.encode('utf-8'))",
                    ]
                ),
                encoding="utf-8",
            )
            processor = TelegramTextToSpeechProcessor(
                config=TextToSpeechConfig(
                    enabled=True,
                    command=f"{sys.executable} {helper}",
                    temp_dir=tmpdir,
                )
            )
            outbound = OutboundMessage(chat_id="1", text="hello world", metadata={"telegram_tts": {"enabled": True}})
            synthesized = processor.synthesize(outbound)
            self.assertEqual(synthesized.audio_bytes, b"OggShello world")
            self.assertEqual(synthesized.filename, "speech.ogg")

    def test_synthesize_rejects_non_ogg_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            helper = Path(tmpdir) / "helper.py"
            helper.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "import argparse",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--text-file', required=True)",
                        "parser.add_argument('--output', required=True)",
                        "args = parser.parse_args()",
                        "Path(args.output).write_bytes(b'not ogg')",
                    ]
                ),
                encoding="utf-8",
            )
            processor = TelegramTextToSpeechProcessor(
                config=TextToSpeechConfig(
                    enabled=True,
                    command=f"{sys.executable} {helper}",
                    temp_dir=tmpdir,
                )
            )
            outbound = OutboundMessage(chat_id="1", text="hello world", metadata={"telegram_tts": {"enabled": True}})

            with self.assertRaises(TextToSpeechSynthesisError) as ctx:
                processor.synthesize(outbound)

            self.assertEqual(ctx.exception.code, "tts-output-invalid")


if __name__ == "__main__":
    unittest.main()
