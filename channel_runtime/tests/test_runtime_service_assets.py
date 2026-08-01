from __future__ import annotations

import pathlib
import subprocess
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class RuntimeServiceAssetTests(unittest.TestCase):
    def test_foreground_wrapper_has_valid_bash_syntax(self) -> None:
        wrapper_path = REPO_ROOT / "scripts" / "run_channel_runtime_foreground.sh"
        subprocess.run(
            ["bash", "-n", str(wrapper_path)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )

    def test_systemd_service_points_to_foreground_wrapper(self) -> None:
        service_path = REPO_ROOT / "ops" / "systemd" / "telegram-channel-runtime.service"
        contents = service_path.read_text(encoding="utf-8")

        self.assertIn("run_channel_runtime_foreground.sh", contents)
        self.assertIn("Restart=always", contents)
        self.assertIn("network-online.target", contents)


if __name__ == "__main__":
    unittest.main()
