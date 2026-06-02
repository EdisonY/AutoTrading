"""Lightweight Binance market-mover sentinel.

It polls the 24h ticker, writes a compact watchlist, and lets scanners include
fresh movers without doing a full heavy scan every few seconds.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from core.market_watchlist import WATCHLIST_RELATIVE_PATH, watchlist_path
from core.audit_log import write_jsonl_with_daily_shard
from core.binance_api_guard import record_public_response, wait_before_public_request
from core.event_store import insert_events
from core.sentinel_event_bus import append_sentinel_events


CST = timezone(timedelta(hours=8))
BASE_URL = os.environ.get("BINANCE_MARKET_BASE_URL", "https://fapi.binance.com")
TICKER_URL = f"{BASE_URL}/fapi/v1/ticker/24hr"

def console_log_level() -> int:
    name = os.environ.get("SENTINEL_CONSOLE_LOG_LEVEL") or os.environ.get("LOG_LEVEL", "INFO")
    return getattr(logging, name.strip().upper(), logging.INFO)


logging.basicConfig(level=console_log_level(), format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("market_mover_sentinel")


def fetch_24h_tickers(timeout: int = 10) -> list[dict[str, Any]]:
    wait_before_public_request("sentinel", TICKER_URL)
    req = urllib.request.Request(TICKER_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {418, 429}:
            record_public_response("sentinel", TICKER_URL, exc.code, body)
        raise
    return data if isinstance(data, list) else []


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def build_watchlist(
    rows: list[dict[str, Any]],
    previous: dict[str, dict[str, float]],
    *,
    top_n: int,
    velocity_threshold: float,
    min_quote_volume: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    pairs = []
    next_state: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol.endswith("USDT"):
            continue
        quote_volume = to_float(row.get("quoteVolume"))
        if quote_volume < min_quote_volume:
            continue
        change_pct = to_float(row.get("priceChangePercent"))
        last_price = to_float(row.get("lastPrice"))
        next_state[symbol] = {"change_pct": change_pct, "quote_volume": quote_volume}
        prev = previous.get(symbol) or {}
        velocity = change_pct - prev.get("change_pct", change_pct)
        volume_delta = max(0.0, quote_volume - prev.get("quote_volume", quote_volume))
        pairs.append(
            {
                "symbol": symbol,
                "change_pct": change_pct,
                "abs_change_pct": abs(change_pct),
                "velocity_pct": velocity,
                "abs_velocity_pct": abs(velocity),
                "quote_volume": quote_volume,
                "volume_delta": volume_delta,
                "last_price": last_price,
            }
        )

    gainers = sorted(pairs, key=lambda x: x["change_pct"], reverse=True)[:top_n]
    losers = sorted(pairs, key=lambda x: x["change_pct"])[:top_n]
    fast = [p for p in sorted(pairs, key=lambda x: x["abs_velocity_pct"], reverse=True) if p["abs_velocity_pct"] >= velocity_threshold][:top_n]
    liquid = sorted(pairs, key=lambda x: x["volume_delta"], reverse=True)[:top_n]

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tag, group in (("涨幅榜", gainers), ("跌幅榜", losers), ("突然加速", fast), ("放量榜", liquid)):
        for item in group:
            symbol = item["symbol"]
            if symbol in seen:
                continue
            seen.add(symbol)
            merged.append({**item, "reason": tag})
    return merged[: top_n * 3], next_state


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_log(root: Path, payload: dict[str, Any]) -> None:
    log_path = root / "logs" / "market_mover_sentinel.jsonl"
    write_jsonl_with_daily_shard(log_path, payload)


def append_watchlist_history(root: Path, payload: dict[str, Any]) -> None:
    history_path = root / "runtime" / "market_mover_watchlist_history.jsonl"
    write_jsonl_with_daily_shard(
        history_path,
        {
            "ts": payload.get("ts"),
            "ts_cst": payload.get("ts_cst"),
            "source": payload.get("source"),
            "interval_sec": payload.get("interval_sec"),
            "symbols": payload.get("symbols") or [],
        },
    )


def material_events(
    symbols: list[dict[str, Any]],
    published: dict[str, dict[str, Any]],
    emitted_at: dict[str, float],
    *,
    velocity_threshold: float,
    cooldown_sec: int,
    change_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, float]]:
    emitted = []
    current: dict[str, dict[str, Any]] = {}
    now = time.monotonic()
    for rank, item in enumerate(symbols, start=1):
        symbol = item["symbol"]
        row = {**item, "rank": rank}
        current[symbol] = row
        old = published.get(symbol)
        last_emit = emitted_at.get(symbol)
        cooldown_passed = last_emit is None or now - last_emit >= cooldown_sec
        old_change = to_float(old.get("change_pct")) if old else to_float(row.get("change_pct"))
        change_jump = abs(to_float(row.get("change_pct")) - old_change) >= change_threshold
        velocity_jump = abs(to_float(row.get("velocity_pct"))) >= max(change_threshold, velocity_threshold * 2)
        significant_change = (
            (old is None and row.get("reason") != "放量榜")
            or (old is not None and row.get("reason") == "突然加速" and old.get("reason") != "突然加速")
            or change_jump
            or velocity_jump
        )
        if cooldown_passed and significant_change:
            emitted.append(row)
            emitted_at[symbol] = now
    return emitted, current, emitted_at


def run_once(
    args: argparse.Namespace,
    previous: dict[str, dict[str, float]],
    published: dict[str, dict[str, Any]],
    emitted_at: dict[str, float],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, Any]], dict[str, float]]:
    rows = fetch_24h_tickers(timeout=args.timeout)
    symbols, next_state = build_watchlist(
        rows,
        previous,
        top_n=args.top_n,
        velocity_threshold=args.velocity_threshold,
        min_quote_volume=args.min_quote_volume,
    )
    now = datetime.now(timezone.utc)
    payload = {
        "ts": now.isoformat(),
        "ts_cst": now.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": TICKER_URL,
        "interval_sec": args.interval,
        "explain": "涨跌榜哨兵：轻量看盘名单。它只提示哪些币值得被下一轮策略扫描，不会直接开仓。",
        "symbols": symbols,
    }
    write_json_atomic(args.watchlist, payload)
    append_watchlist_history(args.root, payload)
    emitted, published, emitted_at = material_events(
        symbols,
        published,
        emitted_at,
        velocity_threshold=args.velocity_threshold,
        cooldown_sec=args.event_cooldown,
        change_threshold=args.event_change_threshold,
    )
    append_sentinel_events(args.root, emitted, scan_ts=payload["ts"], source=TICKER_URL)
    insert_events(
        args.root / "runtime" / "event_store.sqlite3",
        [{"ts": payload["ts"], "event": "SENTINEL_SIGNAL", "category": "sentinel_bus", **item} for item in emitted],
        source="sentinel/events",
    )
    append_log(args.root, {"ts": payload["ts"], "count": len(symbols), "emitted": len(emitted), "top_symbols": [s["symbol"] for s in symbols[:8]]})
    preview = ", ".join(f"{s['symbol']}({s['change_pct']:+.1f}%)" for s in symbols[:8])
    logger.info("哨兵名单 %d 个 / 新事件 %d 个: %s", len(symbols), len(emitted), preview or "-")
    return next_state, published, emitted_at


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="轻量涨跌榜哨兵")
    default_root = ROOT if (ROOT / "core").exists() else ROOT.parent
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--watchlist", type=Path, default=None)
    parser.add_argument("--interval", type=int, default=15, help="拉取 24h ticker 的间隔秒数")
    parser.add_argument("--top-n", type=int, default=20, help="每类榜单最多取多少个")
    parser.add_argument("--velocity-threshold", type=float, default=0.8, help="两次轮询之间涨跌幅变化超过多少百分点算突然加速")
    parser.add_argument("--event-cooldown", type=int, default=300, help="同币种哨兵事件最短间隔秒数")
    parser.add_argument("--event-change-threshold", type=float, default=3.0, help="触发新事件的 24h 涨跌幅最小变化百分点")
    parser.add_argument("--min-quote-volume", type=float, default=3_000_000, help="过滤成交额太小的币，单位 USDT")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    args.watchlist = args.watchlist or watchlist_path(args.root)
    if not args.watchlist.is_absolute():
        args.watchlist = args.root / args.watchlist
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    previous: dict[str, dict[str, float]] = {}
    published: dict[str, dict[str, Any]] = {}
    emitted_at: dict[str, float] = {}
    while True:
        try:
            previous, published, emitted_at = run_once(args, previous, published, emitted_at)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("拉取涨跌榜失败: %s", exc)
        except Exception:
            logger.exception("哨兵异常")
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
