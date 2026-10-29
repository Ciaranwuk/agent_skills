"""Small Telegram-native renderer for the archived V3 BTC trade test.

It deliberately uses only the Python standard library so the channel runtime does
not acquire a plotting dependency.  The HTML viewer already contains the exact
candles and trade ledger, so this renderer cannot silently use a different dataset.
"""
from __future__ import annotations

import csv
import json
import math
import re
import struct
import tempfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

RUN_DIR = Path("/mnt/d/crypto_fund_agents/backtest_outputs/video_trendline_single_asset_v3/single_asset_20260924T212021Z")
HTML_PATH = RUN_DIR / "trade_viewer.html"
_PAYLOAD_RE = re.compile(r"const D=(\{.*?\});const pct=", re.DOTALL)


def build_trade_response(text: str, *, chat_id: str, message_id: str | None) -> dict[str, Any] | None:
    """Return photo metadata for /trade commands, or None for ordinary messages."""
    parts = text.strip().split()
    if not parts or parts[0].lower() not in {"/trade", "/trades"}:
        return None
    if not HTML_PATH.exists():
        return {"text": "The V3 trade data is not available on this host yet."}
    payload = _load_payload()
    trades = payload.get("t") or []
    if not trades:
        return {"text": "The V3 trade ledger is empty."}
    index = 0
    if len(parts) > 1:
        command = parts[1].lower()
        if command in {"next", "+"}:
            index = 1
        elif command in {"prev", "previous", "-"}:
            index = len(trades) - 1
        else:
            try:
                index = int(command) - 1
            except ValueError:
                return {"text": "Usage: /trade 12 (or /trade next /trade prev)"}
    if not 0 <= index < len(trades):
        return {"text": f"Trade number must be between 1 and {len(trades)}."}
    trade = trades[index]
    image_path = _render(payload, index)
    caption = _caption(trade, index, len(trades))
    return {
        "text": caption,
        "photo_path": str(image_path),
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "◀ Previous", "callback_data": f"trade:{max(0, index - 1) + 1}"},
                {"text": f"{index + 1}/{len(trades)}", "callback_data": f"trade:{index + 1}"},
                {"text": "Next ▶", "callback_data": f"trade:{min(len(trades) - 1, index + 1) + 1}"},
            ]]
        },
    }


def _load_payload() -> dict[str, Any]:
    match = _PAYLOAD_RE.search(HTML_PATH.read_text())
    if not match:
        raise ValueError("could not find embedded trade viewer payload")
    return json.loads(match.group(1))


def _render(payload: Mapping[str, Any], index: int) -> Path:
    candles = payload["c"]
    trade = payload["t"][index]
    dates = candles["ts"]
    entry = dates.index(trade["entry_ts"])
    exit = dates.index(trade["exit_ts"])
    pad = max(14, (exit - entry) // 2)
    start, end = max(0, entry - pad), min(len(dates) - 1, exit + pad)
    values = candles["h"][start:end + 1] + candles["l"][start:end + 1]
    line = lambda i: trade["action_line_at_entry"] + (i - entry) * trade["action_slope"]
    values += [trade["entry"], trade["exit"], trade["stop"], line(start), line(end)]
    lo, hi = min(values), max(values)
    w, h = 1200, 760
    pixels = bytearray([245, 247, 250] * (w * h))
    def xy(i: int, price: float) -> tuple[int, int]:
        x = 70 + int((i - start) * (w - 100) / max(1, end - start))
        y = 60 + int((hi - price) * (h - 120) / max(1e-9, hi - lo))
        return x, y
    _line(pixels, w, h, (70, 80, 95), 70, 60, w - 30, 60)
    _line(pixels, w, h, (70, 80, 95), 70, h - 60, w - 30, h - 60)
    for i in range(start, end + 1):
        x, yo = xy(i, candles["o"][i]); _, yc = xy(i, candles["c"][i])
        _, yh = xy(i, candles["h"][i]); _, yl = xy(i, candles["l"][i])
        color = (8, 127, 91) if candles["c"][i] >= candles["o"][i] else (201, 42, 42)
        _line(pixels, w, h, color, x, yh, x, yl)
        _rect(pixels, w, h, color, x - 3, min(yo, yc), x + 3, max(yo, yc))
    _line(pixels, w, h, (8, 145, 178), *xy(start, line(start)), *xy(end, line(end)), width=4)
    _line(pixels, w, h, (239, 68, 68), *xy(start, trade["stop"]), *xy(end, trade["stop"]), dashed=True)
    _dot(pixels, w, h, (124, 58, 237), *xy(entry, trade["entry"]), radius=8)
    _dot(pixels, w, h, (124, 58, 237), *xy(exit, trade["exit"]), radius=8)
    fd, name = tempfile.mkstemp(prefix=f"trade_{index + 1}_", suffix=".png")
    Path(name).write_bytes(_png(w, h, pixels))
    import os
    os.close(fd)
    return Path(name)


def _caption(t: Mapping[str, Any], index: int, total: int) -> str:
    side = t.get("side_name", "Long" if t.get("side") == 1 else "Short")
    result = t.get("result", "Win" if float(t["return"]) > 0 else "Loss")
    return (f"V3 BTCUSD · Trade #{index + 1}/{total} · {side} · {result}\n"
            f"Entry {t['entry_ts'][:10]} ${float(t['entry']):,.2f} → "
            f"Exit {t['exit_ts'][:10]} ${float(t['exit']):,.2f}\n"
            f"Return {float(t['return']) * 100:+.2f}% · {t['hold_bars']} bars · Exit: {t['reason']}\n"
            f"Blue = action/safety line · red dotted = catastrophic ATR stop")


def _set(p: bytearray, w: int, x: int, y: int, c: tuple[int, int, int]) -> None:
    if 0 <= x < w and 0 <= y < len(p) // (w * 3):
        o = (y * w + x) * 3; p[o:o + 3] = bytes(c)

def _line(p: bytearray, w: int, h: int, c: tuple[int, int, int], x0: int, y0: int, x1: int, y1: int, width: int = 1, dashed: bool = False) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for n in range(steps + 1):
        if dashed and (n // 6) % 2: continue
        x = round(x0 + (x1 - x0) * n / steps); y = round(y0 + (y1 - y0) * n / steps)
        for dx in range(-width // 2, width // 2 + 1):
            for dy in range(-width // 2, width // 2 + 1): _set(p, w, x + dx, y + dy, c)

def _rect(p: bytearray, w: int, h: int, c: tuple[int, int, int], x0: int, y0: int, x1: int, y1: int) -> None:
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1): _set(p, w, x, y, c)

def _dot(p: bytearray, w: int, h: int, c: tuple[int, int, int], x: int, y: int, radius: int) -> None:
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius: _set(p, w, x + dx, y + dy, c)

def _png(w: int, h: int, pixels: bytearray) -> bytes:
    raw = b"".join(b"\x00" + bytes(pixels[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
