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

    def test_restart_launches_wrapper_without_dns_import_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pid_file = tmp_path / "channel_runtime.pid"
            log_file = tmp_path / "channel_runtime.log"
            marker_file = tmp_path / "runtime-started.marker"
            shim_log_file = tmp_path / "python-shim.log"
            python_shim = tmp_path / "python-shim.sh"
            python_shim.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        'if [[ "${1:-}" == "-m" && "${2:-}" == "channel_runtime" ]]; then',
                        '  : "${CHANNEL_RUNTIME_SHIM_MARKER:?}"',
                        '  : "${CHANNEL_RUNTIME_SHIM_LOG:?}"',
                        '  touch "$CHANNEL_RUNTIME_SHIM_MARKER"',
                        '  printf "channel_runtime shim started\\n" >> "$CHANNEL_RUNTIME_SHIM_LOG"',
                        '  sleep "${CHANNEL_RUNTIME_SHIM_SLEEP_S:-30}"',
                        "  exit 0",
                        "fi",
                        'exec python3 "$@"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            python_shim.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "CHANNEL_RUNTIME_PID_FILE": str(pid_file),
                    "CHANNEL_RUNTIME_LOG_FILE": str(log_file),
                    "CHANNEL_RUNTIME_CMD": "bash "
                    + str(self.repo_root / "scripts" / "run_channel_runtime_foreground.sh"),
                    "CHANNEL_RUNTIME_PYTHON_BIN": str(python_shim),
                    "CHANNEL_RUNTIME_DNS_PREFLIGHT": "true",
                    "CHANNEL_RUNTIME_DNS_PREFLIGHT_HOSTS": "localhost",
                    "CHANNEL_RUNTIME_DNS_PREFLIGHT_PORT": "443",
                    "CHANNEL_RUNTIME_STARTUP_WAIT_S": "1",
                    "CHANNEL_RUNTIME_SHIM_MARKER": str(marker_file),
                    "CHANNEL_RUNTIME_SHIM_LOG": str(shim_log_file),
                    "CHANNEL_RUNTIME_SHIM_SLEEP_S": "30",
                    "AGENT_SKILLS_ENV_FILE": str(tmp_path / "missing.env"),
                }
            )

            self._run_script(env)
            runtime_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(self._is_running(runtime_pid))
            self.assertTrue(marker_file.exists())
            self.assertNotIn("ModuleNotFoundError", log_file.read_text(encoding="utf-8"))

            os.kill(runtime_pid, signal.SIGTERM)
            self.assertTrue(self._wait_for_not_running(runtime_pid, timeout_s=5.0))

    def test_reports_startup_failure_and_cleans_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pid_file = tmp_path / "channel_runtime.pid"
            log_file = tmp_path / "channel_runtime.log"

            env = os.environ.copy()
            env.update(
                {
                    "CHANNEL_RUNTIME_PID_FILE": str(pid_file),
                    "CHANNEL_RUNTIME_LOG_FILE": str(log_file),
                    "CHANNEL_RUNTIME_CMD": "bash "
                    + str(self.repo_root / "scripts" / "run_channel_runtime_foreground.sh"),
                    "CHANNEL_RUNTIME_PYTHON_BIN": "/bin/false",
                    "CHANNEL_RUNTIME_DNS_PREFLIGHT": "true",
                    "CHANNEL_RUNTIME_STARTUP_WAIT_S": "1",
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
            self.assertIn("channel_runtime failed during startup", result.stderr)
            self.assertFalse(pid_file.exists())

    def test_restart_uses_non_login_shell_wrapper(self) -> None:
        contents = self.script.read_text(encoding="utf-8")
        self.assertIn('nohup bash -c "$RUNTIME_CMD"', contents)
        self.assertNotIn('nohup bash -lc "$RUNTIME_CMD"', contents)

    def test_rotates_oversized_log_without_deleting_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pid_file = tmp_path / "channel_runtime.pid"
            log_file = tmp_path / "channel_runtime.log"
            log_file.write_text("old log payload\n", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "CHANNEL_RUNTIME_PID_FILE": str(pid_file),
                    "CHANNEL_RUNTIME_LOG_FILE": str(log_file),
                    "CHANNEL_RUNTIME_LOG_MAX_BYTES": "1",
                    "CHANNEL_RUNTIME_CMD": 'python3 -c "import time; time.sleep(30)" channel_runtime',
                    "CHANNEL_RUNTIME_STARTUP_WAIT_S": "1",
                }
            )

            result = subprocess.run(
                [str(self.script)],
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            runtime_pid = int(pid_file.read_text(encoding="utf-8").strip())
            rotated_logs = list(tmp_path.glob("channel_runtime.log.*"))
            try:
                self.assertEqual(len(rotated_logs), 1)
                self.assertEqual(rotated_logs[0].read_text(encoding="utf-8"), "old log payload\n")
                self.assertIn("Rotated oversized channel_runtime log", result.stdout)
            finally:
                os.kill(runtime_pid, signal.SIGTERM)
                self.assertTrue(self._wait_for_not_running(runtime_pid, timeout_s=5.0))

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
