"""Composite indicator historical research report.

Local-only research runner for public community-inspired indicator
combinations. The intent is broad hypothesis screening, not live promotion.

Families:
- M/qqe_squeeze: RSI/QQE-style momentum plus BB/KC squeeze release.
- N/utbot_ema: ATR trailing direction plus EMA trend confirmation.
- O/macd_ema_volume: EMA trend, MACD histogram, RSI, volume.
- P/donchian_adx: Donchian breakout plus ADX/DMI and volume.
- Q/bb_rsi_regime_reversion: Bollinger re-entry plus RSI and low ADX.
- R/ichimoku_vwap: Tenkan/Kijun/cloud proxy plus rolling VWAP.

It reads only the local historical Kline warehouse. It never calls Binance,
mutates live config, restarts scanners, places orders, or enables automatic
tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
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
import d_e_f_historical_research_report as shared
import j_k_l_indicator_research_report as base_ind


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m"]
DEFAULT_MAX_VARIANTS = 4
MAX_REPORT_TRADES = 80


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def runtime_dir(root: Path = ROOT) -> Path:
    return root / "runtime"


def reports_dir(root: Path = ROOT) -> Path:
    return root / "reports"


def latest_json_path(root: Path = ROOT) -> Path:
    return runtime_dir(root) / "composite_indicator_research_latest.json"


def latest_html_path(root: Path = ROOT) -> Path:
    return reports_dir(root) / "composite_indicator_research_latest.html"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def close_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("close"))


def open_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("open"), close_price(row))


def high_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("high"), close_price(row))


def low_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("low"), close_price(row))


def ema_series(values: list[float], length: int) -> list[float]:
    length = max(2, int(length))
    if not values:
        return []
    out: list[float] = [values[0]]
    alpha = 2.0 / (length + 1.0)
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1.0 - alpha))
    return out


def sma_at(values: list[float], idx: int, length: int) -> float:
    length = max(1, int(length))
    if idx + 1 < length:
        return 0.0
    window = values[idx - length + 1 : idx + 1]
    return sum(window) / len(window) if window else 0.0


def stddev_at(values: list[float], idx: int, length: int) -> float:
    length = max(2, int(length))
    if idx + 1 < length:
        return 0.0
    window = values[idx - length + 1 : idx + 1]
    return shared.stddev(window)


def macd_hist_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd = [ema_fast[idx] - ema_slow[idx] for idx in range(len(closes))]
    sig = ema_series(macd, signal)
    return [macd[idx] - sig[idx] for idx in range(len(closes))]


def rolling_vwap(values: list[dict[str, Any]], idx: int, length: int) -> float:
    if idx + 1 < length:
        return 0.0
    rows = values[idx - length + 1 : idx + 1]
    total_quote = 0.0
    total_volume = 0.0
    for row in rows:
        volume = safe_float(row.get("volume"))
        quote = safe_float(row.get("quote_volume"))
        if quote <= 0:
            typical = (high_price(row) + low_price(row) + close_price(row)) / 3.0
            quote = typical * volume
        total_quote += quote
        total_volume += volume
    return total_quote / total_volume if total_volume > 0 else 0.0


def rolling_max_series(values: list[float], length: int) -> list[float]:
    out = [0.0 for _ in values]
    queue: deque[int] = deque()
    for idx, value in enumerate(values):
        while queue and queue[0] <= idx - length:
            queue.popleft()
        while queue and values[queue[-1]] <= value:
            queue.pop()
        queue.append(idx)
        if idx + 1 >= length:
            out[idx] = values[queue[0]]
    return out


def rolling_min_series(values: list[float], length: int) -> list[float]:
    out = [0.0 for _ in values]
    queue: deque[int] = deque()
    for idx, value in enumerate(values):
        while queue and queue[0] <= idx - length:
            queue.popleft()
        while queue and values[queue[-1]] >= value:
            queue.pop()
        queue.append(idx)
        if idx + 1 >= length:
            out[idx] = values[queue[0]]
    return out


def rolling_mid_series(highs: list[float], lows: list[float], length: int) -> list[float]:
    hi = rolling_max_series(highs, length)
    lo = rolling_min_series(lows, length)
    return [(hi[idx] + lo[idx]) / 2.0 if hi[idx] > 0 and lo[idx] > 0 else 0.0 for idx in range(len(highs))]


def rolling_vwap_series(bars: list[dict[str, Any]], length: int) -> list[float]:
    out = [0.0 for _ in bars]
    quote_prefix = [0.0]
    volume_prefix = [0.0]
    for row in bars:
        volume = safe_float(row.get("volume"))
        quote = safe_float(row.get("quote_volume"))
        if quote <= 0:
            typical = (high_price(row) + low_price(row) + close_price(row)) / 3.0
            quote = typical * volume
        quote_prefix.append(quote_prefix[-1] + quote)
        volume_prefix.append(volume_prefix[-1] + volume)
    for idx in range(len(bars)):
        if idx + 1 < length:
            continue
        start = idx + 1 - length
        quote = quote_prefix[idx + 1] - quote_prefix[start]
        volume = volume_prefix[idx + 1] - volume_prefix[start]
        out[idx] = quote / volume if volume > 0 else 0.0
    return out


def donchian(bars: list[dict[str, Any]], idx: int, length: int) -> tuple[float, float]:
    if idx <= length:
        return 0.0, 0.0
    window = bars[idx - length : idx]
    return max(high_price(row) for row in window), min(low_price(row) for row in window)


def ichimoku_levels(bars: list[dict[str, Any]], idx: int) -> dict[str, float]:
    def mid(length: int) -> float:
        if idx + 1 < length:
            return 0.0
        window = bars[idx - length + 1 : idx + 1]
        return (max(high_price(row) for row in window) + min(low_price(row) for row in window)) / 2.0

    tenkan = mid(9)
    kijun = mid(26)
    span_b = mid(52)
    span_a = (tenkan + kijun) / 2.0 if tenkan and kijun else 0.0
    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b) if span_a and span_b else 0.0
    return {"tenkan": tenkan, "kijun": kijun, "cloud_top": cloud_top, "cloud_bottom": cloud_bottom}


def squeeze_state(
    bars: list[dict[str, Any]],
    closes: list[float],
    atrs: list[float],
    idx: int,
    *,
    length: int,
    bb_dev: float,
    kc_mult: float,
) -> dict[str, Any]:
    def one(pos: int) -> tuple[bool, float, float]:
        mid = sma_at(closes, pos, length)
        sd = stddev_at(closes, pos, length)
        atr = atrs[pos] if pos < len(atrs) else 0.0
        if mid <= 0 or atr <= 0:
            return False, 0.0, 0.0
        bb_upper = mid + sd * bb_dev
        bb_lower = mid - sd * bb_dev
        kc_upper = mid + atr * kc_mult
        kc_lower = mid - atr * kc_mult
        width = (bb_upper - bb_lower) / mid * 100.0
        kc_width = (kc_upper - kc_lower) / mid * 100.0
        return bb_upper < kc_upper and bb_lower > kc_lower, width, kc_width

    on, width, kc_width = one(idx)
    prev_on = one(idx - 1)[0] if idx > 0 else False
    return {"on": on, "release": prev_on and not on, "bb_width_pct": width, "kc_width_pct": kc_width}


def trade_from_signal(
    *,
    strategy: str,
    adapter: str,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    signal_idx: int,
    side: str,
    params: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    return base_ind.simulate_indicator_trade(
        strategy=strategy,
        adapter=adapter,
        symbol=symbol,
        interval=interval,
        bars=bars,
        signal_idx=signal_idx,
        side=side,
        params=params,
        extra=extra,
    )


def base_params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "atr_stop_multiplier": 1.8,
        "take_profit_atr": 3.2,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 0.8,
        "max_hold_bars": 24,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
    }
    payload.update(extra or {})
    return payload


def m_variants() -> list[dict[str, Any]]:
    rows = []
    for rsi_mid in (50.0, 53.0):
        for adx_min in (16.0, 22.0):
            rows.append(
                {
                    "name": f"rsi_squeeze_mid={rsi_mid:g},adx={adx_min:g}",
                    "params": base_params(
                        {
                            "rsi_length": 14,
                            "rsi_smooth": 5,
                            "rsi_mid": rsi_mid,
                            "squeeze_length": 20,
                            "bb_dev": 2.0,
                            "kc_mult": 1.5,
                            "adx_min": adx_min,
                            "volume_ratio_min": 1.05,
                            "atr_stop_multiplier": 1.7,
                            "take_profit_atr": 3.4,
                            "max_hold_bars": 20,
                        }
                    ),
                }
            )
    return rows


def run_m_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    length = safe_int(params.get("squeeze_length"), 20)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        rsis = base_ind.rsi_series(closes, safe_int(params.get("rsi_length"), 14))
        smooth = ema_series(rsis, safe_int(params.get("rsi_smooth"), 5))
        atrs = base_ind.atr_series(use_bars, 20)
        dmi = base_ind.adx_series(use_bars, 14)
        idx = max(50, length + 3)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            sq = squeeze_state(use_bars, closes, atrs, idx, length=length, bb_dev=safe_float(params.get("bb_dev"), 2.0), kc_mult=safe_float(params.get("kc_mult"), 1.5))
            if not sq.get("release") or dmi["adx"][idx] < safe_float(params.get("adx_min"), 18.0):
                idx += 1
                continue
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0):
                idx += 1
                continue
            side = ""
            if smooth[idx] > safe_float(params.get("rsi_mid"), 50.0) and smooth[idx] > smooth[idx - 1] and closes[idx] > sma_at(closes, idx, length):
                side = "long"
            elif smooth[idx] < 100.0 - safe_float(params.get("rsi_mid"), 50.0) and smooth[idx] < smooth[idx - 1] and closes[idx] < sma_at(closes, idx, length):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="M/qqe_squeeze",
                adapter="historical_research_m",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"rsi_smooth": round(smooth[idx], 4), "adx": round(dmi["adx"][idx], 4), "volume_ratio": round(vol_ratio, 6), **sq},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def n_variants() -> list[dict[str, Any]]:
    rows = []
    for mult in (2.5, 3.5):
        for ema_len in (100, 200):
            rows.append(
                {
                    "name": f"atrtrail={mult:g},ema={ema_len}",
                    "params": base_params(
                        {
                            "supertrend_atr_length": 10,
                            "supertrend_multiplier": mult,
                            "ema_length": ema_len,
                            "rsi_length": 14,
                            "rsi_long_max": 74.0,
                            "rsi_short_min": 26.0,
                            "volume_ratio_min": 1.0,
                            "atr_stop_multiplier": 2.0,
                            "take_profit_atr": 3.8,
                            "trailing_pullback_atr": 1.2,
                            "max_hold_bars": 28,
                        }
                    ),
                }
            )
    return rows


def run_n_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        ema = ema_series(closes, safe_int(params.get("ema_length"), 200))
        rsis = base_ind.rsi_series(closes, safe_int(params.get("rsi_length"), 14))
        dirs = base_ind.supertrend_direction(use_bars, safe_int(params.get("supertrend_atr_length"), 10), safe_float(params.get("supertrend_multiplier"), 3.0))
        idx = max(60, safe_int(params.get("ema_length"), 200) // 2)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            if dirs[idx] == dirs[idx - 1]:
                idx += 1
                continue
            vol_ratio = shared.volume_ratio(use_bars, idx)
            side = ""
            if dirs[idx] > 0 and closes[idx] > ema[idx] and rsis[idx] <= safe_float(params.get("rsi_long_max"), 74.0):
                side = "long"
            elif dirs[idx] < 0 and closes[idx] < ema[idx] and rsis[idx] >= safe_float(params.get("rsi_short_min"), 26.0):
                side = "short"
            if not side or vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0):
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="N/utbot_ema",
                adapter="historical_research_n",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"supertrend_direction": dirs[idx], "ema": round(ema[idx], 8), "rsi": round(rsis[idx], 4), "volume_ratio": round(vol_ratio, 6)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def o_variants() -> list[dict[str, Any]]:
    rows = []
    for fast, slow in ((12, 48), (21, 89)):
        for vol_min in (1.05, 1.30):
            rows.append(
                {
                    "name": f"ema={fast}/{slow},vol={vol_min:g}",
                    "params": base_params(
                        {
                            "ema_fast": fast,
                            "ema_slow": slow,
                            "rsi_length": 14,
                            "rsi_long_min": 48.0,
                            "rsi_long_max": 72.0,
                            "rsi_short_min": 28.0,
                            "rsi_short_max": 52.0,
                            "volume_ratio_min": vol_min,
                            "atr_stop_multiplier": 1.6,
                            "take_profit_atr": 2.8,
                            "trailing_pullback_atr": 0.9,
                            "max_hold_bars": 16,
                        }
                    ),
                }
            )
    return rows


def run_o_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        fast = ema_series(closes, safe_int(params.get("ema_fast"), 12))
        slow = ema_series(closes, safe_int(params.get("ema_slow"), 48))
        hist = macd_hist_series(closes)
        rsis = base_ind.rsi_series(closes, safe_int(params.get("rsi_length"), 14))
        idx = max(60, safe_int(params.get("ema_slow"), 48) + 2)
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if vol_ratio < safe_float(params.get("volume_ratio_min"), 1.05):
                idx += 1
                continue
            side = ""
            if (
                fast[idx] > slow[idx]
                and fast[idx - 1] <= slow[idx - 1] or (fast[idx] > slow[idx] and hist[idx] > 0 and hist[idx] > hist[idx - 1])
            ) and safe_float(params.get("rsi_long_min"), 48.0) <= rsis[idx] <= safe_float(params.get("rsi_long_max"), 72.0):
                side = "long"
            elif (
                fast[idx] < slow[idx]
                and fast[idx - 1] >= slow[idx - 1] or (fast[idx] < slow[idx] and hist[idx] < 0 and hist[idx] < hist[idx - 1])
            ) and safe_float(params.get("rsi_short_min"), 28.0) <= rsis[idx] <= safe_float(params.get("rsi_short_max"), 52.0):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="O/macd_ema_volume",
                adapter="historical_research_o",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"ema_fast": round(fast[idx], 8), "ema_slow": round(slow[idx], 8), "macd_hist": round(hist[idx], 8), "rsi": round(rsis[idx], 4), "volume_ratio": round(vol_ratio, 6)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def p_variants() -> list[dict[str, Any]]:
    rows = []
    for lookback in (24, 48):
        for adx_min in (18.0, 24.0):
            rows.append(
                {
                    "name": f"donchian={lookback},adx={adx_min:g}",
                    "params": base_params(
                        {
                            "donchian_lookback": lookback,
                            "adx_min": adx_min,
                            "volume_ratio_min": 1.1,
                            "atr_pct_min": 0.08,
                            "atr_stop_multiplier": 2.0,
                            "take_profit_atr": 4.2,
                            "trailing_pullback_atr": 1.3,
                            "max_hold_bars": 32,
                        }
                    ),
                }
            )
    return rows


def run_p_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    lookback = safe_int(params.get("donchian_lookback"), 24)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        dmi = base_ind.adx_series(use_bars, 14)
        idx = lookback + 2
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            high, low = donchian(use_bars, idx, lookback)
            close = close_price(use_bars[idx])
            atr = backtest_engine.atr(use_bars, idx)
            atr_pct = atr / close * 100.0 if close > 0 else 0.0
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if dmi["adx"][idx] < safe_float(params.get("adx_min"), 18.0) or vol_ratio < safe_float(params.get("volume_ratio_min"), 1.1):
                idx += 1
                continue
            if atr_pct < safe_float(params.get("atr_pct_min"), 0.08):
                idx += 1
                continue
            side = "long" if close > high and dmi["pdi"][idx] > dmi["mdi"][idx] else "short" if close < low and dmi["mdi"][idx] > dmi["pdi"][idx] else ""
            if not side:
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="P/donchian_adx",
                adapter="historical_research_p",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"donchian_high": round(high, 8), "donchian_low": round(low, 8), "adx": round(dmi["adx"][idx], 4), "atr_pct": round(atr_pct, 6), "volume_ratio": round(vol_ratio, 6)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def q_variants() -> list[dict[str, Any]]:
    rows = []
    for rsi_low, rsi_high in ((28.0, 72.0), (35.0, 65.0)):
        for adx_max in (18.0, 25.0):
            rows.append(
                {
                    "name": f"bb_reentry,rsi={rsi_low:g}/{rsi_high:g},adxmax={adx_max:g}",
                    "params": base_params(
                        {
                            "bb_length": 20,
                            "bb_dev": 2.0,
                            "rsi_length": 14,
                            "rsi_low": rsi_low,
                            "rsi_high": rsi_high,
                            "adx_max": adx_max,
                            "volume_ratio_max": 1.8,
                            "atr_stop_multiplier": 1.6,
                            "take_profit_atr": 2.4,
                            "trailing_pullback_atr": 0.8,
                            "max_hold_bars": 16,
                        }
                    ),
                }
            )
    return rows


def run_q_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    length = safe_int(params.get("bb_length"), 20)
    dev = safe_float(params.get("bb_dev"), 2.0)
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        closes = [close_price(row) for row in use_bars]
        rsis = base_ind.rsi_series(closes, safe_int(params.get("rsi_length"), 14))
        adx = base_ind.adx_series(use_bars, 14)["adx"]
        idx = length + 2
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            prev_mid, prev_upper, prev_lower, _ = base_ind.bollinger_at(closes, idx - 1, length, dev)
            mid, upper, lower, width = base_ind.bollinger_at(closes, idx, length, dev)
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if mid <= 0 or prev_mid <= 0 or adx[idx] > safe_float(params.get("adx_max"), 20.0) or vol_ratio > safe_float(params.get("volume_ratio_max"), 1.8):
                idx += 1
                continue
            side = ""
            if closes[idx - 1] < prev_lower and closes[idx] >= lower and rsis[idx] <= safe_float(params.get("rsi_low"), 30.0):
                side = "long"
            elif closes[idx - 1] > prev_upper and closes[idx] <= upper and rsis[idx] >= safe_float(params.get("rsi_high"), 70.0):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="Q/bb_rsi_regime_reversion",
                adapter="historical_research_q",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"rsi": round(rsis[idx], 4), "adx": round(adx[idx], 4), "band_width_pct": round(width, 6), "volume_ratio": round(vol_ratio, 6)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


def r_variants() -> list[dict[str, Any]]:
    rows = []
    for vwap_len in (24, 48):
        for vol_min in (1.0, 1.25):
            rows.append(
                {
                    "name": f"ichi_vwap={vwap_len},vol={vol_min:g}",
                    "params": base_params(
                        {
                            "vwap_length": vwap_len,
                            "volume_ratio_min": vol_min,
                            "adx_min": 14.0,
                            "atr_stop_multiplier": 2.0,
                            "take_profit_atr": 3.6,
                            "trailing_pullback_atr": 1.2,
                            "max_hold_bars": 26,
                        }
                    ),
                }
            )
    return rows


def run_r_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    params = variant["params"]
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        symbol_trades = 0
        use_bars = shared.split_sequence(bars, split)
        if len(use_bars) < shared.MIN_BARS:
            continue
        highs = [high_price(row) for row in use_bars]
        lows = [low_price(row) for row in use_bars]
        tenkan = rolling_mid_series(highs, lows, 9)
        kijun = rolling_mid_series(highs, lows, 26)
        span_b = rolling_mid_series(highs, lows, 52)
        span_a = [(tenkan[idx] + kijun[idx]) / 2.0 if tenkan[idx] and kijun[idx] else 0.0 for idx in range(len(use_bars))]
        cloud_top = [max(span_a[idx], span_b[idx]) for idx in range(len(use_bars))]
        cloud_bottom = [min(span_a[idx], span_b[idx]) if span_a[idx] and span_b[idx] else 0.0 for idx in range(len(use_bars))]
        vwap_series = rolling_vwap_series(use_bars, safe_int(params.get("vwap_length"), 24))
        dmi = base_ind.adx_series(use_bars, 14)
        idx = 60
        while idx < len(use_bars) - 2 and symbol_trades < shared.MAX_TRADES_PER_SYMBOL:
            close = close_price(use_bars[idx])
            vwap = vwap_series[idx]
            vol_ratio = shared.volume_ratio(use_bars, idx)
            if close <= 0 or vwap <= 0 or vol_ratio < safe_float(params.get("volume_ratio_min"), 1.0) or dmi["adx"][idx] < safe_float(params.get("adx_min"), 14.0):
                idx += 1
                continue
            side = ""
            if tenkan[idx - 1] <= kijun[idx - 1] and tenkan[idx] > kijun[idx] and close > max(cloud_top[idx], vwap):
                side = "long"
            elif tenkan[idx - 1] >= kijun[idx - 1] and tenkan[idx] < kijun[idx] and close < min(cloud_bottom[idx], vwap):
                side = "short"
            if not side:
                idx += 1
                continue
            trade = trade_from_signal(
                strategy="R/ichimoku_vwap",
                adapter="historical_research_r",
                symbol=symbol,
                interval=interval,
                bars=use_bars,
                signal_idx=idx,
                side=side,
                params=params,
                extra={"tenkan": round(tenkan[idx], 8), "kijun": round(kijun[idx], 8), "vwap": round(vwap, 8), "adx": round(dmi["adx"][idx], 4), "volume_ratio": round(vol_ratio, 6)},
            )
            if trade:
                trades.append(trade)
                symbol_trades += 1
                idx += max(1, safe_int(trade.get("bars_held"), 1))
            idx += 1
    return trades


STRATEGIES = {
    "m_qqe_squeeze": {
        "strategy": "M/qqe_squeeze",
        "title": "M QQE/RSI + Squeeze",
        "description": "QQE/RSI 动量方向 + Bollinger/Keltner squeeze release + ADX/volume 过滤。",
        "variants": m_variants,
        "runner": run_m_interval,
    },
    "n_utbot_ema": {
        "strategy": "N/utbot_ema",
        "title": "N UTBot/ATR + EMA",
        "description": "UTBot 类 ATR 方向翻转，用 SuperTrend 近似，再加 EMA 长趋势和 RSI 极端过滤。",
        "variants": n_variants,
        "runner": run_n_interval,
    },
    "o_macd_ema_volume": {
        "strategy": "O/macd_ema_volume",
        "title": "O EMA + MACD + Volume",
        "description": "EMA 趋势、MACD 柱动量、RSI 区间、成交量确认的高频剥头皮组合。",
        "variants": o_variants,
        "runner": run_o_interval,
    },
    "p_donchian_adx": {
        "strategy": "P/donchian_adx",
        "title": "P Donchian + ADX",
        "description": "Donchian 通道突破，加 ADX/DMI 趋势强度、成交量和 ATR 波动过滤。",
        "variants": p_variants,
        "runner": run_p_interval,
    },
    "q_bb_rsi_regime_reversion": {
        "strategy": "Q/bb_rsi_regime_reversion",
        "title": "Q BB + RSI 低趋势回归",
        "description": "Bollinger 越界回归 + RSI 极端 + 低 ADX regime，专门避开强趋势追单。",
        "variants": q_variants,
        "runner": run_q_interval,
    },
    "r_ichimoku_vwap": {
        "strategy": "R/ichimoku_vwap",
        "title": "R Ichimoku + VWAP",
        "description": "Tenkan/Kijun 交叉、云层方向、滚动 VWAP 和 ADX/volume 多条件确认。",
        "variants": r_variants,
        "runner": run_r_interval,
    },
}


def compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "strategy",
        "symbol",
        "interval",
        "side",
        "entry_ts",
        "exit_ts",
        "net_pnl_usdt",
        "fee_usdt",
        "bars_held",
        "exit_reason",
        "adapter",
        "rsi",
        "adx",
        "volume_ratio",
        "signal_phase",
    ]
    return {key: row.get(key) for key in keep if key in row}


def compact_variant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "params": row.get("params"),
        "full": row.get("full") or {},
        "train": row.get("train") or {},
        "validation": row.get("validation") or {},
        "test": row.get("test") or {},
        "robust_score": safe_float(row.get("robust_score")),
        "anti_fit_pass": bool(row.get("anti_fit_pass")),
        "anti_fit_reasons": list(row.get("anti_fit_reasons") or []),
        "trades": [compact_trade(item) for item in (row.get("trades") or [])[-MAX_REPORT_TRADES:]],
    }


def run_strategy(
    *,
    strategy_id: str,
    symbols: list[str],
    intervals: list[str],
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]],
    max_variants: int,
) -> dict[str, Any]:
    spec = STRATEGIES[strategy_id]
    interval_results: dict[str, Any] = {}
    portfolio_net = 0.0
    portfolio_trades = 0
    robust_candidate_intervals = 0
    research_candidates: list[dict[str, Any]] = []
    lines: list[str] = []
    for interval in intervals:
        print(f"[{now_iso()}] {strategy_id} {interval} start", file=sys.stderr, flush=True)
        loaded = {symbol: bars for symbol, bars in loaded_by_interval.get(interval, {}).items() if len(bars) >= shared.MIN_BARS}
        variants = spec["variants"]()[: max(1, max_variants)]
        rows = [shared.evaluate_variant(interval, loaded, spec, variant) for variant in variants]
        rows.sort(key=lambda item: safe_float((item.get("full") or {}).get("net_profit_usdt")), reverse=True)
        best_full = rows[0] if rows else {}
        best_robust = sorted(rows, key=lambda item: safe_float(item.get("robust_score")), reverse=True)[0] if rows else {}
        passed = [row for row in rows if row.get("anti_fit_pass")]
        if passed:
            robust_candidate_intervals += 1
            research_candidates.append(
                {
                    "strategy": spec["strategy"],
                    "interval": interval,
                    "name": passed[0].get("name"),
                    "params": passed[0].get("params"),
                    "full": passed[0].get("full"),
                    "test": passed[0].get("test"),
                }
            )
        best_net = safe_float((best_robust.get("full") or {}).get("net_profit_usdt"))
        best_trades = safe_int((best_robust.get("full") or {}).get("trades"))
        portfolio_net += best_net
        portfolio_trades += best_trades
        lines.append(
            f"{interval}: best_robust {best_net:+.2f} USDT; trades {best_trades}; "
            f"{'OOS passed' if passed else 'OOS failed'}"
        )
        interval_results[interval] = {
            "interval": interval,
            "usable_symbol_count": len(loaded),
            "target_symbol_count": len(symbols),
            "variant_count": len(rows),
            "best_full": compact_variant(best_full) if best_full else {},
            "best_robust": compact_variant(best_robust) if best_robust else {},
            "variants": [compact_variant(row) for row in rows],
            "research_decision": {
                "action": "paper_shadow_review_candidate_requires_operator_approval" if passed else "research_only_reject_for_now",
                "auto_apply_allowed": False,
                "automatic_upgrade_allowed": False,
                "paper_shadow_review_candidate": bool(passed),
                "paper_shadow_allowed": False,
                "reason": "oos_gate_passed_but_operator_approval_required" if passed else "oos_or_risk_gate_failed",
            },
        }
        print(f"[{now_iso()}] {strategy_id} {interval} done net={best_net:+.2f} trades={best_trades}", file=sys.stderr, flush=True)
    return {
        "strategy": spec["strategy"],
        "strategy_id": strategy_id,
        "title": spec["title"],
        "description": spec["description"],
        "interval_results": interval_results,
        "portfolio_summary": {
            "candidate_net_profit_usdt": round(portfolio_net, 6),
            "candidate_return_pct_on_interval_capital": round(portfolio_net / (shared.CAPITAL_USDT * max(1, len(intervals))) * 100.0, 6),
            "candidate_trades": portfolio_trades,
            "intervals": len(intervals),
            "robust_candidate_intervals": robust_candidate_intervals,
            "paper_shadow_review_candidate": robust_candidate_intervals > 0,
            "paper_shadow_allowed": False,
            "auto_apply_allowed": False,
        },
        "operator_summary": {
            "overall_action": "paper_shadow_review_candidate_requires_operator_approval" if robust_candidate_intervals else "research_only_reject_for_now",
            "lines": lines,
            "research_candidates": research_candidates,
            "plain_advice": "通过 OOS 也只代表可人工复核；未经明确批准，不进入 paper shadow，更不进入实盘。",
            "auto_apply_allowed": False,
            "automatic_upgrade_allowed": False,
        },
    }


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


def candidate_scan(strategies: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for payload in strategies.values():
        for interval, result in (payload.get("interval_results") or {}).items():
            for row in result.get("variants") or []:
                full = row.get("full") or {}
                test = row.get("test") or {}
                train = row.get("train") or {}
                validation = row.get("validation") or {}
                rows.append(
                    {
                        "strategy": payload.get("strategy"),
                        "interval": interval,
                        "name": row.get("name"),
                        "params": row.get("params"),
                        "full_net": safe_float(full.get("net_profit_usdt")),
                        "test_net": safe_float(test.get("net_profit_usdt")),
                        "train_net": safe_float(train.get("net_profit_usdt")),
                        "validation_net": safe_float(validation.get("net_profit_usdt")),
                        "profit_factor": safe_float(full.get("profit_factor")),
                        "max_drawdown_pct": safe_float(full.get("max_drawdown_pct")),
                        "trades": safe_int(full.get("trades")),
                        "robust_score": safe_float(row.get("robust_score")),
                        "anti_fit_pass": bool(row.get("anti_fit_pass")),
                        "anti_fit_reasons": row.get("anti_fit_reasons") or [],
                    }
                )
    robust = [row for row in rows if row["anti_fit_pass"]]
    near = [
        row
        for row in rows
        if row["full_net"] > 0 and row["test_net"] > 0 and row["trades"] >= shared.MIN_SPLIT_TRADES and row["max_drawdown_pct"] <= shared.MAX_DRAWDOWN_PCT
    ]
    best = sorted(rows, key=lambda item: (item["robust_score"], item["test_net"], item["full_net"]), reverse=True)[:20]
    return {
        "robust_candidates": robust,
        "near_miss_positive_full_and_test": near[:20],
        "best_by_robust_score": best,
        "action": "manual_review_only" if robust else "research_only_wait_for_composite_edge",
    }


def operator_summary(strategies: dict[str, dict[str, Any]], scan: dict[str, Any]) -> dict[str, Any]:
    lines: list[str] = []
    research_candidates: list[dict[str, Any]] = []
    for payload in strategies.values():
        portfolio = payload.get("portfolio_summary") or {}
        action = (payload.get("operator_summary") or {}).get("overall_action")
        lines.append(
            f"{payload.get('strategy')}: net {safe_float(portfolio.get('candidate_net_profit_usdt')):+.2f} USDT; "
            f"trades {safe_int(portfolio.get('candidate_trades'))}; "
            f"robust intervals {safe_int(portfolio.get('robust_candidate_intervals'))}; {action}"
        )
        research_candidates.extend((payload.get("operator_summary") or {}).get("research_candidates") or [])
    return {
        "overall_action": "manual_review_only" if research_candidates else "research_only_wait_for_composite_edge",
        "lines": lines,
        "research_candidates": research_candidates,
        "near_miss_count": len(scan.get("near_miss_positive_full_and_test") or []),
        "plain_advice": "组合指标池用于找异常点；只有 OOS/反拟合、跨币种、跨周期都过关，才允许人工复核。当前默认禁止 paper/live。",
        "auto_apply_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def metric_cell(row: dict[str, Any], key: str) -> str:
    value = (row.get("full") or {}).get(key)
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.2f}" if key != "trades" else str(int(value))
    return escape(str(value))


def render_strategy_cards(strategies: dict[str, dict[str, Any]]) -> str:
    rows: list[str] = []
    for payload in strategies.values():
        summary = payload.get("portfolio_summary") or {}
        rows.append(
            "<div class='card'>"
            f"<span>{escape(str(payload.get('title')))}</span>"
            f"<b>{safe_float(summary.get('candidate_net_profit_usdt')):+.2f} USDT</b>"
            f"<p>交易 {safe_int(summary.get('candidate_trades'))}；通过周期 {safe_int(summary.get('robust_candidate_intervals'))}/{safe_int(summary.get('intervals'))}</p>"
            "</div>"
        )
    return "".join(rows)


def render_candidate_rows(rows: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for row in rows[:20]:
        out.append(
            "<tr>"
            f"<td>{escape(str(row.get('strategy')))}</td>"
            f"<td>{escape(str(row.get('interval')))}</td>"
            f"<td>{escape(str(row.get('name')))}</td>"
            f"<td>{safe_float(row.get('full_net')):.2f}</td>"
            f"<td>{safe_float(row.get('test_net')):.2f}</td>"
            f"<td>{safe_float(row.get('profit_factor')):.2f}</td>"
            f"<td>{safe_float(row.get('max_drawdown_pct')):.2f}</td>"
            f"<td>{safe_int(row.get('trades'))}</td>"
            f"<td>{'通过' if row.get('anti_fit_pass') else '未通过'}</td>"
            "</tr>"
        )
    return "".join(out)


def render_strategy_sections(strategies: dict[str, dict[str, Any]]) -> str:
    sections: list[str] = []
    for payload in strategies.values():
        interval_rows: list[str] = []
        variant_sections: list[str] = []
        trade_rows: list[str] = []
        for interval, result in (payload.get("interval_results") or {}).items():
            best = result.get("best_robust") or {}
            interval_rows.append(
                "<tr>"
                f"<td>{escape(str(interval))}</td>"
                f"<td>{escape(str(best.get('name') or '-'))}</td>"
                f"<td>{metric_cell(best, 'net_profit_usdt')}</td>"
                f"<td>{metric_cell(best, 'profit_factor')}</td>"
                f"<td>{metric_cell(best, 'max_drawdown_pct')}</td>"
                f"<td>{metric_cell(best, 'trades')}</td>"
                f"<td>{'通过' if best.get('anti_fit_pass') else '未通过'}</td>"
                f"<td>{escape(', '.join(best.get('anti_fit_reasons') or [])[:220])}</td>"
                "</tr>"
            )
            variants = []
            for row in (result.get("variants") or [])[:24]:
                variants.append(
                    "<tr>"
                    f"<td>{escape(str(row.get('name') or '-'))}</td>"
                    f"<td>{metric_cell(row, 'net_profit_usdt')}</td>"
                    f"<td>{metric_cell(row, 'profit_factor')}</td>"
                    f"<td>{metric_cell(row, 'max_drawdown_pct')}</td>"
                    f"<td>{metric_cell(row, 'trades')}</td>"
                    f"<td>{safe_float(row.get('robust_score')):.2f}</td>"
                    f"<td>{'通过' if row.get('anti_fit_pass') else '未通过'}</td>"
                    "</tr>"
                )
            variant_sections.append(
                f"<details><summary>{escape(str(interval))} 参数组</summary>"
                "<table><thead><tr><th>参数</th><th>净收益</th><th>PF</th><th>回撤%</th><th>交易数</th><th>稳健分</th><th>OOS</th></tr></thead>"
                f"<tbody>{''.join(variants)}</tbody></table></details>"
            )
            for trade in (best.get("trades") or [])[-40:]:
                trade_rows.append(
                    "<tr>"
                    f"<td>{escape(str(interval))}</td>"
                    f"<td>{escape(str(trade.get('symbol') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('side') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('entry_ts') or '-'))}</td>"
                    f"<td>{escape(str(trade.get('exit_ts') or '-'))}</td>"
                    f"<td>{safe_float(trade.get('net_pnl_usdt')):.4f}</td>"
                    f"<td>{escape(str(trade.get('exit_reason') or trade.get('adapter') or '-'))}</td>"
                    "</tr>"
                )
        sections.append(
            f"<section><h2>{escape(str(payload.get('title')))}</h2>"
            f"<p>{escape(str(payload.get('description')))}</p>"
            "<table><thead><tr><th>周期</th><th>最佳稳健候选</th><th>净收益</th><th>PF</th><th>回撤%</th><th>交易数</th><th>OOS</th><th>失败原因</th></tr></thead>"
            f"<tbody>{''.join(interval_rows)}</tbody></table>"
            f"{''.join(variant_sections)}"
            "<div class='scroll'><table><thead><tr><th>周期</th><th>标的</th><th>方向</th><th>入场</th><th>出场</th><th>净收益</th><th>原因</th></tr></thead>"
            f"<tbody>{''.join(trade_rows)}</tbody></table></div></section>"
        )
    return "".join(sections)


def render_sources(sources: list[dict[str, str]]) -> str:
    rows = []
    for source in sources:
        rows.append(
            f"<li><a href='{escape(source.get('url', ''))}'>{escape(source.get('label', 'source'))}</a> - {escape(source.get('note', ''))}</li>"
        )
    return "".join(rows)


def render_html(payload: dict[str, Any]) -> str:
    operator = payload.get("operator_summary") or {}
    scan = payload.get("candidate_scan") or {}
    lines = "".join(f"<li>{escape(str(line))}</li>" for line in operator.get("lines") or [])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>组合指标研究报告</title>
<style>
body{{margin:0;background:#0b1118;color:#e7eef7;font-family:Arial,'Microsoft YaHei',sans-serif}}
.wrap{{max-width:1400px;margin:0 auto;padding:28px}}
.hero{{border:1px solid #1d344a;background:#101a25;padding:22px;border-radius:8px}}
h1{{margin:0 0 8px;font-size:28px}} h2{{margin-top:30px}}
.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}}
.card{{background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:14px}}
.card span{{color:#8ea2b7;font-size:12px}} .card b{{display:block;margin-top:6px;font-size:22px}}
table{{width:100%;border-collapse:collapse;background:#0f1722;border:1px solid #22364a;margin:10px 0}}
th,td{{padding:9px;border-bottom:1px solid #203246;text-align:left;font-size:13px;vertical-align:top}}
th{{color:#9db4ca;background:#132031}} details{{margin:10px 0;background:#0f1722;border:1px solid #22364a;border-radius:8px;padding:10px}}
.scroll{{max-height:360px;overflow:auto;border:1px solid #22364a;margin-top:10px}}
code{{color:#9bdcff}} a{{color:#91cdf7}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
<h1>组合指标研究报告</h1>
<p>GitHub/TradingView 社区热门指标思路拆成 M-R 六条组合，使用本地一年期 OHLCV 只读回测。</p>
<p>生成时间：{escape(str(payload.get('generated_at')))}；动作：<b>{escape(str(operator.get('overall_action')))}</b>；本地仓：<code>{escape(str(payload.get('local_store_path')))}</code></p>
</section>
<section class="grid">{render_strategy_cards(payload.get('strategies') or {})}</section>
<h2>操作结论</h2>
<div class="card"><ul>{lines}</ul><p>{escape(str(operator.get('plain_advice') or ''))}</p></div>
<h2>奇点候选</h2>
<div class="card"><p>严格候选：{len(scan.get('robust_candidates') or [])}；近似异常点：{len(scan.get('near_miss_positive_full_and_test') or [])}。</p>
<table><thead><tr><th>策略</th><th>周期</th><th>参数</th><th>全样本PnL</th><th>测试PnL</th><th>PF</th><th>回撤%</th><th>交易</th><th>OOS</th></tr></thead>
<tbody>{render_candidate_rows(scan.get('best_by_robust_score') or [])}</tbody></table></div>
<h2>M-R 分项结果</h2>
{render_strategy_sections(payload.get('strategies') or {})}
<h2>资料与边界</h2>
<div class="card"><ul>{render_sources(payload.get('research_sources') or [])}</ul><p>只借指标思想和公开公式，不复制 Pine/community 代码。所有结果只做研究筛选；不自动进 paper/live。</p></div>
</main></body></html>"""


