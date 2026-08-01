from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render text into an OGG voice file for Telegram runtime TTS.")
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--voice", default="")
    parser.add_argument("--language", default="")
    args = parser.parse_args()

    text_path = Path(args.text_file)
    output_path = Path(args.output)
    if not text_path.exists():
        raise SystemExit(f"text file does not exist: {text_path}")

    engine = shutil.which("espeak-ng") or shutil.which("espeak")
    if engine is None:
        raise SystemExit("missing speech engine: install espeak-ng or espeak")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("missing ffmpeg: install ffmpeg so Telegram TTS can produce OGG output")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_path.parent)) as tmp_dir:
        wav_path = Path(tmp_dir) / "speech.wav"
        command = [engine, "-f", str(text_path), "-w", str(wav_path)]
        voice = str(args.voice).strip()
        if voice:
            command.extend(["-v", voice])
        subprocess.run(command, check=True, capture_output=True, text=True)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(wav_path),
                "-c:a",
                "libopus",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
