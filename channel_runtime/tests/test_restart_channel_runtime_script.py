from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class TestRestartChannelRuntimeScript(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.script = self.repo_root / "scripts" / "restart_channel_runtime.sh"

    def test_restart_replaces_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pid_file = tmp_path / "channel_runtime.pid"
            log_file = tmp_path / "channel_runtime.log"
            marker = "restart_script_test_marker"

            env = os.environ.copy()
            env.update(
                {
                    "CHANNEL_RUNTIME_PID_FILE": str(pid_file),
                    "CHANNEL_RUNTIME_LOG_FILE": str(log_file),
                    "CHANNEL_RUNTIME_PROCESS_MATCH": marker,
                    "CHANNEL_RUNTIME_STOP_WAIT_S": "2",
                    "CHANNEL_RUNTIME_CMD": 'python3 -c "import time; time.sleep(30)" ' + marker,
                }
            )

            self._run_script(env)
            first_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(self._is_running(first_pid))

            self._run_script(env)
            second_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(self._is_running(second_pid))
            self.assertNotEqual(first_pid, second_pid)
            self.assertTrue(self._wait_for_not_running(first_pid, timeout_s=5.0))

            os.kill(second_pid, signal.SIGTERM)
            self.assertTrue(self._wait_for_not_running(second_pid, timeout_s=5.0))

    def test_refuses_to_stop_non_matching_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pid_file = tmp_path / "channel_runtime.pid"
            log_file = tmp_path / "channel_runtime.log"

            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                pid_file.write_text(str(sleeper.pid), encoding="utf-8")

                env = os.environ.copy()
                env.update(
                    {
                        "CHANNEL_RUNTIME_PID_FILE": str(pid_file),
                        "CHANNEL_RUNTIME_LOG_FILE": str(log_file),
                        "CHANNEL_RUNTIME_PROCESS_MATCH": "channel_runtime",
                        "CHANNEL_RUNTIME_CMD": 'python3 -c "import time; time.sleep(1)"',
                    }
                )

                result = subprocess.run(
                    [str(self.script)],
                    cwd=str(self.repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to stop PID", result.stderr)
                self.assertIsNone(sleeper.poll())
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def _run_script(self, env: dict[str, str]) -> None:
        subprocess.run(
            [str(self.script)],
            cwd=str(self.repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    @staticmethod
    def _is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def _wait_for_not_running(pid: int, *, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


if __name__ == "__main__":
    unittest.main()
