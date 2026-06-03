"""Local depth-cache lookup for report-only replay fills."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DepthSnapshot:
    symbol: str
    ts: datetime
    order_book: dict[str, Any]
    source: str
    age_seconds: float


TIME_KEYS = (
    "ts",
    "time",
    "timestamp",
    "event_time",
    "eventTime",
    "captured_at",
    "updated_at",
    "last_update_time",
    "E",
    "T",
)


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().replace("/", "").replace("-", "")


def default_depth_cache_dirs(root: Path, extra_dirs: Iterable[Path] | None = None) -> list[Path]:
    dirs: list[Path] = []
    if extra_dirs:
        dirs.extend(Path(path) for path in extra_dirs)
    dirs.extend(
        [
            Path(root) / "runtime" / "depth_cache",
            Path(root) / "server_logs_tencent" / "runtime" / "depth_cache",
        ]
    )
    out: list[Path] = []
    for path in dirs:
        resolved = Path(path)
        if resolved not in out:
            out.append(resolved)
    return out


def parse_snapshot_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def snapshot_time(payload: dict[str, Any], path: Path) -> datetime | None:
    for key in TIME_KEYS:
        dt = parse_snapshot_ts(payload.get(key))
        if dt:
            return dt
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def has_fillable_side(book: dict[str, Any], side: str) -> bool:
    key = "asks" if str(side or "").lower() == "long" else "bids"
    levels = book.get(key)
    if not isinstance(levels, list):
        return False
    for level in levels:
        try:
            if isinstance(level, dict):
                price = float(level.get("price", level.get("p")))
                qty = float(level.get("quantity", level.get("qty", level.get("q"))))
            else:
                price = float(level[0])
                qty = float(level[1])
        except Exception:
            continue
        if price > 0 and qty > 0:
            return True
    return False


def normalize_order_book(payload: dict[str, Any]) -> dict[str, Any] | None:
    bids = payload.get("bids")
    asks = payload.get("asks")
    if not isinstance(bids, list) and not isinstance(asks, list):
        data = payload.get("data")
        if isinstance(data, dict):
            bids = data.get("bids")
            asks = data.get("asks")
    if not isinstance(bids, list) and not isinstance(asks, list):
        return None
    return {
        "bids": bids if isinstance(bids, list) else [],
        "asks": asks if isinstance(asks, list) else [],
    }


def iter_snapshot_payloads(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(payload, dict):
        return
    book = normalize_order_book(payload)
    if book is not None:
        yield payload
    for key in ("snapshots", "rows", "depths"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict) and normalize_order_book(data) is not None:
        yield data


def candidate_paths(symbol: str, cache_dirs: Iterable[Path]) -> list[Path]:
    sym = normalize_symbol(symbol)
    paths: list[Path] = []
    for cache_dir in cache_dirs:
        path = Path(cache_dir)
        if not path.exists():
            continue
        for pattern in (f"{sym}.json", f"{sym}_latest.json", f"{sym}_*.json"):
            for candidate in path.glob(pattern):
                if candidate.is_file() and candidate not in paths:
                    paths.append(candidate)
    return paths


def load_depth_snapshot(
    symbol: str,
    entry_ts: datetime,
    *,
    side: str,
    cache_dirs: Iterable[Path],
    max_age_seconds: float = 300.0,
) -> DepthSnapshot | None:
    sym = normalize_symbol(symbol)
    if not sym:
        return None
    entry_utc = entry_ts.astimezone(timezone.utc) if entry_ts.tzinfo else entry_ts.replace(tzinfo=timezone.utc)
    best: DepthSnapshot | None = None
    for path in candidate_paths(sym, cache_dirs):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for item in iter_snapshot_payloads(payload):
            item_symbol = normalize_symbol(item.get("symbol") or item.get("s") or sym)
            if item_symbol and item_symbol != sym:
                continue
            book = normalize_order_book(item)
            if book is None or not has_fillable_side(book, side):
                continue
            ts = snapshot_time(item, path)
            if ts is None:
                continue
            age = abs((entry_utc - ts.astimezone(timezone.utc)).total_seconds())
            if age > float(max_age_seconds):
                continue
            snapshot = DepthSnapshot(symbol=sym, ts=ts.astimezone(timezone.utc), order_book=book, source=str(path), age_seconds=age)
            if best is None or snapshot.age_seconds < best.age_seconds:
                best = snapshot
    return best
