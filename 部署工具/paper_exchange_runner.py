"""Maintain full-run paper exchange positions and PnL.

This tool uses local caches first and OKX public market data as fallback. It
never submits Binance requests and never touches real orders.
"""

from __future__ import annotations

import argparse
import json
import os
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OKX_MARKET_DATA_ENABLED", "1")
os.environ.setdefault("OKX_MARKET_DATA_MAX_PER_MIN", "60")

from core.event_store import EventStoreWriter
from core.external_market_data import fetch_okx_funding_rate, fetch_okx_klines, okx_symbol_supported
from core.kline_cache import load_latest_cached_close, save_cached_klines
from core.paper_exchange import PaperExchange, STRATEGIES, safe_float


CST = timezone(timedelta(hours=8))
DEFAULT_BOOTSTRAP_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT",
    "TRXUSDT", "DOTUSDT", "BCHUSDT", "LTCUSDT", "UNIUSDT",
    "APTUSDT", "NEARUSDT", "AAVEUSDT", "FILUSDT", "ARBUSDT",
    "OPUSDT", "INJUSDT", "TIAUSDT", "WIFUSDT", "SEIUSDT",
    "ORDIUSDT", "ETCUSDT", "ATOMUSDT", "FETUSDT", "RENDERUSDT",
    "JUPUSDT", "PYTHUSDT", "ENAUSDT", "ONDOUSDT", "WLDUSDT",
    "1000PEPEUSDT", "1000BONKUSDT", "1000FLOKIUSDT", "GALAUSDT", "LDOUSDT",
]


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def market_rows(root: Path) -> list[dict[str, Any]]:
    cache = read_json(root / "runtime" / "market_data_cache.json")
    preview = cache.get("top_preview")
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in preview or [] if isinstance(row, dict)}
    symbols = cache.get("top_symbols") or cache.get("available_symbols") or []
    out = []
    for sym in symbols:
        symbol = str(sym or "").upper()
        if not symbol or not symbol.endswith("USDT") or not symbol.isascii():
            continue
        row = dict(by_symbol.get(symbol) or {})
        row["symbol"] = symbol
        out.append(row)
    return out


def resolve_price(root: Path, symbol: str) -> tuple[float | None, str]:
    cached = load_latest_cached_close(root, symbol, max_age_sec=86400)
    if cached and cached > 0:
        return float(cached), "local_kline_cache"
    if not okx_symbol_supported(symbol):
        return None, "unsupported_symbol"
    try:
        rows = fetch_okx_klines(symbol, "15m", 2)
        if rows:
            save_cached_klines(root, symbol, "15m", 2, rows)
            return float(rows[-1][4]), "okx_15m"
    except Exception as exc:
        return None, f"okx_error:{str(exc)[:80]}"
    return None, "price_unavailable"


def resolve_funding(symbol: str) -> tuple[float, str]:
    if not okx_symbol_supported(symbol):
        return 0.0, "unsupported_symbol"
    try:
        rate_pct = fetch_okx_funding_rate(symbol)
        if rate_pct is None:
            return 0.0, "okx_unavailable"
        return float(rate_pct) / 100.0, "okx"
    except Exception as exc:
        return 0.0, f"okx_error:{str(exc)[:80]}"


