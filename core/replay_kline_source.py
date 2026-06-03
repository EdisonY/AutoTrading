"""Local Kline loaders for replay/report tools.

This module is read-only and never calls Binance. It prefers accumulated
research_store/klines partitions, then callers can fall back to runtime caches.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace("/", "").replace("-", "")


def normalize_timeframe(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.isdigit():
        return f"{text}m"
    return text


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, CST)
        except Exception:
            return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def partition_day(path: Path) -> str:
    for part in path.parts:
        if part.startswith("date="):
            return part.split("=", 1)[1]
    return ""


def candidate_store_dirs(root: Path, store_dir: Path | None = None) -> list[Path]:
    candidates = []
    if store_dir is not None:
        candidates.append(store_dir)
    candidates.extend([root / "research_store", root / "server_logs_tencent" / "research_store"])
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def research_kline_files(store: Path, start: datetime | None = None, end: datetime | None = None) -> list[Path]:
    table = store / "klines"
    files = sorted([*table.glob("date=*/data.jsonl"), *table.glob("date=*/data.parquet")])
    if not files or start is None or end is None:
        return files
    start_day = (start.astimezone(CST) - timedelta(days=1)).date().isoformat()
    end_day = (end.astimezone(CST) + timedelta(days=1)).date().isoformat()
    return [path for path in files if start_day <= partition_day(path) <= end_day]


def row_open_time_ms(row: dict[str, Any]) -> int:
    value = to_int(row.get("open_time_ms"))
    if value > 0:
        return value
    dt = parse_dt(row.get("open_time"))
    return int(dt.timestamp() * 1000) if dt else 0


def row_to_kline(row: dict[str, Any]) -> list[Any] | None:
    open_time_ms = row_open_time_ms(row)
    if open_time_ms <= 0:
        return None
    close_time_ms = to_int(row.get("close_time_ms"))
    return [
        open_time_ms,
        to_float(row.get("open")),
        to_float(row.get("high")),
        to_float(row.get("low")),
        to_float(row.get("close")),
        to_float(row.get("volume")),
        close_time_ms,
        to_float(row.get("quote_volume")),
    ]


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError:
        return []
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return []
    if frame.empty:
        return []
    frame = frame.where(pd.notnull(frame), None)
    return [dict(row) for row in frame.to_dict(orient="records")]


def load_research_store_kline_rows(
    root: Path,
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    store_dir: Path | None = None,
) -> tuple[list[list[Any]], str]:
    wanted_symbol = normalize_symbol(symbol)
    wanted_interval = normalize_timeframe(timeframe)
    if not wanted_symbol or not wanted_interval:
        return [], ""

    start_ms = int(start.timestamp() * 1000) if start else None
    end_ms = int(end.timestamp() * 1000) if end else None
    for store in candidate_store_dirs(root, store_dir):
        rows: list[list[Any]] = []
        for path in research_kline_files(store, start, end):
            raw_rows = read_parquet_rows(path) if path.suffix == ".parquet" else read_jsonl_rows(path)
            for row in raw_rows:
                if normalize_symbol(row.get("symbol")) != wanted_symbol:
                    continue
                if normalize_timeframe(row.get("interval")) != wanted_interval:
                    continue
                kline = row_to_kline(row)
                if not kline:
                    continue
                open_ms = to_int(kline[0])
                if start_ms is not None and open_ms < start_ms:
                    continue
                if end_ms is not None and open_ms > end_ms:
                    continue
                rows.append(kline)
        if rows:
            deduped = {to_int(row[0]): row for row in rows}
            ordered = [deduped[key] for key in sorted(deduped)]
            return ordered, f"{store}/klines ({len(ordered)} rows)"
    return [], ""
