"""Collect compact market microstructure features for paper B/v16.

Stores features, not raw full tick history. This keeps disk use bounded while
preserving enough evidence for v16 replay/evolution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OKX_MARKET_DATA_ENABLED", "1")
os.environ.setdefault("BYBIT_MARKET_DATA_ENABLED", "1")
os.environ.setdefault("OKX_MARKET_DATA_MAX_PER_MIN", "60")
os.environ.setdefault("BYBIT_MARKET_DATA_MAX_PER_MIN", "60")

from core.external_market_data import (
    fetch_bybit_cvd,
    fetch_bybit_funding_rate,
    fetch_bybit_ofi,
    fetch_okx_cvd,
    fetch_okx_funding_rate,
    fetch_okx_ofi,
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def top_symbols(root: Path, limit: int) -> list[str]:
    cache = read_json(root / "runtime" / "market_data_cache.json")
    symbols = cache.get("top_symbols") or cache.get("available_symbols") or []
    out = []
    for sym in symbols:
        symbol = str(sym or "").upper()
        if symbol.endswith("USDT") and symbol.isascii() and symbol not in out:
            out.append(symbol)
        if len(out) >= limit:
            break
    if out:
        return out
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"][:limit]


def first_value(symbol: str, fetchers: list[tuple[str, Any]]) -> tuple[float | None, str]:
    for source, fn in fetchers:
        try:
            value = fn(symbol)
            if value is not None:
                return float(value), source
        except Exception:
            continue
    return None, "missing"


def collect_symbol(symbol: str, funding_due: bool) -> dict[str, Any]:
    now = time.time()
    ofi, ofi_source = first_value(symbol, [("okx", fetch_okx_ofi), ("bybit", fetch_bybit_ofi)])
    cvd, cvd_source = first_value(symbol, [("okx", fetch_okx_cvd), ("bybit", fetch_bybit_cvd)])
    funding = None
    funding_source = "not_due"
    if funding_due:
        funding, funding_source = first_value(symbol, [("okx", fetch_okx_funding_rate), ("bybit", fetch_bybit_funding_rate)])
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "unix_ts": now,
        "symbol": symbol,
        "ofi": ofi if ofi is not None else 0.0,
        "ofi_source": ofi_source,
        "cvd": cvd if cvd is not None else 0.0,
        "cvd_source": cvd_source,
        "funding_rate": funding if funding is not None else 0.0,
        "funding_source": funding_source,
        "quality": "ok" if ofi_source != "missing" and cvd_source != "missing" else "partial",
    }


def append_jsonl(root: Path, row: dict[str, Any]) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = root / "runtime" / "market_microstructure" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def prune_partitions(root: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    base = root / "runtime" / "market_microstructure"
    for path in base.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def run_once(root: Path, top_limit: int, batch_size: int, cursor: int, funding_interval_sec: int, retention_days: int) -> dict[str, Any]:
    symbols = top_symbols(root, top_limit)
    if not symbols:
        return {"status": "no_symbols", "features": 0, "cursor": cursor}
    latest = read_json(root / "runtime" / "market_microstructure_latest.json")
    features = latest.get("features") if isinstance(latest.get("features"), dict) else {}
    start = cursor % len(symbols)
    batch = [symbols[(start + i) % len(symbols)] for i in range(min(batch_size, len(symbols)))]
    now = time.time()
    written = 0
    for symbol in batch:
        old = features.get(symbol) if isinstance(features.get(symbol), dict) else {}
        last_funding = float(old.get("funding_unix_ts") or 0.0)
        funding_due = now - last_funding >= funding_interval_sec
        row = collect_symbol(symbol, funding_due)
        if not funding_due and old:
            row["funding_rate"] = float(old.get("funding_rate") or 0.0)
            row["funding_source"] = old.get("funding_source") or "cached"
            row["funding_unix_ts"] = last_funding
        else:
            row["funding_unix_ts"] = now
        features[symbol] = row
        append_jsonl(root, row)
        written += 1
    removed = prune_partitions(root, retention_days)
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "unix_ts": now,
        "source": "okx_primary_bybit_fallback",
        "top_limit": top_limit,
        "batch_size": batch_size,
        "cursor": (start + written) % len(symbols),
        "coverage_symbols": len(features),
        "fresh_symbols_240s": sum(1 for row in features.values() if isinstance(row, dict) and now - float(row.get("unix_ts") or 0.0) <= 240),
        "retention_days": retention_days,
        "removed_partitions": removed,
        "features": features,
    }
    atomic_write(root / "runtime" / "market_microstructure_latest.json", out)
    return {"status": "ok", "written": written, "coverage": out["coverage_symbols"], "fresh_240s": out["fresh_symbols_240s"], "cursor": out["cursor"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect compact market microstructure features")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--interval", type=int, default=int(os.environ.get("MICROSTRUCTURE_INTERVAL_SEC", "30")))
    parser.add_argument("--top-limit", type=int, default=int(os.environ.get("MICROSTRUCTURE_TOP_LIMIT", "100")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("MICROSTRUCTURE_BATCH_SIZE", "15")))
    parser.add_argument("--funding-interval-sec", type=int, default=int(os.environ.get("MICROSTRUCTURE_FUNDING_INTERVAL_SEC", "1800")))
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("MICROSTRUCTURE_RETENTION_DAYS", "14")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    cursor = int(read_json(args.root / "runtime" / "market_microstructure_latest.json").get("cursor") or 0)
    while True:
        result = run_once(args.root, args.top_limit, args.batch_size, cursor, args.funding_interval_sec, args.retention_days)
        cursor = int(result.get("cursor") or 0)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
