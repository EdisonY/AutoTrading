from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent if ROOT.name == "策略文件" else ROOT
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("OKX_MARKET_DATA_ENABLED", "1")
os.environ.setdefault("BYBIT_MARKET_DATA_ENABLED", "1")
os.environ.setdefault("COINGECKO_MARKET_DATA_ENABLED", "1")

from core.external_market_data import fetch_bybit_tickers, fetch_coingecko_top_markets, fetch_okx_tickers


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


def build_payload(
    prev_volumes: dict[str, float],
    top_limit: int,
    coingecko_top: list[dict] | None = None,
    *,
    coingecko_age_sec: float | None = None,
) -> tuple[dict, dict[str, float]]:
    raw_rows = []
    source_errors = []
    for source, fetcher in (("okx", fetch_okx_tickers), ("bybit", fetch_bybit_tickers)):
        try:
            fetched = fetcher()
            raw_rows.extend(fetched)
        except Exception as exc:
            source_errors.append({"source": source, "error": str(exc)[:180]})

    coingecko_top = coingecko_top or []

    merged: dict[str, dict] = {}
    for item in raw_rows:
        sym = str(item.get("symbol") or "").upper()
        if not sym.endswith("USDT") or not sym.isascii():
            continue
        quote_volume = float(item.get("quote_volume") or 0.0)
        change_pct = float(item.get("change_pct") or 0.0)
        existing = merged.get(sym)
        if existing is None or quote_volume > float(existing.get("quote_volume") or 0.0):
            merged[sym] = {
                "symbol": sym,
                "quote_volume": quote_volume,
                "change_pct": change_pct,
                "last": float(item.get("last") or 0.0),
                "source": item.get("source") or "external",
                "sources": sorted({str(item.get("source") or "external")} | set(existing.get("sources", []) if existing else [])),
            }
        elif existing is not None:
            existing["sources"] = sorted(set(existing.get("sources") or []) | {str(item.get("source") or "external")})

    market_cap_rank: dict[str, int] = {}
    for idx, item in enumerate(coingecko_top, start=1):
        sym = str(item.get("symbol") or "").upper()
        if sym.endswith("USDT") and sym.isascii():
            market_cap_rank.setdefault(sym, idx)
            merged.setdefault(sym, {
                "symbol": sym,
                "quote_volume": 0.0,
                "change_pct": 0.0,
                "last": float(item.get("price") or 0.0),
                "source": "coingecko",
                "sources": ["coingecko"],
            })

    rows = []
    current_volumes = {}
    for sym, item in merged.items():
        quote_volume = float(item.get("quote_volume") or 0.0)
        current_volumes[sym] = quote_volume
        rows.append((sym, quote_volume, float(item.get("change_pct") or 0.0), item))
    rows.sort(key=lambda x: (x[1], -market_cap_rank.get(x[0], 999999)), reverse=True)
    spikes = []
    for sym, volume in current_volumes.items():
        prev = prev_volumes.get(sym, 0)
        if prev > 0 and volume > prev * 5:
            spikes.append((sym, volume / prev, volume))
    spikes.sort(key=lambda x: x[1], reverse=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "unix_ts": time.time(),
        "source": "okx_bybit_coingecko",
        "sources": sorted({src for _, _, _, item in rows for src in (item.get("sources") or [item.get("source") or "external"])}),
        "source_errors": source_errors,
        "coingecko_age_sec": coingecko_age_sec,
        "available_symbols": [sym for sym, _, _, _ in rows],
        "top_symbols": [sym for sym, _, _, _ in rows[:top_limit]],
        "spike_symbols": [sym for sym, _, _ in spikes[:top_limit]],
        "coingecko_top_symbols": [str(item.get("symbol") or "").upper() for item in coingecko_top],
        "top_preview": [
            {
                "symbol": sym,
                "quote_volume": volume,
                "change_pct": change,
                "source": item.get("source"),
                "sources": item.get("sources") or [item.get("source")],
                "market_cap_rank": market_cap_rank.get(sym),
            }
            for sym, volume, change, item in rows[:20]
        ],
        "spike_preview": [
            {"symbol": sym, "volume_mult": mult, "quote_volume": volume}
            for sym, mult, volume in spikes[:20]
        ],
    }
    return payload, current_volumes


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified lightweight market data cache service.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--top-limit", type=int, default=160)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    out = args.root / "runtime" / "market_data_cache.json"
    prev_volumes: dict[str, float] = {}
    coingecko_top: list[dict] = []
    coingecko_last_fetch = 0.0
    coingecko_refresh_sec = max(300, env_int("COINGECKO_REFRESH_INTERVAL_SEC", 1800))
    while True:
        try:
            now = time.time()
            if not coingecko_top or now - coingecko_last_fetch >= coingecko_refresh_sec:
                try:
                    coingecko_top = fetch_coingecko_top_markets(limit=max(100, min(250, args.top_limit)))
                    coingecko_last_fetch = now
                except Exception as exc:
                    print(json.dumps({"status": "warn", "source": "coingecko", "error": str(exc)[:180]}), flush=True)
            age = (time.time() - coingecko_last_fetch) if coingecko_last_fetch else None
            payload, prev_volumes = build_payload(
                prev_volumes,
                args.top_limit,
                coingecko_top,
                coingecko_age_sec=age,
            )
            atomic_write(out, payload)
            print(json.dumps({"status": "ok", "symbols": len(payload["available_symbols"]), "top": len(payload["top_symbols"]), "spikes": len(payload["spike_symbols"])}), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)[:240]}), flush=True)
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