def run_all(
    root: Path,
    symbols: list[str],
    intervals: list[str],
    start: datetime,
    end: datetime,
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
) -> dict[str, Any]:
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for interval in intervals:
        print(f"[{now_iso()}] load {interval}", file=sys.stderr, flush=True)
        loaded_by_interval[interval] = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    coverage = shared.coverage_rows(loaded_by_interval)
    strategies: dict[str, dict[str, Any]] = {}
    for strategy_id in STRATEGIES:
        strategies[strategy_id] = run_strategy(
            strategy_id=strategy_id,
            symbols=symbols,
            intervals=intervals,
            loaded_by_interval=loaded_by_interval,
            max_variants=max_variants,
        )
    scan = candidate_scan(strategies)
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "module": "composite_indicator_research",
        "title": "组合指标研究报告",
        "engine_parity": "historical_research_adapter",
        "local_store_path": str(root / "research_store" / "historical_klines"),
        "period": {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"), "days": (end - start).days},
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
            "max_variants_per_strategy_interval": max_variants,
            "capital_usdt_per_interval": shared.CAPITAL_USDT,
            "fee_bps": shared.FEE_BPS,
            "oos_rules": {
                "min_split_trades": shared.MIN_SPLIT_TRADES,
                "min_profit_factor": shared.MIN_PROFIT_FACTOR,
                "max_drawdown_pct": shared.MAX_DRAWDOWN_PCT,
                "all_splits_must_be_profitable": True,
            },
        },
        "research_sources": [
            {
                "label": "Freqtrade strategies repository",
                "url": "https://github.com/freqtrade/freqtrade-strategies",
                "note": "open crypto strategy examples frequently using RSI, Bollinger, EMA, MACD, ADX, volume filters.",
            },
            {
                "label": "technicalindicators GitHub library",
                "url": "https://github.com/anandanand84/technicalindicators",
                "note": "popular JS indicator library with RSI, MACD, Bollinger Bands, ADX, Ichimoku and related formulas.",
            },
            {
                "label": "TradingView UT Bot Alerts",
                "url": "https://www.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/",
                "note": "community ATR trailing-stop style alert idea; implemented here only as independent ATR/SuperTrend approximation.",
            },
            {
                "label": "TradingView Squeeze Momentum",
                "url": "https://www.tradingview.com/script/nqQ1DT5a-Squeeze-Momentum-Indicator-LazyBear/",
                "note": "BB/Keltner volatility squeeze-release concept; implemented here with local formulas.",
            },
            {
                "label": "TradingView SuperTrend support",
                "url": "https://www.tradingview.com/support/solutions/43000634738-supertrend/",
                "note": "ATR-based trend-following reference used for N family approximation.",
            },
        ],
        "historical_quality": shared.historical_payload(root).get("quality", {}),
        "strategies": strategies,
        "candidate_scan": scan,
        "operator_summary": operator_summary(strategies, scan),
        "safety": safety_payload(),
        "report_path": str(latest_html_path(root)),
    }
    write_json(latest_json_path(root), payload)
    latest_html_path(root).parent.mkdir(parents=True, exist_ok=True)
    latest_html_path(root).write_text(render_html(payload), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local composite indicator historical research")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    intervals = shared.parse_csv(args.intervals, DEFAULT_INTERVALS)
    symbols = shared.universe_symbols(root, shared.parse_csv(args.symbols, []) if args.symbols else None)
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, args.days))
    payload = run_all(root, symbols, intervals, start, end, max_variants=max(1, args.max_variants))
    print(json.dumps(payload["operator_summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