def event_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["payload_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def recent_candidates(root: Path, strategy: str, limit: int) -> list[dict[str, Any]]:
    db_path = root / "runtime" / "event_store.sqlite3"
    if not db_path.exists():
        return []
    since = (datetime.now(CST) - timedelta(hours=24)).isoformat()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, ts, event_type, symbol, side, score, reason, payload_json
            from events
            where strategy=? and ts>=? and event_type in ('SIGNAL','OPEN_SKIPPED')
              and symbol<>''
            order by id desc
            limit 300
            """,
            (strategy, since),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    for row in rows:
        payload = event_payload(row)
        symbol = str(row["symbol"] or payload.get("symbol") or "").upper()
        if not symbol or symbol in seen or not symbol.isascii():
            continue
        side = str(payload.get("side") or row["side"] or "").lower()
        if side not in {"long", "short"}:
            side = "short" if safe_float(payload.get("score") or row["score"]) < 0 else "long"
        out.append({
            "symbol": symbol,
            "side": side,
            "score": abs(safe_float(payload.get("score") or row["score"])),
            "source": f"{row['event_type']}#{row['id']}",
            "reason": str(payload.get("reason") or row["reason"] or "recent_strategy_candidate"),
        })
        seen.add(symbol)
        if len(out) >= limit:
            break
    return out


def bootstrap_candidates(root: Path, strategy: str, limit: int) -> list[dict[str, Any]]:
    rows = market_rows(root)
    if not rows:
        rows = [{"symbol": symbol, "change_pct": 0.0} for symbol in DEFAULT_BOOTSTRAP_SYMBOLS]
    offset = {"A/v11": 0, "B/v16": 17, "C/v14": 34}.get(strategy, 0)
    ordered = rows[offset:] + rows[:offset]
    out = []
    for row in ordered:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or not okx_symbol_supported(symbol):
            continue
        change = safe_float(row.get("change_pct"))
        side = "long" if change >= 0 else "short"
        out.append({
            "symbol": symbol,
            "side": side,
            "score": abs(change),
            "source": "top100_market_cache",
            "reason": "Top100 paper exchange bootstrap",
        })
        if len(out) >= limit:
            break
    return out


def open_bootstrap_positions(root: Path, exchange: PaperExchange, target_per_strategy: int, margin_usdt: float, leverage: int) -> int:
    state = exchange.load()
    open_by_strategy = {
        strategy: [p for p in state.get("positions", {}).values() if p.get("strategy") == strategy]
        for strategy in STRATEGIES
    }
    created = 0
    writer = EventStoreWriter(root / "runtime" / "event_store.sqlite3")
    for strategy in STRATEGIES:
        need = max(0, target_per_strategy - len(open_by_strategy.get(strategy, [])))
        if need <= 0:
            continue
        held = {str(p.get("symbol") or "").upper() for p in open_by_strategy.get(strategy, [])}
        candidates = recent_candidates(root, strategy, need * 2)
        candidates.extend(bootstrap_candidates(root, strategy, need * 3))
        selected: list[dict[str, Any]] = []
        seen = set(held)
        for item in candidates:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or symbol in seen:
                continue
            price, price_source = resolve_price(root, symbol)
            if not price or price <= 0:
                continue
            item = dict(item)
            item["price"] = price
            item["price_source"] = price_source
            selected.append(item)
            seen.add(symbol)
            if len(selected) >= need:
                break
        for item in selected:
            price = float(item["price"])
            qty = (margin_usdt * leverage) / price
            side = str(item["side"])
            order_id = f"PAPERX-{strategy.replace('/', '-')}-{item['symbol']}-{int(datetime.now(CST).timestamp())}"
            exchange.open_market(
                strategy=strategy,
                symbol=item["symbol"],
                side=side,
                qty=qty,
                price=price,
                leverage=leverage,
                order_id=order_id,
                reason=str(item.get("reason") or "paper_exchange_bootstrap"),
                context={
                    "source": item.get("source"),
                    "price_source": item.get("price_source"),
                    "margin_usdt": margin_usdt,
                    "paper_exchange_bootstrap": True,
                },
            )
            writer.write_event(
                {
                    "time": datetime.now(CST).isoformat(),
                    "event": "OPEN",
                    "strategy": strategy,
                    "symbol": item["symbol"],
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "exchange_qty": qty,
                    "leverage": leverage,
                    "score": item.get("score", 0),
                    "reason": "paper_exchange_bootstrap",
                    "entry_reason": item.get("reason"),
                    "timeframe": "paper_exchange",
                    "category": "opened",
                    "decision_stage": "open",
                    "filter_layer": "paper_exchange",
                    "order_id": order_id,
                    "paper": True,
                    "mode": "paper_exchange",
                    "simulation_only": True,
                    "expected_notional_usdt": round(qty * price, 6),
                    "target_margin_usdt": margin_usdt,
                    "price_source": item.get("price_source"),
                    "source_candidate": item.get("source"),
                },
                source=f"{strategy}/paper_exchange",
            )
            created += 1
    writer.close()
    return created


def close_hit_positions(root: Path, exchange: PaperExchange, take_profit_pct: float, stop_loss_pct: float) -> int:
    state = exchange.load()
    closed = 0
    writer = EventStoreWriter(root / "runtime" / "event_store.sqlite3")
    for pos in list(state.get("positions", {}).values()):
        entry = safe_float(pos.get("entry_price"))
        mark = safe_float(pos.get("mark_price"))
        if entry <= 0 or mark <= 0:
            continue
        side = str(pos.get("side") or "").lower()
        pnl_pct = (mark - entry) / entry * 100 if side == "long" else (entry - mark) / entry * 100
        reason = ""
        if pnl_pct >= take_profit_pct:
            reason = f"paper止盈 {pnl_pct:.2f}%"
        elif pnl_pct <= -abs(stop_loss_pct):
            reason = f"paper止损 {pnl_pct:.2f}%"
        if not reason:
            continue
        strategy = str(pos.get("strategy"))
        symbol = str(pos.get("symbol"))
        qty = safe_float(pos.get("qty"))
        order_id = f"PAPERX-CLOSE-{strategy.replace('/', '-')}-{symbol}-{int(datetime.now(CST).timestamp())}"
        before = exchange.load()
        exchange.close_market(strategy=strategy, symbol=symbol, side=side, qty=qty, price=mark, order_id=order_id, reason=reason)
        after_fill = (exchange.load().get("fills") or [])[-1]
        writer.write_event(
            {
                "time": datetime.now(CST).isoformat(),
                "event": "CLOSE",
                "strategy": strategy,
                "symbol": symbol,
                "side": side,
                "exit_price": mark,
                "entry_price": entry,
                "qty": qty,
                "reason": reason,
                "pnl_usd": after_fill.get("realized_pnl"),
                "fee": after_fill.get("fee"),
                "timeframe": "paper_exchange",
                "category": "closed",
                "decision_stage": "close",
                "filter_layer": "paper_exchange",
                "order_id": order_id,
                "paper": True,
                "mode": "paper_exchange",
                "simulation_only": True,
            },
            source=f"{strategy}/paper_exchange",
        )
        closed += 1
    writer.close()
    return closed


def run(root: Path, target_per_strategy: int, margin_usdt: float, leverage: int, take_profit_pct: float, stop_loss_pct: float) -> dict[str, Any]:
    exchange = PaperExchange(root)
    summary = exchange.mark_to_market(lambda symbol: resolve_price(root, symbol), resolve_funding)
    closed = close_hit_positions(root, exchange, take_profit_pct, stop_loss_pct)
    opened = open_bootstrap_positions(root, exchange, target_per_strategy, margin_usdt, leverage)
    summary = exchange.mark_to_market(lambda symbol: resolve_price(root, symbol), resolve_funding)
    summary.update({
        "opened_this_run": opened,
        "closed_this_run": closed,
        "target_per_strategy": target_per_strategy,
        "safety": "paper_exchange_only_no_binance_order_no_signed_request",
    })
    exchange.latest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paper exchange mark/open/close maintenance")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--target-per-strategy", type=int, default=int(os.environ.get("PAPER_EXCHANGE_TARGET_PER_STRATEGY", "5")))
    parser.add_argument("--margin-usdt", type=float, default=float(os.environ.get("PAPER_EXCHANGE_MARGIN_USDT", "100")))
    parser.add_argument("--leverage", type=int, default=int(os.environ.get("PAPER_EXCHANGE_LEVERAGE", "4")))
    parser.add_argument("--take-profit-pct", type=float, default=float(os.environ.get("PAPER_EXCHANGE_TAKE_PROFIT_PCT", "4")))
    parser.add_argument("--stop-loss-pct", type=float, default=float(os.environ.get("PAPER_EXCHANGE_STOP_LOSS_PCT", "3")))
    args = parser.parse_args(argv)
    result = run(args.root, args.target_per_strategy, args.margin_usdt, args.leverage, args.take_profit_pct, args.stop_loss_pct)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
