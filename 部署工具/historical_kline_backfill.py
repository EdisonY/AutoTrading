"""Top30 historical Kline backfill for offline strategy replay.

This tool is deliberately separate from live scanners. It only writes
research_store historical Kline partitions and progress reports. It never calls
Binance, never starts services, never changes strategy cadence, and only sends
public OKX/Bybit market-data requests when ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from core.external_market_data import bybit_interval, bybit_public_get, okx_bar, okx_inst_id, okx_public_get, okx_symbol_supported
from research_kline_features import dedupe_kline_rows, export_dataset, load_existing_rows


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ("15m", "30m", "1h", "4h")
INTERVAL_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
STABLE_OR_WRAPPED_BASES = {
    "USDT",
    "USDC",
    "USDS",
    "USDE",
    "DAI",
    "PYUSD",
    "USD1",
    "USDG",
    "USYC",
    "BUIDL",
    "WBT",
    "WETH",
    "WBTC",
    "XAUT",
    "XAU",
    "XAG",
}
SAFETY = "offline_public_bybit_okx_only_no_binance_no_live_scanner_no_service_restart"
TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "temporarily",
    "network",
    "unreachable",
    "rate_budget",
    "too many",
    "429",
    "500",
    "502",
    "503",
    "504",
    "connection",
    "reset",
    "refused",
)
TERMINAL_UNAVAILABLE_MARKERS = (
    "empty",
    "doesn't exist",
    "does not exist",
    "not supported",
    "unsupported",
    "invalid symbol",
    "symbol is invalid",
    "symbol invalid",
    "instrument id does not exist",
    "symbol not found",
)
PROVIDER_BAR_LIMITS = {
    "bybit": 1000,
    "okx": 300,
}
UNAVAILABLE_TASK_STATUSES = {"unavailable", "provider_empty_or_unsupported"}
PARTIAL_TASK_STATUSES = {"partial_available"}
SYMBOL_UNAVAILABLE_MARKERS = (
    "doesn't exist",
    "does not exist",
    "not supported",
    "unsupported",
    "invalid symbol",
    "symbol is invalid",
    "symbol invalid",
    "instrument id",
    "symbol not found",
)


def now_cst() -> datetime:
    return datetime.now(CST)


def parse_dt(value: Any) -> datetime:
    if not value:
        return now_cst()
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, CST)


def ms_to_iso(ms: int) -> str:
    return ms_to_dt(ms).isoformat(timespec="seconds")


def align_start_ms(ms: int, step_ms: int) -> int:
    if step_ms <= 0:
        return ms
    return ((int(ms) + step_ms - 1) // step_ms) * step_ms


def align_end_ms(ms: int, step_ms: int) -> int:
    if step_ms <= 0:
        return ms
    return (int(ms) // step_ms) * step_ms


def to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def csv_values(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().strip().replace("/", "").replace("-", "")


def base_asset(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def valid_backtest_symbol(symbol: str, available: set[str] | None = None) -> bool:
    symbol = normalize_symbol(symbol)
    base = base_asset(symbol)
    if not symbol.endswith("USDT") or not symbol.isascii() or not base.isalnum():
        return False
    if base in STABLE_OR_WRAPPED_BASES:
        return False
    if available is not None and symbol not in available:
        return False
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def market_cache_candidates(runtime_dir: Path) -> list[Path]:
    return [
        runtime_dir / "market_data_cache.json",
        runtime_dir.parent / "server_logs_tencent" / "runtime" / "market_data_cache.json",
    ]


def load_market_cache(runtime_dir: Path) -> tuple[dict[str, Any], Path | None]:
    for path in market_cache_candidates(runtime_dir):
        cache = read_json(path)
        if cache.get("coingecko_top_symbols") or cache.get("top_symbols") or cache.get("available_symbols"):
            return cache, path
    return {}, None


def choose_top_symbols(runtime_dir: Path, explicit: list[str], top_n: int) -> tuple[list[str], dict[str, Any]]:
    cache, cache_path = load_market_cache(runtime_dir)
    available_values = cache.get("available_symbols") if isinstance(cache.get("available_symbols"), list) else []
    available = {normalize_symbol(item) for item in available_values if normalize_symbol(item)}
    candidates = explicit
    source = "explicit"
    if not candidates:
        candidates = [normalize_symbol(item) for item in (cache.get("coingecko_top_symbols") or [])]
        source = "market_data_cache.coingecko_top_symbols"
    if not candidates:
        candidates = [normalize_symbol(item) for item in (cache.get("top_symbols") or [])]
        source = "market_data_cache.top_symbols_fallback"
    selected: list[str] = []
    rejected: list[str] = []
    for item in candidates:
        symbol = normalize_symbol(item)
        if valid_backtest_symbol(symbol, available if available else None):
            if symbol not in selected:
                selected.append(symbol)
        else:
            rejected.append(symbol)
        if len(selected) >= max(1, int(top_n)):
            break
    return selected, {
        "source": source,
        "available_symbols": len(available),
        "rejected_preview": rejected[:20],
        "cache_ts": cache.get("ts"),
        "cache_path": str(cache_path) if cache_path else "",
    }


def chunk_tasks(symbols: list[str], intervals: list[str], start_ms: int, end_ms: int, limit: int) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for symbol in symbols:
        for interval in intervals:
            step = INTERVAL_MS.get(interval)
            if not step:
                continue
            interval_start_ms = align_start_ms(start_ms, step)
            interval_end_ms = align_end_ms(end_ms, step)
            if interval_start_ms > interval_end_ms:
                continue
            span = step * max(1, int(limit))
            cursor = interval_start_ms
            while cursor <= interval_end_ms:
                chunk_end = min(interval_end_ms, cursor + span - step)
                expected_bars = max(1, int(math.floor((chunk_end - cursor) / step)) + 1)
                tasks.append({
                    "symbol": symbol,
                    "interval": interval,
                    "start_ms": cursor,
                    "end_ms": chunk_end,
                    "expected_bars": expected_bars,
                })
                cursor = chunk_end + step
    return tasks


def read_existing_keys(store: Path, fmt: str) -> set[tuple[str, str, int]]:
    if fmt == "jsonl":
        return read_existing_jsonl_keys(store, "historical_klines")
    rows = load_existing_rows(store, "historical_klines", fmt)
    keys: set[tuple[str, str, int]] = set()
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        interval = str(row.get("interval") or "")
        open_time_ms = to_int(row.get("open_time_ms"))
        if symbol and interval and open_time_ms > 0:
            keys.add((symbol, interval, open_time_ms))
    return keys


def read_existing_jsonl_keys(store: Path, table: str) -> set[tuple[str, str, int]]:
    keys: set[tuple[str, str, int]] = set()
    for path in sorted((store / table).glob("date=*/data.jsonl")):
        for row in read_jsonl_rows(path):
            symbol = normalize_symbol(row.get("symbol"))
            interval = str(row.get("interval") or "")
            open_time_ms = to_int(row.get("open_time_ms"))
            if symbol and interval and open_time_ms > 0:
                keys.add((symbol, interval, open_time_ms))
    return keys


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def write_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)
    return len(rows)


def jsonl_partition_path(store: Path, table: str, day: str) -> Path:
    return store / table / f"date={day}" / "data.jsonl"


def count_jsonl_rows(store: Path, table: str) -> int:
    total = 0
    for path in (store / table).glob("date=*/data.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                total += sum(1 for line in fh if line.strip())
        except Exception:
            continue
    return total


def task_covered(task: dict[str, Any], existing: set[tuple[str, str, int]]) -> bool:
    symbol = str(task["symbol"])
    interval = str(task["interval"])
    step = INTERVAL_MS.get(interval)
    if not step:
        return False
    cursor = int(task["start_ms"])
    end_ms = int(task["end_ms"])
    while cursor <= end_ms:
        if (symbol, interval, cursor) not in existing:
            return False
        cursor += step
    return True


def effective_task_limit(provider_order: list[str], requested_limit: int) -> int:
    caps = [PROVIDER_BAR_LIMITS[item] for item in provider_order if item in PROVIDER_BAR_LIMITS]
    provider_cap = min(caps) if caps else int(requested_limit)
    return max(1, min(max(1, int(requested_limit)), provider_cap))


def task_key(task: dict[str, Any]) -> tuple[str, str, int, int]:
    return (str(task["symbol"]), str(task["interval"]), int(task["start_ms"]), int(task["end_ms"]))


def task_status_path(store: Path) -> Path:
    return store / "historical_kline_task_status" / "data.jsonl"


def unavailable_path(store: Path) -> Path:
    return store / "historical_kline_unavailable" / "data.jsonl"


def read_task_statuses(store: Path) -> dict[tuple[str, str, int, int], str]:
    statuses: dict[tuple[str, str, int, int], str] = {}
    for path in (task_status_path(store), unavailable_path(store)):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            symbol = normalize_symbol(row.get("symbol"))
            interval = str(row.get("interval") or "")
            start_ms = to_int(row.get("start_ms"))
            end_ms = to_int(row.get("end_ms"))
            status = str(row.get("status") or row.get("reason") or "")
            if symbol and interval and start_ms > 0 and end_ms >= start_ms and status:
                statuses[(symbol, interval, start_ms, end_ms)] = status
    return statuses


def append_task_status(store: Path, task: dict[str, Any], status: str, provider: str = "", rows: int = 0, error: str = "") -> None:
    path = task_status_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "symbol": str(task["symbol"]),
        "interval": str(task["interval"]),
        "date": ms_to_iso(int(task["start_ms"]))[:10],
        "start": ms_to_iso(int(task["start_ms"])),
        "end": ms_to_iso(int(task["end_ms"])),
        "start_ms": int(task["start_ms"]),
        "end_ms": int(task["end_ms"]),
        "expected_bars": int(task.get("expected_bars") or 0),
        "status": status,
        "provider": str(provider or ""),
        "rows": int(rows or 0),
        "error": str(error or ""),
        "cache_ts": now_cst().isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_unavailable_task(store: Path, task: dict[str, Any], error: str) -> None:
    append_task_status(store, task, "unavailable", error=error)
    path = unavailable_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "symbol": str(task["symbol"]),
        "interval": str(task["interval"]),
        "date": ms_to_iso(int(task["start_ms"]))[:10],
        "start": ms_to_iso(int(task["start_ms"])),
        "end": ms_to_iso(int(task["end_ms"])),
        "start_ms": int(task["start_ms"]),
        "end_ms": int(task["end_ms"]),
        "reason": "provider_empty_or_unsupported",
        "error": str(error or ""),
        "cache_ts": now_cst().isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def terminal_unavailable_error(message: str) -> bool:
    text = str(message or "").lower()
    if any(marker in text for marker in TRANSIENT_ERROR_MARKERS):
        return False
    return any(marker in text for marker in TERMINAL_UNAVAILABLE_MARKERS)


def symbol_unavailable_error(message: str) -> bool:
    text = str(message or "").lower()
    if any(marker in text for marker in TRANSIENT_ERROR_MARKERS):
        return False
    return any(marker in text for marker in SYMBOL_UNAVAILABLE_MARKERS)


def bybit_kline_window(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int) -> list[list[Any]]:
    payload = bybit_public_get(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": symbol,
            "interval": bybit_interval(interval),
            "start": int(start_ms),
            "end": int(end_ms),
            "limit": int(limit),
        },
        timeout=15,
    )
    rows = []
    step_ms = INTERVAL_MS.get(interval, 60_000)
    for row in ((payload.get("result") or {}).get("list") or []):
        if len(row) < 7:
            continue
        open_ms = int(float(row[0]))
        rows.append([
            str(open_ms),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(open_ms + step_ms - 1),
            str(row[6]),
        ])
    rows.sort(key=lambda item: int(float(item[0])))
    return rows


def okx_kline_window(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int) -> list[list[Any]]:
    if not okx_symbol_supported(symbol):
        return []
    payload = okx_public_get(
        "/api/v5/market/history-candles",
        {
            "instId": okx_inst_id(symbol),
            "bar": okx_bar(interval),
            "after": int(end_ms + 1),
            "limit": min(max(1, int(limit)), 300),
        },
        timeout=15,
    )
    rows = []
    step_ms = INTERVAL_MS.get(interval, 60_000)
    for row in payload.get("data") or []:
        if len(row) < 8:
            continue
        open_ms = int(float(row[0]))
        if not (start_ms <= open_ms <= end_ms):
            continue
        rows.append([
            str(open_ms),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(open_ms + step_ms - 1),
            str(row[7]),
        ])
    rows.sort(key=lambda item: int(float(item[0])))
    return rows


def raw_rows_to_records(symbol: str, interval: str, raw_rows: list[list[Any]], source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_rows:
        if len(raw) < 8:
            continue
        open_ms = to_int(raw[0])
        if open_ms <= 0:
            continue
        records.append({
            "symbol": symbol,
            "interval": interval,
            "date": ms_to_iso(open_ms)[:10],
            "open_time": ms_to_iso(open_ms),
            "open_time_ms": open_ms,
            "close_time_ms": to_int(raw[6]),
            "open": to_float(raw[1]),
            "high": to_float(raw[2]),
            "low": to_float(raw[3]),
            "close": to_float(raw[4]),
            "volume": to_float(raw[5]),
            "quote_volume": to_float(raw[7]),
            "cache_ts": now_cst().isoformat(timespec="seconds"),
            "source_file": source,
        })
    return records


def fetch_task(task: dict[str, Any], provider_order: list[str], limit: int) -> tuple[list[dict[str, Any]], str, str, bool]:
    symbol = str(task["symbol"])
    interval = str(task["interval"])
    start_ms = int(task["start_ms"])
    end_ms = int(task["end_ms"])
    errors: list[str] = []
    transient = False
    terminal = False
    for provider in provider_order:
        try:
            if provider == "bybit":
                raw = bybit_kline_window(symbol, interval, start_ms, end_ms, limit)
            elif provider == "okx":
                raw = okx_kline_window(symbol, interval, start_ms, end_ms, min(limit, 300))
            else:
                continue
            if raw:
                return raw_rows_to_records(symbol, interval, raw, provider), provider, "", False
            errors.append(f"{provider}:empty")
            terminal = True
        except Exception as exc:
            message = str(exc)
            errors.append(f"{provider}:{message}")
            if terminal_unavailable_error(message):
                terminal = True
            else:
                transient = True
    return [], "", "; ".join(errors), bool(terminal and not transient)


def merge_write_rows(store: Path, table: str, rows: list[dict[str, Any]], fmt: str) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "merged_rows": 0, "files": 0}
    if fmt == "jsonl":
        return merge_write_jsonl_partitions(store, table, rows)
    existing = load_existing_rows(store, table, fmt)
    merged = dedupe_kline_rows([*existing, *rows])
    result = export_dataset(merged, store, table, fmt)
    return {"rows": len(rows), "merged_rows": len(merged), "files": int(result.get("files") or 0)}


def merge_write_jsonl_partitions(store: Path, table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = ms_to_iso(to_int(row.get("open_time_ms")))[:10] if to_int(row.get("open_time_ms")) > 0 else str(row.get("date") or "unknown")[:10]
        by_date.setdefault(day, []).append({**row, "date": day})
    files = 0
    for day, new_rows in sorted(by_date.items()):
        target = jsonl_partition_path(store, table, day)
        existing_rows = read_jsonl_rows(target)
        merged = dedupe_kline_rows([*existing_rows, *new_rows])
        if write_jsonl_rows(target, merged):
            files += 1
    return {"rows": len(rows), "merged_rows": count_jsonl_rows(store, table), "files": files}


def progress_payload(
    *,
    args: argparse.Namespace,
    status: str,
    symbols: list[str],
    universe_meta: dict[str, Any],
    total_tasks: int,
    pending_tasks: int,
    completed_requests: int,
    failed_requests: int,
    skipped_existing: int,
    skipped_unavailable: int,
    skipped_partial: int,
    fetched_rows: int,
    written_rows: int,
    errors: list[str],
    started_at: datetime,
    last_task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planned_bars = sum(
        int(INTERVAL_MS.get(interval, 0) and math.ceil((max(1, args.days) * 86_400_000) / INTERVAL_MS[interval]))
        for interval in csv_values(args.intervals)
    ) * len(symbols)
    task_percent = (completed_requests + skipped_existing + skipped_unavailable + skipped_partial) / total_tasks * 100 if total_tasks else 0.0
    row_percent = written_rows / planned_bars * 100 if planned_bars else 0.0
    percent = max(task_percent, row_percent)
    return {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "safety": SAFETY,
        "status": status,
        "mode": "apply" if args.apply else "plan_only",
        "apply_enabled": bool(args.apply),
        "live_scanner_impact": "none",
        "binance_requests_enabled": False,
        "strategy_frequency_change": False,
        "config": {
            "days": int(args.days),
            "intervals": csv_values(args.intervals),
            "top_n": int(args.top_n),
            "limit": int(args.limit),
            "task_limit": int(getattr(args, "task_limit", args.limit)),
            "provider_order": csv_values(args.providers),
            "max_rps": float(args.max_rps),
            "max_requests": int(args.max_requests),
            "max_runtime_sec": int(args.max_runtime_sec),
            "format": args.format,
        },
        "universe": {
            "symbols": symbols,
            **universe_meta,
        },
        "progress": {
            "total_tasks": int(total_tasks),
            "pending_tasks": int(pending_tasks),
            "completed_requests": int(completed_requests),
            "failed_requests": int(failed_requests),
            "skipped_existing": int(skipped_existing),
            "skipped_unavailable": int(skipped_unavailable),
            "skipped_partial": int(skipped_partial),
            "percent": round(percent, 2),
            "fetched_rows": int(fetched_rows),
            "written_rows": int(written_rows),
            "planned_bars_estimate": int(planned_bars),
        },
        "last_task": last_task or {},
        "errors": errors[-20:],
    }


def write_progress(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "historical_kline_backfill_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "historical_kline_backfill_latest.md").write_text(render_md(payload), encoding="utf-8")


def render_md(payload: dict[str, Any]) -> str:
    progress = payload.get("progress") or {}
    cfg = payload.get("config") or {}
    universe = payload.get("universe") or {}
    last = payload.get("last_task") or {}
    lines = [
        "# Historical Kline Backfill",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Safety: `{payload.get('safety')}`",
        f"- Live scanner impact: `{payload.get('live_scanner_impact')}`",
        f"- Binance requests enabled: `{payload.get('binance_requests_enabled')}`",
        f"- Strategy frequency change: `{payload.get('strategy_frequency_change')}`",
        "",
        "## Progress",
        "",
        f"- Tasks: `{progress.get('completed_requests', 0)}` completed / `{progress.get('total_tasks', 0)}` total; skipped existing `{progress.get('skipped_existing', 0)}`; unavailable `{progress.get('skipped_unavailable', 0)}`; partial `{progress.get('skipped_partial', 0)}`",
        f"- Percent: `{progress.get('percent', 0)}%`",
        f"- Rows fetched: `{progress.get('fetched_rows', 0)}`",
        f"- Rows written: `{progress.get('written_rows', 0)}`",
        f"- Failed requests: `{progress.get('failed_requests', 0)}`",
        "",
        "## Scope",
        "",
        f"- Universe: `{len(universe.get('symbols') or [])}` symbols from `{universe.get('source', '-')}`",
        f"- Symbols: `{', '.join(universe.get('symbols') or [])}`",
        f"- Days: `{cfg.get('days')}`",
        f"- Intervals: `{', '.join(cfg.get('intervals') or [])}`",
        f"- Providers: `{', '.join(cfg.get('provider_order') or [])}`",
        f"- Task limit: `{cfg.get('task_limit', cfg.get('limit'))}` bars",
        f"- Max RPS: `{cfg.get('max_rps')}`",
        f"- Max requests this run: `{cfg.get('max_requests')}`",
        "",
        "## Last Task",
        "",
        f"- Symbol: `{last.get('symbol', '-')}`",
        f"- Interval: `{last.get('interval', '-')}`",
        f"- Window: `{last.get('start', '-')}` -> `{last.get('end', '-')}`",
        f"- Provider: `{last.get('provider', '-')}`",
        f"- Rows: `{last.get('rows', 0)}`",
    ]
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        lines.extend(["", "## Errors", "", *[f"- `{item}`" for item in errors[-10:]]])
    return "\n".join(lines) + "\n"


def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    store = Path(args.research_store)
    started_at = now_cst()
    end_dt = parse_dt(args.end) if args.end else started_at
    start_dt = end_dt - timedelta(days=max(1, int(args.days)))
    symbols, universe_meta = choose_top_symbols(runtime_dir, [normalize_symbol(v) for v in csv_values(args.symbols)], args.top_n)
    intervals = [item for item in csv_values(args.intervals) if item in INTERVAL_MS]
    provider_order = csv_values(args.providers)
    task_limit = effective_task_limit(provider_order, int(args.limit))
    setattr(args, "task_limit", task_limit)
    tasks = chunk_tasks(symbols, intervals, dt_to_ms(start_dt), dt_to_ms(end_dt) - 1, task_limit)
    existing = read_existing_keys(store, args.format)
    statuses = read_task_statuses(store)
    stored_rows = len(existing)
    pending = []
    skipped_existing = 0
    skipped_unavailable = 0
    skipped_partial = 0
    for task in tasks:
        status = statuses.get(task_key(task), "")
        if task_covered(task, existing):
            skipped_existing += 1
        elif status in UNAVAILABLE_TASK_STATUSES:
            skipped_unavailable += 1
        elif status in PARTIAL_TASK_STATUSES:
            skipped_partial += 1
        else:
            pending.append(task)
    payload = progress_payload(
        args=args,
        status="planned" if not args.apply else "running",
        symbols=symbols,
        universe_meta=universe_meta,
        total_tasks=len(tasks),
        pending_tasks=len(pending),
        completed_requests=0,
        failed_requests=0,
        skipped_existing=skipped_existing,
        skipped_unavailable=skipped_unavailable,
        skipped_partial=skipped_partial,
        fetched_rows=0,
        written_rows=stored_rows,
        errors=[],
        started_at=started_at,
    )
    write_progress(runtime_dir, reports_dir, payload)
    if not args.apply:
        return payload

    max_requests = max(0, int(args.max_requests))
    max_runtime_sec = max(1, int(args.max_runtime_sec))
    request_gap = 1.0 / max(float(args.max_rps), 0.01)
    flush_requests = max(1, int(args.flush_requests))
    completed = 0
    failed = 0
    marked_unavailable = 0
    fetched_rows = 0
    errors: list[str] = []
    buffer: list[dict[str, Any]] = []
    buffer_statuses: list[tuple[dict[str, Any], str, str, int, str]] = []
    marked_symbols_unavailable: set[str] = set()
    last_task: dict[str, Any] | None = None
    deadline = time.monotonic() + max_runtime_sec
    status = "running"
    for task in pending:
        if str(task["symbol"]) in marked_symbols_unavailable:
            append_unavailable_task(store, task, "symbol_unavailable_after_prior_provider_result")
            marked_unavailable += 1
            skipped_unavailable += 1
            last_task = {
                "symbol": task["symbol"],
                "interval": task["interval"],
                "start": ms_to_iso(int(task["start_ms"])),
                "end": ms_to_iso(int(task["end_ms"])),
                "provider": "unavailable_symbol",
                "rows": 0,
            }
            continue
        attempted = completed + failed + marked_unavailable
        if max_requests and attempted >= max_requests:
            status = "paused_request_budget"
            break
        if time.monotonic() >= deadline:
            status = "paused_time_budget"
            break
        rows, provider, error, terminal_unavailable = fetch_task(task, provider_order, int(args.limit))
        if rows:
            buffer.extend(rows)
            task_status = "complete" if len(rows) >= int(task.get("expected_bars") or 0) else "partial_available"
            buffer_statuses.append((task, task_status, provider, len(rows), ""))
            completed += 1
            fetched_rows += len(rows)
            last_task = {
                "symbol": task["symbol"],
                "interval": task["interval"],
                "start": ms_to_iso(int(task["start_ms"])),
                "end": ms_to_iso(int(task["end_ms"])),
                "provider": provider,
                "rows": len(rows),
            }
        elif terminal_unavailable:
            append_unavailable_task(store, task, error)
            marked_unavailable += 1
            skipped_unavailable += 1
            if symbol_unavailable_error(error):
                marked_symbols_unavailable.add(str(task["symbol"]))
            if error:
                errors.append(f"{task['symbol']} {task['interval']} {ms_to_iso(int(task['start_ms']))}: unavailable {error}")
            last_task = {
                "symbol": task["symbol"],
                "interval": task["interval"],
                "start": ms_to_iso(int(task["start_ms"])),
                "end": ms_to_iso(int(task["end_ms"])),
                "provider": "unavailable",
                "rows": 0,
            }
        else:
            failed += 1
            if error:
                errors.append(f"{task['symbol']} {task['interval']} {ms_to_iso(int(task['start_ms']))}: {error}")
            last_task = {
                "symbol": task["symbol"],
                "interval": task["interval"],
                "start": ms_to_iso(int(task["start_ms"])),
                "end": ms_to_iso(int(task["end_ms"])),
                "provider": "",
                "rows": 0,
            }
        if buffer and (completed % flush_requests == 0):
            write_result = merge_write_rows(store, "historical_klines", buffer, args.format)
            stored_rows = int(write_result.get("merged_rows") or stored_rows)
            for item_task, item_status, item_provider, item_rows, item_error in buffer_statuses:
                append_task_status(store, item_task, item_status, provider=item_provider, rows=item_rows, error=item_error)
            buffer = []
            buffer_statuses = []
        payload = progress_payload(
            args=args,
            status=status,
            symbols=symbols,
            universe_meta=universe_meta,
            total_tasks=len(tasks),
            pending_tasks=max(0, len(pending) - completed - failed - marked_unavailable),
            completed_requests=completed,
            failed_requests=failed,
            skipped_existing=skipped_existing,
            skipped_unavailable=skipped_unavailable,
            skipped_partial=skipped_partial,
            fetched_rows=fetched_rows,
            written_rows=stored_rows,
            errors=errors,
            started_at=started_at,
            last_task=last_task,
        )
        write_progress(runtime_dir, reports_dir, payload)
        if request_gap > 0:
            time.sleep(request_gap)
    if buffer:
        write_result = merge_write_rows(store, "historical_klines", buffer, args.format)
        stored_rows = int(write_result.get("merged_rows") or stored_rows)
        for item_task, item_status, item_provider, item_rows, item_error in buffer_statuses:
            append_task_status(store, item_task, item_status, provider=item_provider, rows=item_rows, error=item_error)
        buffer = []
        buffer_statuses = []
    if status == "running":
        status = "complete" if completed + failed + marked_unavailable >= len(pending) else "paused"
    payload = progress_payload(
        args=args,
        status=status,
        symbols=symbols,
        universe_meta=universe_meta,
        total_tasks=len(tasks),
        pending_tasks=max(0, len(pending) - completed - failed - marked_unavailable),
        completed_requests=completed,
        failed_requests=failed,
        skipped_existing=skipped_existing,
        skipped_unavailable=skipped_unavailable,
        skipped_partial=skipped_partial,
        fetched_rows=fetched_rows,
        written_rows=stored_rows,
        errors=errors,
        started_at=started_at,
        last_task=last_task,
    )
    write_progress(runtime_dir, reports_dir, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Top30 public historical Kline backfill")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--research-store", default=str(ROOT / "research_store"))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--end", default="")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--providers", default="bybit,okx")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-rps", type=float, default=0.5)
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--max-runtime-sec", type=int, default=900)
    parser.add_argument("--flush-requests", type=int, default=10)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--apply", action="store_true", help="Actually request public Bybit/OKX data. Omit for plan/progress only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_backfill(args)
    print(json.dumps({
        "status": payload.get("status"),
        "mode": payload.get("mode"),
        "progress": payload.get("progress"),
        "safety": payload.get("safety"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
