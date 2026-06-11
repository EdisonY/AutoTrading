"""D/E/F one-year historical strategy research report.

Read-only Tencent research runner. It evaluates three new strategy families
against the existing one-year historical Kline warehouse:

- D/trend_breakout: Donchian-style breakout with ATR exits.
- E/cross_sectional_momentum: Top30 relative-strength long/short portfolio.
- F/pairs_mean_reversion: pre-registered crypto pair spread reversion.

It never changes live config, restarts scanners, calls Binance, submits orders,
or enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
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
from core.replay_fill import ReplayFillRequest, simulate_replay_fill


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
DEFAULT_UNIVERSE = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "TRXUSDT",
    "DOGEUSDT",
    "HYPEUSDT",
    "LEOUSDT",
    "RAINUSDT",
    "ZECUSDT",
    "CCUSDT",
    "XLMUSDT",
    "ADAUSDT",
    "XMRUSDT",
    "LINKUSDT",
    "TONUSDT",
    "BCHUSDT",
    "MUSDT",
    "HBARUSDT",
    "LTCUSDT",
    "SUIUSDT",
    "AVAXUSDT",
    "SHIBUSDT",
    "NEARUSDT",
    "LABUSDT",
    "CROUSDT",
    "USDYUSDT",
    "TAOUSDT",
    "PAXGUSDT",
]
PAIR_UNIVERSE = [
    ("BTCUSDT", "ETHUSDT"),
    ("SOLUSDT", "ETHUSDT"),
    ("BNBUSDT", "ETHUSDT"),
    ("BCHUSDT", "LTCUSDT"),
    ("XRPUSDT", "XLMUSDT"),
    ("ADAUSDT", "XLMUSDT"),
    ("AVAXUSDT", "SOLUSDT"),
    ("NEARUSDT", "SOLUSDT"),
    ("SUIUSDT", "SOLUSDT"),
    ("LINKUSDT", "ETHUSDT"),
    ("DOGEUSDT", "SHIBUSDT"),
    ("TAOUSDT", "BTCUSDT"),
]
MIN_BARS = 120
MIN_SPLIT_TRADES = 20
MIN_PROFIT_FACTOR = 1.10
MAX_DRAWDOWN_PCT = 20.0
CAPITAL_USDT = 10_000.0
FEE_BPS = 4.0
MAX_TRADES_PER_SYMBOL = 800
MAX_REPORT_TRADES = 500


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def runtime_dir(root: Path = ROOT) -> Path:
    return root / "runtime"


def reports_dir(root: Path = ROOT) -> Path:
    return root / "reports"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def latest_json_path(root: Path, strategy_id: str) -> Path:
    return runtime_dir(root) / f"{strategy_id}_historical_research_latest.json"


def latest_html_path(root: Path, strategy_id: str) -> Path:
    return reports_dir(root) / f"{strategy_id}_historical_research_latest.html"


def historical_payload(root: Path = ROOT) -> dict[str, Any]:
    candidates = [
        runtime_dir(root) / "historical_kline_backfill_latest.json",
        root / "server_logs_tencent" / "runtime" / "historical_kline_backfill_latest.json",
    ]
    best: tuple[tuple[float, float, float], dict[str, Any]] | None = None
    for path in candidates:
        payload = read_json(path)
        if not payload:
            continue
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        rank = (
            backtest_engine.safe_float(progress.get("written_rows")),
            backtest_engine.safe_float(progress.get("percent")),
            mtime,
        )
        if best is None or rank > best[0]:
            best = (rank, payload)
    return best[1] if best else {}


def universe_symbols(root: Path = ROOT, explicit: list[str] | None = None) -> list[str]:
    if explicit:
        return [item.upper() for item in explicit if item.strip()]
    hist = historical_payload(root)
    universe = hist.get("universe") if isinstance(hist.get("universe"), dict) else {}
    symbols = universe.get("symbols") if isinstance(universe.get("symbols"), list) else []
    clean = [str(item).upper().strip() for item in symbols if str(item).strip()]
    return clean or list(DEFAULT_UNIVERSE)


def parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    rows = [item.strip() for item in value.split(",") if item.strip()]
    return rows or list(default)


def date_range(start: datetime, end: datetime) -> list[str]:
    day = start.date()
    end_day = end.date()
    out: list[str] = []
    while day <= end_day:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def row_to_bar(row: dict[str, Any]) -> dict[str, Any]:
    open_ms = backtest_engine.safe_int(row.get("open_time_ms"))
    return {
        "ts": row.get("open_time") or backtest_engine.ms_to_iso(open_ms),
        "open_time_ms": open_ms,
        "open": backtest_engine.safe_float(row.get("open")),
        "high": backtest_engine.safe_float(row.get("high")),
        "low": backtest_engine.safe_float(row.get("low")),
        "close": backtest_engine.safe_float(row.get("close")),
        "volume": backtest_engine.safe_float(row.get("volume")),
        "quote_volume": backtest_engine.safe_float(row.get("quote_volume")),
    }


def load_interval_bars(
    *,
    root: Path,
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    symbol_set = {item.upper() for item in symbols}
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    table = root / "research_store" / "historical_klines"
    loaded: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    for day in date_range(start, end):
        path = table / f"date={day}" / "data.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in symbol_set or str(row.get("interval") or "") != interval:
                continue
            open_ms = backtest_engine.safe_int(row.get("open_time_ms"))
            if not (start_ms <= open_ms <= end_ms):
                continue
            bar = row_to_bar(row)
            if bar["open"] > 0 and bar["high"] > 0 and bar["low"] > 0 and bar["close"] > 0:
                loaded[symbol].append(bar)
    for symbol, rows in list(loaded.items()):
        rows.sort(key=lambda item: int(item.get("open_time_ms") or 0))
        deduped: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            key = int(row.get("open_time_ms") or 0)
            if key in seen:
                continue
            deduped.append(row)
            seen.add(key)
        loaded[symbol] = deduped
    return loaded


def coverage_rows(loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for interval, by_symbol in loaded_by_interval.items():
        for symbol, bars in by_symbol.items():
            rows.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "bars": len(bars),
                    "first": bars[0].get("ts") if bars else "",
                    "last": bars[-1].get("ts") if bars else "",
                    "usable": len(bars) >= MIN_BARS,
                }
            )
    return rows


def sma(values: list[float], idx: int, length: int) -> float:
    if idx + 1 < length:
        return 0.0
    window = values[idx - length + 1 : idx + 1]
    return sum(window) / len(window)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a > 0 else 0.0


def volume_ratio(bars: list[dict[str, Any]], idx: int, length: int = 20) -> float:
    if idx + 1 < length:
        return 1.0
    current = backtest_engine.safe_float(bars[idx].get("quote_volume"), backtest_engine.safe_float(bars[idx].get("volume")))
    vals = [
        backtest_engine.safe_float(row.get("quote_volume"), backtest_engine.safe_float(row.get("volume")))
        for row in bars[idx - length + 1 : idx + 1]
    ]
    avg = sum(vals) / len(vals) if vals else 0.0
    return current / avg if avg > 0 else 1.0


def summarize_trades(trades: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    return backtest_engine.metrics(trades, CAPITAL_USDT)


def split_sequence(rows: list[dict[str, Any]], split: str | None) -> list[dict[str, Any]]:
    if split is None:
        return rows
    parts = backtest_engine.split_bars(rows)
    return parts.get(split, rows)


def d_variants() -> list[dict[str, Any]]:
    variants = []
    for lookback in (20, 40, 80):
        for atr_mult in (2.0, 3.0):
            for volume_min in (1.0, 1.2):
                variants.append(
                    {
                        "name": f"donchian={lookback},stop={atr_mult:g},vol={volume_min:g}",
                        "params": {
                            "donchian_lookback": lookback,
                            "atr_stop_multiplier": atr_mult,
                            "take_profit_atr": 6.0,
                            "trailing_pullback_atr": 1.5,
                            "trailing_activation_atr": 1.0,
                            "volume_ratio_min": volume_min,
                            "atr_pct_min": 0.15,
                            "atr_pct_max": 8.0,
                            "trade_size_usdt": 100.0,
                            "leverage": 2.0,
                            "max_hold_bars": 96,
                        },
                    }
                )
    return variants


def simulate_d_symbol(symbol: str, interval: str, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    lookback = max(10, backtest_engine.safe_int(params.get("donchian_lookback"), 40))
    max_hold = max(4, backtest_engine.safe_int(params.get("max_hold_bars"), 96))
    trade_size = backtest_engine.safe_float(params.get("trade_size_usdt"), 100.0)
    leverage = backtest_engine.safe_float(params.get("leverage"), 2.0)
    fee_bps = FEE_BPS
    out: list[dict[str, Any]] = []
    idx = lookback + 1
    while idx < len(bars) - 2 and len(out) < MAX_TRADES_PER_SYMBOL:
        close = backtest_engine.safe_float(bars[idx].get("close"))
        if close <= 0:
            idx += 1
            continue
        prev = bars[idx - lookback : idx]
        channel_high = max(backtest_engine.safe_float(row.get("high")) for row in prev)
        channel_low = min(backtest_engine.safe_float(row.get("low")) for row in prev)
        atr_value = backtest_engine.atr(bars, idx)
        atr_pct = atr_value / close * 100.0 if close > 0 else 0.0
        vol_ratio = volume_ratio(bars, idx)
        if not (backtest_engine.safe_float(params.get("atr_pct_min"), 0.15) <= atr_pct <= backtest_engine.safe_float(params.get("atr_pct_max"), 8.0)):
            idx += 1
            continue
        if vol_ratio < backtest_engine.safe_float(params.get("volume_ratio_min"), 1.0):
            idx += 1
            continue
        side = ""
        if close > channel_high:
            side = "long"
        elif close < channel_low:
            side = "short"
        if not side:
            idx += 1
            continue
        entry_idx = idx + 1
        entry = backtest_engine.safe_float(bars[entry_idx].get("open"), backtest_engine.safe_float(bars[entry_idx].get("close")))
        if entry <= 0:
            idx += 1
            continue
        atr_value = max(atr_value, entry * 0.001)
        stop_mult = backtest_engine.safe_float(params.get("atr_stop_multiplier"), 2.0)
        tp_mult = backtest_engine.safe_float(params.get("take_profit_atr"), 6.0)
        if side == "long":
            stop_loss = entry - atr_value * stop_mult
            take_profit = entry + atr_value * tp_mult
        else:
            stop_loss = entry + atr_value * stop_mult
            take_profit = entry - atr_value * tp_mult
        qty = trade_size / entry
        forward = bars[entry_idx : min(len(bars), entry_idx + max_hold)]
        if len(forward) < 2:
            break
        try:
            fill = simulate_replay_fill(
                ReplayFillRequest(
                    symbol=symbol,
                    side=side,
                    entry_price=entry,
                    quantity=qty,
                    stop_loss=max(0.0, stop_loss),
                    take_profit=max(0.0, take_profit),
                    trailing_stop_atr=backtest_engine.safe_float(params.get("trailing_pullback_atr"), 1.5),
                    trailing_activation_atr=backtest_engine.safe_float(params.get("trailing_activation_atr"), 1.0),
                    atr=atr_value,
                    leverage=leverage,
                    fee_bps=fee_bps,
                    slippage_bps=0.0,
                    conservative_intrabar=True,
                ),
                forward,
            )
        except Exception:
            idx += 1
            continue
        row = fill.to_dict()
        row.update(
            {
                "strategy": "D/trend_breakout",
                "symbol": symbol,
                "interval": interval,
                "entry_signal_ts": bars[idx].get("ts"),
                "entry_ts": bars[entry_idx].get("ts"),
                "donchian_high": round(channel_high, 8),
                "donchian_low": round(channel_low, 8),
                "atr_pct": round(atr_pct, 6),
                "volume_ratio": round(vol_ratio, 6),
                "adapter": "historical_research_d",
            }
        )
        out.append(row)
        idx = entry_idx + max(1, int(fill.bars_held))
    return out


def run_d_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = split_sequence(bars, split)
        if len(use_bars) >= MIN_BARS:
            trades.extend(simulate_d_symbol(symbol, interval, use_bars, variant["params"]))
    return trades


def e_variants() -> list[dict[str, Any]]:
    variants = []
    for lookback in (24, 48, 96):
        for top_n in (3, 5):
            for hold in (4, 8):
                variants.append(
                    {
                        "name": f"lookback={lookback},top={top_n},hold={hold}",
                        "params": {
                            "lookback_bars": lookback,
                            "top_n": top_n,
                            "hold_bars": hold,
                            "leg_notional_usdt": 100.0,
                            "leverage": 1.0,
                            "min_available_symbols": 16,
                        },
                    }
                )
    return variants


def time_index(loaded: dict[str, list[dict[str, Any]]]) -> dict[int, dict[str, dict[str, Any]]]:
    by_time: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for symbol, bars in loaded.items():
        for row in bars:
            by_time[int(row.get("open_time_ms") or 0)][symbol] = row
    return dict(sorted(by_time.items()))


def run_e_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    if split is not None:
        loaded = {symbol: split_sequence(bars, split) for symbol, bars in loaded.items()}
    idx_by_time = time_index(loaded)
    times = sorted(idx_by_time)
    if len(times) < MIN_BARS:
        return []
    lookback = max(4, backtest_engine.safe_int(params.get("lookback_bars"), 48))
    hold = max(1, backtest_engine.safe_int(params.get("hold_bars"), 4))
    top_n = max(1, backtest_engine.safe_int(params.get("top_n"), 3))
    min_symbols = max(top_n * 2, backtest_engine.safe_int(params.get("min_available_symbols"), 16))
    leg_notional = backtest_engine.safe_float(params.get("leg_notional_usdt"), 100.0)
    leverage = backtest_engine.safe_float(params.get("leverage"), 1.0)
    fee_rate = FEE_BPS / 10_000.0
    trades: list[dict[str, Any]] = []
    pos = lookback
    while pos + hold < len(times):
        entry_time = times[pos]
        lookback_time = times[pos - lookback]
        exit_time = times[pos + hold]
        entry_rows = idx_by_time.get(entry_time, {})
        lookback_rows = idx_by_time.get(lookback_time, {})
        exit_rows = idx_by_time.get(exit_time, {})
        ranks: list[tuple[str, float, float, float]] = []
        for symbol, row in entry_rows.items():
            if symbol not in lookback_rows or symbol not in exit_rows:
                continue
            past = backtest_engine.safe_float(lookback_rows[symbol].get("close"))
            entry = backtest_engine.safe_float(row.get("close"))
            exit_price = backtest_engine.safe_float(exit_rows[symbol].get("close"))
            if past > 0 and entry > 0 and exit_price > 0:
                ranks.append((symbol, pct_change(past, entry), entry, exit_price))
        if len(ranks) < min_symbols:
            pos += hold
            continue
        ranks.sort(key=lambda item: item[1], reverse=True)
        legs = [(symbol, "long", entry, exit_price, mom) for symbol, mom, entry, exit_price in ranks[:top_n]]
        legs.extend((symbol, "short", entry, exit_price, mom) for symbol, mom, entry, exit_price in ranks[-top_n:])
        net = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        leg_rows = []
        fees = 0.0
        for symbol, side, entry, exit_price, mom in legs:
            raw_ret = (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
            pnl = leg_notional * leverage * raw_ret
            fee = leg_notional * leverage * fee_rate * 2.0
            net_leg = pnl - fee
            fees += fee
            net += net_leg
            if net_leg >= 0:
                gross_profit += net_leg
            else:
                gross_loss += abs(net_leg)
            leg_rows.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "momentum_pct": round(mom, 6),
                    "entry": round(entry, 10),
                    "exit": round(exit_price, 10),
                    "net_pnl_usdt": round(net_leg, 6),
                }
            )
        trades.append(
            {
                "strategy": "E/cross_sectional_momentum",
                "symbol": "PORTFOLIO",
                "interval": interval,
                "side": "long_short",
                "entry_ts": backtest_engine.ms_to_iso(entry_time),
                "exit_ts": backtest_engine.ms_to_iso(exit_time),
                "net_pnl_usdt": round(net, 6),
                "fee_usdt": round(fees, 6),
                "slippage_usdt": 0.0,
                "gross_profit_usdt": round(gross_profit, 6),
                "gross_loss_usdt": round(gross_loss, 6),
                "bars_held": hold,
                "legs": leg_rows,
                "adapter": "historical_research_e",
            }
        )
        pos += hold
    return trades


def f_variants() -> list[dict[str, Any]]:
    variants = []
    for lookback in (80, 120):
        for z_entry in (1.5, 2.0, 2.5):
            for exit_z in (0.25, 0.5):
                variants.append(
                    {
                        "name": f"lookback={lookback},entry_z={z_entry:g},exit_z={exit_z:g}",
                        "params": {
                            "lookback_bars": lookback,
                            "entry_z": z_entry,
                            "exit_z": exit_z,
                            "stop_z": 3.5,
                            "max_hold_bars": 96,
                            "leg_notional_usdt": 100.0,
                            "leverage": 1.0,
                        },
                    }
                )
    return variants


def align_pair(a_bars: list[dict[str, Any]], b_bars: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    by_a = {int(row.get("open_time_ms") or 0): row for row in a_bars}
    by_b = {int(row.get("open_time_ms") or 0): row for row in b_bars}
    return [(ts, by_a[ts], by_b[ts]) for ts in sorted(set(by_a) & set(by_b))]


def run_f_pair(interval: str, pair: tuple[str, str], rows: list[tuple[int, dict[str, Any], dict[str, Any]]], params: dict[str, Any]) -> list[dict[str, Any]]:
    lookback = max(20, backtest_engine.safe_int(params.get("lookback_bars"), 120))
    entry_z = backtest_engine.safe_float(params.get("entry_z"), 2.0)
    exit_z = backtest_engine.safe_float(params.get("exit_z"), 0.5)
    stop_z = backtest_engine.safe_float(params.get("stop_z"), 3.5)
    max_hold = max(4, backtest_engine.safe_int(params.get("max_hold_bars"), 96))
    leg_notional = backtest_engine.safe_float(params.get("leg_notional_usdt"), 100.0)
    leverage = backtest_engine.safe_float(params.get("leverage"), 1.0)
    fee_rate = FEE_BPS / 10_000.0
    spreads: list[float] = []
    for _ts, a, b in rows:
        ac = backtest_engine.safe_float(a.get("close"))
        bc = backtest_engine.safe_float(b.get("close"))
        spreads.append(math.log(ac / bc) if ac > 0 and bc > 0 else 0.0)
    trades: list[dict[str, Any]] = []
    idx = lookback
    while idx + 2 < len(rows):
        window = spreads[idx - lookback : idx]
        sd = stddev(window)
        if sd <= 0:
            idx += 1
            continue
        mean = sum(window) / len(window)
        z = (spreads[idx] - mean) / sd
        if abs(z) < entry_z:
            idx += 1
            continue
        side_a = "short" if z > 0 else "long"
        side_b = "long" if z > 0 else "short"
        entry_idx = idx + 1
        entry_ts, a_entry_row, b_entry_row = rows[entry_idx]
        a_entry = backtest_engine.safe_float(a_entry_row.get("open"), backtest_engine.safe_float(a_entry_row.get("close")))
        b_entry = backtest_engine.safe_float(b_entry_row.get("open"), backtest_engine.safe_float(b_entry_row.get("close")))
        if a_entry <= 0 or b_entry <= 0:
            idx += 1
            continue
        exit_idx = min(len(rows) - 1, entry_idx + max_hold)
        exit_reason = "max_hold"
        for pos in range(entry_idx + 1, min(len(rows), entry_idx + max_hold + 1)):
            sub_window = spreads[max(0, pos - lookback) : pos]
            sub_sd = stddev(sub_window)
            if sub_sd <= 0:
                continue
            sub_mean = sum(sub_window) / len(sub_window)
            sub_z = (spreads[pos] - sub_mean) / sub_sd
            if abs(sub_z) <= exit_z:
                exit_idx = pos
                exit_reason = "mean_revert"
                break
            if abs(sub_z) >= stop_z:
                exit_idx = pos
                exit_reason = "z_stop"
                break
        exit_ts, a_exit_row, b_exit_row = rows[exit_idx]
        a_exit = backtest_engine.safe_float(a_exit_row.get("close"))
        b_exit = backtest_engine.safe_float(b_exit_row.get("close"))
        if a_exit <= 0 or b_exit <= 0:
            idx += 1
            continue
        a_ret = (a_exit - a_entry) / a_entry if side_a == "long" else (a_entry - a_exit) / a_entry
        b_ret = (b_exit - b_entry) / b_entry if side_b == "long" else (b_entry - b_exit) / b_entry
        gross = leg_notional * leverage * a_ret + leg_notional * leverage * b_ret
        fee = leg_notional * leverage * fee_rate * 4.0
        net = gross - fee
        trades.append(
            {
                "strategy": "F/pairs_mean_reversion",
                "symbol": f"{pair[0]}/{pair[1]}",
                "interval": interval,
                "side": f"{side_a}_{side_b}",
                "entry_ts": backtest_engine.ms_to_iso(entry_ts),
                "exit_ts": backtest_engine.ms_to_iso(exit_ts),
                "entry_z": round(z, 6),
                "exit_reason": exit_reason,
                "net_pnl_usdt": round(net, 6),
                "fee_usdt": round(fee, 6),
                "slippage_usdt": 0.0,
                "bars_held": exit_idx - entry_idx,
                "legs": [
                    {"symbol": pair[0], "side": side_a, "entry": round(a_entry, 10), "exit": round(a_exit, 10)},
                    {"symbol": pair[1], "side": side_b, "entry": round(b_entry, 10), "exit": round(b_exit, 10)},
                ],
                "adapter": "historical_research_f",
            }
        )
        idx = exit_idx + 1
    return trades


def run_f_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    for pair in PAIR_UNIVERSE:
        if pair[0] not in loaded or pair[1] not in loaded:
            continue
        a_bars = split_sequence(loaded[pair[0]], split)
        b_bars = split_sequence(loaded[pair[1]], split)
        aligned = align_pair(a_bars, b_bars)
        if len(aligned) >= MIN_BARS:
            trades.extend(run_f_pair(interval, pair, aligned, params))
    return trades


STRATEGIES = {
    "d_trend": {
        "strategy": "D/trend_breakout",
        "title": "D 低频趋势突破",
        "description": "Donchian 突破 + ATR 止损/止盈/追踪，优先验证 1h/4h 低频趋势。",
        "variants": d_variants,
        "runner": run_d_interval,
    },
    "e_cross_section": {
        "strategy": "E/cross_sectional_momentum",
        "title": "E Top30 横截面强弱",
        "description": "按 Top30 近期强弱排序，做多强者、做空弱者，测试组合相对强弱。",
        "variants": e_variants,
        "runner": run_e_interval,
    },
    "f_pairs": {
        "strategy": "F/pairs_mean_reversion",
        "title": "F 配对均值回归",
        "description": "预注册高相关币对，价差 z-score 偏离后做均值回归。",
        "variants": f_variants,
        "runner": run_f_interval,
    },
}


def anti_fit(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    full = row.get("full") or {}
    for split in ("train", "validation", "test"):
        metrics = row.get(split) or {}
        if backtest_engine.safe_int(metrics.get("trades")) < MIN_SPLIT_TRADES:
            reasons.append(f"{split}_trade_count_low")
        if backtest_engine.safe_float(metrics.get("net_profit_usdt")) <= 0:
            reasons.append(f"{split}_net_not_positive")
        if backtest_engine.safe_float(metrics.get("profit_factor")) < MIN_PROFIT_FACTOR:
            reasons.append(f"{split}_profit_factor_below_{MIN_PROFIT_FACTOR:g}")
    if backtest_engine.safe_float(full.get("max_drawdown_pct")) > MAX_DRAWDOWN_PCT:
        reasons.append("drawdown_above_20pct")
    return {"anti_fit_pass": not reasons, "anti_fit_reasons": reasons}


def robust_score(row: dict[str, Any]) -> float:
    full = row.get("full") or {}
    train = row.get("train") or {}
    validation = row.get("validation") or {}
    test = row.get("test") or {}
    nets = [
        backtest_engine.safe_float(train.get("net_profit_usdt")),
        backtest_engine.safe_float(validation.get("net_profit_usdt")),
        backtest_engine.safe_float(test.get("net_profit_usdt")),
    ]
    penalty = max(0.0, backtest_engine.safe_float(full.get("max_drawdown_pct")) - MAX_DRAWDOWN_PCT) * 50.0
    return round(min(nets) + backtest_engine.safe_float(test.get("net_profit_usdt")) * 0.5 - penalty, 6)


def evaluate_variant(interval: str, loaded: dict[str, list[dict[str, Any]]], strategy_spec: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    runner = strategy_spec["runner"]
    full_trades = runner(interval, loaded, variant, None)
    train_trades = runner(interval, loaded, variant, "train")
    validation_trades = runner(interval, loaded, variant, "validation")
    test_trades = runner(interval, loaded, variant, "test")
    full, charts = summarize_trades(full_trades)
    train, _ = summarize_trades(train_trades)
    validation, _ = summarize_trades(validation_trades)
    test, _ = summarize_trades(test_trades)
    row = {
        "name": variant["name"],
        "params": variant["params"],
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "charts": {
            "equity_curve": charts["equity_curve"][-500:],
            "drawdown": charts["drawdown"][-500:],
            "monthly_returns": charts["monthly_returns"],
        },
        "trades": sorted(full_trades, key=lambda item: str(item.get("exit_ts") or ""))[-MAX_REPORT_TRADES:],
    }
    row["robust_score"] = robust_score(row)
    row.update(anti_fit(row))
    return row


def run_strategy(
    *,
    root: Path,
    strategy_id: str,
    symbols: list[str],
    intervals: list[str],
    start: datetime,
    end: datetime,
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]],
    coverage: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = STRATEGIES[strategy_id]
    interval_results: dict[str, Any] = {}
    portfolio_best_net = 0.0
    portfolio_best_trades = 0
    robust_candidate_intervals = 0
    lines: list[str] = []
    research_candidates: list[dict[str, Any]] = []
    for interval in intervals:
        print(f"[{now_iso()}] {strategy_id} {interval} start", file=sys.stderr, flush=True)
        loaded = {sym: bars for sym, bars in loaded_by_interval.get(interval, {}).items() if len(bars) >= MIN_BARS}
        variants = spec["variants"]()
        rows = [evaluate_variant(interval, loaded, spec, variant) for variant in variants]
        rows.sort(key=lambda item: backtest_engine.safe_float((item.get("full") or {}).get("net_profit_usdt")), reverse=True)
        best_full = rows[0] if rows else {}
        best_robust = sorted(rows, key=lambda item: backtest_engine.safe_float(item.get("robust_score")), reverse=True)[0] if rows else {}
        passed = [row for row in rows if row.get("anti_fit_pass")]
        if passed:
            robust_candidate_intervals += 1
            research_candidates.append(
                {
                    "interval": interval,
                    "name": best_robust.get("name"),
                    "params": best_robust.get("params"),
                    "full": best_robust.get("full"),
                    "test": best_robust.get("test"),
                }
            )
        best_net = backtest_engine.safe_float((best_robust.get("full") or {}).get("net_profit_usdt"))
        best_trades = backtest_engine.safe_int((best_robust.get("full") or {}).get("trades"))
        portfolio_best_net += best_net
        portfolio_best_trades += best_trades
        action = "paper_shadow_review_candidate_requires_operator_approval" if passed else "no_paper_shadow"
        lines.append(
            f"{interval}: best_robust {best_net:+.2f} USDT; trades {best_trades}; "
            f"{'OOS passed' if passed else 'OOS failed'}"
        )
        interval_results[interval] = {
            "interval": interval,
            "usable_symbol_count": len(loaded),
            "target_symbol_count": len(symbols),
            "variant_count": len(rows),
            "best_full": best_full,
            "best_robust": best_robust,
            "variants": rows,
            "research_decision": {
                "action": action,
                "auto_apply_allowed": False,
                "automatic_upgrade_allowed": False,
                "paper_shadow_review_candidate": bool(passed),
                "paper_shadow_allowed": False,
                "reason": "oos_gate_passed_but_operator_approval_required" if passed else "oos_or_risk_gate_failed",
            },
        }
        print(
            f"[{now_iso()}] {strategy_id} {interval} done net={best_net:+.2f} "
            f"trades={best_trades} oos={'pass' if passed else 'fail'}",
            file=sys.stderr,
            flush=True,
        )
    overall_action = "paper_shadow_review_candidate_requires_operator_approval" if robust_candidate_intervals else "research_only_reject_for_now"
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "strategy": spec["strategy"],
        "strategy_id": strategy_id,
        "title": spec["title"],
        "description": spec["description"],
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"), "days": (end - start).days},
        "engine_parity": "research_adapter",
        "adapter_note": "Historical Kline research only; not live scanner/order execution.",
        "coverage": {
            "target_symbols": len(symbols),
            "target_symbol_intervals": len(symbols) * len(intervals),
            "usable_symbols": len({row["symbol"] for row in coverage if row.get("usable")}),
            "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
            "rows": coverage,
        },
        "config": {
            "symbols": symbols,
            "intervals": intervals,
            "capital_usdt_per_interval": CAPITAL_USDT,
            "fee_bps": FEE_BPS,
            "max_variants_per_strategy_interval": max(len(spec["variants"]()), 0),
            "oos_rules": {
                "min_split_trades": MIN_SPLIT_TRADES,
                "min_profit_factor": MIN_PROFIT_FACTOR,
                "max_drawdown_pct": MAX_DRAWDOWN_PCT,
                "all_splits_must_be_profitable": True,
            },
        },
        "historical_quality": historical_payload(root).get("quality", {}),
        "interval_results": interval_results,
        "portfolio_summary": {
            "candidate_net_profit_usdt": round(portfolio_best_net, 6),
            "candidate_return_pct_on_interval_capital": round(portfolio_best_net / (CAPITAL_USDT * max(1, len(intervals))) * 100.0, 6),
            "candidate_trades": portfolio_best_trades,
            "intervals": len(intervals),
            "robust_candidate_intervals": robust_candidate_intervals,
            "paper_shadow_review_candidate": robust_candidate_intervals > 0,
            "paper_shadow_allowed": False,
            "auto_apply_allowed": False,
        },
        "operator_summary": {
            "overall_action": overall_action,
            "lines": lines,
            "research_candidates": research_candidates,
            "plain_advice": "通过 OOS/回撤/PF 门槛只代表可人工复核；未经明确批准，不进入 paper shadow，更不进入实盘。",
            "auto_apply_allowed": False,
            "automatic_upgrade_allowed": False,
        },
        "safety": safety_payload(),
        "report_path": str(latest_html_path(root, strategy_id)),
    }
    return payload


def safety_payload() -> dict[str, Any]:
    return {
        "binance_requests_enabled": False,
        "strategy_frequency_change": False,
        "live_scanner_impact": "none",
        "paper_or_real_orders": False,
        "live_config_mutation": False,
        "auto_apply_allowed": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def metric_cell(row: dict[str, Any], key: str) -> str:
    value = (row.get("full") or {}).get(key)
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.2f}" if key != "trades" else str(int(value))
    return escape(str(value))


def render_html(payload: dict[str, Any]) -> str:
    title = escape(str(payload.get("title") or payload.get("strategy")))
    summary = payload.get("portfolio_summary") or {}
    operator = payload.get("operator_summary") or {}
    rows_html = []
    for interval, result in (payload.get("interval_results") or {}).items():
        best = result.get("best_robust") or {}
        rows_html.append(
            "<tr>"
            f"<td>{escape(str(interval))}</td>"
            f"<td>{escape(str(best.get('name') or '-'))}</td>"
            f"<td>{metric_cell(best, 'net_profit_usdt')}</td>"
            f"<td>{metric_cell(best, 'profit_factor')}</td>"
            f"<td>{metric_cell(best, 'max_drawdown_pct')}</td>"
            f"<td>{metric_cell(best, 'win_rate_pct')}</td>"
            f"<td>{metric_cell(best, 'trades')}</td>"
            f"<td>{'通过' if best.get('anti_fit_pass') else '未通过'}</td>"
            f"<td>{escape(', '.join(best.get('anti_fit_reasons') or [])[:260])}</td>"
            "</tr>"
        )
    variant_sections = []
    for interval, result in (payload.get("interval_results") or {}).items():
        rows = []
        for row in (result.get("variants") or [])[:24]:
            rows.append(
                "<tr>"
                f"<td>{escape(str(row.get('name') or '-'))}</td>"
                f"<td>{metric_cell(row, 'net_profit_usdt')}</td>"
                f"<td>{metric_cell(row, 'profit_factor')}</td>"
                f"<td>{metric_cell(row, 'max_drawdown_pct')}</td>"
                f"<td>{metric_cell(row, 'trades')}</td>"
                f"<td>{escape(str(row.get('robust_score') or 0))}</td>"
                f"<td>{'通过' if row.get('anti_fit_pass') else '未通过'}</td>"
                "</tr>"
            )
        variant_sections.append(
            f"<details><summary>{escape(str(interval))} 参数组</summary>"
            "<table><thead><tr><th>参数</th><th>净收益</th><th>PF</th><th>回撤%</th><th>交易数</th><th>稳健分</th><th>OOS</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></details>"
        )
    trade_rows = []
    for interval, result in (payload.get("interval_results") or {}).items():
        best = result.get("best_robust") or {}
        for trade in (best.get("trades") or [])[-80:]:
            trade_rows.append(
                "<tr>"
                f"<td>{escape(str(interval))}</td>"
                f"<td>{escape(str(trade.get('symbol') or '-'))}</td>"
                f"<td>{escape(str(trade.get('side') or '-'))}</td>"
                f"<td>{escape(str(trade.get('entry_ts') or '-'))}</td>"
                f"<td>{escape(str(trade.get('exit_ts') or '-'))}</td>"
                f"<td>{backtest_engine.safe_float(trade.get('net_pnl_usdt')):.4f}</td>"
                f"<td>{escape(str(trade.get('exit_reason') or trade.get('adapter') or '-'))}</td>"
                "</tr>"
            )
    lines = "".join(f"<li>{escape(str(line))}</li>" for line in operator.get("lines") or [])
    candidates = operator.get("research_candidates") or []
    candidate_text = "有候选可人工复核；未经明确批准不进入 paper shadow" if candidates else "暂无人工复核候选"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;background:#0b1118;color:#e7eef7;font-family:Arial,'Microsoft YaHei',sans-serif}}
.wrap{{max-width:1280px;margin:0 auto;padding:28px}}
.hero{{border:1px solid #1d344a;background:#101a25;padding:22px;border-radius:8px}}
h1{{margin:0 0 8px;font-size:28px}} h2{{margin-top:28px}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}}
.card{{background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:14px}}
.card span{{color:#8ea2b7;font-size:12px}} .card b{{display:block;margin-top:6px;font-size:22px}}
table{{width:100%;border-collapse:collapse;background:#0f1722;border:1px solid #22364a}}
th,td{{padding:9px;border-bottom:1px solid #203246;text-align:left;font-size:13px;vertical-align:top}}
th{{color:#9db4ca;background:#132031}} .bad{{color:#ffb4a8}} .good{{color:#7ee787}}
details{{margin:10px 0;background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:10px}}
.scroll{{max-height:420px;overflow:auto;border:1px solid #22364a}}
code{{color:#9bdcff}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
<h1>{title}</h1>
<p>{escape(str(payload.get('description') or ''))}</p>
<p>生成时间：{escape(str(payload.get('generated_at')))}；引擎：<code>{escape(str(payload.get('engine_parity')))}</code>；动作：<b>{escape(str(operator.get('overall_action')))}</b></p>
</section>
<section class="grid">
<div class="card"><span>候选净收益</span><b>{backtest_engine.safe_float(summary.get('candidate_net_profit_usdt')):+.2f}</b></div>
<div class="card"><span>候选交易数</span><b>{int(summary.get('candidate_trades') or 0)}</b></div>
<div class="card"><span>通过周期</span><b>{int(summary.get('robust_candidate_intervals') or 0)} / {int(summary.get('intervals') or 0)}</b></div>
<div class="card"><span>Paper Shadow</span><b>{'需人工批准' if summary.get('paper_shadow_review_candidate') else '不允许'}</b></div>
</section>
<h2>操作结论</h2>
<div class="card"><b>{escape(candidate_text)}</b><ul>{lines}</ul><p>{escape(str(operator.get('plain_advice') or ''))}</p></div>
<h2>分周期最佳稳健结果</h2>
<table><thead><tr><th>周期</th><th>候选</th><th>净收益</th><th>PF</th><th>最大回撤%</th><th>胜率%</th><th>交易数</th><th>OOS</th><th>失败原因</th></tr></thead><tbody>{''.join(rows_html)}</tbody></table>
<h2>参数组</h2>
{''.join(variant_sections)}
<h2>最近交易样本</h2>
<div class="scroll"><table><thead><tr><th>周期</th><th>标的</th><th>方向</th><th>入场</th><th>出场</th><th>净收益</th><th>原因</th></tr></thead><tbody>{''.join(trade_rows)}</tbody></table></div>
<h2>安全边界</h2>
<div class="card"><p>只读历史研究；不调用 Binance，不改扫描频率，不改 live config，不开 paper/real order，不启用自动调参/回滚/升级。</p></div>
</main></body></html>"""


