"""辅助指标计算模块

配合 MACD 三段背离主信号使用，提供辅助过滤/确认功能。
不单独产生交易信号，仅对主信号进行加减分调整。

包含：
  - EMA (9/21/55) — 趋势方向确认
  - RSI (14) — 超买超卖区域
  - ADX (14) — 趋势强度
  - Stochastic (14,3,3) — 动量
  - Bollinger Bands (20,2) — 波动异常
  - ATR (14) — 止损距离计算
  - OBV — 量价关系
  - VWAP — 日内关键价位（需日内数据）
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger("cat.analyzer.aux")


@dataclass
class AuxiliaryResult:
    """辅助指标综合结果"""
    # 各指标值
    ema_9: float = 0
    ema_21: float = 0
    ema_55: float = 0
    rsi_14: float = 0
    adx_14: float = 0
    stoch_k: float = 0
    stoch_d: float = 0
    bb_upper: float = 0
    bb_middle: float = 0
    bb_lower: float = 0
    atr_14: float = 0
    obv_trend: str = "neutral"   # up / down / neutral
    vwap: Optional[float] = None

    # 综合评分调整（-20 ~ +20）
    score_adjustment: float = 0.0

    # 各项详细评分
    details: dict = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


def calc_ema(
    prices: pd.Series | np.ndarray,
    period: int,
) -> pd.Series:
    """指数移动平均线"""
    return pd.Series(prices, dtype=float).ewm(span=period, adjust=False).mean()


def calc_rsi(
    closes: pd.Series | np.ndarray,
    period: int = 14,
) -> pd.Series:
    """相对强弱指数 RSI"""
    closes = pd.Series(closes, dtype=float)
    delta = closes.diff()

    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.finfo(float).eps)
    rsi = 100 - (100 / (1 + rs))

    return rsi


def calc_adx(
    highs: pd.Series | np.ndarray,
    lows: pd.Series | np.ndarray,
    closes: pd.Series | np.ndarray,
    period: int = 14,
) -> pd.Series:
    """平均趋向指标 ADX"""
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    c = pd.Series(closes, dtype=float)

    tr1 = h - l
    tr2 = abs(h - c.shift(1))
    tr3 = abs(l - c.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up = h - h.shift(1)
    down = l.shift(1) - l

    plus_dm = where(up > down, up, 0.0)
    minus_dm = where(down > up, down, 0.0)

    atr_val = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (
        pd.Series(plus_dm).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        / atr_val.replace(0, np.finfo(float).eps)
    )
    minus_di = 100 * (
        pd.Series(minus_dm).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        / atr_val.replace(0, np.finfo(float).eps)
    )

    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.finfo(float).eps)
    adx = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    return adx


def where(condition, x, y):
    """numpy.where 的 Series 安全版本"""
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return x.where(condition, y)
    return np.where(condition, x, y)


def calc_stochastic(
    highs: pd.Series | np.ndarray,
    lows: pd.Series | np.ndarray,
    closes: pd.Series | np.ndarray,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """随机指标 Stochastic Oscillator"""
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    c = pd.Series(closes, dtype=float)

    lowest_low = l.rolling(k_period).min()
    highest_high = h.rolling(k_period).max()

    k = 100 * (c - lowest_low) / (highest_high - lowest_low).replace(0, np.finfo(float).eps)
    d = k.rolling(d_period).mean()

    return k, d


def calc_bollinger_bands(
    closes: pd.Series | np.ndarray,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """布林带"""
    c = pd.Series(closes, dtype=float)
    middle = c.rolling(period).mean()
    std = c.rolling(period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std

    return upper, middle, lower


def calc_atr(
    highs: pd.Series | np.ndarray,
    lows: pd.Series | np.ndarray,
    closes: pd.Series | np.ndarray,
    period: int = 14,
) -> pd.Series:
    """平均真实波幅 ATR"""
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    c = pd.Series(closes, dtype=float)

    prev_c = c.shift(1)
    tr1 = h - l
    tr2 = abs(h - prev_c)
    tr3 = abs(l - prev_c)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return atr


def calc_obv(closes: pd.Series | np.ndarray, volumes: pd.Series | np.ndarray) -> pd.Series:
    """能量潮 OBV"""
    c = pd.Series(closes, dtype=float)
    v = pd.Series(volumes, dtype=float)

    direction = np.sign(c.diff())
    direction = direction.fillna(0)

    obv = (direction * v).cumsum()
    return obv


def calc_vwap(
    highs: pd.Series | np.ndarray,
    lows: pd.Series | np.ndarray,
    closes: pd.Series | np.ndarray,
    volumes: pd.Series | np.ndarray,
) -> pd.Series:
    """成交量加权平均价 VWAP（通常用于日内）"""
    typical_price = (pd.Series(highs) + pd.Series(lows) + pd.Series(closes)) / 3
    tp_vol = typical_price * pd.Series(volumes, dtype=float)
    cum_tp_vol = tp_vol.cumsum()
    cum_volume = pd.Series(volumes, dtype=float).cumsum()

    vwap = cum_tp_vol / cum_volume.replace(0, np.finfo(float).eps)
    return vwap


def calc_mfi(closes: pd.Series | np.ndarray, highs: pd.Series | np.ndarray,
             lows: pd.Series | np.ndarray, volumes: pd.Series | np.ndarray,
             period: int = 14) -> pd.Series:
    """
    资金流量指数 MFI (Money Flow Index) —— 带成交量的 RI

    计算步骤：
    1. 典型价格 TP = (H+L+C)/3
    2. 资金流量 MF = TP × Volume
    3. 正/负资金流量按 TP 涨跌分类
    4. MFI = 100 - 100/(1 + 正MF和/负MF和)

    Returns:
        MFI 序列 (0~100)
    """
    c = pd.Series(closes, dtype=float)
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    v = pd.Series(volumes, dtype=float)

    tp = (h + l + c) / 2
    mf = tp * v

    # 当前 TP vs 前一日 TP
    tp_diff = tp.diff()

    pos_mf = pd.Series(np.where(tp_diff > 0, mf, 0.0), index=c.index)
    neg_mf = pd.Series(np.where(tp_diff < 0, mf, 0.0), index=c.index)

    pos_sum = pos_mf.rolling(window=period, min_periods=period).sum()
    neg_sum = neg_mf.rolling(window=period, min_periods=period).sum()

    mfi_ratio = pos_sum / neg_sum.replace(0, np.finfo(float).eps)
    mfi = 100 - (100 / (1 + mfi_ratio))
    return mfi


def calc_supertrend(
    highs: pd.Series | np.ndarray,
    lows: pd.Series | np.ndarray,
    closes: pd.Series | np.ndarray,
    atr_val: float | pd.Series | None = None,
    period: int = 10,
    multiplier: float = 3.0,
):
    """
    超级趋势指标 SuperTrend（量价加权版）

    传统 SuperTrend 用 HL/2 作为基本带中线。
    量价加权版用 VWAP 替代 HL/2，使指标融入成交量信息。

    Args:
        high/low/close: OHLC 数据
        atr_val: 可选，预计算的 ATR 值或序列；若为 None 则内部计算
        period: ATR 周期（默认10，比传统14更灵敏）
        multiplier: ATR 乘数（默认3.0）

    Returns:
        dict:
            'st_line':     SuperTrend 趋势线序列
            'direction':   方向序列 (1=多头/up, -1=空头/down)
            'trend':       趋势文字 ('up'/'down')
            'upper_band':  上轨
            'lower_band':  下轨
            'vwap':        VWAP 序列（用于替代 HL/2 的量价加权基准线）
    """
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    c = pd.Series(closes, dtype=float)
    v = getattr(h, 'volume', None) or pd.Series(np.ones(len(c)), index=c.index)

    n = len(c)

    # ── 1. 计算 ATR ──
    if atr_val is not None and isinstance(atr_val, (int, float)):
        atr_series = pd.Series(float(atr_val), index=c.index)
    else:
        if atr_val is not None:
            atr_series = pd.Series(atr_val)
        else:
            tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
            atr_series = tr.ewm(alpha=1.0 / period, adjust=False).mean()

    # ── 2. VWAP（量价加权中位线）替代传统 HL/2 ──
    typical_price = (h + l + c) / 3
    tp_vol = typical_price * v
    vwap = tp_vol.cumsum() / v.cumsum().replace(0, 1e-9)

    # ── 3. 基本带（VWAP 加权）──
    basic_band = vwap
    upper_band = basic_band - multiplier * atr_series
    lower_band = basic_band + multiplier * atr_series

    # ── 4. SuperTrend 核心逻辑（只进不退原则）──
    st_line = pd.Series(np.nan, index=c.index)
    direction = pd.Series(-1, index=c.index)  # 初始假设空头

    for i in range(n):
        if i == 0:
            st_line.iloc[i] = upper_band.iloc[i]
            direction.iloc[i] = -1
            continue

        prev_st = st_line.iloc[i - 1]
        prev_dir = direction.iloc[i - 1]

        if prev_dir == -1:  # 当前处于下跌趋势
            if c.iloc[i] > prev_st:  # 收盘价突破上轨 → 翻多
                direction.iloc[i] = 1
                st_line.iloc[i] = lower_band.iloc[i]  # 切换到下轨跟踪
            else:
                direction.iloc[i] = -1
                # 只能向下走（取 min）
                st_line.iloc[i] = min(upper_band.iloc[i], prev_st)
        else:  # 当前处于上涨趋势
            if c.iloc[i] < prev_st:  # 收盘价跌破下轨 → 翻空
                direction.iloc[i] = -1
                st_line.iloc[i] = upper_band.iloc[i]  # 切换到上轨跟踪
            else:
                direction.iloc[i] = 1
                # 只能向上走（取 max）
                st_line.iloc[i] = max(lower_band.iloc[i], prev_st)

    trend = direction.map({1: 'up', -1: 'down'})

    return {
        'st_line': st_line,
        'direction': direction,
        'trend': trend,
        'upper_band': upper_band,
        'lower_band': lower_band,
        'vwap': vwap,
    }


# ============================================================
# 综合评估：将所有辅助指标汇总为单一评分调整
# ============================================================

def evaluate_auxiliary(
    df: pd.DataFrame,
    signal_direction: str,     # "long" 或 "short"
    config_weights: dict = None,
) -> AuxiliaryResult:
    """
    基于所有辅助指标，给出综合评分调整

    Args:
        df: K线 DataFrame (至少含 open/high/low/close/volume)
        signal_direction: 主信号的交易方向
        config_weights: 配置中的 aux_weights 参数

    Returns:
        AuxiliaryResult 包含各指标值和综合评分调整
    """
    result = AuxiliaryResult()

    # 默认权重
    w = {
        "rsi_extreme": 5,
        "ema_alignment": 5,
        "obv_confirm": 5,
        "adx_trend": 5,
        "macd_line_divergence": 10,
    }
    if config_weights:
        w.update(config_weights)

    adjustment = 0.0
    details = {}

    try:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        vol = df.get("volume", pd.Series([0]*len(df))).values

        # === 1. RSI 超买超卖 ===
        rsi_series = calc_rsi(close)
        rsi_latest = rsi_series.iloc[-1] if not rsi_series.empty else 50.0
        result.rsi_14 = float(rsi_latest)

        if signal_direction == "long":
            if rsi_latest < 30:
                adjustment += w["rsi_extreme"]
                details["rsi"] = f"RSI={rsi_latest:.1f}(超卖) +{w['rsi_extreme']}"
            elif rsi_latest < 45:
                adjustment += w["rsi_extreme"] * 0.5
                details["rsi"] = f"RSI={rsi_latest:.1f}(偏低) +{int(w['rsi_extreme']*0.5)}"
            elif rsi_latest > 70:
                adjustment -= w["rsi_extreme"]
                details["rsi"] = f"RSI={rsi_latest:.1f}(超买) -{w['rsi_extreme']}"
            else:
                details["rsi"] = f"RSI={rsi_latest:.1f}(中性) 0"
        else:  # short
            if rsi_latest > 70:
                adjustment += w["rsi_extreme"]
                details["rsi"] = f"RSI={rsi_latest:.1f}(超买) +{w['rsi_extreme']}"
            elif rsi_latest > 55:
                adjustment += w["rsi_extreme"] * 0.5
                details["rsi"] = f"RSI={rsi_latest:.1f}(偏高) +{int(w['rsi_extreme']*0.5)}"
            elif rsi_latest < 30:
                adjustment -= w["rsi_extreme"]
                details["rsi"] = f"RSI={rsi_latest:.1f}(超卖) -{w['rsi_extreme']}"
            else:
                details["rsi"] = f"RSI={rsi_latest:.1f}(中性) 0"

        # === 2. EMA 排列 ===
        ema9 = calc_ema(close, 9)
        ema21 = calc_ema(close, 21)
        ema55 = calc_ema(close, 55)

        latest_ema9 = float(ema9.iloc[-1]) if not ema9.empty else 0
        latest_ema21 = float(ema21.iloc[-1]) if not ema21.empty else 0
        latest_ema55 = float(ema55.iloc[-1]) if not ema55.empty else 0

        result.ema_9 = latest_ema9
        result.ema_21 = latest_ema21
        result.ema_55 = latest_ema55

        ema_aligned_long = latest_ema9 > latest_ema21 > latest_ema55
        ema_aligned_short = latest_ema9 < latest_ema21 < latest_ema55

        if signal_direction == "long" and ema_aligned_long:
            adjustment += w["ema_alignment"]
            details["ema"] = f"EMA多头排列 +{w['ema_alignment']}"
        elif signal_direction == "short" and ema_aligned_short:
            adjustment += w["ema_alignment"]
            details["ema"] = f"EMA空头排列 +{w['ema_alignment']}"
        elif signal_direction == "long" and ema_aligned_short:
            adjustment -= w["ema_alignment"]
            details["ema"] = f"EMA空头排列(矛盾!) -{w['ema_alignment']}"
        elif signal_direction == "short" and ema_aligned_long:
            adjustment -= w["ema_alignment"]
            details["ema"] = f"EMA多头排列(矛盾!) -{w['ema_alignment']}"
        else:
            details["ema"] = "EMA纠缠 0"

        # === 3. ADX 趋势强度 ===
        adx_series = calc_adx(high, low, close)
        adx_latest = float(adx_series.iloc[-1]) if not adx_series.empty else 20.0
        result.adx_14 = adx_latest

        if adx_latest < 20:
            adjustment -= w["adx_trend"] * 2  # 无趋势时大幅减分
            details["adx"] = f"ADX={adx_latest:.1f}(无趋势) -{w['adx_trend']*2}"
        elif adx_latest >= 25:
            adjustment += w["adx_trend"]
            details["adx"] = f"ADX={adx_latest:.1f}(明确趋势) +{w['adx_trend']}"
        else:
            details["adx"] = f"ADX={adx_latest:.1f}(弱趋势) 0"

        # === 4. Stochastic ===
        stoch_k, stoch_d = calc_stochastic(high, low, close)
        result.stoch_k = float(stoch_k.iloc[-1]) if not stoch_k.empty else 50
        result.stoch_d = float(stoch_d.iloc[-1]) if not stoch_d.empty else 50

        # === 5. 布林带位置 ===
        bb_u, bb_m, bb_l = calc_bollinger_bands(close)
        result.bb_upper = float(bb_u.iloc[-1]) if not bb_u.empty else 0
        result.bb_middle = float(bb_m.iloc[-1]) if not bb_m.empty else 0
        result.bb_lower = float(bb_l.iloc[-1]) if not bb_l.empty else 0

        current_price = float(pd.Series(close).iloc[-1])

        if signal_direction == "long":
            if current_price <= result.bb_lower:
                details["bb"] = "价格触及下轨(强支撑) +3"
                adjustment += 3
            elif current_price < result.bb_middle:
                details["bb"] = "价格在下轨与中轨之间 +1"
                adjustment += 1
            else:
                details["bb"] = "价格在中轨上方 0"
        else:
            if current_price >= result.bb_upper:
                details["bb"] = "价格触及上轨(强阻力) +3"
                adjustment += 3
            elif current_price > result.bb_middle:
                details["bb"] = "价格在中轨与上轨之间 +1"
                adjustment += 1
            else:
                details["bb"] = "价格在中轨下方 0"

        # === 6. ATR（只记录，不参与打分，用于仓位管理）===
        atr_series = calc_atr(high, low, close)
        result.atr_14 = float(atr_series.iloc[-1]) if not atr_series.empty else 0

        # === 7. OBV ===
        obv_series = calc_obv(close, vol)
        if len(obv_series) > 10:
            recent_obv_slope = obv_series.iloc[-5] - obv_series.iloc[-10]
            if recent_obv_slope > 0:
                result.obv_trend = "up"
                if signal_direction == "long":
                    adjustment += w["obv_confirm"] * 0.5
                    details["obv"] = "OBV上升 +2"
                else:
                    details["obv"] = "OBV上升(反向) 0"
            elif recent_obv_slope < 0:
                result.obv_trend = "down"
                if signal_direction == "short":
                    adjustment += w["obv_confirm"] * 0.5
                    details["obv"] = "OBV下降 +2"
                else:
                    details["obv"] = "OBV下降(反向) 0"
            else:
                details["obv"] = "OBV持平 0"
        else:
            details["obv"] = "OBV数据不足 0"

        # === 8. VWAP（如果有多日数据才有意义）===
        if len(vol) > 30:
            vwap_series = calc_vwap(high, low, close, vol)
            result.vwap = float(vwap_series.iloc[-1])

            if signal_direction == "long":
                if current_price > result.vwap:
                    details["vwap"] = f"价>VWAP +1"
                    adjustment += 1
                else:
                    details["vwap"] = f"价<VWAP 0"
            else:
                if current_price < result.vwap:
                    details["vwap"] = f"价<VWAP +1"
                    adjustment += 1
                else:
                    details["vwap"] = f"价>VWAP 0"
        else:
            details["vwap"] = "数据不足"

    except Exception as e:
        logger.error(f"辅助指标计算异常: {e}")
        details["error"] = str(e)

    # 限制调整范围在 ±20
    result.score_adjustment = max(-20, min(20, adjustment))
    result.details = details

    logger.debug(
        f"辅助指标评估完成: 方向={signal_direction}, "
        f"调整={result.score_adjustment:+.1f}, "
        f"详情={details}"
    )

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("=" * 60)
    print("辅助指标计算模块 — 自检")
    print("=" * 60)

    # 模拟数据
    np.random.seed(42)
    n = 200
    t = pd.date_range("2025-06-01", periods=n, freq="4h")
    base = 85000
    trend = np.linspace(0, 3000, n)
    noise = np.random.randn(n) * 400
    close = base + trend + noise
    high = close + abs(np.random.randn(n) * 150)
    low = close - abs(np.random.randn(n) * 150)
    volume = np.random.uniform(100, 10000, n)

    df = pd.DataFrame({
        "timestamp": t,
        "open": close - noise * 0.3,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })

    result = evaluate_auxiliary(df, signal_direction="long")

    print(f"\n📊 辅助指标结果:")
    print(f"  EMA(9/21/55): {result.ema_9:.1f} / {result.ema_21:.1f} / {result.ema_55:.1f}")
    print(f"  RSI(14):      {result.rsi_14:.1f}")
    print(f"  ADX(14):      {result.adx_14:.1f}")
    print(f"  Stoch(K/D):   {result.stoch_k:.1f} / {result.stoch_d:.1f}")
    print(f"  BB:           [{result.bb_lower:.1f}, {result.bb_middle:.1f}, {result.bb_upper:.1f}]")
    print(f"  ATR(14):      {result.atr_14:.1f}")
    print(f"  OBV 趋向:     {result.obv_trend}")
    print(f"  VWAP:         {result.vwap:.1f}")
    print(f"\n  ⚡ 综合调整分: {result.score_adjustment:+.1f}")
    print(f"\n  详细评分:")
    for key, val in result.details.items():
        print(f"    {key}: {val}")
