"""Plan and ingest queued Kline backfill requests for the research store.

This tool does not call Binance directly. It can submit public Kline request
intent to the central API queue, then later ingest completed queue results into
research_store klines/features partitions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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

from core.binance_api_queue import BinanceApiQueue, PRIORITY_NORMAL, STATUS_DONE, now_ms
from research_kline_features import build_features, dedupe_kline_rows, export_dataset, load_existing_rows


CST = timezone(timedelta(hours=8))
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}
DEFAULT_INTERVALS = ("15m", "30m", "1h")
SYMBOL_SOURCE_TABLES = ("events", "sentinel_scans")
DEFAULT_KLINE_BASE_URL = "https://fapi.binance.com"


def now_cst() -> datetime:
    return datetime.now(CST)


def parse_dt(value: str | None) -> datetime:
    if not value:
        return now_cst()
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, CST).isoformat(timespec="seconds")


def csv_values(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def row_open_ms(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("open_time_ms") or 0))
    except Exception:
        return 0


def target_coverage_days(rows: list[dict[str, Any]], *, start_ms: int, end_ms: int) -> int:
    days = {
        datetime.fromtimestamp(ts / 1000, CST).strftime("%Y-%m-%d")
        for row in rows
        for ts in [row_open_ms(row)]
        if start_ms <= ts < end_ms
    }
    return len(days)


def group_existing(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        interval = str(row.get("interval") or "")
        if symbol and interval:
            grouped.setdefault((symbol, interval), []).append(row)
    return grouped


def choose_symbols(rows: list[dict[str, Any]], explicit: list[str], max_symbols: int) -> list[str]:
    if explicit:
        return sorted({normalize_symbol(symbol) for symbol in explicit if normalize_symbol(symbol)})
    counts: dict[str, int] = {}
    for row in rows:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if symbol:
            counts[symbol] = counts.get(symbol, 0) + 1
    return [symbol for symbol, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(0, max_symbols)]]


def data_files(store: Path, table: str, fmt: str) -> list[Path]:
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    return sorted((store / table).glob(f"date=*/data.{suffix}"))


def load_symbol_seed_rows(store: Path, fmt: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in SYMBOL_SOURCE_TABLES:
        for path in data_files(store, table, fmt):
            if fmt == "parquet":
                try:
                    import pandas as pd
                except ImportError as exc:
                    raise SystemExit("pandas is required for parquet symbol seed loading") from exc
                try:
                    frame = pd.read_parquet(path, columns=["symbol"])
                except Exception:
                    continue
                rows.extend({"symbol": symbol} for symbol in frame.get("symbol", []) if normalize_symbol(symbol))
            else:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            try:
                                row = json.loads(line)
                            except Exception:
                                continue
                            symbol = normalize_symbol(row.get("symbol") if isinstance(row, dict) else "")
                            if symbol:
                                rows.append({"symbol": symbol})
                except OSError:
                    continue
    return rows


def chunk_windows(start_ms: int, end_ms: int, interval: str, limit: int) -> list[dict[str, int]]:
    interval_ms = INTERVAL_MS[interval]
    chunk_span = interval_ms * max(1, int(limit))
    chunks: list[dict[str, int]] = []
    cursor = start_ms
    while cursor < end_ms:
        next_cursor = min(end_ms, cursor + chunk_span)
        rows = max(1, min(int(limit), (next_cursor - cursor + interval_ms - 1) // interval_ms))
        chunks.append({"start_ms": cursor, "end_ms": next_cursor - 1, "limit": rows})
        cursor = next_cursor
    return chunks


def build_backfill_plan(
    existing_rows: list[dict[str, Any]],
    *,
    symbol_rows: list[dict[str, Any]] | None = None,
    symbols: list[str],
    intervals: list[str],
    target_days: int,
    end_dt: datetime,
    limit: int,
    max_symbols: int,
) -> dict[str, Any]:
    selected_symbols = choose_symbols([*(symbol_rows or []), *existing_rows], symbols, max_symbols)
    supported_intervals = [interval for interval in intervals if interval in INTERVAL_MS]
    end_ms = dt_to_ms(end_dt)
    start_ms = dt_to_ms(end_dt - timedelta(days=max(1, int(target_days))))
    grouped = group_existing(existing_rows)
    items: list[dict[str, Any]] = []
    for symbol in selected_symbols:
        for interval in supported_intervals:
            rows = grouped.get((symbol, interval), [])
            coverage_days = target_coverage_days(rows, start_ms=start_ms, end_ms=end_ms)
            if coverage_days >= target_days:
                continue
            for idx, chunk in enumerate(chunk_windows(start_ms, end_ms, interval, limit)):
                item = {
                    "symbol": symbol,
                    "interval": interval,
                    "start_ms": chunk["start_ms"],
                    "end_ms": chunk["end_ms"],
                    "start_time": ms_to_iso(chunk["start_ms"]),
                    "end_time": ms_to_iso(chunk["end_ms"]),
                    "limit": chunk["limit"],
                    "coverage_days": coverage_days,
                    "target_days": target_days,
                    "reason": "target_coverage_gap",
                    "chunk_index": idx,
                    "idempotency_key": (
                        f"kline_backfill:{symbol}:{interval}:{chunk['start_ms']}:{chunk['end_ms']}:{chunk['limit']}"
                    ),
                    "body": {
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": chunk["start_ms"],
                        "endTime": chunk["end_ms"],
                        "limit": chunk["limit"],
                    },
                }
                items.append(item)
    return {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "target_days": target_days,
        "target_start": ms_to_iso(start_ms),
        "target_end": ms_to_iso(end_ms),
        "symbols": selected_symbols,
        "intervals": supported_intervals,
        "items": items,
        "summary": {
            "symbols": len(selected_symbols),
            "intervals": len(supported_intervals),
            "requests": len(items),
            "status": "ready" if items else "covered_or_no_symbols",
        },
    }


def submit_plan(queue: BinanceApiQueue, items: list[dict[str, Any]], *, stagger_sec: int, base_url: str = DEFAULT_KLINE_BASE_URL) -> dict[str, Any]:
    submitted = 0
    existing = 0
    request_ids: list[str] = []
    base_ms = now_ms()
    kline_url = f"{str(base_url).strip().rstrip('/')}/fapi/v1/klines"
    for idx, item in enumerate(items):
        before_counts = queue.summary().get("counts", {})
        request = queue.submit_request(
            scope="public",
            label="research_kline_backfill",
            method="GET",
            path="/fapi/v1/klines",
            url=kline_url,
            body=item.get("body") if isinstance(item.get("body"), dict) else {},
            priority=PRIORITY_NORMAL,
            earliest_ms=base_ms + idx * max(0, int(stagger_sec)) * 1000,
            idempotency_key=str(item.get("idempotency_key") or ""),
        )
        after_counts = queue.summary().get("counts", {})
        request_ids.append(request.request_id)
        if before_counts == after_counts and request.idempotency_key == item.get("idempotency_key"):
            existing += 1
        else:
            submitted += 1
    return {"submitted": submitted, "existing": existing, "request_ids": request_ids}


def read_done_backfill_requests(queue_db: Path) -> list[dict[str, Any]]:
    if not queue_db.exists():
        return []
    conn = sqlite3.connect(str(queue_db), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select request_id, idempotency_key, body_json, result_body_json, updated_at_ms
            from api_requests
            where status = ? and path = '/fapi/v1/klines' and idempotency_key like 'kline_backfill:%'
            order by updated_at_ms
            """,
            (STATUS_DONE,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def kline_rows_from_done_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for request in requests:
        body = json_loads(request.get("body_json"), {})
        result = json_loads(request.get("result_body_json"), [])
        if not isinstance(body, dict) or not isinstance(result, list):
            continue
        symbol = normalize_symbol(str(body.get("symbol") or ""))
        interval = str(body.get("interval") or "")
        limit = int(body.get("limit") or 0)
        if not symbol or not interval:
            continue
        for raw in result:
            if not isinstance(raw, list) or len(raw) < 8:
                continue
            try:
                open_time_ms = int(raw[0])
                close_time_ms = int(raw[6])
            except Exception:
                continue
            open_dt = datetime.fromtimestamp(open_time_ms / 1000, CST)
            rows.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "limit": limit,
                    "date": open_dt.strftime("%Y-%m-%d"),
                    "open_time": open_dt.isoformat(timespec="seconds"),
                    "open_time_ms": open_time_ms,
                    "close_time_ms": close_time_ms,
                    "open": float(raw[1] or 0),
                    "high": float(raw[2] or 0),
                    "low": float(raw[3] or 0),
                    "close": float(raw[4] or 0),
                    "volume": float(raw[5] or 0),
                    "quote_volume": float(raw[7] or 0),
                    "cache_ts": now_cst().isoformat(timespec="seconds"),
                    "source_file": f"queue:{request.get('request_id')}",
                    "source": "api_queue_backfill",
                }
            )
    return rows


