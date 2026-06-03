"""Export synced kline cache into research_store klines/features partitions."""

from __future__ import annotations

import argparse
import json
import re
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
CST = timezone(timedelta(hours=8))
NAME_RE = re.compile(r"^(?P<symbol>[A-Z0-9]+)_(?P<interval>[0-9a-z]+)_(?P<limit>[0-9]+)\.json$")


def now_cst() -> datetime:
    return datetime.now(CST)


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


def dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, CST)


def normalize_date(value: Any, open_time_ms: Any = None) -> str:
    text = str(value or "")
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    ms = to_int(open_time_ms)
    if ms > 0:
        return dt_from_ms(ms).strftime("%Y-%m-%d")
    return "unknown"


def normalize_kline_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    open_time_ms = to_int(out.get("open_time_ms"))
    out["symbol"] = str(out.get("symbol") or "").upper()
    out["interval"] = str(out.get("interval") or "")
    out["open_time_ms"] = open_time_ms
    if open_time_ms > 0:
        open_dt = dt_from_ms(open_time_ms)
        out["date"] = open_dt.strftime("%Y-%m-%d")
        out["open_time"] = open_dt.isoformat(timespec="seconds")
    else:
        out["date"] = normalize_date(out.get("date"), open_time_ms)
    if "close_time_ms" in out:
        out["close_time_ms"] = to_int(out.get("close_time_ms"))
    return out


