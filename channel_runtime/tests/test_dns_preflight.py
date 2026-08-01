from __future__ import annotations

import subprocess
import socket
import unittest
from pathlib import Path

from channel_runtime.dns_preflight import (
    DEFAULT_DNS_PREFLIGHT_HOSTS,
    DnsPreflightError,
    parse_host_list,
    resolve_hosts,
)


class DnsPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.script = self.repo_root / "scripts" / "check_channel_runtime_dns.py"

    def test_parse_host_list_accepts_comma_and_space_delimiters(self) -> None:
        hosts = parse_host_list("api.telegram.org, openai.com www.youtube.com")
        self.assertEqual(
            hosts,
            ("api.telegram.org", "openai.com", "www.youtube.com"),
        )

    def test_parse_host_list_falls_back_to_default_hosts(self) -> None:
        self.assertEqual(parse_host_list(" ,  "), DEFAULT_DNS_PREFLIGHT_HOSTS)

    def test_resolve_hosts_returns_first_resolved_address_per_host(self) -> None:
        def resolver(host: str, port: int) -> list[tuple[object, object, object, object, tuple[object, ...]]]:
            self.assertEqual(port, 443)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.11", port)),
            ]

        results = resolve_hosts(("api.telegram.org",), resolver=resolver)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].host, "api.telegram.org")
        self.assertEqual(results[0].address, "203.0.113.10")

    def test_resolve_hosts_raises_with_failure_details(self) -> None:
        def resolver(host: str, port: int) -> list[tuple[object, object, object, object, tuple[object, ...]]]:
            raise socket.gaierror(-3, "Temporary failure in name resolution")

        with self.assertRaises(DnsPreflightError) as ctx:
            resolve_hosts(("api.telegram.org", "www.youtube.com"), resolver=resolver)
        self.assertIn("api.telegram.org", ctx.exception.failures)
        self.assertIn("www.youtube.com", ctx.exception.failures)
        self.assertIn("Temporary failure in name resolution", str(ctx.exception))

    def test_check_channel_runtime_dns_script_runs_as_direct_file(self) -> None:
        result = subprocess.run(
            ["python3", str(self.script), "--hosts", "localhost", "--port", "443"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("localhost", result.stdout)


if __name__ == "__main__":
    unittest.main()