def ingest_done_requests(queue_db: Path, out_dir: Path, fmt: str) -> dict[str, Any]:
    done_requests = read_done_backfill_requests(queue_db)
    backfill_rows = kline_rows_from_done_requests(done_requests)
    existing_rows = load_existing_rows(out_dir, "klines", fmt)
    merged_rows = dedupe_kline_rows([*existing_rows, *backfill_rows])
    features = build_features(merged_rows)
    results = [
        export_dataset(merged_rows, out_dir, "klines", fmt),
        export_dataset(features, out_dir, "features", fmt),
    ]
    return {
        "done_requests": len(done_requests),
        "backfill_rows": len(backfill_rows),
        "existing_rows": len(existing_rows),
        "merged_rows": len(merged_rows),
        "results": results,
    }


def render_md(payload: dict[str, Any]) -> str:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    submit = payload.get("submit") if isinstance(payload.get("submit"), dict) else {}
    ingest = payload.get("ingest") if isinstance(payload.get("ingest"), dict) else {}
    sample_rows = []
    for item in (plan.get("items") or [])[:12]:
        sample_rows.append(
            "| {symbol} | {interval} | {start} | {end} | {coverage}/{target} |".format(
                symbol=item.get("symbol"),
                interval=item.get("interval"),
                start=item.get("start_time"),
                end=item.get("end_time"),
                coverage=item.get("coverage_days"),
                target=item.get("target_days"),
            )
        )
    table = "\n".join(["| symbol | interval | start | end | coverage |", "| --- | --- | --- | --- | --- |", *sample_rows])
    if not sample_rows:
        table = "_No backfill requests planned._"
    return "\n\n".join(
        [
            "# Research Kline Backfill",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Queue DB: `{payload.get('queue_db')}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Plan status: `{summary.get('status', 'unknown')}`; requests `{summary.get('requests', 0)}`",
            f"- Submit: submitted `{submit.get('submitted', 0)}`, existing `{submit.get('existing', 0)}`",
            f"- Ingest: done requests `{ingest.get('done_requests', 0)}`, rows `{ingest.get('backfill_rows', 0)}`, merged `{ingest.get('merged_rows', 0)}`",
            "## Planned Requests",
            table,
        ]
    )


def write_outputs(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "research_kline_backfill_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "research_kline_backfill_latest.md").write_text(render_md(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/submit/ingest queued Kline backfill for research_store")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--queue-db", default=str(ROOT / "runtime" / "binance_api_queue.sqlite3"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--target-days", type=int, default=30)
    parser.add_argument("--end", default="")
    parser.add_argument("--limit", type=int, default=1500)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--submit", action="store_true", help="Submit planned requests to the local central API queue")
    parser.add_argument("--stagger-sec", type=int, default=60)
    parser.add_argument("--kline-base-url", default=DEFAULT_KLINE_BASE_URL)
    parser.add_argument("--ingest-done", action="store_true", help="Merge completed queue Kline responses into research_store")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = Path(args.store)
    queue_db = Path(args.queue_db)
    existing_rows = load_existing_rows(store, "klines", args.format)
    symbol_rows = load_symbol_seed_rows(store, args.format)
    plan = build_backfill_plan(
        existing_rows,
        symbol_rows=symbol_rows,
        symbols=csv_values(args.symbols),
        intervals=csv_values(args.intervals),
        target_days=args.target_days,
        end_dt=parse_dt(args.end),
        limit=args.limit,
        max_symbols=args.max_symbols,
    )
    submit_result: dict[str, Any] = {}
    if args.submit and plan.get("items"):
        submit_result = submit_plan(
            BinanceApiQueue(queue_db),
            plan["items"],
            stagger_sec=args.stagger_sec,
            base_url=args.kline_base_url,
        )
    ingest_result: dict[str, Any] = {}
    if args.ingest_done:
        ingest_result = ingest_done_requests(queue_db, store, args.format)
        manifest_path = store / "kline_backfill_manifest_latest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(ingest_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    payload = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "store_dir": str(store),
        "queue_db": str(queue_db),
        "format": args.format,
        "symbol_source_rows": len(symbol_rows),
        "kline_base_url": args.kline_base_url,
        "plan": plan,
        "submit": submit_result,
        "ingest": ingest_result,
        "live_impact": "none; no direct Binance request is made by this tool",
    }
    write_outputs(Path(args.runtime_dir), Path(args.reports_dir), payload)
    print(json.dumps({"plan_requests": len(plan.get("items") or []), "submit": submit_result, "ingest": ingest_result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
