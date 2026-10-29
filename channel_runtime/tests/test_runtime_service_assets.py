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

    def test_disk_alert_assets_are_valid_and_hourly(self) -> None:
        wrapper_path = REPO_ROOT / "scripts" / "run_disk_space_alert.sh"
        subprocess.run(
            ["bash", "-n", str(wrapper_path)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        service = (REPO_ROOT / "ops" / "systemd" / "disk-space-alert.service").read_text(encoding="utf-8")
        timer = (REPO_ROOT / "ops" / "systemd" / "disk-space-alert.timer").read_text(encoding="utf-8")
        self.assertIn("run_disk_space_alert.sh", service)
        self.assertIn("Environment=DISK_ALERT_PATH=/mnt/c", service)
        self.assertIn("OnUnitActiveSec=1h", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
