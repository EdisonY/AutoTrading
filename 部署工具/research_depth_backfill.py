"""Plan and ingest queued depth snapshot requests for the research store.

Depth snapshots are current-state data, not historical Klines. This tool does
not call Binance directly. It can plan a safe sampling request per symbol, submit
that intent to the central API queue, and later ingest DONE queue responses into
research_store/depth_snapshots plus runtime/depth_cache for report-only replay.
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
from research_kline_features import export_dataset, load_existing_rows


CST = timezone(timedelta(hours=8))
SYMBOL_SOURCE_TABLES = ("events", "sentinel_scans")


def now_cst() -> datetime:
    return datetime.now(CST)


def csv_values(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper().replace("/", "").replace("-", "")


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
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, CST).isoformat(timespec="seconds")


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


def choose_symbols(existing_rows: list[dict[str, Any]], symbol_rows: list[dict[str, Any]], explicit: list[str], max_symbols: int) -> list[str]:
    if explicit:
        return sorted({normalize_symbol(symbol) for symbol in explicit if normalize_symbol(symbol)})
    counts: dict[str, int] = {}
    for row in [*symbol_rows, *existing_rows]:
        symbol = normalize_symbol(row.get("symbol"))
        if symbol:
            counts[symbol] = counts.get(symbol, 0) + 1
    return [symbol for symbol, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(0, max_symbols)]]


def latest_snapshot_by_symbol(rows: list[dict[str, Any]]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        dt = parse_dt(row.get("snapshot_time") or row.get("captured_at") or row.get("ts") or row.get("time"))
        if dt is None:
            ms = row.get("snapshot_time_ms") or row.get("time_ms")
            dt = parse_dt(ms)
        if dt is None:
            continue
        if symbol not in latest or dt > latest[symbol]:
            latest[symbol] = dt
    return latest


def build_depth_plan(
    existing_rows: list[dict[str, Any]],
    *,
    symbol_rows: list[dict[str, Any]] | None = None,
    symbols: list[str],
    limit: int,
    max_symbols: int,
    max_age_sec: int,
    sample_bucket_sec: int,
    now_dt: datetime,
) -> dict[str, Any]:
    selected_symbols = choose_symbols(existing_rows, symbol_rows or [], symbols, max_symbols)
    latest = latest_snapshot_by_symbol(existing_rows)
    now_ms_value = dt_to_ms(now_dt)
    bucket_sec = max(1, int(sample_bucket_sec))
    bucket_ms = (now_ms_value // (bucket_sec * 1000)) * bucket_sec * 1000
    items: list[dict[str, Any]] = []
    for symbol in selected_symbols:
        latest_dt = latest.get(symbol)
        age_seconds = int((now_dt - latest_dt).total_seconds()) if latest_dt else None
        if age_seconds is not None and age_seconds <= max(0, int(max_age_sec)):
            continue
        item = {
            "symbol": symbol,
            "limit": int(limit),
            "reason": "missing_depth_snapshot" if latest_dt is None else "stale_depth_snapshot",
            "latest_snapshot_time": latest_dt.isoformat(timespec="seconds") if latest_dt else "",
            "age_seconds": age_seconds,
            "max_age_seconds": int(max_age_sec),
            "sample_bucket_time": ms_to_iso(bucket_ms),
            "idempotency_key": f"depth_snapshot:{symbol}:{int(limit)}:{bucket_ms}",
            "body": {"symbol": symbol, "limit": int(limit)},
        }
        items.append(item)
    return {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "limit": int(limit),
        "max_age_seconds": int(max_age_sec),
        "sample_bucket_seconds": bucket_sec,
        "sample_bucket_time": ms_to_iso(bucket_ms),
        "symbols": selected_symbols,
        "items": items,
        "summary": {
            "symbols": len(selected_symbols),
            "requests": len(items),
            "status": "ready" if items else "fresh_or_no_symbols",
        },
    }


def submit_plan(queue: BinanceApiQueue, items: list[dict[str, Any]], *, stagger_sec: int) -> dict[str, Any]:
    submitted = 0
    existing = 0
    request_ids: list[str] = []
    base_ms = now_ms()
    for idx, item in enumerate(items):
        before_counts = queue.summary().get("counts", {})
        request = queue.submit_request(
            scope="public",
            label="research_depth_snapshot",
            method="GET",
            path="/fapi/v1/depth",
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


def read_done_depth_requests(queue_db: Path) -> list[dict[str, Any]]:
    if not queue_db.exists():
        return []
    conn = sqlite3.connect(str(queue_db), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select request_id, idempotency_key, body_json, result_body_json, updated_at_ms
            from api_requests
            where status = ? and path = '/fapi/v1/depth' and idempotency_key like 'depth_snapshot:%'
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


def parse_levels(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    out: list[list[str]] = []
    for level in value:
        try:
            if isinstance(level, dict):
                price = level.get("price", level.get("p"))
                qty = level.get("quantity", level.get("qty", level.get("q")))
            else:
                price = level[0]
                qty = level[1]
            if to_float(price) > 0 and to_float(qty) > 0:
                out.append([str(price), str(qty)])
        except Exception:
            continue
    return out


def snapshot_time_ms(result: dict[str, Any], updated_at_ms: Any) -> int:
    for key in ("E", "T", "last_update_time", "time", "timestamp"):
        value = result.get(key)
        if value not in (None, ""):
            parsed = to_int(value)
            if parsed > 0:
                return parsed if parsed > 10_000_000_000 else parsed * 1000
    return to_int(updated_at_ms)


def depth_rows_from_done_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for request in requests:
        body = json_loads(request.get("body_json"), {})
        result = json_loads(request.get("result_body_json"), {})
        if not isinstance(body, dict) or not isinstance(result, dict):
            continue
        symbol = normalize_symbol(body.get("symbol") or result.get("symbol") or result.get("s"))
        if not symbol:
            continue
        bids = parse_levels(result.get("bids"))
        asks = parse_levels(result.get("asks"))
        if not bids and not asks:
            continue
        ts_ms = snapshot_time_ms(result, request.get("updated_at_ms"))
        if ts_ms <= 0:
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000, CST)
        best_bid = to_float(bids[0][0]) if bids else 0.0
        best_ask = to_float(asks[0][0]) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid else 0.0
        rows.append(
            {
                "symbol": symbol,
                "date": ts.strftime("%Y-%m-%d"),
                "snapshot_time": ts.isoformat(timespec="seconds"),
                "snapshot_time_ms": ts_ms,
                "last_update_id": to_int(result.get("lastUpdateId") or result.get("u")),
                "limit": to_int(body.get("limit")),
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": round(spread_bps, 6),
                "bids_json": json.dumps(bids, ensure_ascii=False, separators=(",", ":")),
                "asks_json": json.dumps(asks, ensure_ascii=False, separators=(",", ":")),
                "source_file": f"queue:{request.get('request_id')}",
                "source": "api_queue_depth_snapshot",
            }
        )
    return rows


def depth_key(row: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        normalize_symbol(row.get("symbol")),
        to_int(row.get("snapshot_time_ms")),
        to_int(row.get("last_update_id")),
        to_int(row.get("limit")),
    )


def dedupe_depth_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for row in rows:
        key = depth_key(row)
        if not key[0] or key[1] <= 0:
            continue
        by_key[key] = dict(row)
    return sorted(by_key.values(), key=lambda item: (str(item.get("symbol") or ""), to_int(item.get("snapshot_time_ms"))))


def write_depth_cache(rows: list[dict[str, Any]], cache_dir: Path) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        previous = latest.get(symbol)
        if previous is None or to_int(row.get("snapshot_time_ms")) > to_int(previous.get("snapshot_time_ms")):
            latest[symbol] = row
    cache_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for symbol, row in latest.items():
        bids = json_loads(row.get("bids_json"), [])
        asks = json_loads(row.get("asks_json"), [])
        payload = {
            "symbol": symbol,
            "captured_at": row.get("snapshot_time"),
            "lastUpdateId": row.get("last_update_id"),
            "limit": row.get("limit"),
            "bids": bids if isinstance(bids, list) else [],
            "asks": asks if isinstance(asks, list) else [],
            "source": row.get("source"),
        }
        (cache_dir / f"{symbol}_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    return {"cache_dir": str(cache_dir), "symbols": len(latest), "files_written": written}


def ingest_done_requests(queue_db: Path, out_dir: Path, fmt: str, *, cache_dir: Path | None = None) -> dict[str, Any]:
    done_requests = read_done_depth_requests(queue_db)
    backfill_rows = depth_rows_from_done_requests(done_requests)
    existing_rows = load_existing_rows(out_dir, "depth_snapshots", fmt)
    merged_rows = dedupe_depth_rows([*existing_rows, *backfill_rows])
    result = export_dataset(merged_rows, out_dir, "depth_snapshots", fmt)
    cache_result = write_depth_cache(merged_rows, cache_dir) if cache_dir is not None and merged_rows else {}
    return {
        "done_requests": len(done_requests),
        "backfill_rows": len(backfill_rows),
        "existing_rows": len(existing_rows),
        "merged_rows": len(merged_rows),
        "result": result,
        "depth_cache": cache_result,
    }


def render_md(payload: dict[str, Any]) -> str:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    submit = payload.get("submit") if isinstance(payload.get("submit"), dict) else {}
    ingest = payload.get("ingest") if isinstance(payload.get("ingest"), dict) else {}
    sample_rows = []
    for item in (plan.get("items") or [])[:12]:
        sample_rows.append(
            "| {symbol} | {limit} | {reason} | {age} | {bucket} |".format(
                symbol=item.get("symbol"),
                limit=item.get("limit"),
                reason=item.get("reason"),
                age=item.get("age_seconds") if item.get("age_seconds") is not None else "-",
                bucket=item.get("sample_bucket_time"),
            )
        )
    table = "\n".join(["| symbol | limit | reason | age_seconds | bucket |", "| --- | ---: | --- | ---: | --- |", *sample_rows])
    if not sample_rows:
        table = "_No depth snapshot requests planned._"
    cache = ingest.get("depth_cache") if isinstance(ingest.get("depth_cache"), dict) else {}
    return "\n\n".join(
        [
            "# Research Depth Snapshot Plan",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Queue DB: `{payload.get('queue_db')}`",
            f"- Store: `{payload.get('store_dir')}`",
            f"- Plan status: `{summary.get('status', 'unknown')}`; requests `{summary.get('requests', 0)}`",
            f"- Submit: submitted `{submit.get('submitted', 0)}`, existing `{submit.get('existing', 0)}`",
            f"- Ingest: done requests `{ingest.get('done_requests', 0)}`, rows `{ingest.get('backfill_rows', 0)}`, merged `{ingest.get('merged_rows', 0)}`",
            f"- Depth cache: files `{cache.get('files_written', 0)}`, symbols `{cache.get('symbols', 0)}`",
            "- Note: depth is current snapshot sampling only; no direct Binance call is made by this tool.",
            "## Planned Requests",
            table,
        ]
    )


def write_outputs(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "research_depth_backfill_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (reports_dir / "research_depth_backfill_latest.md").write_text(render_md(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/submit/ingest queued depth snapshot sampling for research_store")
    parser.add_argument("--store", default=str(ROOT / "research_store"))
    parser.add_argument("--queue-db", default=str(ROOT / "runtime" / "binance_api_queue.sqlite3"))
    parser.add_argument("--runtime-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--max-age-sec", type=int, default=300)
    parser.add_argument("--sample-bucket-sec", type=int, default=300)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--submit", action="store_true", help="Submit planned requests to the local central API queue")
    parser.add_argument("--stagger-sec", type=int, default=10)
    parser.add_argument("--ingest-done", action="store_true", help="Merge completed queue depth responses into research_store")
    parser.add_argument("--depth-cache-dir", default="", help="Write latest ingested snapshots to this depth_cache dir")
    parser.add_argument("--no-depth-cache", action="store_true", help="Do not refresh runtime/depth_cache on ingest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = Path(args.store)
    queue_db = Path(args.queue_db)
    existing_rows = load_existing_rows(store, "depth_snapshots", args.format)
    symbol_rows = load_symbol_seed_rows(store, args.format)
    plan = build_depth_plan(
        existing_rows,
        symbol_rows=symbol_rows,
        symbols=csv_values(args.symbols),
        limit=args.limit,
        max_symbols=args.max_symbols,
        max_age_sec=args.max_age_sec,
        sample_bucket_sec=args.sample_bucket_sec,
        now_dt=now_cst(),
    )
    submit_result: dict[str, Any] = {}
    if args.submit and plan.get("items"):
        submit_result = submit_plan(BinanceApiQueue(queue_db), plan["items"], stagger_sec=args.stagger_sec)
    ingest_result: dict[str, Any] = {}
    if args.ingest_done:
        cache_dir = None
        if not args.no_depth_cache:
            cache_dir = Path(args.depth_cache_dir) if args.depth_cache_dir else Path(args.runtime_dir) / "depth_cache"
        ingest_result = ingest_done_requests(queue_db, store, args.format, cache_dir=cache_dir)
        manifest_path = store / "depth_backfill_manifest_latest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(ingest_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    payload = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "store_dir": str(store),
        "queue_db": str(queue_db),
        "format": args.format,
        "symbol_source_rows": len(symbol_rows),
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
