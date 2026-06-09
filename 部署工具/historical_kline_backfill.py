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
    rows = load_existing_rows(store, "historical_klines", fmt)
    keys: set[tuple[str, str, int]] = set()
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        interval = str(row.get("interval") or "")
        open_time_ms = to_int(row.get("open_time_ms"))
        if symbol and interval and open_time_ms > 0:
            keys.add((symbol, interval, open_time_ms))
    return keys


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


def fetch_task(task: dict[str, Any], provider_order: list[str], limit: int) -> tuple[list[dict[str, Any]], str, str]:
    symbol = str(task["symbol"])
    interval = str(task["interval"])
    start_ms = int(task["start_ms"])
    end_ms = int(task["end_ms"])
    errors: list[str] = []
    for provider in provider_order:
        try:
            if provider == "bybit":
                raw = bybit_kline_window(symbol, interval, start_ms, end_ms, limit)
            elif provider == "okx":
                raw = okx_kline_window(symbol, interval, start_ms, end_ms, min(limit, 300))
            else:
                continue
            if raw:
                return raw_rows_to_records(symbol, interval, raw, provider), provider, ""
            errors.append(f"{provider}:empty")
        except Exception as exc:
            errors.append(f"{provider}:{exc}")
    return [], "", "; ".join(errors)


def merge_write_rows(store: Path, table: str, rows: list[dict[str, Any]], fmt: str) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "merged_rows": 0, "files": 0}
    existing = load_existing_rows(store, table, fmt)
    merged = dedupe_kline_rows([*existing, *rows])
    result = export_dataset(merged, store, table, fmt)
    return {"rows": len(rows), "merged_rows": len(merged), "files": int(result.get("files") or 0)}


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
    task_percent = (completed_requests + skipped_existing) / total_tasks * 100 if total_tasks else 0.0
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
        f"- Tasks: `{progress.get('completed_requests', 0)}` completed / `{progress.get('total_tasks', 0)}` total; skipped existing `{progress.get('skipped_existing', 0)}`",
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
    tasks = chunk_tasks(symbols, intervals, dt_to_ms(start_dt), dt_to_ms(end_dt) - 1, int(args.limit))
    existing = read_existing_keys(store, args.format)
    stored_rows = len(existing)
    pending = []
    skipped_existing = 0
    for task in tasks:
        if task_covered(task, existing):
            skipped_existing += 1
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
        fetched_rows=0,
        written_rows=stored_rows,
        errors=[],
        started_at=started_at,
    )
    write_progress(runtime_dir, reports_dir, payload)
    if not args.apply:
        return payload

    provider_order = csv_values(args.providers)
    max_requests = max(0, int(args.max_requests))
    max_runtime_sec = max(1, int(args.max_runtime_sec))
    request_gap = 1.0 / max(float(args.max_rps), 0.01)
    flush_requests = max(1, int(args.flush_requests))
    completed = 0
    failed = 0
    fetched_rows = 0
    errors: list[str] = []
    buffer: list[dict[str, Any]] = []
    last_task: dict[str, Any] | None = None
    deadline = time.monotonic() + max_runtime_sec
    status = "running"
    for task in pending:
        if max_requests and completed + failed >= max_requests:
            status = "paused_request_budget"
            break
        if time.monotonic() >= deadline:
            status = "paused_time_budget"
            break
        rows, provider, error = fetch_task(task, provider_order, int(args.limit))
        if rows:
            buffer.extend(rows)
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
            buffer = []
        payload = progress_payload(
            args=args,
            status=status,
            symbols=symbols,
            universe_meta=universe_meta,
            total_tasks=len(tasks),
            pending_tasks=max(0, len(pending) - completed - failed),
            completed_requests=completed,
            failed_requests=failed,
            skipped_existing=skipped_existing,
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
        buffer = []
    if status == "running":
        status = "complete" if completed + failed >= len(pending) else "paused"
    payload = progress_payload(
        args=args,
        status=status,
        symbols=symbols,
        universe_meta=universe_meta,
        total_tasks=len(tasks),
        pending_tasks=max(0, len(pending) - completed - failed),
        completed_requests=completed,
        failed_requests=failed,
        skipped_existing=skipped_existing,
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
