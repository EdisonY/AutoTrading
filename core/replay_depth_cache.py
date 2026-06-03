"""Local depth snapshot lookup for report-only replay fills.

The loader is read-only and never calls Binance. It can consume the latest
runtime depth cache plus accumulated research_store/depth_snapshots partitions.
"""

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
    "snapshot_time",
    "snapshotTime",
    "snapshot_time_ms",
    "time_ms",
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
            Path(root) / "research_store",
            Path(root) / "server_logs_tencent" / "research_store",
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


def parse_json_levels(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def normalize_order_book(payload: dict[str, Any]) -> dict[str, Any] | None:
    bids = parse_json_levels(payload.get("bids"))
    asks = parse_json_levels(payload.get("asks"))
    if not isinstance(bids, list):
        bids = parse_json_levels(payload.get("bids_json"))
    if not isinstance(asks, list):
        asks = parse_json_levels(payload.get("asks_json"))
    if not isinstance(bids, list) and not isinstance(asks, list):
        data = payload.get("data")
        if isinstance(data, dict):
            bids = parse_json_levels(data.get("bids"))
            asks = parse_json_levels(data.get("asks"))
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


def partition_day(path: Path) -> str:
    for part in path.parts:
        if part.startswith("date="):
            return part.split("=", 1)[1]
    return ""


def research_depth_files(store_or_table: Path, entry_ts: datetime, max_age_seconds: float) -> list[Path]:
    table = store_or_table if store_or_table.name == "depth_snapshots" else store_or_table / "depth_snapshots"
    if not table.exists():
        return []
    files = sorted([*table.glob("date=*/data.jsonl"), *table.glob("date=*/data.parquet")])
    if not files:
        return []
    day_window = max(1, int(float(max_age_seconds) // 86_400) + 2)
    start_day = entry_ts.date().toordinal() - day_window
    end_day = entry_ts.date().toordinal() + day_window
    filtered: list[Path] = []
    for path in files:
        day = partition_day(path)
        if not day:
            filtered.append(path)
            continue
        try:
            ordinal = datetime.fromisoformat(day).date().toordinal()
        except Exception:
            filtered.append(path)
            continue
        if start_day <= ordinal <= end_day:
            filtered.append(path)
    return filtered


def read_jsonl_payloads(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def read_parquet_payloads(path: Path) -> Iterable[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError:
        return
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return
    if frame.empty:
        return
    frame = frame.where(pd.notnull(frame), None)
    for row in frame.to_dict(orient="records"):
        if isinstance(row, dict):
            yield row


def iter_research_snapshot_payloads(cache_dirs: Iterable[Path], entry_ts: datetime, max_age_seconds: float) -> Iterable[tuple[Path, dict[str, Any]]]:
    seen: set[Path] = set()
    for cache_dir in cache_dirs:
        for path in research_depth_files(Path(cache_dir), entry_ts, max_age_seconds):
            if path in seen:
                continue
            seen.add(path)
            reader = read_parquet_payloads if path.suffix == ".parquet" else read_jsonl_payloads
            for item in reader(path):
                yield path, item


def apply_snapshot_candidate(
    best: DepthSnapshot | None,
    *,
    sym: str,
    side: str,
    entry_utc: datetime,
    path: Path,
    item: dict[str, Any],
    max_age_seconds: float,
) -> DepthSnapshot | None:
    item_symbol = normalize_symbol(item.get("symbol") or item.get("s") or sym)
    if item_symbol and item_symbol != sym:
        return best
    book = normalize_order_book(item)
    if book is None or not has_fillable_side(book, side):
        return best
    ts = snapshot_time(item, path)
    if ts is None:
        return best
    age = abs((entry_utc - ts.astimezone(timezone.utc)).total_seconds())
    if age > float(max_age_seconds):
        return best
    snapshot = DepthSnapshot(symbol=sym, ts=ts.astimezone(timezone.utc), order_book=book, source=str(path), age_seconds=age)
    if best is None or snapshot.age_seconds < best.age_seconds:
        return snapshot
    return best


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
            best = apply_snapshot_candidate(
                best,
                sym=sym,
                side=side,
                entry_utc=entry_utc,
                path=path,
                item=item,
                max_age_seconds=max_age_seconds,
            )
    for path, item in iter_research_snapshot_payloads(cache_dirs, entry_utc, max_age_seconds):
        best = apply_snapshot_candidate(
            best,
            sym=sym,
            side=side,
            entry_utc=entry_utc,
            path=path,
            item=item,
            max_age_seconds=max_age_seconds,
        )
    return best
