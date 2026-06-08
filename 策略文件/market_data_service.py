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

from core.external_market_data import fetch_bybit_klines, fetch_bybit_tickers, fetch_coingecko_top_markets, fetch_okx_klines, fetch_okx_tickers
from core.audit_log import write_jsonl_with_daily_shard
from core.event_store import insert_events
from core.kline_cache import load_cached_klines, save_cached_klines
from core.market_watchlist import watchlist_path
from core.sentinel_event_bus import append_sentinel_events


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def previous_metric(previous: dict, symbol: str, key: str, default: float = 0.0) -> float:
    item = previous.get(symbol)
    if isinstance(item, dict):
        return to_float(item.get(key), default)
    if key == "quote_volume":
        return to_float(item, default)
    return default


def parse_kline_specs(raw: str, default: list[tuple[str, int]]) -> list[tuple[str, int]]:
    specs: list[tuple[str, int]] = []
    text = str(raw or "").strip()
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" in item:
            tf, limit_text = item.split(":", 1)
        else:
            tf, limit_text = item, "100"
        tf = tf.strip()
        try:
            limit = int(float(limit_text.strip()))
        except Exception:
            continue
        if tf and limit > 0:
            specs.append((tf, limit))
    return specs or list(default)


def unique_symbols(symbols: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in symbols:
        sym = str(raw or "").upper()
        if not sym.endswith("USDT") or not sym.isascii() or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def kline_prefetch_tasks(payload: dict) -> tuple[list[tuple[str, str, int]], dict]:
    symbol_limit = max(1, env_int("MARKET_KLINE_PREFETCH_SYMBOL_LIMIT", 180))
    hot_limit = max(0, env_int("MARKET_KLINE_PREFETCH_HOT_SYMBOL_LIMIT", 100))
    hot_specs = parse_kline_specs(
        os.environ.get("MARKET_KLINE_PREFETCH_HOT_SPECS", "15m:100,30m:100,1h:200,4h:100"),
        [("15m", 100), ("30m", 100), ("1h", 200), ("4h", 100)],
    )
    warm_specs = parse_kline_specs(
        os.environ.get("MARKET_KLINE_PREFETCH_WARM_SPECS", "15m:100,1h:200"),
        [("15m", 100), ("1h", 200)],
    )
    symbols = unique_symbols(
        list(payload.get("top_symbols") or []) + list(payload.get("market_mover_symbols") or []),
        symbol_limit,
    )
    tasks: list[tuple[str, str, int]] = []
    for idx, sym in enumerate(symbols):
        specs = hot_specs if idx < hot_limit else warm_specs
        for tf, limit in specs:
            tasks.append((sym, tf, limit))
    meta = {
        "symbol_limit": symbol_limit,
        "hot_symbol_limit": hot_limit,
        "symbols": len(symbols),
        "hot_specs": [f"{tf}:{limit}" for tf, limit in hot_specs],
        "warm_specs": [f"{tf}:{limit}" for tf, limit in warm_specs],
    }
    return tasks, meta


def prefetch_klines(root: Path, payload: dict, cursor: int) -> tuple[dict, int]:
    if not env_bool("MARKET_KLINE_PREFETCH_ENABLED", False):
        return {"enabled": False, "reason": "disabled"}, cursor

    tasks, meta = kline_prefetch_tasks(payload)
    max_requests = max(0, env_int("MARKET_KLINE_PREFETCH_MAX_REQUESTS_PER_RUN", 45))
    cache_max_age_sec = max(0, env_int("MARKET_KLINE_PREFETCH_CACHE_MAX_AGE_SEC", 600))
    status = {
        "enabled": True,
        "source": "okx_primary_bybit_fallback",
        "cache_max_age_sec": cache_max_age_sec,
        "max_requests_per_run": max_requests,
        "tasks_total": len(tasks),
        "visited": 0,
        "fresh": 0,
        "attempted": 0,
        "saved": 0,
        "failed": 0,
        "rate_limited": "",
        "source_counts": {},
        **meta,
    }
    if not tasks or max_requests <= 0:
        status["cursor"] = 0
        return status, 0

    cursor = int(cursor) % len(tasks)
    visited = 0
    while visited < len(tasks) and status["attempted"] < max_requests:
        idx = (cursor + visited) % len(tasks)
        sym, tf, limit = tasks[idx]
        visited += 1
        if load_cached_klines(root, sym, tf, limit, max_age_sec=cache_max_age_sec):
            status["fresh"] += 1
            continue

        status["attempted"] += 1
        rows: list[list[str]] = []
        source = ""
        try:
            rows = fetch_okx_klines(sym, tf, limit)
            source = "okx" if rows else ""
        except Exception as exc:
            if "okx_rate_budget_exhausted" in str(exc):
                status["rate_limited"] = "okx"
                break
        if not rows:
            try:
                rows = fetch_bybit_klines(sym, tf, limit)
                source = "bybit" if rows else ""
            except Exception as bybit_exc:
                if "bybit_rate_budget_exhausted" in str(bybit_exc):
                    status["rate_limited"] = "bybit"
                    break
                rows = []
        if rows:
            save_cached_klines(root, sym, tf, limit, rows)
            status["saved"] += 1
            counts = status["source_counts"]
            counts[source] = int(counts.get(source, 0)) + 1
        else:
            status["failed"] += 1

    next_cursor = (cursor + visited) % len(tasks)
    status["visited"] = visited
    status["cursor"] = next_cursor
    if tasks:
        next_sym, next_tf, next_limit = tasks[next_cursor]
        status["next_task"] = f"{next_sym}:{next_tf}:{next_limit}"
    return status, next_cursor


def build_market_mover_watchlist(
    rows: list[tuple[str, float, float, dict]],
    previous_state: dict,
    *,
    top_n: int,
    interval_sec: int,
    velocity_threshold: float,
    min_quote_volume: float,
    volume_spike_mult: float,
    early_enabled: bool = True,
    early_price_tick_threshold: float = 0.45,
    early_max_change_pct: float = 5.0,
    early_min_quote_volume: float = 1_000_000,
) -> dict:
    pairs = []
    lowest_min_volume = min_quote_volume
    if early_enabled:
        lowest_min_volume = min(min_quote_volume, early_min_quote_volume)
    for sym, quote_volume, change_pct, item in rows:
        if quote_volume < lowest_min_volume:
            continue
        prev_change = previous_metric(previous_state, sym, "change_pct", change_pct)
        prev_volume = previous_metric(previous_state, sym, "quote_volume", quote_volume)
        last_price = to_float(item.get("last"))
        prev_price = previous_metric(previous_state, sym, "last_price", last_price)
        velocity_pct = change_pct - prev_change
        price_tick_pct = ((last_price - prev_price) / prev_price * 100.0) if prev_price > 0 and last_price > 0 else 0.0
        volume_delta = max(0.0, quote_volume - prev_volume)
        volume_mult = quote_volume / prev_volume if prev_volume > 0 else 1.0
        pairs.append(
            {
                "symbol": sym,
                "change_pct": change_pct,
                "abs_change_pct": abs(change_pct),
                "velocity_pct": velocity_pct,
                "abs_velocity_pct": abs(velocity_pct),
                "price_tick_pct": price_tick_pct,
                "abs_price_tick_pct": abs(price_tick_pct),
                "quote_volume": quote_volume,
                "volume_delta": volume_delta,
                "volume_mult": volume_mult,
                "last_price": last_price,
                "source": item.get("source") or "external",
                "sources": item.get("sources") or [item.get("source") or "external"],
            }
        )

    normal_pairs = [p for p in pairs if p["quote_volume"] >= min_quote_volume]
    gainers = [p for p in sorted(normal_pairs, key=lambda x: x["change_pct"], reverse=True) if p["change_pct"] > 0][:top_n]
    losers = [p for p in sorted(normal_pairs, key=lambda x: x["change_pct"]) if p["change_pct"] < 0][:top_n]
    fast = [p for p in sorted(normal_pairs, key=lambda x: x["abs_velocity_pct"], reverse=True) if p["abs_velocity_pct"] >= velocity_threshold][:top_n]
    volume = [p for p in sorted(normal_pairs, key=lambda x: x["volume_mult"], reverse=True) if p["volume_mult"] >= volume_spike_mult][:top_n]
    early_up: list[dict] = []
    early_down: list[dict] = []
    if early_enabled:
        early_candidates = [
            p for p in pairs
            if p["quote_volume"] >= early_min_quote_volume
            and abs(p["change_pct"]) <= early_max_change_pct
            and p["abs_price_tick_pct"] >= early_price_tick_threshold
        ]
        early_up = [
            {**p, "phase": "起涨初段"}
            for p in sorted(early_candidates, key=lambda x: x["price_tick_pct"], reverse=True)
            if p["price_tick_pct"] > 0
        ][:top_n]
        early_down = [
            {**p, "phase": "起跌初段"}
            for p in sorted(early_candidates, key=lambda x: x["price_tick_pct"])
            if p["price_tick_pct"] < 0
        ][:top_n]

    merged = []
    seen = set()
    for reason, group in (
        ("起涨捕捉", early_up),
        ("起跌捕捉", early_down),
        ("突然加速", fast),
        ("成交额突增", volume),
        ("涨幅榜", gainers),
        ("跌幅榜", losers),
    ):
        for item in group:
            symbol = item["symbol"]
            if symbol in seen:
                continue
            seen.add(symbol)
            merged.append({**item, "reason": reason, "rank": len(merged) + 1})

    now = datetime.now(timezone.utc)
    return {
        "ts": now.isoformat(),
        "source": "okx_bybit_coingecko",
        "interval_sec": interval_sec,
        "explain": "外部行情涨跌榜哨兵：用 OKX/Bybit/CoinGecko 公共数据捕捉暴涨暴跌、突然加速和成交额突增。它只提示下一轮策略优先扫描，不直接开仓。",
        "thresholds": {
            "velocity_pct": velocity_threshold,
            "min_quote_volume": min_quote_volume,
            "volume_spike_mult": volume_spike_mult,
            "early_price_tick_pct": early_price_tick_threshold,
            "early_max_24h_change_pct": early_max_change_pct,
            "early_min_quote_volume": early_min_quote_volume,
        },
        "early_mover_count": len(early_up) + len(early_down),
        "symbols": merged[: top_n * 3],
    }


def append_watchlist_history(root: Path, payload: dict) -> None:
    write_jsonl_with_daily_shard(
        root / "runtime" / "market_mover_watchlist_history.jsonl",
        {
            "ts": payload.get("ts"),
            "source": payload.get("source"),
            "interval_sec": payload.get("interval_sec"),
            "symbols": payload.get("symbols") or [],
        },
    )


def material_events(
    symbols: list[dict],
    published: dict[str, dict],
    emitted_at: dict[str, float],
    *,
    velocity_threshold: float,
    cooldown_sec: int,
    change_threshold: float,
) -> tuple[list[dict], dict[str, dict], dict[str, float]]:
    emitted = []
    current: dict[str, dict] = {}
    now = time.monotonic()
    for rank, item in enumerate(symbols, start=1):
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        row = {**item, "rank": rank}
        current[symbol] = row
        old = published.get(symbol)
        last_emit = emitted_at.get(symbol)
        cooldown_passed = last_emit is None or now - last_emit >= cooldown_sec
        old_change = to_float(old.get("change_pct")) if old else to_float(row.get("change_pct"))
        change_jump = abs(to_float(row.get("change_pct")) - old_change) >= change_threshold
        velocity_jump = abs(to_float(row.get("velocity_pct"))) >= max(change_threshold, velocity_threshold * 2)
        significant_change = (
            (old is None and row.get("reason") != "成交额突增")
            or (old is not None and row.get("reason") == "突然加速" and old.get("reason") != "突然加速")
            or change_jump
            or velocity_jump
        )
        if cooldown_passed and significant_change:
            emitted.append(row)
            emitted_at[symbol] = now
    return emitted, current, emitted_at


def build_payload(
    previous_state: dict,
    top_limit: int,
    coingecko_top: list[dict] | None = None,
    *,
    coingecko_age_sec: float | None = None,
    interval_sec: int = 60,
) -> tuple[dict, dict[str, dict[str, float]], dict]:
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
            sources = sorted({str(item.get("source") or "external")} | set(existing.get("sources", []) if existing else []))
            if abs(change_pct) <= 0 and existing is not None:
                change_pct = float(existing.get("change_pct") or 0.0)
            merged[sym] = {
                "symbol": sym,
                "quote_volume": quote_volume,
                "change_pct": change_pct,
                "last": float(item.get("last") or 0.0),
                "source": item.get("source") or "external",
                "sources": sources,
            }
        elif existing is not None:
            existing["sources"] = sorted(set(existing.get("sources") or []) | {str(item.get("source") or "external")})
            if abs(change_pct) > abs(float(existing.get("change_pct") or 0.0)):
                existing["change_pct"] = change_pct
            if float(item.get("last") or 0.0) > 0 and float(existing.get("last") or 0.0) <= 0:
                existing["last"] = float(item.get("last") or 0.0)

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
    current_state: dict[str, dict[str, float]] = {}
    for sym, item in merged.items():
        quote_volume = float(item.get("quote_volume") or 0.0)
        change_pct = float(item.get("change_pct") or 0.0)
        current_state[sym] = {"quote_volume": quote_volume, "change_pct": change_pct, "last_price": float(item.get("last") or 0.0)}
        rows.append((sym, quote_volume, change_pct, item))
    rows.sort(key=lambda x: (x[1], -market_cap_rank.get(x[0], 999999)), reverse=True)
    spikes = []
    for sym, item in current_state.items():
        volume = item["quote_volume"]
        prev = previous_metric(previous_state, sym, "quote_volume", 0.0)
        if prev > 0 and volume > prev * 5:
            spikes.append((sym, volume / prev, volume))
    spikes.sort(key=lambda x: x[1], reverse=True)
    watchlist = build_market_mover_watchlist(
        rows,
        previous_state,
        top_n=env_int("MARKET_MOVER_TOP_N", 20),
        interval_sec=interval_sec,
        velocity_threshold=env_float("MARKET_MOVER_VELOCITY_THRESHOLD_PCT", 0.8),
        min_quote_volume=env_float("MARKET_MOVER_MIN_QUOTE_VOLUME", 3_000_000),
        volume_spike_mult=env_float("MARKET_MOVER_VOLUME_SPIKE_MULT", 5.0),
        early_enabled=env_bool("MARKET_MOVER_EARLY_ENABLED", True),
        early_price_tick_threshold=env_float("MARKET_MOVER_EARLY_PRICE_TICK_PCT", 0.45),
        early_max_change_pct=env_float("MARKET_MOVER_EARLY_MAX_24H_CHANGE_PCT", 5.0),
        early_min_quote_volume=env_float("MARKET_MOVER_EARLY_MIN_QUOTE_VOLUME", 1_000_000),
    )
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
        "market_mover_symbols": [str(item.get("symbol") or "").upper() for item in watchlist.get("symbols") or []],
        "market_mover_count": len(watchlist.get("symbols") or []),
        "early_mover_count": int(watchlist.get("early_mover_count") or 0),
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
        "market_mover_preview": (watchlist.get("symbols") or [])[:20],
    }
    return payload, current_state, watchlist


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
    watchlist_out = watchlist_path(args.root)
    previous_state: dict[str, dict[str, float]] = {}
    published: dict[str, dict] = {}
    emitted_at: dict[str, float] = {}
    coingecko_top: list[dict] = []
    coingecko_last_fetch = 0.0
    coingecko_refresh_sec = max(300, env_int("COINGECKO_REFRESH_INTERVAL_SEC", 1800))
    kline_prefetch_cursor = 0
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
            payload, previous_state, watchlist = build_payload(
                previous_state,
                args.top_limit,
                coingecko_top,
                coingecko_age_sec=age,
                interval_sec=max(5, args.interval),
            )
            kline_prefetch_status, kline_prefetch_cursor = prefetch_klines(args.root, payload, kline_prefetch_cursor)
            payload["kline_prefetch"] = kline_prefetch_status
            atomic_write(out, payload)
            atomic_write(watchlist_out, watchlist)
            append_watchlist_history(args.root, watchlist)
            emitted, published, emitted_at = material_events(
                watchlist.get("symbols") or [],
                published,
                emitted_at,
                velocity_threshold=env_float("MARKET_MOVER_VELOCITY_THRESHOLD_PCT", 0.8),
                cooldown_sec=env_int("MARKET_MOVER_EVENT_COOLDOWN_SEC", 300),
                change_threshold=env_float("MARKET_MOVER_EVENT_CHANGE_THRESHOLD_PCT", 3.0),
            )
            try:
                append_sentinel_events(args.root, emitted, scan_ts=watchlist["ts"], source=watchlist["source"])
                insert_events(
                    args.root / "runtime" / "event_store.sqlite3",
                    [{"ts": watchlist["ts"], "event": "SENTINEL_SIGNAL", "category": "sentinel_bus", **item} for item in emitted],
                    source="market-data/sentinel",
                )
            except Exception as exc:
                print(json.dumps({"status": "warn", "sink": "sentinel_events", "error": str(exc)[:180]}), flush=True)
            print(json.dumps({
                "status": "ok",
                "symbols": len(payload["available_symbols"]),
                "top": len(payload["top_symbols"]),
                "spikes": len(payload["spike_symbols"]),
                "movers": len(watchlist.get("symbols") or []),
                "early_movers": int(watchlist.get("early_mover_count") or 0),
                "events": len(emitted),
                "kline_prefetch": {
                    "enabled": kline_prefetch_status.get("enabled"),
                    "attempted": kline_prefetch_status.get("attempted"),
                    "saved": kline_prefetch_status.get("saved"),
                    "fresh": kline_prefetch_status.get("fresh"),
                    "rate_limited": kline_prefetch_status.get("rate_limited"),
                },
            }), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)[:240]}), flush=True)
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
