"""Research-only paper strategy adapter for selected local backtest clues.

Runs three independent paper strategy IDs:

- R-L1-NEG-JUMP-1H
- R-J3-RANGE-CHOP-4H
- R-E-CSMOM-4H

It consumes existing runtime caches only. It does not call Binance, does not
fetch exchange data directly, and does not touch A/B scanner behavior.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_engine
import j3_v2_strategy_research as j3_research
import l1_edge_strategy_reconstruction as l1_research
import signal_edge_lab
from core.event_store import EventStoreWriter
from core.kline_cache import load_cached_klines, load_latest_cached_close
from core.paper_exchange import PaperExchange, RESEARCH_STRATEGIES, is_dust_position, safe_float


CST = timezone(timedelta(hours=8))
INTERVAL_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
RUNTIME_JSON = ROOT / "runtime" / "research_paper_strategy_latest.json"
STATE_JSON = ROOT / "runtime" / "research_paper_strategy_state.json"

RESEARCH_UNIVERSE = [
    "ADAUSDT",
    "AVAXUSDT",
    "BCHUSDT",
    "BEATUSDT",
    "BNBUSDT",
    "BTCUSDT",
    "CCUSDT",
    "CROUSDT",
    "DOGEUSDT",
    "ETHUSDT",
    "HBARUSDT",
    "HYPEUSDT",
    "LABUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "MUSDT",
    "NEARUSDT",
    "SHIBUSDT",
    "SOLUSDT",
    "SUIUSDT",
    "TAOUSDT",
    "TONUSDT",
    "TRXUSDT",
    "XLMUSDT",
    "XMRUSDT",
    "XRPUSDT",
    "ZECUSDT",
]
EXCLUDED_SYMBOL_RE = re.compile(r"(USDTUSDT|USDCUSDT|DAIUSDT|USDYUSDT|USDEUSDT|USD1USDT|PAXGUSDT|XAUTUSDT|XAUUSDT|XAGUSDT|LEOUSDT|RAINUSDT)$")

BACKTEST_STATS = {
    "R-L1-NEG-JUMP-1H": {
        "source": "l1_edge_strategy_reconstruction",
        "variant": "l1_neg_jump_12bar_time",
        "full_net_usdt": 920.765112,
        "profit_factor": 1.193060,
        "trades": 2924,
        "note": "positive full/test, fragile validation/test PF",
    },
    "R-J3-RANGE-CHOP-4H": {
        "source": "j3_v2_strategy_research",
        "variant": "j3v2_range_chop_1bar_matched_gate",
        "full_net_usdt": 59.191277,
        "profit_factor": 1.776237,
        "trades": 112,
        "note": "positive full and cost stress, failed validation/test",
    },
    "R-E-CSMOM-4H": {
        "source": "candidate_ab_match_research",
        "variant": "lookback=48,top=3,hold=8",
        "full_net_usdt": 2055.015189,
        "profit_factor": 1.839034,
        "trades": 540,
        "note": "best PF E/4h standalone manual-review row",
    },
}


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def market_cache(root: Path) -> dict[str, Any]:
    return read_json(root / "runtime" / "market_data_cache.json")


def usable_symbols(root: Path, limit: int) -> list[str]:
    cache = market_cache(root)
    available = {str(item).upper() for item in cache.get("available_symbols") or []}
    out: list[str] = []
    for symbol in RESEARCH_UNIVERSE:
        if available and symbol not in available:
            continue
        if EXCLUDED_SYMBOL_RE.search(symbol):
            continue
        out.append(symbol)
        if len(out) >= limit:
            break
    return out


def row_to_bar(row: Any, interval: str) -> dict[str, Any] | None:
    if isinstance(row, dict):
        bar = backtest_engine.row_to_bar(row)
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        open_ms = backtest_engine.safe_int(row[0])
        bar = {
            "ts": backtest_engine.ms_to_iso(open_ms),
            "open_time_ms": open_ms,
            "open": backtest_engine.safe_float(row[1]),
            "high": backtest_engine.safe_float(row[2]),
            "low": backtest_engine.safe_float(row[3]),
            "close": backtest_engine.safe_float(row[4]),
            "volume": backtest_engine.safe_float(row[5]),
            "quote_volume": backtest_engine.safe_float(row[7]) if len(row) > 7 else backtest_engine.safe_float(row[5]),
        }
    else:
        return None
    if bar["open"] <= 0 or bar["high"] <= 0 or bar["low"] <= 0 or bar["close"] <= 0:
        return None
    bar["interval"] = interval
    return bar


def load_bars(root: Path, symbol: str, interval: str, limit: int, max_age_sec: int) -> list[dict[str, Any]]:
    rows = load_cached_klines(root, symbol, interval, limit, max_age_sec=max_age_sec) or []
    bars = [bar for row in rows if (bar := row_to_bar(row, interval))]
    bars.sort(key=lambda item: int(item.get("open_time_ms") or 0))
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for bar in bars:
        key = int(bar.get("open_time_ms") or 0)
        if key not in seen:
            deduped.append(bar)
            seen.add(key)
    if len(deduped) >= 2:
        return deduped[:-1]
    return deduped


def resolve_price(root: Path, symbol: str, max_age_sec: int) -> tuple[float | None, str]:
    cached = load_latest_cached_close(root, symbol, max_age_sec=max_age_sec)
    if cached and cached > 0:
        return float(cached), "local_kline_cache"
    for interval, limit in (("15m", 2), ("1h", 2), ("4h", 2)):
        bars = load_bars(root, symbol, interval, limit, max_age_sec)
        if bars:
            close = safe_float(bars[-1].get("close"))
            if close > 0:
                return close, f"{interval}_cache_close"
    return None, "cache_price_unavailable"


def load_state(root: Path) -> dict[str, Any]:
    state = read_json(root / "runtime" / "research_paper_strategy_state.json")
    state.setdefault("strategies", {})
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(root / "runtime" / "research_paper_strategy_state.json", state)


def strategy_state(state: dict[str, Any], strategy: str) -> dict[str, Any]:
    strategies = state.setdefault("strategies", {})
    row = strategies.setdefault(strategy, {})
    row.setdefault("seen_signals", {})
    return row


def open_positions(exchange: PaperExchange, strategy: str | None = None) -> list[dict[str, Any]]:
    state = exchange.load()
    positions = []
    for pos in (state.get("positions") or {}).values():
        if not isinstance(pos, dict) or is_dust_position(pos):
            continue
        if strategy and pos.get("strategy") != strategy:
            continue
        positions.append(pos)
    return positions


def write_event(writer: EventStoreWriter, payload: dict[str, Any], strategy: str) -> None:
    writer.write_event(payload, source=f"{strategy}/research_paper")


def latest_fill(summary: dict[str, Any]) -> dict[str, Any]:
    fills = summary.get("recent_fills") if isinstance(summary, dict) else []
    fill = fills[-1] if isinstance(fills, list) and fills else {}
    return fill if isinstance(fill, dict) else {}


def open_research_position(
    *,
    root: Path,
    exchange: PaperExchange,
    writer: EventStoreWriter,
    strategy: str,
    symbol: str,
    side: str,
    price: float,
    price_source: str,
    margin_usdt: float,
    leverage: float,
    reason: str,
    context: dict[str, Any],
) -> bool:
    if price <= 0 or margin_usdt <= 0 or leverage <= 0:
        return False
    qty = margin_usdt * leverage / price
    order_id = f"RPAPER-{strategy}-{symbol}-{int(time.time())}"
    context = {
        **context,
        "research_strategy": True,
        "paper_research": True,
        "backtest_promoted": True,
        "rollout_evidence_eligible": False,
        "price_source": price_source,
        "margin_usdt": margin_usdt,
        "source_backtest": BACKTEST_STATS.get(strategy, {}),
        "api_pressure": "cache_only_no_direct_exchange_request",
    }
    summary = exchange.open_market(
        strategy=strategy,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        leverage=leverage,
        order_id=order_id,
        reason=reason,
        context=context,
    )
    fill = latest_fill(summary)
    event_price = safe_float(fill.get("executed_price"), price)
    event_qty = safe_float(fill.get("executed_qty"), qty)
    write_event(
        writer,
        {
            "time": now_iso(),
            "event": "OPEN",
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "price": event_price,
            "requested_price": price,
            "qty": event_qty,
            "requested_qty": qty,
            "leverage": leverage,
            "reason": reason,
            "category": "opened",
            "decision_stage": "research_paper_open",
            "filter_layer": "research_paper",
            "order_id": order_id,
            "paper": True,
            "mode": "paper_exchange",
            "simulation_only": True,
            "research_strategy": True,
            "paper_research": True,
            "rollout_evidence_eligible": False,
            "expected_notional_usdt": round(event_qty * event_price, 6),
            "target_margin_usdt": margin_usdt,
            "timeframe": context.get("interval"),
            "source_timeframe": context.get("interval"),
            "price_source": price_source,
            "paper_fill": fill,
            "research_context": context,
        },
        strategy,
    )
    return True


def close_research_position(
    *,
    root: Path,
    exchange: PaperExchange,
    writer: EventStoreWriter,
    pos: dict[str, Any],
    price: float,
    price_source: str,
    reason: str,
) -> bool:
    strategy = str(pos.get("strategy") or "")
    symbol = str(pos.get("symbol") or "")
    side = str(pos.get("side") or "").lower()
    qty = safe_float(pos.get("qty"))
    if not strategy or not symbol or side not in {"long", "short"} or qty <= 0 or price <= 0:
        return False
    order_id = f"RPAPER-CLOSE-{strategy}-{symbol}-{int(time.time())}"
    summary = exchange.close_market(strategy=strategy, symbol=symbol, side=side, qty=qty, price=price, order_id=order_id, reason=reason)
    fill = latest_fill(summary)
    exit_price = safe_float(fill.get("executed_price"), price)
    event_qty = safe_float(fill.get("executed_qty"), qty)
    context = pos.get("context") if isinstance(pos.get("context"), dict) else {}
    write_event(
        writer,
        {
            "time": now_iso(),
            "event": "CLOSE",
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "exit_price": exit_price,
            "requested_exit_price": price,
            "entry_price": pos.get("entry_price"),
            "entry_time": pos.get("opened_at"),
            "qty": event_qty,
            "requested_qty": qty,
            "reason": reason,
            "pnl_usd": fill.get("realized_pnl"),
            "fee": fill.get("fee"),
            "category": "closed",
            "decision_stage": "research_paper_close",
            "filter_layer": "research_paper",
            "order_id": order_id,
            "paper": True,
            "mode": "paper_exchange",
            "simulation_only": True,
            "research_strategy": True,
            "rollout_evidence_eligible": False,
            "timeframe": context.get("interval"),
            "source_timeframe": context.get("interval"),
            "price_source": price_source,
            "paper_fill": fill,
            "research_context": context,
        },
        strategy,
    )
    return True


def close_due_positions(root: Path, exchange: PaperExchange, writer: EventStoreWriter, price_max_age_sec: int) -> tuple[int, list[dict[str, Any]]]:
    closed = 0
    checks: list[dict[str, Any]] = []
    for pos in open_positions(exchange):
        strategy = str(pos.get("strategy") or "")
        if strategy not in RESEARCH_STRATEGIES:
            continue
        context = pos.get("context") if isinstance(pos.get("context"), dict) else {}
        symbol = str(pos.get("symbol") or "")
        side = str(pos.get("side") or "").lower()
        price, source = resolve_price(root, symbol, price_max_age_sec)
        if not price:
            checks.append({"strategy": strategy, "symbol": symbol, "status": "price_gap"})
            continue
        stop_loss = safe_float(context.get("stop_loss"))
        take_profit = safe_float(context.get("take_profit"))
        expiry_ms = backtest_engine.safe_int(context.get("expiry_bar_ms"))
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        reason = ""
        if side == "long":
            if stop_loss > 0 and price <= stop_loss:
                reason = "research_stop_loss"
            elif take_profit > 0 and price >= take_profit:
                reason = "research_take_profit"
        elif side == "short":
            if stop_loss > 0 and price >= stop_loss:
                reason = "research_stop_loss"
            elif take_profit > 0 and price <= take_profit:
                reason = "research_take_profit"
        if not reason and expiry_ms > 0 and now_ms >= expiry_ms:
            reason = "research_time_exit"
        if reason and close_research_position(root=root, exchange=exchange, writer=writer, pos=pos, price=price, price_source=source, reason=reason):
            closed += 1
        checks.append({"strategy": strategy, "symbol": symbol, "status": reason or "hold", "price": price, "source": source})
    return closed, checks


def open_event_strategy(
    *,
    root: Path,
    exchange: PaperExchange,
    writer: EventStoreWriter,
    state: dict[str, Any],
    strategy: str,
    symbol: str,
    side: str,
    signal_bar: dict[str, Any],
    interval: str,
    params: dict[str, Any],
    evidence: dict[str, Any],
    max_positions: int,
    margin_usdt: float,
    leverage: float,
    price_max_age_sec: int,
) -> tuple[bool, str]:
    existing = open_positions(exchange, strategy)
    if len(existing) >= max_positions:
        return False, "max_positions"
    if any(pos.get("symbol") == symbol for pos in existing):
        return False, "already_open_symbol"
    row_state = strategy_state(state, strategy)
    signal_key = f"{symbol}:{signal_bar.get('open_time_ms')}:{side}:{BACKTEST_STATS[strategy]['variant']}"
    if row_state["seen_signals"].get(symbol) == signal_key:
        return False, "duplicate_signal"
    price, price_source = resolve_price(root, symbol, price_max_age_sec)
    if not price:
        return False, "price_gap"
    atr_bars = load_bars(root, symbol, interval, 320, price_max_age_sec)
    atr = backtest_engine.atr(atr_bars, max(0, len(atr_bars) - 1)) if atr_bars else 0.0
    if atr <= 0:
        atr = price * 0.01
    stop_mult = safe_float(params.get("atr_stop_multiplier"), 2.0)
    tp_mult = safe_float(params.get("take_profit_atr"), 0.0)
    if side == "long":
        stop_loss = price - atr * stop_mult
        take_profit = price + atr * tp_mult if tp_mult > 0 else 0.0
    else:
        stop_loss = price + atr * stop_mult
        take_profit = price - atr * tp_mult if tp_mult > 0 else 0.0
    opened_ms = backtest_engine.safe_int(signal_bar.get("open_time_ms"))
    max_hold = max(1, backtest_engine.safe_int(params.get("max_hold_bars"), 12))
    context = {
        "interval": interval,
        "variant": BACKTEST_STATS[strategy]["variant"],
        "signal_bar_ms": opened_ms,
        "signal_bar_ts": signal_bar.get("ts"),
        "expiry_bar_ms": opened_ms + (max_hold + 1) * INTERVAL_MS.get(interval, 60_000),
        "max_hold_bars": max_hold,
        "stop_loss": round(stop_loss, 10) if stop_loss > 0 else 0.0,
        "take_profit": round(take_profit, 10) if take_profit > 0 else 0.0,
        "atr": round(atr, 10),
        "evidence": evidence,
    }
    opened = open_research_position(
        root=root,
        exchange=exchange,
        writer=writer,
        strategy=strategy,
        symbol=symbol,
        side=side,
        price=price,
        price_source=price_source,
        margin_usdt=margin_usdt,
        leverage=leverage,
        reason=f"{strategy} research signal",
        context=context,
    )
    if opened:
        row_state["seen_signals"][symbol] = signal_key
    return opened, "opened" if opened else "open_failed"


def run_l1(root: Path, exchange: PaperExchange, writer: EventStoreWriter, state: dict[str, Any], args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    strategy = "R-L1-NEG-JUMP-1H"
    variant = next(row for row in l1_research.variants() if row["name"] == "l1_neg_jump_12bar_time")
    params = variant["params"]
    rows: list[dict[str, Any]] = []
    opened = 0
    for symbol in symbols:
        bars = load_bars(root, symbol, "1h", args.l1_bars, args.kline_max_age_sec)
        if len(bars) < l1_research.MIN_FEATURE_IDX + 32:
            rows.append({"symbol": symbol, "status": "data_gap", "bars": len(bars), "need": l1_research.MIN_FEATURE_IDX + 32})
            continue
        prepared = l1_research.prepare_series({symbol: bars})
        series = prepared.get(symbol)
        if not series:
            rows.append({"symbol": symbol, "status": "feature_gap", "bars": len(bars)})
            continue
        idx = len(series["bars"]) - 1
        if l1_research.matches_signal(series["features"], idx, params):
            evidence = {
                "ret1_pct": round(series["features"]["ret1"][idx], 6),
                "sigma96_pct": round(series["features"]["sigma96"][idx], 6),
                "volume_pctile": round(series["features"]["volume_pctile"][idx], 3),
                "regime": series["features"]["regime_v2"][idx],
            }
            did_open, status = open_event_strategy(
                root=root,
                exchange=exchange,
                writer=writer,
                state=state,
                strategy=strategy,
                symbol=symbol,
                side="long",
                signal_bar=series["bars"][idx],
                interval="1h",
                params=params,
                evidence=evidence,
                max_positions=args.l1_max_positions,
                margin_usdt=args.event_margin_usdt,
                leverage=args.event_leverage,
                price_max_age_sec=args.price_max_age_sec,
            )
            opened += 1 if did_open else 0
            rows.append({"symbol": symbol, "status": status, **evidence})
        else:
            rows.append({"symbol": symbol, "status": "no_signal", "bars": len(bars), "last_bar": bars[-1].get("ts")})
    return {"strategy": strategy, "opened": opened, "signals": rows, "backtest": BACKTEST_STATS[strategy]}


def run_j3(root: Path, exchange: PaperExchange, writer: EventStoreWriter, state: dict[str, Any], args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    strategy = "R-J3-RANGE-CHOP-4H"
    variant = next(row for row in j3_research.variants(set()) if row["name"] == "j3v2_range_chop_1bar_matched_gate")
    params = variant["params"]
    allowed = set(params.get("allowed_regimes") or [])
    rows: list[dict[str, Any]] = []
    opened = 0
    for symbol in symbols:
        bars = load_bars(root, symbol, "4h", args.j3_bars, args.kline_max_age_sec)
        if len(bars) < 260:
            rows.append({"symbol": symbol, "status": "data_gap", "bars": len(bars), "need": 260})
            continue
        features = signal_edge_lab.build_edge_features(bars)
        idx = len(bars) - 1
        signal = j3_research.j3_signal(bars, features, idx)
        if not signal:
            rows.append({"symbol": symbol, "status": "no_signal", "bars": len(bars), "last_bar": bars[-1].get("ts")})
            continue
        side, extra = signal
        if allowed and extra.get("regime") not in allowed:
            rows.append({"symbol": symbol, "status": "regime_rejected", **extra})
            continue
        did_open, status = open_event_strategy(
            root=root,
            exchange=exchange,
            writer=writer,
            state=state,
            strategy=strategy,
            symbol=symbol,
            side=side,
            signal_bar=bars[idx],
            interval="4h",
            params=params,
            evidence=extra,
            max_positions=args.j3_max_positions,
            margin_usdt=args.event_margin_usdt,
            leverage=args.event_leverage,
            price_max_age_sec=args.price_max_age_sec,
        )
        opened += 1 if did_open else 0
        rows.append({"symbol": symbol, "status": status, "side": side, **extra})
    return {"strategy": strategy, "opened": opened, "signals": rows, "backtest": BACKTEST_STATS[strategy]}


def e_rank_signal(loaded: dict[str, list[dict[str, Any]]], lookback: int, top_n: int, min_symbols: int) -> dict[str, Any]:
    by_time: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for symbol, bars in loaded.items():
        for row in bars:
            by_time[int(row.get("open_time_ms") or 0)][symbol] = row
    times = sorted(by_time)
    if len(times) <= lookback:
        return {"status": "data_gap", "times": len(times), "need": lookback + 1}
    for pos in range(len(times) - 1, lookback - 1, -1):
        entry_time = times[pos]
        lookback_time = times[pos - lookback]
        ranks: list[tuple[str, float, float]] = []
        for symbol, row in by_time.get(entry_time, {}).items():
            past_row = by_time.get(lookback_time, {}).get(symbol)
            if not past_row:
                continue
            past = safe_float(past_row.get("close"))
            close = safe_float(row.get("close"))
            if past > 0 and close > 0:
                ranks.append((symbol, (close - past) / past * 100.0, close))
        if len(ranks) >= min_symbols:
            ranks.sort(key=lambda item: item[1], reverse=True)
            return {
                "status": "ok",
                "entry_time_ms": entry_time,
                "entry_time": backtest_engine.ms_to_iso(entry_time),
                "lookback_time_ms": lookback_time,
                "available_symbols": len(ranks),
                "longs": ranks[:top_n],
                "shorts": ranks[-top_n:],
            }
    return {"status": "data_gap", "times": len(times), "need_symbols": min_symbols}


def run_e(root: Path, exchange: PaperExchange, writer: EventStoreWriter, state: dict[str, Any], args: argparse.Namespace, symbols: list[str]) -> dict[str, Any]:
    strategy = "R-E-CSMOM-4H"
    lookback = 48
    top_n = 3
    hold = 8
    loaded = {symbol: load_bars(root, symbol, "4h", args.e_bars, args.kline_max_age_sec) for symbol in symbols}
    loaded = {symbol: bars for symbol, bars in loaded.items() if len(bars) >= lookback + hold + 2}
    signal = e_rank_signal(loaded, lookback=lookback, top_n=top_n, min_symbols=max(top_n * 2, args.e_min_symbols))
    row_state = strategy_state(state, strategy)
    opened = 0
    closed = 0
    if signal.get("status") != "ok":
        return {"strategy": strategy, "opened": 0, "closed": 0, "status": signal.get("status"), "signal": signal, "backtest": BACKTEST_STATS[strategy]}
    entry_time = backtest_engine.safe_int(signal.get("entry_time_ms"))
    last_rebalance = backtest_engine.safe_int(row_state.get("last_rebalance_ms"))
    if last_rebalance and entry_time < last_rebalance + hold * INTERVAL_MS["4h"]:
        return {"strategy": strategy, "opened": 0, "closed": 0, "status": "hold_existing_portfolio", "signal": signal, "backtest": BACKTEST_STATS[strategy]}
    if row_state.get("last_signal_ms") == entry_time:
        return {"strategy": strategy, "opened": 0, "closed": 0, "status": "duplicate_rebalance", "signal": signal, "backtest": BACKTEST_STATS[strategy]}

    for pos in open_positions(exchange, strategy):
        price, price_source = resolve_price(root, str(pos.get("symbol") or ""), args.price_max_age_sec)
        if price and close_research_position(root=root, exchange=exchange, writer=writer, pos=pos, price=price, price_source=price_source, reason="research_rebalance"):
            closed += 1

    targets = [(symbol, "long", mom) for symbol, mom, _close in signal.get("longs") or []]
    targets.extend((symbol, "short", mom) for symbol, mom, _close in signal.get("shorts") or [])
    for symbol, side, momentum in targets[: args.e_max_legs]:
        price, price_source = resolve_price(root, symbol, args.price_max_age_sec)
        if not price:
            continue
        opened_ok = open_research_position(
            root=root,
            exchange=exchange,
            writer=writer,
            strategy=strategy,
            symbol=symbol,
            side=side,
            price=price,
            price_source=price_source,
            margin_usdt=args.e_leg_margin_usdt,
            leverage=args.e_leverage,
            reason="E/4h cross-sectional rebalance",
            context={
                "interval": "4h",
                "variant": BACKTEST_STATS[strategy]["variant"],
                "signal_bar_ms": entry_time,
                "signal_bar_ts": signal.get("entry_time"),
                "expiry_bar_ms": entry_time + hold * INTERVAL_MS["4h"],
                "max_hold_bars": hold,
                "momentum_pct": round(float(momentum), 6),
                "available_symbols": signal.get("available_symbols"),
                "portfolio_role": side,
            },
        )
        opened += 1 if opened_ok else 0
    row_state["last_rebalance_ms"] = entry_time
    row_state["last_signal_ms"] = entry_time
    return {"strategy": strategy, "opened": opened, "closed": closed, "status": "rebalanced", "signal": signal, "backtest": BACKTEST_STATS[strategy]}


def summarize_status(results: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter()
    for result in results:
        counter[str(result.get("status") or "checked")] += 1
        for row in result.get("signals") or []:
            counter[str(row.get("status") or "unknown")] += 1
    return dict(counter)


def run(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    symbols = usable_symbols(root, args.symbol_limit)
    state = load_state(root)
    exchange = PaperExchange(root)
    writer = EventStoreWriter(root / "runtime" / "event_store.sqlite3")
    closed, close_checks = close_due_positions(root, exchange, writer, args.price_max_age_sec)
    results = [
        run_l1(root, exchange, writer, state, args, symbols),
        run_j3(root, exchange, writer, state, args, symbols),
        run_e(root, exchange, writer, state, args, symbols),
    ]
    writer.close()
    save_state(root, state)
    summary = exchange.mark_to_market(lambda symbol: resolve_price(root, symbol, args.price_max_age_sec))
    payload = {
        "generated_at": now_iso(),
        "module": "research_paper_strategy_runner",
        "status": "completed",
        "closed_this_run": closed + sum(backtest_engine.safe_int(row.get("closed")) for row in results),
        "opened_this_run": sum(backtest_engine.safe_int(row.get("opened")) for row in results),
        "symbols": symbols,
        "symbol_count": len(symbols),
        "results": results,
        "close_checks": close_checks[-80:],
        "paper_summary": {
            "open_positions": summary.get("open_positions"),
            "total_unrealized_pnl": summary.get("total_unrealized_pnl"),
            "by_strategy": {k: v for k, v in (summary.get("by_strategy") or {}).items() if k in RESEARCH_STRATEGIES},
        },
        "status_counts": summarize_status(results),
        "api_pressure": {
            "binance_requests_enabled": False,
            "direct_exchange_requests": 0,
            "market_data_source": "existing runtime/kline_cache and market_data_cache only",
            "new_request_cadence": "none",
        },
        "safety": {
            "paper_only": True,
            "real_orders": False,
            "scanner_mutation": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
        },
        "state_path": str(root / "runtime" / "research_paper_strategy_state.json"),
    }
    write_json(root / "runtime" / "research_paper_strategy_latest.json", payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research paper strategy adapter from local caches.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--symbol-limit", type=int, default=int(os.environ.get("RESEARCH_PAPER_SYMBOL_LIMIT", "27")))
    parser.add_argument("--kline-max-age-sec", type=int, default=int(os.environ.get("RESEARCH_PAPER_KLINE_MAX_AGE_SEC", "21600")))
    parser.add_argument("--price-max-age-sec", type=int, default=int(os.environ.get("RESEARCH_PAPER_PRICE_MAX_AGE_SEC", "900")))
    parser.add_argument("--event-margin-usdt", type=float, default=float(os.environ.get("RESEARCH_PAPER_EVENT_MARGIN_USDT", "25")))
    parser.add_argument("--event-leverage", type=float, default=float(os.environ.get("RESEARCH_PAPER_EVENT_LEVERAGE", "2")))
    parser.add_argument("--e-leg-margin-usdt", type=float, default=float(os.environ.get("RESEARCH_PAPER_E_LEG_MARGIN_USDT", "20")))
    parser.add_argument("--e-leverage", type=float, default=float(os.environ.get("RESEARCH_PAPER_E_LEVERAGE", "1")))
    parser.add_argument("--e-max-legs", type=int, default=int(os.environ.get("RESEARCH_PAPER_E_MAX_LEGS", "6")))
    parser.add_argument("--e-min-symbols", type=int, default=int(os.environ.get("RESEARCH_PAPER_E_MIN_SYMBOLS", "16")))
    parser.add_argument("--l1-max-positions", type=int, default=int(os.environ.get("RESEARCH_PAPER_L1_MAX_POSITIONS", "2")))
    parser.add_argument("--j3-max-positions", type=int, default=int(os.environ.get("RESEARCH_PAPER_J3_MAX_POSITIONS", "2")))
    parser.add_argument("--l1-bars", type=int, default=int(os.environ.get("RESEARCH_PAPER_L1_BARS", "300")))
    parser.add_argument("--j3-bars", type=int, default=int(os.environ.get("RESEARCH_PAPER_J3_BARS", "300")))
    parser.add_argument("--e-bars", type=int, default=int(os.environ.get("RESEARCH_PAPER_E_BARS", "120")))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run(Path(args.root).resolve(), args)
    print(json.dumps({"status": payload["status"], "opened": payload["opened_this_run"], "closed": payload["closed_this_run"], "json": str(Path(args.root) / "runtime" / "research_paper_strategy_latest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
