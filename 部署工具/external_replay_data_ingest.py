"""Ingest public OKX/Bybit replay data into research_store.

This tool is public-data only. It does not use Binance, does not submit the API
queue, does not restart services, and does not change strategy behavior.
"""

from __future__ import annotations

import argparse
import json
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

from core.external_market_data import bybit_interval, bybit_public_get, fetch_bybit_klines
from research_kline_features import build_features, dedupe_kline_rows, export_dataset, load_existing_rows


CST = timezone(timedelta(hours=8))
INTERVAL_MS = {
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
}
DEFAULT_INTERVALS = ("15m", "30m", "1h")


def now_cst() -> datetime:
    return datetime.now(CST)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, CST).isoformat(timespec="seconds")


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


def csv_symbols(raw: str) -> list[str]:
    return [item.upper() for item in csv_values(raw)]


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def choose_symbols(runtime_dir: Path, explicit: list[str], max_symbols: int) -> list[str]:
    if explicit:
        return sorted({symbol for symbol in explicit if symbol.endswith("USDT")})
    cache = read_json(runtime_dir / "market_data_cache.json")
    values = cache.get("available_symbols") or cache.get("top_symbols") or cache.get("symbols") or []
    symbols: list[str] = []
    for item in values:
        symbol = item if isinstance(item, str) else item.get("symbol") if isinstance(item, dict) else ""
        symbol = str(symbol or "").upper()
        if symbol.endswith("USDT") and symbol.isascii() and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= max(0, int(max_symbols)):
            break
    return symbols


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
    )
    rows = []
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
            str(open_ms + INTERVAL_MS.get(interval, 60_000) - 1),
            str(row[6]),
        ])
    rows.sort(key=lambda item: int(float(item[0])))
    return rows