def read_cache(path: Path) -> tuple[dict[str, Any], list[list[Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, []
    rows = payload.get("rows")
    return payload if isinstance(payload, dict) else {}, rows if isinstance(rows, list) else []


def parse_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    match = NAME_RE.match(path.name)
    if not match:
        return [], []
    symbol = match.group("symbol")
    interval = match.group("interval")
    limit = to_int(match.group("limit"))
    payload, rows = read_cache(path)
    cache_ts = to_float(payload.get("ts"))
    cache_dt = datetime.fromtimestamp(cache_ts, CST) if cache_ts else None
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            continue
        open_time_ms = to_int(row[0])
        close_time_ms = to_int(row[6])
        if not open_time_ms:
            continue
        open_dt = dt_from_ms(open_time_ms)
        open_price = to_float(row[1])
        high = to_float(row[2])
        low = to_float(row[3])
        close = to_float(row[4])
        volume = to_float(row[5])
        quote_volume = to_float(row[7])
        parsed.append(
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "date": open_dt.strftime("%Y-%m-%d"),
                "open_time": open_dt.isoformat(timespec="seconds"),
                "open_time_ms": open_time_ms,
                "close_time_ms": close_time_ms,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "quote_volume": quote_volume,
                "cache_ts": cache_dt.isoformat(timespec="seconds") if cache_dt else "",
                "source_file": path.name,
            }
        )
    parsed.sort(key=lambda item: (item["symbol"], item["interval"], item["open_time_ms"]))
    features: list[dict[str, Any]] = []
    closes: list[float] = []
    for idx, item in enumerate(parsed):
        open_price = float(item["open"] or 0)
        close = float(item["close"] or 0)
        high = float(item["high"] or 0)
        low = float(item["low"] or 0)
        prev_close = closes[-1] if closes else 0.0
        closes.append(close)
        ret_1 = (close - prev_close) / prev_close * 100 if prev_close else 0.0
        ret_open_close = (close - open_price) / open_price * 100 if open_price else 0.0
        range_pct = (high - low) / open_price * 100 if open_price else 0.0
        rolling_3 = closes[-3:]
        rolling_10 = closes[-10:]
        ret_3 = (close - rolling_3[0]) / rolling_3[0] * 100 if len(rolling_3) >= 3 and rolling_3[0] else 0.0
        ret_10 = (close - rolling_10[0]) / rolling_10[0] * 100 if len(rolling_10) >= 10 and rolling_10[0] else 0.0
        features.append(
            {
                "symbol": item["symbol"],
                "interval": item["interval"],
                "date": item["date"],
                "open_time": item["open_time"],
                "open_time_ms": item["open_time_ms"],
                "close": close,
                "return_1_pct": round(ret_1, 6),
                "return_3_pct": round(ret_3, 6),
                "return_10_pct": round(ret_10, 6),
                "body_pct": round(ret_open_close, 6),
                "range_pct": round(range_pct, 6),
                "quote_volume": item["quote_volume"],
                "bar_index_in_cache": idx,
                "source_file": item["source_file"],
            }
        )
    return parsed, features


def kline_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (str(row.get("symbol") or ""), str(row.get("interval") or ""), to_int(row.get("open_time_ms")))


def dedupe_kline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        row = normalize_kline_row(row)
        key = kline_key(row)
        if not key[0] or not key[1] or key[2] <= 0:
            continue
        previous = by_key.get(key)
        if previous is None or str(row.get("cache_ts") or "") >= str(previous.get("cache_ts") or ""):
            by_key[key] = row
    return sorted(by_key.values(), key=lambda item: (item["symbol"], item["interval"], item["open_time_ms"]))


def build_features(kline_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in dedupe_kline_rows(kline_rows):
        grouped.setdefault((str(row["symbol"]), str(row["interval"])), []).append(row)
    for (_symbol, _interval), rows in sorted(grouped.items()):
        closes: list[float] = []
        for idx, item in enumerate(sorted(rows, key=lambda row: to_int(row.get("open_time_ms")))):
            open_price = float(item.get("open") or 0)
            close = float(item.get("close") or 0)
            high = float(item.get("high") or 0)
            low = float(item.get("low") or 0)
            prev_close = closes[-1] if closes else 0.0
            closes.append(close)
            ret_1 = (close - prev_close) / prev_close * 100 if prev_close else 0.0
            ret_open_close = (close - open_price) / open_price * 100 if open_price else 0.0
            range_pct = (high - low) / open_price * 100 if open_price else 0.0
            rolling_3 = closes[-3:]
            rolling_10 = closes[-10:]
            ret_3 = (close - rolling_3[0]) / rolling_3[0] * 100 if len(rolling_3) >= 3 and rolling_3[0] else 0.0
            ret_10 = (close - rolling_10[0]) / rolling_10[0] * 100 if len(rolling_10) >= 10 and rolling_10[0] else 0.0
            features.append(
                {
                    "symbol": item["symbol"],
                    "interval": item["interval"],
                    "date": normalize_date(item.get("date"), item.get("open_time_ms")),
                    "open_time": item["open_time"],
                    "open_time_ms": item["open_time_ms"],
                    "close": close,
                    "return_1_pct": round(ret_1, 6),
                    "return_3_pct": round(ret_3, 6),
                    "return_10_pct": round(ret_10, 6),
                    "body_pct": round(ret_open_close, 6),
                    "range_pct": round(range_pct, 6),
                    "quote_volume": item.get("quote_volume"),
                    "bar_index_in_cache": idx,
                    "bar_index_in_series": idx,
                    "source_file": item.get("source_file", ""),
                }
            )
    return features


def load_existing_rows(out_dir: Path, table: str, fmt: str) -> list[dict[str, Any]]:
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    files = sorted((out_dir / table).glob(f"date=*/data.{suffix}"))
    if not files:
        return []
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required for research_kline_features.py") from exc
    frames = []
    for path in files:
        try:
            frame = pd.read_parquet(path) if fmt == "parquet" else pd.read_json(path, orient="records", lines=True)
        except Exception:
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return []
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.where(pd.notnull(merged), None)
    return [dict(row) for row in merged.to_dict(orient="records")]


def write_frame(rows: list[dict[str, Any]], path: Path, fmt: str) -> int:
    if not rows:
        return 0
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required for research_kline_features.py") from exc
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if fmt == "parquet":
        try:
            df.to_parquet(tmp, index=False)
        except ImportError as exc:
            raise SystemExit("pyarrow is required for --format parquet. Install requirements or rerun with --format jsonl.") from exc
    elif fmt == "jsonl":
        df.to_json(tmp, orient="records", lines=True, force_ascii=False)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    tmp.replace(path)
    return int(len(df))


def export_dataset(rows: list[dict[str, Any]], out_dir: Path, table: str, fmt: str) -> dict[str, Any]:
    suffix = "parquet" if fmt == "parquet" else "jsonl"
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = normalize_date(row.get("date"), row.get("open_time_ms"))
        by_date.setdefault(day, []).append({**row, "date": day})
    partitions: dict[str, dict[str, Any]] = {}
    files = 0
    total = 0
    for day, day_rows in sorted(by_date.items()):
        target = out_dir / table / f"date={day}" / f"data.{suffix}"
        count = write_frame(day_rows, target, fmt)
        if count:
            files += 1
            total += count
            partitions[day] = {"rows": count, "path": str(target), "status": "written"}
    return {"table": table, "status": "ok", "files": files, "rows": total, "partitions": partitions}


def coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"rows": 0, "symbols": 0, "intervals": [], "first_open_time": "", "latest_open_time": ""}
    return {
        "rows": len(rows),
        "symbols": len({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
        "intervals": sorted({str(row.get("interval") or "") for row in rows if row.get("interval")}),
        "first_open_time": min(str(row.get("open_time") or "") for row in rows if row.get("open_time")),
        "latest_open_time": max(str(row.get("open_time") or "") for row in rows if row.get("open_time")),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export synced kline cache into research_store klines/features")
    parser.add_argument("--cache-dir", default=str(ROOT / "server_logs_tencent" / "runtime" / "kline_cache"))
    parser.add_argument("--out-dir", default=str(ROOT / "research_store"))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--limit-files", type=int, default=500)
    parser.add_argument("--format", choices=["parquet", "jsonl"], default="parquet")
    parser.add_argument("--no-merge-existing", action="store_true", help="Do not merge existing research_store Kline partitions")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out_dir)
    cutoff = now_cst() - timedelta(days=max(1, args.days))
    files = []
    if cache_dir.exists():
        files = [
            path for path in cache_dir.glob("*.json")
            if datetime.fromtimestamp(path.stat().st_mtime, CST) >= cutoff
        ]
    files = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[: max(1, args.limit_files)]
    kline_rows: list[dict[str, Any]] = []
    for path in files:
        rows, _features = parse_file(path)
        kline_rows.extend(rows)
    existing_rows = [] if args.no_merge_existing else load_existing_rows(out_dir, "klines", args.format)
    merged_kline_rows = dedupe_kline_rows([*existing_rows, *kline_rows])
    feature_rows = build_features(merged_kline_rows)
    results = [
        export_dataset(merged_kline_rows, out_dir, "klines", args.format),
        export_dataset(feature_rows, out_dir, "features", args.format),
    ]
    manifest = {
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "cache_dir": str(cache_dir),
        "out_dir": str(out_dir),
        "days": args.days,
        "format": args.format,
        "merge_existing": not args.no_merge_existing,
        "files_scanned": len(files),
        "cache_rows": len(kline_rows),
        "existing_rows": len(existing_rows),
        "merged_rows": len(merged_kline_rows),
        "coverage": coverage(merged_kline_rows),
        "results": results,
    }
    path = out_dir / "kline_features_manifest_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