def run_all(root: Path, symbols: list[str], intervals: list[str], start: datetime, end: datetime) -> dict[str, Any]:
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for interval in intervals:
        loaded_by_interval[interval] = load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    coverage = coverage_rows(loaded_by_interval)
    all_payloads: dict[str, Any] = {}
    for strategy_id in STRATEGIES:
        payload = run_strategy(
            root=root,
            strategy_id=strategy_id,
            symbols=symbols,
            intervals=intervals,
            start=start,
            end=end,
            loaded_by_interval=loaded_by_interval,
            coverage=coverage,
        )
        write_json(latest_json_path(root, strategy_id), payload)
        html = render_html(payload)
        path = latest_html_path(root, strategy_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        all_payloads[strategy_id] = payload
    index_payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "strategies": {strategy_id: payload.get("portfolio_summary") for strategy_id, payload in all_payloads.items()},
        "operator_summary": {
            strategy_id: (payload.get("operator_summary") or {}).get("overall_action")
            for strategy_id, payload in all_payloads.items()
        },
        "safety": safety_payload(),
        "reports": {strategy_id: str(latest_html_path(root, strategy_id)) for strategy_id in all_payloads},
    }
    write_json(runtime_dir(root) / "d_e_f_historical_research_latest.json", index_payload)
    return index_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run D/E/F historical research reports")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--symbols", default="")
    args = parser.parse_args(argv)
    root = args.root
    intervals = parse_csv(args.intervals, DEFAULT_INTERVALS)
    symbols = universe_symbols(root, parse_csv(args.symbols, []) if args.symbols else None)
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, int(args.days)))
    payload = run_all(root, symbols, intervals, start, end)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
