from __future__ import annotations

import argparse
import socket
import sys
from dataclasses import dataclass
from typing import Callable, Sequence


DEFAULT_DNS_PREFLIGHT_HOSTS = (
    "api.telegram.org",
    "openai.com",
    "www.youtube.com",
)
DEFAULT_DNS_PREFLIGHT_PORT = 443


@dataclass(frozen=True)
class DnsResolutionResult:
    host: str
    address: str


class DnsPreflightError(RuntimeError):
    def __init__(self, message: str, *, failures: dict[str, str]) -> None:
        super().__init__(message)
        self.failures = dict(failures)


ResolverFn = Callable[[str, int], list[tuple[object, object, object, object, tuple[object, ...]]]]


def parse_host_list(raw_value: str) -> tuple[str, ...]:
    normalized = str(raw_value).replace(",", " ")
    hosts = tuple(part.strip() for part in normalized.split() if part.strip())
    return hosts or DEFAULT_DNS_PREFLIGHT_HOSTS


def resolve_hosts(
    hosts: Sequence[str],
    *,
    port: int = DEFAULT_DNS_PREFLIGHT_PORT,
    resolver: ResolverFn = socket.getaddrinfo,
) -> tuple[DnsResolutionResult, ...]:
    results: list[DnsResolutionResult] = []
    failures: dict[str, str] = {}
    for raw_host in hosts:
        host = str(raw_host).strip()
        if not host:
            continue
        try:
            resolved = resolver(host, port)
        except OSError as exc:
            failures[host] = f"{type(exc).__name__}: {exc}"
            continue

        address = ""
        for _, _, _, _, sockaddr in resolved:
            if isinstance(sockaddr, tuple) and sockaddr:
                address = str(sockaddr[0]).strip()
                if address:
                    break
        if not address:
            failures[host] = "resolver returned no usable socket address"
            continue
        results.append(DnsResolutionResult(host=host, address=address))

    if failures:
        details = ", ".join(f"{host} -> {error}" for host, error in failures.items())
        raise DnsPreflightError(
            "DNS preflight failed: " + details,
            failures=failures,
        )
    return tuple(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_channel_runtime_dns")
    parser.add_argument(
        "--hosts",
        default=",".join(DEFAULT_DNS_PREFLIGHT_HOSTS),
        help="Comma- or space-separated hostnames to resolve before starting the runtime.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DNS_PREFLIGHT_PORT,
        help="Port passed to socket.getaddrinfo for the resolution probe.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    hosts = parse_host_list(args.hosts)
    try:
        results = resolve_hosts(hosts, port=args.port)
    except DnsPreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for result in results:
        print(f"{result.host}\t{result.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