def fetch_kline_rows(symbols: list[str], intervals: list[str], target_days: int, request_gap_sec: float, max_requests: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    end_dt = now_cst()
    start_ms = dt_to_ms(end_dt - timedelta(days=max(1, int(target_days))))
    end_ms = dt_to_ms(end_dt)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    for symbol in symbols:
        for interval in intervals:
            interval_ms = INTERVAL_MS.get(interval)
            if not interval_ms:
                continue
            chunk_span = interval_ms * 1000
            cursor = start_ms
            while cursor < end_ms:
                if requests >= max_requests:
                    return rows, {"requests": requests, "errors": errors, "truncated": True}
                chunk_end = min(end_ms, cursor + chunk_span - 1)
                try:
                    raw_rows = bybit_kline_window(symbol, interval, cursor, chunk_end, 1000)
                    source = "bybit"
                except Exception as exc:
                    try:
                        raw_rows = fetch_bybit_klines(symbol, interval, 1000)
                        source = "bybit_recent_fallback"
                    except Exception:
                        errors.append(f"{symbol} {interval}: {exc}")
                        raw_rows = []
                        source = ""
                requests += 1
                for raw in raw_rows:
                    open_ms = to_int(raw[0])
                    if not (start_ms <= open_ms <= end_ms):
                        continue
                    rows.append({
                        "symbol": symbol,
                        "interval": interval,
                        "limit": len(raw_rows),
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
                        "cache_ts": end_dt.isoformat(timespec="seconds"),
                        "source_file": source,
                    })
                cursor = chunk_end + 1
                if request_gap_sec > 0:
                    time.sleep(request_gap_sec)
    return rows, {"requests": requests, "errors": errors, "truncated": False}


def fetch_depth_rows(symbols: list[str], samples_per_symbol: int, limit: int, request_gap_sec: float, max_requests: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    requests = 0
    for sample_idx in range(max(0, int(samples_per_symbol))):
        for symbol in symbols:
            if requests >= max_requests:
                return rows, {"requests": requests, "errors": errors, "truncated": True}
            captured = now_cst()
            try:
                payload = bybit_public_get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": int(limit)})
                result = payload.get("result") or {}
                bids = [[to_float(x[0]), to_float(x[1])] for x in (result.get("b") or []) if len(x) >= 2]
                asks = [[to_float(x[0]), to_float(x[1])] for x in (result.get("a") or []) if len(x) >= 2]
            except Exception as exc:
                errors.append(f"{symbol} depth: {exc}")
                bids, asks = [], []
            requests += 1
            if bids and asks:
                best_bid = bids[0][0]
                best_ask = asks[0][0]
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
                spread_bps = (best_ask - best_bid) / mid * 10000 if mid else 0.0
                rows.append({
                    "symbol": symbol,
                    "date": captured.strftime("%Y-%m-%d"),
                    "snapshot_time": captured.isoformat(timespec="seconds"),
                    "snapshot_time_ms": dt_to_ms(captured),
                    "sample_index": sample_idx,
                    "bid_levels": len(bids),
                    "ask_levels": len(asks),
                    "spread_bps": round(spread_bps, 6),
                    "bids_json": json.dumps(bids, separators=(",", ":")),
                    "asks_json": json.dumps(asks, separators=(",", ":")),
                    "source": "bybit_orderbook",
                })
            if request_gap_sec > 0:
                time.sleep(request_gap_sec)
    return rows, {"requests": requests, "errors": errors, "truncated": False}


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# External Replay Data Ingest",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Safety: `{payload.get('safety')}`",
        f"- Symbols: `{summary.get('symbols')}`",
        f"- Kline rows merged: `{summary.get('kline_rows_merged')}`",
        f"- Depth rows merged: `{summary.get('depth_rows_merged')}`",
        f"- Requests: `{summary.get('requests')}`",
        f"- Errors: `{summary.get('errors')}`",
        "",
        "## Rule",
        "",
        "- Public OKX/Bybit data only.",
        "- No Binance request, no queue submit, no service restart, no strategy/config mutation.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest public external replay data")
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--research-store", default=str(ROOT / "research_store"))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=10)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--target-days", type=int, default=30)
    parser.add_argument("--depth-samples-per-symbol", type=int, default=1)
    parser.add_argument("--depth-limit", type=int, default=50)
    parser.add_argument("--request-gap-sec", type=float, default=0.2)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--format", choices=["jsonl", "parquet"], default="jsonl")
    args = parser.parse_args(argv)

    runtime_dir = Path(args.runtime_dir)
    reports_dir = Path(args.reports_dir)
    store = Path(args.research_store)
    symbols = choose_symbols(runtime_dir, csv_symbols(args.symbols), args.max_symbols)
    intervals = [item for item in csv_values(args.intervals) if item in INTERVAL_MS]
    kline_rows, kline_meta = fetch_kline_rows(symbols, intervals, args.target_days, args.request_gap_sec, args.max_requests)
    remaining_requests = max(0, int(args.max_requests) - int(kline_meta.get("requests") or 0))
    depth_rows, depth_meta = fetch_depth_rows(symbols, args.depth_samples_per_symbol, args.depth_limit, args.request_gap_sec, remaining_requests)

    existing_klines = load_existing_rows(store, "klines", args.format)
    existing_depth = load_existing_rows(store, "depth_snapshots", args.format)
    merged_klines = dedupe_kline_rows([*existing_klines, *kline_rows])
    merged_depth = [*existing_depth, *depth_rows]
    kline_export = export_dataset(merged_klines, store, "klines", args.format)
    feature_export = export_dataset(build_features(merged_klines), store, "features", args.format)
    depth_export = export_dataset(merged_depth, store, "depth_snapshots", args.format)
    payload = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "safety": "public_okx_bybit_only_no_binance_no_queue_no_service_restart",
        "symbols": symbols,
        "intervals": intervals,
        "kline_meta": kline_meta,
        "depth_meta": depth_meta,
        "exports": {"klines": kline_export, "features": feature_export, "depth_snapshots": depth_export},
        "summary": {
            "symbols": len(symbols),
            "kline_rows_fetched": len(kline_rows),
            "kline_rows_merged": len(merged_klines),
            "depth_rows_fetched": len(depth_rows),
            "depth_rows_merged": len(merged_depth),
            "requests": int(kline_meta.get("requests") or 0) + int(depth_meta.get("requests") or 0),
            "errors": len(kline_meta.get("errors") or []) + len(depth_meta.get("errors") or []),
            "truncated": bool(kline_meta.get("truncated") or depth_meta.get("truncated")),
        },
    }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "external_replay_data_ingest_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "external_replay_data_ingest_latest.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
