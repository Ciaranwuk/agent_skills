#!/usr/bin/env python3
"""Generate dependency-light SVG charts for a backtest run artifact."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
from datetime import datetime
from typing import Any, Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-artifact", required=True, help="Backtest JSON artifact")
    parser.add_argument("--output-dir", required=True, help="Directory for SVG chart outputs")
    parser.add_argument("--prefix", default="", help="Optional filename prefix")
    return parser.parse_args()


def load_json(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run artifact must be a JSON object")
    return payload


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def points_from_equity_curve(curve: Any) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    if not isinstance(curve, list):
        return points
    for item in curve:
        if not isinstance(item, dict):
            continue
        ts = parse_ts(item.get("ts_utc"))
        equity = as_float(item.get("equity_usd"))
        if ts is not None and equity is not None:
            points.append((ts, equity))
    return sorted(points, key=lambda row: row[0])


def points_from_balance_curve(curve: Any) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    if not isinstance(curve, list):
        return points
    for item in curve:
        if not isinstance(item, dict):
            continue
        ts = parse_ts(item.get("ts_utc"))
        balance = as_float(item.get("balance_usd"))
        if ts is not None and balance is not None:
            points.append((ts, balance))
    return sorted(points, key=lambda row: row[0])


def points_from_closed_trades(trades: Any, starting_equity: float) -> list[tuple[datetime, float]]:
    if not isinstance(trades, list):
        return []
    balance = starting_equity
    points: list[tuple[datetime, float]] = []
    for trade in sorted(
        (trade for trade in trades if isinstance(trade, dict)),
        key=lambda trade: str(trade.get("exit_ts_utc", "")),
    ):
        ts = parse_ts(trade.get("exit_ts_utc"))
        pnl = as_float(trade.get("net_pnl_usd"))
        if ts is None or pnl is None:
            continue
        balance += pnl
        points.append((ts, balance))
    return points


def drawdown_points(equity_points: Iterable[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    peak: float | None = None
    out: list[tuple[datetime, float]] = []
    for ts, equity in equity_points:
        peak = equity if peak is None else max(peak, equity)
        drawdown = 0.0 if not peak else ((equity / peak) - 1.0) * 100.0
        out.append((ts, drawdown))
    return out


def scale_points(points: list[tuple[datetime, float]], width: int, height: int) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if len(points) < 2:
        return "", ["not enough points to draw chart"]

    left, right, top, bottom = 70, 24, 24, 48
    t_min = min(ts.timestamp() for ts, _ in points)
    t_max = max(ts.timestamp() for ts, _ in points)
    y_min = min(value for _, value in points)
    y_max = max(value for _, value in points)
    if t_max == t_min:
        t_max += 1.0
        warnings.append("timestamp range was zero")
    if y_max == y_min:
        padding = abs(y_max) * 0.01 or 1.0
        y_min -= padding
        y_max += padding
        warnings.append("value range was zero")

    plot_w = width - left - right
    plot_h = height - top - bottom
    coords: list[str] = []
    for ts, value in points:
        x = left + ((ts.timestamp() - t_min) / (t_max - t_min)) * plot_w
        y = top + (1.0 - ((value - y_min) / (y_max - y_min))) * plot_h
        coords.append(f"{x:.2f},{y:.2f}")
    return " ".join(coords), warnings


def write_svg(path: pathlib.Path, title: str, ylabel: str, points: list[tuple[datetime, float]]) -> list[str]:
    width, height = 960, 360
    polyline, warnings = scale_points(points, width, height)
    values = [value for _, value in points]
    y_min = min(values) if values else 0.0
    y_max = max(values) if values else 0.0
    x_start = points[0][0].date().isoformat() if points else "n/a"
    x_end = points[-1][0].date().isoformat() if points else "n/a"
    safe_title = html.escape(title)
    safe_ylabel = html.escape(ylabel)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{safe_title}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="70" y="18" font-family="Arial, sans-serif" font-size="14" fill="#111827">{safe_title}</text>
  <text x="70" y="340" font-family="Arial, sans-serif" font-size="11" fill="#4b5563">{html.escape(x_start)} to {html.escape(x_end)}</text>
  <text x="8" y="32" font-family="Arial, sans-serif" font-size="11" fill="#4b5563">{safe_ylabel}</text>
  <line x1="70" y1="24" x2="70" y2="312" stroke="#d1d5db"/>
  <line x1="70" y1="312" x2="936" y2="312" stroke="#d1d5db"/>
  <text x="12" y="32" font-family="Arial, sans-serif" font-size="10" fill="#6b7280">{y_max:.2f}</text>
  <text x="12" y="312" font-family="Arial, sans-serif" font-size="10" fill="#6b7280">{y_min:.2f}</text>
  <polyline fill="none" stroke="#2563eb" stroke-width="2" points="{polyline}"/>
</svg>
"""
    path.write_text(svg, encoding="utf-8")
    return warnings


def main() -> int:
    args = parse_args()
    artifact_path = pathlib.Path(args.run_artifact).expanduser().resolve()
    output_dir = pathlib.Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    artifact = load_json(artifact_path)
    prefix = f"{args.prefix}_" if args.prefix else ""
    warnings: list[str] = []

    equity = points_from_equity_curve(artifact.get("equity_curve"))
    if len(equity) < 2:
        raise ValueError("artifact needs at least two equity_curve points with ts_utc and equity_usd")

    metrics = artifact.get("metrics") if isinstance(artifact.get("metrics"), dict) else {}
    starting_equity = as_float(metrics.get("starting_equity_usd")) or equity[0][1]
    balance = points_from_balance_curve(artifact.get("balance_curve"))
    balance_source = "balance_curve"
    if len(balance) < 2:
        balance = points_from_closed_trades(artifact.get("trades"), starting_equity)
        balance_source = "closed_trades"
    if len(balance) < 2:
        balance = equity
        balance_source = "equity_proxy"
        warnings.append("balance unavailable; used equity curve as proxy")

    generated: list[str] = []
    for filename, title, ylabel, series in [
        (f"{prefix}equity.svg", "Equity Curve", "equity_usd", equity),
        (f"{prefix}balance.svg", f"Realized Balance ({balance_source})", "balance_usd", balance),
        (f"{prefix}drawdown.svg", "Drawdown", "drawdown_pct", drawdown_points(equity)),
    ]:
        path = output_dir / filename
        warnings.extend(write_svg(path, title, ylabel, series))
        generated.append(str(path))

    manifest = {
        "source_artifact": str(artifact_path),
        "generated_files": generated,
        "equity_points": len(equity),
        "balance_points": len(balance),
        "balance_source": balance_source,
        "warnings": sorted(set(warnings)),
    }
    manifest_path = output_dir / f"{prefix}chart_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
