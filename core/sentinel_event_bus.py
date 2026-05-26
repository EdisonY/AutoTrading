"""Append-only sentinel event stream helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.audit_log import write_jsonl_with_daily_shard


EVENT_STREAM_RELATIVE_PATH = Path("runtime") / "sentinel_events.jsonl"


def event_stream_path(root: Path) -> Path:
    return root / EVENT_STREAM_RELATIVE_PATH


def _parse_ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def append_sentinel_events(root: Path, items: list[dict[str, Any]], *, scan_ts: str, source: str = "") -> int:
    path = event_stream_path(root)
    count = 0
    for idx, item in enumerate(items, start=1):
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        row = {
            "ts": scan_ts,
            "event": "SENTINEL_SIGNAL",
            "event_id": f"{scan_ts}|{idx}|{symbol}",
            "symbol": symbol,
            "rank": idx,
            "reason": item.get("reason", ""),
            "source": source,
            "change_pct": item.get("change_pct"),
            "abs_change_pct": item.get("abs_change_pct"),
            "velocity_pct": item.get("velocity_pct"),
            "abs_velocity_pct": item.get("abs_velocity_pct"),
            "quote_volume": item.get("quote_volume"),
            "volume_delta": item.get("volume_delta"),
            "last_price": item.get("last_price"),
        }
        write_jsonl_with_daily_shard(path, row)
        count += 1
    return count


def load_recent_sentinel_events(
    root: Path,
    *,
    max_age_sec: int = 180,
    limit: int = 40,
    max_lines: int = 1200,
) -> list[dict[str, Any]]:
    path = event_stream_path(root)
    if not path.exists():
        return []
    try:
        lines = _read_tail_lines(path, max_lines)
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts = _parse_ts(row.get("ts"))
        if not ts:
            continue
        age = (now - ts).total_seconds()
        if age < 0 or age > max_age_sec:
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol.endswith("USDT") or symbol in seen:
            continue
        seen.add(symbol)
        out.append(row)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _read_tail_lines(path: Path, max_lines: int, block_size: int = 65536) -> list[str]:
    """Read at most the last N lines without loading a growing event file."""
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        chunks: list[bytes] = []
        line_count = 0
        while pos > 0 and line_count <= max_lines:
            size = min(block_size, pos)
            pos -= size
            f.seek(pos)
            chunk = f.read(size)
            chunks.append(chunk)
            line_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return data.splitlines()[-max_lines:]
