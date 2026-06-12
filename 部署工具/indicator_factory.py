"""Local indicator factory.

Builds a local research system around public indicator ideas:

- registry of popular indicators and parameter ranges;
- automatic 2/3/4-indicator combination generation;
- local historical-Kline backtests with train/validation/test gates;
- SQLite result storage and HTML review report;
- local two-year data extension planning.

This is local research only. It reads ``research_store/historical_klines`` and
never mutates live strategy config, restarts scanners, submits orders, or
enables automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sqlite3
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
import composite_indicator_research_report as ci
import d_e_f_historical_research_report as shared
import j_k_l_indicator_research_report as base_ind


CST = timezone(timedelta(hours=8))
LAB_DIR = ROOT / "research_lab" / "indicator_factory"
DB_PATH = LAB_DIR / "results.sqlite"
RUNS_DIR = LAB_DIR / "runs"
TRADES_DIR = LAB_DIR / "trades"
CANDIDATES_DIR = LAB_DIR / "candidates"
REPORT_PATH = LAB_DIR / "indicator_factory_latest.html"
RUNTIME_JSON = ROOT / "runtime" / "indicator_factory_latest.json"
DEFAULT_INTERVALS = ["15m", "30m"]
DEFAULT_DAYS = 365
DEFAULT_MAX_COMBOS = 120
MAX_TRADES_PER_SYMBOL = 160
MIN_SPLIT_TRADES = 20
MIN_PROFIT_FACTOR = 1.10
MAX_DRAWDOWN_PCT = 20.0
CAPITAL_USDT = shared.CAPITAL_USDT


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def safe_int(value: Any, default: int = 0) -> int:
    return backtest_engine.safe_int(value, default)


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def stable_id(parts: list[str]) -> str:
    text = "|".join(parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def ensure_dirs(root: Path = ROOT) -> dict[str, Path]:
    lab = root / "research_lab" / "indicator_factory"
    paths = {
        "lab": lab,
        "runs": lab / "runs",
        "trades": lab / "trades",
        "candidates": lab / "candidates",
        "reports": lab,
        "runtime": root / "runtime",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def indicator_registry() -> list[dict[str, Any]]:
    """Popular public indicators, with v1 implementation flags.

    Implemented entries can participate in automatic combo testing now.
    Planned entries are stored and shown in the report for future expansion.
    """

    implemented = {
        "rsi_momentum",
        "rsi_reversion",
        "qqe_proxy",
        "macd_momentum",
        "ema_trend",
        "ema_cross",
        "sma_trend",
        "bollinger_reentry",
        "bollinger_breakout",
        "squeeze_release",
        "supertrend_flip",
        "donchian_breakout",
        "adx_dmi",
        "volume_spike",
        "vwap_trend",
        "ichimoku_cross",
        "cci_reversion",
        "stoch_reversion",
        "atr_expansion",
        "bb_width_low",
    }
    rows = [
        ("rsi_momentum", "RSI Momentum", "momentum", "direction", "RSI(14) > 55/<45 and slope", {"length": 14, "long": 55, "short": 45}),
        ("rsi_reversion", "RSI Reversion", "mean_reversion", "direction", "RSI(14) 30/70 reversal", {"length": 14, "low": 30, "high": 70}),
        ("qqe_proxy", "QQE Proxy", "momentum", "direction", "smoothed RSI direction proxy for QQE", {"rsi_length": 14, "smooth": 5, "mid": 53}),
        ("macd_momentum", "MACD Histogram", "momentum", "direction", "MACD 12/26/9 histogram direction", {"fast": 12, "slow": 26, "signal": 9}),
        ("ema_trend", "EMA Trend", "trend", "direction", "EMA50/EMA200 trend filter", {"fast": 50, "slow": 200}),
        ("ema_cross", "EMA Cross", "trend", "direction", "EMA21/EMA89 cross or aligned trend", {"fast": 21, "slow": 89}),
        ("sma_trend", "SMA Trend", "trend", "direction", "SMA50/SMA200 trend filter", {"fast": 50, "slow": 200}),
        ("bollinger_reentry", "Bollinger Re-entry", "mean_reversion", "direction", "price re-enters BB after outside close", {"length": 20, "dev": 2.0}),
        ("bollinger_breakout", "Bollinger Breakout", "breakout", "direction", "close outside Bollinger band", {"length": 20, "dev": 2.0}),
        ("squeeze_release", "BB/KC Squeeze Release", "volatility", "direction", "Bollinger inside Keltner then release", {"length": 20, "bb_dev": 2.0, "kc_mult": 1.5}),
        ("supertrend_flip", "SuperTrend Flip", "trend", "direction", "ATR trend line flip", {"atr_length": 10, "mult": 3.0}),
        ("donchian_breakout", "Donchian Breakout", "breakout", "direction", "close breaks previous channel", {"lookback": 48}),
        ("adx_dmi", "ADX + DMI", "trend_strength", "direction", "ADX >= 22 and DMI direction", {"length": 14, "min": 22}),
        ("volume_spike", "Volume Spike", "volume", "filter", "quote-volume ratio >= 1.3", {"length": 20, "min": 1.3}),
        ("vwap_trend", "Rolling VWAP Trend", "trend", "direction", "close above/below rolling VWAP", {"length": 48}),
        ("ichimoku_cross", "Ichimoku Cross", "trend", "direction", "Tenkan/Kijun cross with cloud", {"tenkan": 9, "kijun": 26, "span_b": 52}),
        ("cci_reversion", "CCI Reversion", "mean_reversion", "direction", "CCI +/-100 reversion", {"length": 20, "low": -100, "high": 100}),
        ("stoch_reversion", "Stochastic Reversion", "mean_reversion", "direction", "%K 20/80 reversal", {"length": 14, "low": 20, "high": 80}),
        ("atr_expansion", "ATR Expansion", "volatility", "filter", "ATR percent inside tradable range", {"length": 14, "min_pct": 0.10, "max_pct": 6.0}),
        ("bb_width_low", "Bollinger Width Low", "regime", "filter", "low-volatility regime filter", {"length": 20, "dev": 2.0, "max_width_pct": 6.0}),
        ("keltner_channel", "Keltner Channel", "volatility", "planned", "EMA plus ATR channel", {"length": 20, "mult": 1.5}),
        ("parabolic_sar", "Parabolic SAR", "trend", "planned", "classic SAR trend stop", {"step": 0.02, "max": 0.2}),
        ("hma_trend", "Hull MA Trend", "trend", "planned", "Hull moving average slope", {"length": 55}),
        ("aroon", "Aroon", "trend_strength", "planned", "Aroon up/down trend age", {"length": 25}),
        ("obv", "OBV", "volume", "planned", "On-Balance Volume direction", {"length": 20}),
        ("mfi", "Money Flow Index", "volume", "planned", "volume-weighted RSI style oscillator", {"length": 14}),
        ("cmf", "Chaikin Money Flow", "volume", "planned", "accumulation/distribution pressure", {"length": 20}),
        ("wavetrend", "WaveTrend", "momentum", "planned", "popular TradingView oscillator", {"n1": 10, "n2": 21}),
        ("tsi", "True Strength Index", "momentum", "planned", "double-smoothed momentum", {"long": 25, "short": 13}),
        ("roc", "Rate Of Change", "momentum", "planned", "percent momentum over lookback", {"length": 12}),
        ("williams_r", "Williams %R", "mean_reversion", "planned", "range oscillator", {"length": 14}),
        ("kdj", "KDJ", "momentum", "planned", "stochastic-derived K/D/J", {"length": 9}),
        ("chop", "Choppiness Index", "regime", "planned", "trend vs chop regime", {"length": 14}),
        ("heikin_ashi", "Heikin Ashi Trend", "trend", "planned", "smoothed candle direction", {"length": 1}),
        ("pivot_breakout", "Pivot Breakout", "breakout", "planned", "daily/session pivot break", {"lookback": 20}),
        ("fractal_breakout", "Fractal Breakout", "breakout", "planned", "Bill Williams fractal break", {"left": 2, "right": 2}),
        ("elder_ray", "Elder Ray", "momentum", "planned", "bull/bear power", {"ema": 13}),
        ("dpo", "Detrended Price Oscillator", "mean_reversion", "planned", "cycle oscillator", {"length": 20}),
        ("trix", "TRIX", "momentum", "planned", "triple EMA rate of change", {"length": 15}),
        ("rvi", "Relative Vigor Index", "momentum", "planned", "close/open vigor oscillator", {"length": 10}),
        ("zscore_reversion", "Z-score Reversion", "mean_reversion", "planned", "rolling z-score mean reversion", {"length": 40, "z": 2.0}),
        ("linreg_slope", "Linear Regression Slope", "trend", "planned", "rolling regression slope", {"length": 50}),
        ("volume_roc", "Volume ROC", "volume", "planned", "volume acceleration", {"length": 20}),
        ("range_expansion", "Range Expansion", "volatility", "planned", "true-range expansion", {"length": 20}),
        ("market_structure", "Market Structure", "regime", "planned", "higher-high/lower-low regime", {"lookback": 20}),
    ]
    sources = {
        "tradingview": "https://www.tradingview.com/scripts/",
        "freqtrade": "https://github.com/freqtrade/freqtrade-strategies",
        "technical": "https://github.com/freqtrade/technical",
        "technicalindicators": "https://github.com/anandanand84/technicalindicators",
    }
    out = []
    for idx, (key, name, category, role, desc, params) in enumerate(rows, start=1):
        out.append(
            {
                "id": key,
                "rank": idx,
                "name": name,
                "category": category,
                "role": role,
                "implemented": key in implemented,
                "description": desc,
                "default_params": params,
                "range_hint": {k: [v] for k, v in params.items()},
                "source_url": sources["tradingview"] if role != "planned" else sources["technicalindicators"],
            }
        )
    return out


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("pragma journal_mode=wal")
    conn.execute(
        """
        create table if not exists indicators (
            id text primary key,
            name text not null,
            category text not null,
            role text not null,
            implemented integer not null,
            description text not null,
            source_url text not null,
            default_params_json text not null,
            range_hint_json text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists combos (
            combo_id text primary key,
            size integer not null,
            indicator_ids_json text not null,
            categories_json text not null,
            status text not null,
            created_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists runs (
            run_id text primary key,
            created_at text not null,
            stage text not null,
            days integer not null,
            intervals_json text not null,
            symbols_json text not null,
            combo_count integer not null,
            status text not null,
            summary_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists combo_results (
            run_id text not null,
            combo_id text not null,
            interval text not null,
            net_profit_usdt real not null,
            test_net_profit_usdt real not null,
            profit_factor real not null,
            max_drawdown_pct real not null,
            win_rate_pct real not null,
            trades integer not null,
            robust_score real not null,
            decision text not null,
            anti_fit_reasons_json text not null,
            params_json text not null,
            primary key (run_id, combo_id, interval)
        )
        """
    )
    conn.execute(
        """
        create table if not exists combo_result_details (
            run_id text not null,
            combo_id text not null,
            combo_name text not null,
            interval text not null,
            indicator_ids_json text not null,
            full_json text not null,
            train_json text not null,
            validation_json text not null,
            test_json text not null,
            robust_score real not null,
            decision text not null,
            anti_fit_reasons_json text not null,
            params_json text not null,
            updated_at text not null,
            primary key (run_id, combo_id, interval)
        )
        """
    )
    conn.execute(
        """
        create table if not exists run_progress (
            run_id text primary key,
            stage text not null,
            status text not null,
            completed_combos integer not null,
            total_combos integer not null,
            completed_combo_intervals integer not null,
            total_combo_intervals integer not null,
            candidate_count integer not null,
            latest_combo_id text not null,
            latest_interval text not null,
            updated_at text not null,
            progress_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists candidates (
            run_id text not null,
            combo_id text not null,
            interval text not null,
            status text not null,
            score real not null,
            summary_json text not null,
            created_at text not null,
            primary key (run_id, combo_id, interval)
        )
        """
    )
    conn.commit()
    return conn


def upsert_indicators(conn: sqlite3.Connection, registry: list[dict[str, Any]]) -> None:
    now = now_iso()
    for item in registry:
        conn.execute(
            """
            insert into indicators(id,name,category,role,implemented,description,source_url,default_params_json,range_hint_json,updated_at)
            values(?,?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set
                name=excluded.name,
                category=excluded.category,
                role=excluded.role,
                implemented=excluded.implemented,
                description=excluded.description,
                source_url=excluded.source_url,
                default_params_json=excluded.default_params_json,
                range_hint_json=excluded.range_hint_json,
                updated_at=excluded.updated_at
            """,
            (
                item["id"],
                item["name"],
                item["category"],
                item["role"],
                1 if item.get("implemented") else 0,
                item["description"],
                item["source_url"],
                jdump(item.get("default_params") or {}),
                jdump(item.get("range_hint") or {}),
                now,
            ),
        )
    conn.commit()


def combo_id(indicators: list[dict[str, Any]]) -> str:
    ids = [item["id"] for item in indicators]
    return f"cf-{len(ids)}-{stable_id(ids)}"


def combo_valid(items: tuple[dict[str, Any], ...]) -> bool:
    roles = [str(item.get("role")) for item in items]
    categories = [str(item.get("category")) for item in items]
    if "direction" not in roles:
        return False
    if len(set(categories)) < min(2, len(items)):
        return False
    if sum(1 for role in roles if role == "filter") > 2:
        return False
    if max(categories.count(category) for category in set(categories)) > 2:
        return False
    return True


def generate_combos(registry: list[dict[str, Any]], min_size: int = 2, max_size: int = 4) -> list[dict[str, Any]]:
    implemented = [item for item in registry if item.get("implemented")]
    rows: list[dict[str, Any]] = []
    for size in range(min_size, max_size + 1):
        for items in itertools.combinations(implemented, size):
            if not combo_valid(items):
                continue
            ids = [item["id"] for item in items]
            categories = [item["category"] for item in items]
            rows.append(
                {
                    "combo_id": combo_id(list(items)),
                    "size": size,
                    "indicator_ids": ids,
                    "categories": categories,
                    "name": " + ".join(item["name"] for item in items),
                }
            )
    rows.sort(key=lambda item: (item["size"], item["combo_id"]))
    return rows


def upsert_combos(conn: sqlite3.Connection, combos: list[dict[str, Any]]) -> None:
    now = now_iso()
    for combo in combos:
        conn.execute(
            """
            insert into combos(combo_id,size,indicator_ids_json,categories_json,status,created_at)
            values(?,?,?,?,?,?)
            on conflict(combo_id) do update set
                size=excluded.size,
                indicator_ids_json=excluded.indicator_ids_json,
                categories_json=excluded.categories_json,
                status=excluded.status
            """,
            (
                combo["combo_id"],
                int(combo["size"]),
                jdump(combo["indicator_ids"]),
                jdump(combo["categories"]),
                "generated",
                now,
            ),
        )
    conn.commit()


def close_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("close"))


def high_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("high"), close_price(row))


def low_price(row: dict[str, Any]) -> float:
    return safe_float(row.get("low"), close_price(row))


def rolling_sma_series(values: list[float], length: int) -> list[float]:
    out = [0.0 for _ in values]
    total = 0.0
    q: deque[float] = deque()
    for idx, value in enumerate(values):
        total += value
        q.append(value)
        if len(q) > length:
            total -= q.popleft()
        if len(q) == length:
            out[idx] = total / length
    return out


def cci_series(bars: list[dict[str, Any]], length: int = 20) -> list[float]:
    typical = [(high_price(row) + low_price(row) + close_price(row)) / 3.0 for row in bars]
    sma = rolling_sma_series(typical, length)
    out = [0.0 for _ in bars]
    for idx in range(len(bars)):
        if idx + 1 < length or sma[idx] <= 0:
            continue
        window = typical[idx - length + 1 : idx + 1]
        mean_dev = sum(abs(value - sma[idx]) for value in window) / length
        out[idx] = (typical[idx] - sma[idx]) / (0.015 * mean_dev) if mean_dev > 0 else 0.0
    return out


def stoch_k_series(bars: list[dict[str, Any]], length: int = 14) -> list[float]:
    highs = [high_price(row) for row in bars]
    lows = [low_price(row) for row in bars]
    hi = ci.rolling_max_series(highs, length)
    lo = ci.rolling_min_series(lows, length)
    out = [50.0 for _ in bars]
    for idx, row in enumerate(bars):
        denom = hi[idx] - lo[idx]
        out[idx] = (close_price(row) - lo[idx]) / denom * 100.0 if denom > 0 else 50.0
    return out


def bollinger_series(closes: list[float], length: int = 20, dev: float = 2.0) -> dict[str, list[float]]:
    mid = rolling_sma_series(closes, length)
    upper = [0.0 for _ in closes]
    lower = [0.0 for _ in closes]
    width = [0.0 for _ in closes]
    for idx in range(len(closes)):
        if idx + 1 < length or mid[idx] <= 0:
            continue
        sd = shared.stddev(closes[idx - length + 1 : idx + 1])
        upper[idx] = mid[idx] + sd * dev
        lower[idx] = mid[idx] - sd * dev
        width[idx] = (upper[idx] - lower[idx]) / mid[idx] * 100.0
    return {"mid": mid, "upper": upper, "lower": lower, "width": width}


def build_features(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [close_price(row) for row in bars]
    highs = [high_price(row) for row in bars]
    lows = [low_price(row) for row in bars]
    rsis = base_ind.rsi_series(closes, 14)
    rsi_smooth = ci.ema_series(rsis, 5)
    ema12 = ci.ema_series(closes, 12)
    ema21 = ci.ema_series(closes, 21)
    ema50 = ci.ema_series(closes, 50)
    ema89 = ci.ema_series(closes, 89)
    ema200 = ci.ema_series(closes, 200)
    sma50 = rolling_sma_series(closes, 50)
    sma200 = rolling_sma_series(closes, 200)
    macd_hist = ci.macd_hist_series(closes)
    atrs = base_ind.atr_series(bars, 14)
    dmi = base_ind.adx_series(bars, 14)
    st_dir = base_ind.supertrend_direction(bars, 10, 3.0)
    bb = bollinger_series(closes)
    bb_wide = bollinger_series(closes, 20, 2.0)
    vwap48 = ci.rolling_vwap_series(bars, 48)
    tenkan = ci.rolling_mid_series(highs, lows, 9)
    kijun = ci.rolling_mid_series(highs, lows, 26)
    span_b = ci.rolling_mid_series(highs, lows, 52)
    span_a = [(tenkan[idx] + kijun[idx]) / 2.0 if tenkan[idx] and kijun[idx] else 0.0 for idx in range(len(bars))]
    cloud_top = [max(span_a[idx], span_b[idx]) for idx in range(len(bars))]
    cloud_bottom = [min(span_a[idx], span_b[idx]) if span_a[idx] and span_b[idx] else 0.0 for idx in range(len(bars))]
    donchian_high = ci.rolling_max_series(highs, 48)
    donchian_low = ci.rolling_min_series(lows, 48)
    cci = cci_series(bars)
    stoch = stoch_k_series(bars)
    squeeze_release = [0 for _ in bars]
    for idx in range(21, len(bars)):
        sq = ci.squeeze_state(bars, closes, atrs, idx, length=20, bb_dev=2.0, kc_mult=1.5)
        if sq.get("release"):
            squeeze_release[idx] = 1 if closes[idx] > bb["mid"][idx] else -1 if closes[idx] < bb["mid"][idx] else 0
    return {
        "closes": closes,
        "rsis": rsis,
        "rsi_smooth": rsi_smooth,
        "ema12": ema12,
        "ema21": ema21,
        "ema50": ema50,
        "ema89": ema89,
        "ema200": ema200,
        "sma50": sma50,
        "sma200": sma200,
        "macd_hist": macd_hist,
        "atrs": atrs,
        "adx": dmi["adx"],
        "pdi": dmi["pdi"],
        "mdi": dmi["mdi"],
        "supertrend": st_dir,
        "bb": bb,
        "bb_wide": bb_wide,
        "vwap48": vwap48,
        "tenkan": tenkan,
        "kijun": kijun,
        "cloud_top": cloud_top,
        "cloud_bottom": cloud_bottom,
        "donchian_high": donchian_high,
        "donchian_low": donchian_low,
        "cci": cci,
        "stoch": stoch,
        "squeeze_release": squeeze_release,
    }


def vote(indicator_id: str, bars: list[dict[str, Any]], f: dict[str, Any], idx: int) -> tuple[int | None, str]:
    close = f["closes"][idx]
    if close <= 0:
        return 0, "bad_price"
    if indicator_id == "volume_spike":
        return (None, "pass") if shared.volume_ratio(bars, idx) >= 1.3 else (0, "volume_low")
    if indicator_id == "atr_expansion":
        atr_pct = f["atrs"][idx] / close * 100.0 if close > 0 else 0.0
        return (None, "pass") if 0.10 <= atr_pct <= 6.0 else (0, "atr_out")
    if indicator_id == "bb_width_low":
        width = f["bb_wide"]["width"][idx]
        return (None, "pass") if 0 < width <= 6.0 else (0, "bb_width_high")
    if indicator_id == "rsi_momentum":
        if f["rsis"][idx] > 55 and f["rsis"][idx] > f["rsis"][idx - 1]:
            return 1, "rsi_up"
        if f["rsis"][idx] < 45 and f["rsis"][idx] < f["rsis"][idx - 1]:
            return -1, "rsi_down"
    elif indicator_id == "rsi_reversion":
        if f["rsis"][idx] <= 30:
            return 1, "rsi_oversold"
        if f["rsis"][idx] >= 70:
            return -1, "rsi_overbought"
    elif indicator_id == "qqe_proxy":
        if f["rsi_smooth"][idx] > 53 and f["rsi_smooth"][idx] > f["rsi_smooth"][idx - 1]:
            return 1, "qqe_up"
        if f["rsi_smooth"][idx] < 47 and f["rsi_smooth"][idx] < f["rsi_smooth"][idx - 1]:
            return -1, "qqe_down"
    elif indicator_id == "macd_momentum":
        if f["macd_hist"][idx] > 0 and f["macd_hist"][idx] > f["macd_hist"][idx - 1]:
            return 1, "macd_up"
        if f["macd_hist"][idx] < 0 and f["macd_hist"][idx] < f["macd_hist"][idx - 1]:
            return -1, "macd_down"
    elif indicator_id == "ema_trend":
        if close > f["ema200"][idx] and f["ema50"][idx] > f["ema200"][idx]:
            return 1, "ema_trend_up"
        if close < f["ema200"][idx] and f["ema50"][idx] < f["ema200"][idx]:
            return -1, "ema_trend_down"
    elif indicator_id == "ema_cross":
        if f["ema21"][idx] > f["ema89"][idx] and f["ema21"][idx] > f["ema21"][idx - 1]:
            return 1, "ema_cross_up"
        if f["ema21"][idx] < f["ema89"][idx] and f["ema21"][idx] < f["ema21"][idx - 1]:
            return -1, "ema_cross_down"
    elif indicator_id == "sma_trend":
        if close > f["sma200"][idx] > 0 and f["sma50"][idx] > f["sma200"][idx]:
            return 1, "sma_trend_up"
        if close < f["sma200"][idx] and f["sma50"][idx] < f["sma200"][idx]:
            return -1, "sma_trend_down"
    elif indicator_id == "bollinger_reentry":
        bb = f["bb"]
        if f["closes"][idx - 1] < bb["lower"][idx - 1] and close >= bb["lower"][idx]:
            return 1, "bb_reentry_up"
        if f["closes"][idx - 1] > bb["upper"][idx - 1] and close <= bb["upper"][idx]:
            return -1, "bb_reentry_down"
    elif indicator_id == "bollinger_breakout":
        bb = f["bb"]
        if close > bb["upper"][idx] > 0:
            return 1, "bb_break_up"
        if close < bb["lower"][idx] and bb["lower"][idx] > 0:
            return -1, "bb_break_down"
    elif indicator_id == "squeeze_release":
        return f["squeeze_release"][idx], "squeeze_release"
    elif indicator_id == "supertrend_flip":
        if f["supertrend"][idx] != f["supertrend"][idx - 1]:
            return 1 if f["supertrend"][idx] > 0 else -1, "supertrend_flip"
    elif indicator_id == "donchian_breakout":
        if close > f["donchian_high"][idx - 1] > 0:
            return 1, "donchian_up"
        if close < f["donchian_low"][idx - 1] and f["donchian_low"][idx - 1] > 0:
            return -1, "donchian_down"
    elif indicator_id == "adx_dmi":
        if f["adx"][idx] >= 22 and f["pdi"][idx] > f["mdi"][idx]:
            return 1, "adx_up"
        if f["adx"][idx] >= 22 and f["mdi"][idx] > f["pdi"][idx]:
            return -1, "adx_down"
    elif indicator_id == "vwap_trend":
        if close > f["vwap48"][idx] > 0 and f["vwap48"][idx] >= f["vwap48"][idx - 1]:
            return 1, "vwap_up"
        if close < f["vwap48"][idx] and f["vwap48"][idx] > 0 and f["vwap48"][idx] <= f["vwap48"][idx - 1]:
            return -1, "vwap_down"
    elif indicator_id == "ichimoku_cross":
        if f["tenkan"][idx - 1] <= f["kijun"][idx - 1] and f["tenkan"][idx] > f["kijun"][idx] and close > f["cloud_top"][idx]:
            return 1, "ichimoku_up"
        if f["tenkan"][idx - 1] >= f["kijun"][idx - 1] and f["tenkan"][idx] < f["kijun"][idx] and close < f["cloud_bottom"][idx]:
            return -1, "ichimoku_down"
    elif indicator_id == "cci_reversion":
        if f["cci"][idx] <= -100:
            return 1, "cci_low"
        if f["cci"][idx] >= 100:
            return -1, "cci_high"
    elif indicator_id == "stoch_reversion":
        if f["stoch"][idx] <= 20 and f["stoch"][idx] >= f["stoch"][idx - 1]:
            return 1, "stoch_low"
        if f["stoch"][idx] >= 80 and f["stoch"][idx] <= f["stoch"][idx - 1]:
            return -1, "stoch_high"
    return 0, "no_signal"


def signal_for_combo(combo: dict[str, Any], bars: list[dict[str, Any]], features: dict[str, Any], idx: int) -> tuple[str, dict[str, Any]] | None:
    dirs: list[int] = []
    reasons: list[str] = []
    for indicator_id in combo["indicator_ids"]:
        direction, reason = vote(indicator_id, bars, features, idx)
        if direction == 0:
            return None
        if direction is not None:
            dirs.append(direction)
        reasons.append(f"{indicator_id}:{reason}")
    if not dirs:
        return None
    if any(value != dirs[0] for value in dirs):
        return None
    return ("long" if dirs[0] > 0 else "short"), {"combo_reasons": "; ".join(reasons[:8])}


def simulate_combo_symbol(symbol: str, interval: str, bars: list[dict[str, Any]], combo: dict[str, Any], common: dict[str, Any]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    if len(bars) < shared.MIN_BARS:
        return trades
    features = build_features(bars)
    idx = 220
    while idx < len(bars) - 2 and len(trades) < MAX_TRADES_PER_SYMBOL:
        signal = signal_for_combo(combo, bars, features, idx)
        if not signal:
            idx += 1
            continue
        side, extra = signal
        trade = base_ind.simulate_indicator_trade(
            strategy="IndicatorFactory",
            adapter="indicator_factory",
            symbol=symbol,
            interval=interval,
            bars=bars,
            signal_idx=idx,
            side=side,
            params=common,
            extra={
                "combo_id": combo["combo_id"],
                "combo_name": combo.get("name"),
                **extra,
            },
        )
        if trade:
            trades.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return trades


def run_combo_interval(interval: str, loaded: dict[str, list[dict[str, Any]]], variant: dict[str, Any], split: str | None = None) -> list[dict[str, Any]]:
    combo = variant["params"]["combo"]
    common = variant["params"]["common"]
    trades: list[dict[str, Any]] = []
    for symbol, bars in loaded.items():
        use_bars = shared.split_sequence(bars, split)
        trades.extend(simulate_combo_symbol(symbol, interval, use_bars, combo, common))
    return trades


def build_signal_cache(
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]],
    registry: list[dict[str, Any]],
) -> dict[str, dict[str | None, dict[str, dict[str, Any]]]]:
    implemented_ids = [item["id"] for item in registry if item.get("implemented")]
    cache: dict[str, dict[str | None, dict[str, dict[str, Any]]]] = {}
    for interval, by_symbol in loaded_by_interval.items():
        cache[interval] = {}
        for split in (None, "train", "validation", "test"):
            cache[interval][split] = {}
            for symbol, bars in by_symbol.items():
                use_bars = shared.split_sequence(bars, split)
                if len(use_bars) < shared.MIN_BARS:
                    continue
                features = build_features(use_bars)
                votes: dict[str, list[int]] = {}
                for indicator_id in implemented_ids:
                    arr = [0 for _ in use_bars]
                    for idx in range(1, len(use_bars)):
                        direction, _reason = vote(indicator_id, use_bars, features, idx)
                        arr[idx] = 2 if direction is None else int(direction)
                    votes[indicator_id] = arr
                cache[interval][split][symbol] = {"bars": use_bars, "votes": votes}
    return cache


def cached_combo_side(combo: dict[str, Any], votes: dict[str, list[int]], idx: int) -> str | None:
    dirs: list[int] = []
    for indicator_id in combo["indicator_ids"]:
        arr = votes.get(indicator_id)
        if not arr or idx >= len(arr):
            return None
        value = arr[idx]
        if value == 0:
            return None
        if value in {-1, 1}:
            dirs.append(value)
    if not dirs or any(value != dirs[0] for value in dirs):
        return None
    return "long" if dirs[0] > 0 else "short"


def simulate_cached_combo_symbol(symbol: str, interval: str, cached: dict[str, Any], combo: dict[str, Any], common: dict[str, Any]) -> list[dict[str, Any]]:
    bars = cached["bars"]
    votes = cached["votes"]
    trades: list[dict[str, Any]] = []
    idx = 220
    while idx < len(bars) - 2 and len(trades) < MAX_TRADES_PER_SYMBOL:
        side = cached_combo_side(combo, votes, idx)
        if not side:
            idx += 1
            continue
        trade = base_ind.simulate_indicator_trade(
            strategy="IndicatorFactory",
            adapter="indicator_factory_cached",
            symbol=symbol,
            interval=interval,
            bars=bars,
            signal_idx=idx,
            side=side,
            params=common,
            extra={"combo_id": combo["combo_id"], "combo_name": combo.get("name")},
        )
        if trade:
            trades.append(trade)
            idx += max(1, safe_int(trade.get("bars_held"), 1))
        idx += 1
    return trades


def cached_combo_trades(
    interval_cache: dict[str | None, dict[str, dict[str, Any]]],
    combo: dict[str, Any],
    common: dict[str, Any],
    split: str | None,
    interval: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for symbol, cached in interval_cache.get(split, {}).items():
        out.extend(simulate_cached_combo_symbol(symbol, interval, cached, combo, common))
    return out


def evaluate_combo_cached(
    interval: str,
    interval_cache: dict[str | None, dict[str, dict[str, Any]]],
    combo: dict[str, Any],
    common: dict[str, Any],
) -> dict[str, Any]:
    full_trades = cached_combo_trades(interval_cache, combo, common, None, interval)
    train_trades = cached_combo_trades(interval_cache, combo, common, "train", interval)
    validation_trades = cached_combo_trades(interval_cache, combo, common, "validation", interval)
    test_trades = cached_combo_trades(interval_cache, combo, common, "test", interval)
    full, charts = shared.summarize_trades(full_trades)
    train, _ = shared.summarize_trades(train_trades)
    validation, _ = shared.summarize_trades(validation_trades)
    test, _ = shared.summarize_trades(test_trades)
    row = {
        "name": combo["combo_id"],
        "params": {"combo": combo, "common": common},
        "full": full,
        "train": train,
        "validation": validation,
        "test": test,
        "charts": {
            "equity_curve": charts["equity_curve"][-500:],
            "drawdown": charts["drawdown"][-500:],
            "monthly_returns": charts["monthly_returns"],
        },
        "trades": sorted(full_trades, key=lambda item: str(item.get("exit_ts") or ""))[-shared.MAX_REPORT_TRADES:],
    }
    row["robust_score"] = shared.robust_score(row)
    row.update(shared.anti_fit(row))
    return row


def anti_fit_decision(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = list(row.get("anti_fit_reasons") or [])
    full = row.get("full") or {}
    test = row.get("test") or {}
    if row.get("anti_fit_pass"):
        return "singularity_candidate", []
    if (
        safe_float(full.get("net_profit_usdt")) > 0
        and safe_float(test.get("net_profit_usdt")) > 0
        and safe_int(full.get("trades")) >= MIN_SPLIT_TRADES * 3
        and safe_float(full.get("max_drawdown_pct")) <= MAX_DRAWDOWN_PCT
    ):
        return "near_miss", reasons
    return "rejected", reasons


def write_progress(
    *,
    paths: dict[str, Path],
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    status: str,
    completed_combos: int,
    total_combos: int,
    completed_combo_intervals: int,
    total_combo_intervals: int,
    candidate_count: int,
    latest_combo_id: str = "",
    latest_interval: str = "",
) -> None:
    payload = {
        "generated_at": now_iso(),
        "module": "indicator_factory_progress",
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "completed_combos": completed_combos,
        "total_combos": total_combos,
        "completed_combo_intervals": completed_combo_intervals,
        "total_combo_intervals": total_combo_intervals,
        "candidate_count": candidate_count,
        "latest_combo_id": latest_combo_id,
        "latest_interval": latest_interval,
        "progress_pct": round((completed_combo_intervals / total_combo_intervals) * 100.0, 4) if total_combo_intervals else 0.0,
        "safety": safety_payload(),
    }
    write_json_atomic(paths["runtime"] / "indicator_factory_progress_latest.json", payload)
    conn.execute(
        """
        insert or replace into run_progress(run_id,stage,status,completed_combos,total_combos,completed_combo_intervals,total_combo_intervals,candidate_count,latest_combo_id,latest_interval,updated_at,progress_json)
        values(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            stage,
            status,
            int(completed_combos),
            int(total_combos),
            int(completed_combo_intervals),
            int(total_combo_intervals),
            int(candidate_count),
            latest_combo_id,
            latest_interval,
            payload["generated_at"],
            jdump(payload),
        ),
    )


def data_coverage_plan(root: Path, target_days: int = 730) -> dict[str, Any]:
    table = root / "research_store" / "historical_klines"
    days: list[str] = []
    if table.exists():
        for path in table.glob("date=*"):
            if path.is_dir():
                days.append(path.name.replace("date=", ""))
    days = sorted(set(days))
    first = days[0] if days else ""
    last = days[-1] if days else ""
    current_days = len(days)
    missing_days = max(0, target_days - current_days)
    store_bytes = 0
    if table.exists():
        for path in table.rglob("*.jsonl"):
            try:
                store_bytes += path.stat().st_size
            except Exception:
                pass
    command = (
        "python 部署工具\\historical_kline_backfill.py --apply --runtime-dir runtime "
        "--reports-dir reports --research-store research_store --top-n 30 --days 730 "
        "--intervals 15m,30m,1h,4h --providers bybit,okx --format jsonl "
        "--max-rps 0.2 --max-requests 240 --max-runtime-sec 1200 "
        "--flush-requests 10 --output-prefix historical_kline_backfill_2y_local"
    )
    return {
        "target_days": target_days,
        "current_partition_days": current_days,
        "first_day": first,
        "last_day": last,
        "missing_partition_days_estimate": missing_days,
        "store_bytes": store_bytes,
        "store_mb": round(store_bytes / 1024 / 1024, 3),
        "estimated_2y_store_mb": round((store_bytes / max(1, current_days) * target_days) / 1024 / 1024, 3),
        "local_extension_possible": True,
        "api_policy": "local_public_bybit_okx_low_rps_no_cloud_no_binance",
        "recommended_command": command,
    }


def run_factory(root: Path, *, days: int, intervals: list[str], max_combos: int, all_combos: bool, stage: str) -> dict[str, Any]:
    paths = ensure_dirs(root)
    registry = indicator_registry()
    conn = init_db(paths["lab"] / "results.sqlite")
    upsert_indicators(conn, registry)
    combos = generate_combos(registry, 2, 4)
    upsert_combos(conn, combos)
    selected = combos if all_combos or max_combos <= 0 else combos[:max_combos]
    symbols = shared.universe_symbols(root, None)
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    loaded_by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for interval in intervals:
        print(f"[{now_iso()}] load {interval}", file=sys.stderr, flush=True)
        loaded_by_interval[interval] = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    coverage = shared.coverage_rows(loaded_by_interval)
    print(f"[{now_iso()}] precompute indicator signals", file=sys.stderr, flush=True)
    signal_cache = build_signal_cache(loaded_by_interval, registry)
    run_id = f"if-{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}-{stable_id([stage, str(days), ','.join(intervals), str(len(selected))])}"
    common = {
        "atr_stop_multiplier": 1.8,
        "take_profit_atr": 3.2,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 0.8,
        "max_hold_bars": 24,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
    }
    result_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    trades_out = paths["trades"] / f"{run_id}.jsonl"
    total_combo_intervals = len(selected) * len(intervals)
    completed_combo_intervals = 0
    write_progress(
        paths=paths,
        conn=conn,
        run_id=run_id,
        stage=stage,
        status="running",
        completed_combos=0,
        total_combos=len(selected),
        completed_combo_intervals=0,
        total_combo_intervals=total_combo_intervals,
        candidate_count=0,
    )
    conn.commit()
    for pos, combo in enumerate(selected, start=1):
        print(f"[{now_iso()}] combo {pos}/{len(selected)} {combo['combo_id']} {combo['name']}", file=sys.stderr, flush=True)
        variant_params = {"combo": combo, "common": common}
        for interval in intervals:
            row = evaluate_combo_cached(interval, signal_cache.get(interval, {}), combo, common)
            compact = {
                "run_id": run_id,
                "combo_id": combo["combo_id"],
                "combo_name": combo["name"],
                "indicator_ids": combo["indicator_ids"],
                "interval": interval,
                "full": row.get("full") or {},
                "train": row.get("train") or {},
                "validation": row.get("validation") or {},
                "test": row.get("test") or {},
                "robust_score": safe_float(row.get("robust_score")),
                "anti_fit_pass": bool(row.get("anti_fit_pass")),
                "anti_fit_reasons": row.get("anti_fit_reasons") or [],
            }
            decision, reasons = anti_fit_decision(row)
            compact["decision"] = decision
            compact["anti_fit_reasons"] = reasons
            result_rows.append(compact)
            full = compact["full"]
            test = compact["test"]
            conn.execute(
                """
                insert or replace into combo_results(run_id,combo_id,interval,net_profit_usdt,test_net_profit_usdt,profit_factor,max_drawdown_pct,win_rate_pct,trades,robust_score,decision,anti_fit_reasons_json,params_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    combo["combo_id"],
                    interval,
                    safe_float(full.get("net_profit_usdt")),
                    safe_float(test.get("net_profit_usdt")),
                    safe_float(full.get("profit_factor")),
                    safe_float(full.get("max_drawdown_pct")),
                    safe_float(full.get("win_rate_pct")),
                    safe_int(full.get("trades")),
                    compact["robust_score"],
                    decision,
                    jdump(reasons),
                    jdump(variant_params),
                ),
            )
            conn.execute(
                """
                insert or replace into combo_result_details(run_id,combo_id,combo_name,interval,indicator_ids_json,full_json,train_json,validation_json,test_json,robust_score,decision,anti_fit_reasons_json,params_json,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    combo["combo_id"],
                    combo["name"],
                    interval,
                    jdump(combo["indicator_ids"]),
                    jdump(compact["full"]),
                    jdump(compact["train"]),
                    jdump(compact["validation"]),
                    jdump(compact["test"]),
                    compact["robust_score"],
                    decision,
                    jdump(reasons),
                    jdump(variant_params),
                    now_iso(),
                ),
            )
            for trade in (row.get("trades") or [])[-60:]:
                trades_out.parent.mkdir(parents=True, exist_ok=True)
                with trades_out.open("a", encoding="utf-8") as fh:
                    fh.write(jdump({"run_id": run_id, "combo_id": combo["combo_id"], "interval": interval, "trade": trade}) + "\n")
            if decision in {"singularity_candidate", "near_miss"}:
                candidate_rows.append(compact)
                conn.execute(
                    """
                    insert or replace into candidates(run_id,combo_id,interval,status,score,summary_json,created_at)
                    values(?,?,?,?,?,?,?)
                    """,
                    (run_id, combo["combo_id"], interval, decision, compact["robust_score"], jdump(compact), now_iso()),
                )
                (paths["candidates"] / f"{run_id}_{combo['combo_id']}_{interval}.json").write_text(jdump(compact), encoding="utf-8")
            completed_combo_intervals += 1
            if completed_combo_intervals == total_combo_intervals or completed_combo_intervals % max(1, len(intervals) * 10) == 0:
                write_progress(
                    paths=paths,
                    conn=conn,
                    run_id=run_id,
                    stage=stage,
                    status="running",
                    completed_combos=pos - 1,
                    total_combos=len(selected),
                    completed_combo_intervals=completed_combo_intervals,
                    total_combo_intervals=total_combo_intervals,
                    candidate_count=len(candidate_rows),
                    latest_combo_id=combo["combo_id"],
                    latest_interval=interval,
                )
        conn.commit()
        write_progress(
            paths=paths,
            conn=conn,
            run_id=run_id,
            stage=stage,
            status="running",
            completed_combos=pos,
            total_combos=len(selected),
            completed_combo_intervals=completed_combo_intervals,
            total_combo_intervals=total_combo_intervals,
            candidate_count=len(candidate_rows),
            latest_combo_id=combo["combo_id"],
            latest_interval=intervals[-1] if intervals else "",
        )
        conn.commit()
    summary = summarize_run(registry, combos, selected, result_rows, candidate_rows, coverage, data_coverage_plan(root, 730), run_id, stage, days, intervals)
    conn.execute(
        "insert or replace into runs(run_id,created_at,stage,days,intervals_json,symbols_json,combo_count,status,summary_json) values(?,?,?,?,?,?,?,?,?)",
        (run_id, now_iso(), stage, int(days), jdump(intervals), jdump(symbols), len(selected), "completed", jdump(summary)),
    )
    conn.commit()
    write_progress(
        paths=paths,
        conn=conn,
        run_id=run_id,
        stage=stage,
        status="completed",
        completed_combos=len(selected),
        total_combos=len(selected),
        completed_combo_intervals=completed_combo_intervals,
        total_combo_intervals=total_combo_intervals,
        candidate_count=len(candidate_rows),
        latest_combo_id=selected[-1]["combo_id"] if selected else "",
        latest_interval=intervals[-1] if intervals else "",
    )
    conn.commit()
    payload = {
        "generated_at": now_iso(),
        "status": "completed",
        "module": "indicator_factory",
        "run_id": run_id,
        "summary": summary,
        "results": result_rows[:1000],
        "candidates": candidate_rows,
        "safety": safety_payload(),
        "paths": {
            "db": str(paths["lab"] / "results.sqlite"),
            "html": str(paths["lab"] / "indicator_factory_latest.html"),
            "trades": str(trades_out),
            "lab": str(paths["lab"]),
        },
    }
    (paths["runs"] / f"{run_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_json_atomic(paths["runtime"] / "indicator_factory_latest.json", payload)
    html = render_html(payload, registry, result_rows, candidate_rows)
    (paths["lab"] / "indicator_factory_latest.html").write_text(html, encoding="utf-8")
    conn.close()
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def safety_payload() -> dict[str, Any]:
    return {
        "binance_requests_enabled": False,
        "cloud_compute": False,
        "live_config_mutation": False,
        "paper_or_real_orders": False,
        "strategy_frequency_change": False,
        "automatic_tuning_allowed": False,
        "automatic_rollback_allowed": False,
        "automatic_upgrade_allowed": False,
    }


def summarize_run(
    registry: list[dict[str, Any]],
    combos: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    data_plan: dict[str, Any],
    run_id: str,
    stage: str,
    days: int,
    intervals: list[str],
) -> dict[str, Any]:
    decisions: dict[str, int] = {}
    for row in results:
        decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
    best = sorted(
        results,
        key=lambda row: (
            row["decision"] == "singularity_candidate",
            row["decision"] == "near_miss",
            safe_int((row.get("full") or {}).get("trades")) >= MIN_SPLIT_TRADES,
            safe_int((row.get("test") or {}).get("trades")) > 0,
            row["robust_score"],
            safe_float(row["test"].get("net_profit_usdt")),
            safe_float(row["full"].get("net_profit_usdt")),
        ),
        reverse=True,
    )[:30]
    return {
        "run_id": run_id,
        "stage": stage,
        "days": days,
        "intervals": intervals,
        "registered_indicators": len(registry),
        "implemented_indicators": sum(1 for item in registry if item.get("implemented")),
        "generated_valid_combos": len(combos),
        "tested_combos": len(selected),
        "tested_combo_intervals": len(results),
        "decision_counts": decisions,
        "candidate_count": len(candidates),
        "coverage": {
            "target_symbol_intervals": len(coverage),
            "usable_symbol_intervals": sum(1 for row in coverage if row.get("usable")),
            "usable_symbols": len({row["symbol"] for row in coverage if row.get("usable")}),
        },
        "best_rows": best,
        "data_plan": data_plan,
        "action": "manual_review_candidates" if candidates else "continue_search_no_live_change",
    }


def render_html(payload: dict[str, Any], registry: list[dict[str, Any]], results: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    summary = payload["summary"]
    categories = sorted(set(item["category"] for item in registry))
    implemented = [item for item in registry if item.get("implemented")]
    planned = [item for item in registry if not item.get("implemented")]
    best = summary.get("best_rows") or []
    decision_counts = summary.get("decision_counts") or {}
    data_plan = summary.get("data_plan") or {}

    def kpi(label: str, value: Any, note: str = "") -> str:
        return f"<div class='kpi'><span>{escape(label)}</span><b>{escape(str(value))}</b><small>{escape(note)}</small></div>"

    indicator_cards = []
    for category in categories:
        items = [item for item in registry if item["category"] == category]
        chips = "".join(
            f"<span class='chip {'on' if item.get('implemented') else 'off'}'>{escape(item['name'])}</span>"
            for item in items
        )
        indicator_cards.append(f"<section class='panel'><h3>{escape(category)}</h3><div class='chips'>{chips}</div></section>")

    best_rows = []
    for row in best[:40]:
        full = row.get("full") or {}
        test = row.get("test") or {}
        best_rows.append(
            "<tr>"
            f"<td>{escape(row.get('decision',''))}</td>"
            f"<td>{escape(row.get('combo_id',''))}</td>"
            f"<td>{escape(' + '.join(row.get('indicator_ids') or []))}</td>"
            f"<td>{escape(row.get('interval',''))}</td>"
            f"<td>{safe_float(full.get('net_profit_usdt')):.2f}</td>"
            f"<td>{safe_float(test.get('net_profit_usdt')):.2f}</td>"
            f"<td>{safe_float(full.get('profit_factor')):.2f}</td>"
            f"<td>{safe_float(full.get('max_drawdown_pct')):.2f}</td>"
            f"<td>{safe_int(full.get('trades'))}</td>"
            f"<td>{safe_float(row.get('robust_score')):.2f}</td>"
            "</tr>"
        )

    candidate_rows = []
    for row in candidates[:30]:
        full = row.get("full") or {}
        candidate_rows.append(
            f"<li><b>{escape(row['combo_id'])}</b> {escape(' + '.join(row.get('indicator_ids') or []))} "
            f"{escape(row.get('interval',''))}: {safe_float(full.get('net_profit_usdt')):.2f} USDT</li>"
        )

    source_rows = [
        ("TradingView Scripts", "https://www.tradingview.com/scripts/"),
        ("Freqtrade strategies", "https://github.com/freqtrade/freqtrade-strategies"),
        ("freqtrade technical", "https://github.com/freqtrade/technical"),
        ("technicalindicators", "https://github.com/anandanand84/technicalindicators"),
    ]
    sources = "".join(f"<a href='{escape(url)}'>{escape(name)}</a>" for name, url in source_rows)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>本地指标工厂</title>
<style>
:root{{--bg:#071016;--panel:#0d1a23;--line:#1d3544;--text:#e9f3fa;--muted:#8fa8b8;--cyan:#76e4ff;--green:#9ff2b2;--red:#ff8e9a;--gold:#ffd36d}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 20% 0%,#0f2633 0,#071016 34%,#05090d 100%);color:var(--text);font-family:Arial,'Microsoft YaHei',sans-serif}}
.wrap{{max-width:1480px;margin:0 auto;padding:30px}}
.hero{{min-height:230px;display:grid;grid-template-columns:1.15fr .85fr;gap:18px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:22px}}
h1{{font-size:44px;margin:0 0 10px;letter-spacing:0}} h2{{margin:34px 0 14px;font-size:24px}} h3{{margin:0 0 12px;color:var(--cyan)}}
.hero p{{color:var(--muted);font-size:16px;line-height:1.65;max-width:880px}}
.status{{border:1px solid var(--line);background:linear-gradient(145deg,#102332,#0b151d);border-radius:8px;padding:18px}}
.status b{{font-size:30px;color:var(--cyan)}} .status small{{display:block;color:var(--muted);margin-top:8px;line-height:1.5}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:20px}}
.kpi,.panel{{border:1px solid var(--line);background:rgba(13,26,35,.88);border-radius:8px;padding:15px}}
.kpi span{{display:block;color:var(--muted);font-size:12px}} .kpi b{{display:block;font-size:26px;margin:8px 0;color:var(--green)}} .kpi small{{color:var(--muted)}}
.panels{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px}} .chip{{border:1px solid var(--line);padding:6px 9px;border-radius:999px;font-size:12px;background:#0b151d}} .chip.on{{border-color:#2f9c78;color:var(--green)}} .chip.off{{color:#7e929f}}
table{{width:100%;border-collapse:collapse;background:rgba(13,26,35,.92);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{padding:10px;border-bottom:1px solid var(--line);font-size:13px;text-align:left;vertical-align:top}} th{{background:#122331;color:#a9c6d7}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .empty{{color:var(--muted);line-height:1.7}} a{{color:var(--cyan);margin-right:14px}} code{{color:var(--gold)}} li{{margin:8px 0}}
</style>
</head>
<body><main class="wrap">
<section class="hero">
<div>
<h1>本地指标工厂</h1>
<p>自动收录公开热门指标，生成 2-4 指标组合，在本地历史 K线仓做分层回测，并把淘汰、近似异常点、奇点候选写入 SQLite 和本地 HTML。云端不参与。</p>
<p>运行：<code>{escape(payload.get('run_id',''))}</code>；动作：<b>{escape(summary.get('action',''))}</b></p>
</div>
<div class="status"><b>{escape(str(decision_counts.get('singularity_candidate',0)))} 奇点</b><small>近似异常点 {escape(str(decision_counts.get('near_miss',0)))}；淘汰 {escape(str(decision_counts.get('rejected',0)))}；默认不进 paper/live。</small></div>
</section>
<section class="grid">
{kpi('已收录指标', summary.get('registered_indicators'), f"已实现 {summary.get('implemented_indicators')}")}
{kpi('有效组合池', summary.get('generated_valid_combos'), f"本轮测试 {summary.get('tested_combos')}")}
{kpi('测试组合周期', summary.get('tested_combo_intervals'), f"周期 {', '.join(summary.get('intervals') or [])}")}
{kpi('2年数据计划', f"{data_plan.get('current_partition_days')}/{data_plan.get('target_days')} 天", f"预计 {data_plan.get('estimated_2y_store_mb')} MB")}
</section>
<h2>指标注册库</h2>
<div class="panels">{''.join(indicator_cards)}</div>
<h2>组合结果榜</h2>
<table><thead><tr><th>决策</th><th>组合ID</th><th>指标组合</th><th>周期</th><th>全样本PnL</th><th>测试PnL</th><th>PF</th><th>回撤%</th><th>交易</th><th>稳健分</th></tr></thead><tbody>{''.join(best_rows)}</tbody></table>
<h2>候选池</h2>
<div class="split"><div class="panel"><h3>奇点/近似异常</h3><ul>{''.join(candidate_rows) if candidate_rows else "<li class='empty'>本轮无候选。坏结果已经记录，后续不应重复消耗。</li>"}</ul></div>
<div class="panel"><h3>2年数据扩展</h3><p class="empty">当前本地分区：{escape(str(data_plan.get('first_day')))} -> {escape(str(data_plan.get('last_day')))}，约 {escape(str(data_plan.get('current_partition_days')))} 天。补到 2 年建议本地低速命令：</p><p><code>{escape(str(data_plan.get('recommended_command')))}</code></p></div></div>
<h2>来源</h2>
<div class="panel">{sources}<p class="empty">TradingView/Pine 只借公开思路，不复制社区源码。实际公式在本地 Python 中实现。</p></div>
<h2>本地文件</h2>
<div class="panel"><p>SQLite：<code>{escape(str(payload.get('paths',{}).get('db')))}</code></p><p>交易样本：<code>{escape(str(payload.get('paths',{}).get('trades')))}</code></p><p>报告：<code>{escape(str(payload.get('paths',{}).get('html')))}</code></p></div>
</main></body></html>"""


def run_data_plan(root: Path, days: int) -> dict[str, Any]:
    paths = ensure_dirs(root)
    plan = data_coverage_plan(root, days)
    payload = {"generated_at": now_iso(), "module": "indicator_factory_data_plan", "plan": plan, "safety": safety_payload()}
    write_json_atomic(paths["runtime"] / "indicator_factory_data_plan_latest.json", payload)
    return payload


def compact_cli_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") or {}
    data_plan = summary.get("data_plan") or {}
    return {
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "stage": summary.get("stage"),
        "registered_indicators": summary.get("registered_indicators"),
        "implemented_indicators": summary.get("implemented_indicators"),
        "generated_valid_combos": summary.get("generated_valid_combos"),
        "tested_combos": summary.get("tested_combos"),
        "tested_combo_intervals": summary.get("tested_combo_intervals"),
        "decision_counts": summary.get("decision_counts"),
        "candidate_count": summary.get("candidate_count"),
        "action": summary.get("action"),
        "two_year_plan": {
            "current_days": data_plan.get("current_partition_days"),
            "target_days": data_plan.get("target_days"),
            "estimated_mb": data_plan.get("estimated_2y_store_mb"),
        },
        "paths": payload.get("paths"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local indicator factory")
    sub = parser.add_subparsers(dest="cmd")
    p_registry = sub.add_parser("build-registry")
    p_registry.add_argument("--root", type=Path, default=ROOT)
    p_plan = sub.add_parser("data-plan")
    p_plan.add_argument("--root", type=Path, default=ROOT)
    p_plan.add_argument("--days", type=int, default=730)
    p_run = sub.add_parser("run")
    p_run.add_argument("--root", type=Path, default=ROOT)
    p_run.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p_run.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    p_run.add_argument("--max-combos", type=int, default=DEFAULT_MAX_COMBOS)
    p_run.add_argument("--all-combos", action="store_true")
    p_run.add_argument("--stage", default="coarse")
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    root = Path(getattr(args, "root", ROOT)).resolve()
    if cmd == "build-registry":
        paths = ensure_dirs(root)
        conn = init_db(paths["lab"] / "results.sqlite")
        registry = indicator_registry()
        upsert_indicators(conn, registry)
        combos = generate_combos(registry)
        upsert_combos(conn, combos)
        print(json.dumps({"indicators": len(registry), "implemented": sum(1 for i in registry if i.get("implemented")), "combos": len(combos), "db": str(paths["lab"] / "results.sqlite")}, ensure_ascii=False, indent=2))
        return 0
    if cmd == "data-plan":
        payload = run_data_plan(root, int(args.days))
        print(json.dumps(payload["plan"], ensure_ascii=False, indent=2))
        return 0
    intervals = shared.parse_csv(getattr(args, "intervals", ""), DEFAULT_INTERVALS)
    payload = run_factory(root, days=int(args.days), intervals=intervals, max_combos=int(args.max_combos), all_combos=bool(args.all_combos), stage=str(args.stage))
    print(json.dumps(compact_cli_summary(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
