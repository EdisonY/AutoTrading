"""
半木夏第二套策略：量价突破系统（Volume-Price Breakout, VPB）

核心逻辑：
  1. 【量能异动】当前成交量 >= 过去N根的均量 X 倍 → 大资金入场信号
  2. 【价格突破】收盘价突破近期高/低点（N根K线的最高/最低价）
  3. 【K线形态】吞噬/锤子/启明星等反转/持续形态加分
  4. 【资金费率过滤】资金费率极端时（>0.1%），逆向布局

与半木夏策略的区别：
  - 半木夏侧重趋势指标（MACD/EMA/ADX/ST），适合趋势行情
  - VPB 侧重量能和突破，适合震荡后启动、缩量收敛后爆发
  - 两套策略互补：半木夏信号+VPB信号同方向，可视为超强共振

评分系统（满分100）：
  - 量能异动（20-40分）：2倍均量+20, 3倍+30, 5倍+40（v8: 5x+封顶，极端放量不加分）
  - 价格突破（25分）：突破N根高/低点
  - K线形态（0-20分）：吞噬+20, 锤子/流星+15, 十字星+10
  - 资金费率（0-15分）：极端资金费率逆向布局
  - 均线方向（0-10分）：突破方向与EMA方向一致
  - 成交量持续（5分）：量能连续放大
  
  阈值：VPB 5m >= 80 / 15m >= 60（v8: 5m降权，噪声大需更高分数）

作者：半木夏量化系统 v8
"""

import numpy as np
import pandas as pd
from typing import Optional
import json
import urllib.error
import urllib.request

from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request


# ═══════════════════════════════════════════════════════════════
# VPB 策略参数
# ═══════════════════════════════════════════════════════════════
VPB_SCORE_THRESHOLD_5M = 80    # VPB 5m评分阈值（v8: 60→80，5m噪声大降权）
VPB_SCORE_THRESHOLD_15M = 70   # v11优化(2026-05-15): 60→70，减少低质量VPB信号
VPB_VOL_WINDOW = 20           # 均量计算窗口（根K线）
VPB_BREAKOUT_WINDOW = 20      # 突破判断窗口（近N根的最高/最低价）
VPB_VOL_MULT_MAX = 5.0        # 最高成交量倍数上限（v8: 5x+胜率仅25%，过滤极端放量）
VPB_VOL_MULT_MIN = 2.0        # 最低成交量倍数（小于此值不触发量能信号）
VPB_SL_MULT = 1.2             # VPB止损：1.2×ATR（v7: 1.5→1.2，更紧止损，突破失败就走）
VPB_TP_MULT = 4.5             # VPB止盈：4.5×ATR（v7: 4.0→4.5，放宽止盈让利润跑）


def fetch_json(url: str, timeout: int = 10) -> dict:
    if api_queue_client_enabled():
        data = queued_api_request(scope="public", label="strategy-breakout", method="GET", path=url, url=url, timeout_sec=timeout + 5)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("strategy-breakout", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        record_public_response("strategy-breakout", url, exc.code, body)
        raise


def fetch_funding_rate(symbol: str) -> float:
    """获取资金费率（Binance Testnet）。失败返回0"""
    try:
        url = f"https://testnet.binancefuture.com/fapi/v1/premiumIndex?symbol={symbol}"
        data = fetch_json(url)
        return float(data.get("lastFundingRate", 0))
    except Exception:
        return 0.0


def analyze_candle_pattern(opens: np.ndarray, closes: np.ndarray,
                            highs: np.ndarray, lows: np.ndarray) -> dict:
    """
    检测最近3根K线的形态：
    - 吞噬形态（Engulfing）：强反转信号
    - 锤子线/流星线（Hammer/Shooting Star）：单根反转
    - 十字星（Doji）：变盘信号
    - 三根阳线/三根阴线（Three White Soldiers / Three Black Crows）：趋势延续
    返回 {pattern, score, direction}
    """
    if len(closes) < 3:
        return {"pattern": "无", "score": 0, "direction": 0}

    c1, c2, c3 = closes[-3], closes[-2], closes[-1]
    o1, o2, o3 = opens[-3], opens[-2], opens[-1]
    h1, h2, h3 = highs[-3], highs[-2], highs[-1]
    l1, l2, l3 = lows[-3], lows[-2], lows[-1]

    body2 = abs(c2 - o2)
    body3 = abs(c3 - o3)
    range2 = h2 - l2 + 1e-9
    range3 = h3 - l3 + 1e-9

    # 1. 看涨吞噬：前一根阴线，当前根阳线完全覆盖
    if c2 < o2 and c3 > o3 and o3 <= c2 and c3 >= o2:
        return {"pattern": "看涨吞噬", "score": 20, "direction": 1}

    # 2. 看跌吞噬：前一根阳线，当前根阴线完全覆盖
    if c2 > o2 and c3 < o3 and o3 >= c2 and c3 <= o2:
        return {"pattern": "看跌吞噬", "score": 20, "direction": -1}

    # 3. 锤子线：下影线 >= 2×实体，上影线短，实体在上方 → 看涨
    lower_shadow3 = min(o3, c3) - l3
    upper_shadow3 = h3 - max(o3, c3)
    if body3 > 0 and lower_shadow3 >= 2 * body3 and upper_shadow3 <= 0.3 * body3:
        return {"pattern": "锤子线", "score": 15, "direction": 1}

    # 4. 流星线：上影线 >= 2×实体，下影线短，实体在下方 → 看跌
    if body3 > 0 and upper_shadow3 >= 2 * body3 and lower_shadow3 <= 0.3 * body3:
        return {"pattern": "流星线", "score": 15, "direction": -1}

    # 5. 十字星：实体极小（< 10% range）
    if body3 < 0.1 * range3:
        return {"pattern": "十字星", "score": 10, "direction": 0}

    # 6. 三根阳线（Three White Soldiers）
    if c3 > o3 and c2 > o2 and c1 > o1 and c3 > c2 > c1:
        return {"pattern": "三根阳线", "score": 12, "direction": 1}

    # 7. 三根阴线（Three Black Crows）
    if c3 < o3 and c2 < o2 and c1 < o1 and c3 < c2 < c1:
        return {"pattern": "三根阴线", "score": 12, "direction": -1}

    return {"pattern": "无", "score": 0, "direction": 0}


def analyze_vpb(symbol: str, klines: list, bar: str = "5m") -> Optional[dict]:
    """
    VPB 策略核心分析函数。
    
    参数:
        symbol: 交易对
        klines: fetch_klines 返回的原始K线数据
        bar: 时间周期
    
    返回:
        {
            "symbol", "price", "atr", "vpb_score", "trade_side",
            "reasons", "sl_long", "sl_short", "tp_long", "tp_short",
            "can_trade", "vol_mult", "pattern", "funding_rate",
            "breakout_type", "timeframe"
        }
        或 None（数据不足/不满足条件）
    """
    if len(klines) < VPB_BREAKOUT_WINDOW + 5:
        return None

    closes = np.array([float(r[4]) for r in klines])
    highs  = np.array([float(r[2]) for r in klines])
    lows   = np.array([float(r[3]) for r in klines])
    opens  = np.array([float(r[1]) for r in klines])
    vols   = np.array([float(r[5]) for r in klines])

    price = float(closes[-1])
    if price <= 0:
        return None

    # ── ATR ──
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    atr_val = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
    if atr_val <= 0:
        return None

    # ── 均量 ──
    avg_vol = float(np.mean(vols[-VPB_VOL_WINDOW-1:-1]))  # 用前N根（不含当前）
    cur_vol = float(vols[-1])
    if avg_vol <= 0:
        return None
    vol_mult = cur_vol / avg_vol

    # ── 价格突破 ──
    recent_high = float(np.max(highs[-VPB_BREAKOUT_WINDOW-1:-1]))  # 前N根最高价
    recent_low  = float(np.min(lows[-VPB_BREAKOUT_WINDOW-1:-1]))   # 前N根最低价

    breakout_up   = price > recent_high   # 向上突破
    breakout_down = price < recent_low    # 向下突破

    # ── EMA 方向 ──
    ema20 = float(pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1])
    ema_up   = price > ema20    # 价格在EMA上方
    ema_down = price < ema20    # 价格在EMA下方

    # ── K线形态 ──
    pattern_info = analyze_candle_pattern(opens, closes, highs, lows)

    # ── 资金费率 ──
    funding_rate = fetch_funding_rate(symbol)

    # ═══ VPB 打分 ═══
    score_long = 0.0
    score_short = 0.0
    reasons_long = []
    reasons_short = []

    # 1. 量能异动（核心条件）
    # v8: 5x+胜率仅25%，极端放量不可靠，设上限过滤
    vol_mult_capped = min(vol_mult, VPB_VOL_MULT_MAX)  # 超过5x按5x计分，极端放量不加分
    if vol_mult_capped >= VPB_VOL_MULT_MIN:
        if vol_mult_capped >= 5:
            vol_score = 40
            vol_desc = f"量能5倍异动({vol_mult:.1f}x{'封顶' if vol_mult > VPB_VOL_MULT_MAX else ''})"
        elif vol_mult_capped >= 3:
            vol_score = 30
            vol_desc = f"量能3倍异动({vol_mult:.1f}x)"
        else:
            vol_score = 20
            vol_desc = f"量能2倍异动({vol_mult:.1f}x)"
        # 成交量本身不区分方向，配合突破方向加分
        if breakout_up or (not breakout_down and ema_up):
            score_long += vol_score
            reasons_long.append(vol_desc)
        if breakout_down or (not breakout_up and ema_down):
            score_short += vol_score
            reasons_short.append(vol_desc)

    # 2. 价格突破（方向性信号）
    if breakout_up:
        score_long += 25
        reasons_long.append(f"突破{VPB_BREAKOUT_WINDOW}根高点{recent_high:.4f}")
    elif breakout_down:
        score_short += 25
        reasons_short.append(f"跌破{VPB_BREAKOUT_WINDOW}根低点{recent_low:.4f}")

    # 3. K线形态
    pat = pattern_info
    if pat["direction"] == 1 and pat["score"] > 0:
        score_long += pat["score"]
        reasons_long.append(pat["pattern"])
    elif pat["direction"] == -1 and pat["score"] > 0:
        score_short += pat["score"]
        reasons_short.append(pat["pattern"])
    elif pat["direction"] == 0 and pat["score"] > 0:
        # 十字星：双向加分（变盘）
        score_long  += pat["score"] * 0.5
        score_short += pat["score"] * 0.5

    # 4. 资金费率（极端时逆向布局）
    # 资金费率 > 0: 多方支付，说明市场偏多 → 极端时偏空
    # 资金费率 < 0: 空方支付，说明市场偏空 → 极端时偏多
    if funding_rate > 0.001:    # > 0.1%，极端偏多 → 看空
        fr_score = min(15, funding_rate * 10000)
        score_short += fr_score
        reasons_short.append(f"资金费率极高{funding_rate*100:.3f}%")
    elif funding_rate < -0.001:  # < -0.1%，极端偏空 → 看多
        fr_score = min(15, abs(funding_rate) * 10000)
        score_long += fr_score
        reasons_long.append(f"资金费率极低{funding_rate*100:.3f}%")

    # 5. EMA 方向一致性加分
    if ema_up and score_long > score_short:
        score_long += 10
        reasons_long.append("EMA多头区域")
    elif ema_down and score_short > score_long:
        score_short += 10
        reasons_short.append("EMA空头区域")

    # 6. 成交量连续放大（连续2根K线都超均量）
    if len(vols) >= 3:
        prev_vol = float(vols[-2])
        if prev_vol > avg_vol * 1.5 and cur_vol > avg_vol * 1.5:
            if score_long >= score_short:
                score_long += 5
                reasons_long.append("量能持续放大")
            else:
                score_short += 5
                reasons_short.append("量能持续放大")

    # ═══ 综合判定 ═══
    vpb_score = score_long - score_short  # 正=看多，负=看空

    # 核心过滤：必须有量能异动（没有量就不是VPB信号）
    if vol_mult < VPB_VOL_MULT_MIN:
        return None

    # 必须有突破或形态（纯量能不代表方向）
    if not breakout_up and not breakout_down and pat["direction"] == 0:
        return None

    # 高波动过滤：ATR/Price > 5% 跳过（与半木夏策略一致，避免止盈止损价格计算异常）
    atr_pct = atr_val / price if price > 0 else 0
    if atr_pct > 0.05:
        return None

    # 阈值过滤（v10: 15m阈值60，30m阈值55）
    threshold_map = {"5m": 80, "15m": VPB_SCORE_THRESHOLD_15M, "30m": 55}
    threshold = threshold_map.get(bar, VPB_SCORE_THRESHOLD_15M)
    if abs(vpb_score) < threshold:
        return None

    trade_side = "long" if vpb_score > 0 else "short"
    reasons = reasons_long if trade_side == "long" else reasons_short

    # 止损止盈（VPB比半木夏策略更紧，快进快出）
    sl_long  = round(price - atr_val * VPB_SL_MULT, 6)
    sl_short = round(price + atr_val * VPB_SL_MULT, 6)
    tp_long  = round(price + atr_val * VPB_TP_MULT, 6)
    tp_short = round(price - atr_val * VPB_TP_MULT, 6)

    # 止盈止损价格合理性校验：确保价格在合理范围内
    if trade_side == "long":
        if sl_long <= 0 or sl_long >= price:
            return None  # 多单止损价异常
        if tp_long <= price:
            return None  # 多单止盈价应在开仓价之上
    else:
        if sl_short <= price:
            return None  # 空单止损价应在开仓价之上
        if tp_short <= 0 or tp_short >= price:
            return None  # 空单止盈价应在开仓价之下

    return {
        "symbol": symbol,
        "price": price,
        "atr": round(atr_val, 6),
        "vpb_score": round(vpb_score, 1),
        "net_score": round(vpb_score, 1),    # 兼容scanner接口
        "trade_side": trade_side,
        "reasons": reasons,
        "reasons_long": reasons_long,
        "reasons_short": reasons_short,
        "sl_long": sl_long,
        "sl_short": sl_short,
        "tp_long": tp_long,
        "tp_short": tp_short,
        "can_trade": True,
        "vol_mult": round(vol_mult, 2),
        "pattern": pat["pattern"],
        "funding_rate": funding_rate,
        "breakout_type": "up" if breakout_up else ("down" if breakout_down else "none"),
        "timeframe": bar,
        "strategy": "VPB",
        # 兼容 scanner._open_position 所需字段
        "divergence_primary": {"description": f"VPB:{pat['pattern']}", "entry_signal": False},
        "st_direction": 1 if trade_side == "long" else -1,
        "st_flipped": breakout_up or breakout_down,
        "score_long": round(score_long, 1),
        "score_short": round(score_short, 1),
        "rsi": 50.0,
        "adx": 0.0,
    }


# ═══════════════════════════════════════════════════════════════
# 给 Scanner 调用的入口函数
# ═══════════════════════════════════════════════════════════════
def analyze_symbol_vpb(symbol: str, klines_15m: list, klines_30m: list) -> list[dict]:
    """
    对单个币种运行 VPB 分析，返回所有有效信号列表。
    v10: 同时分析 15m 和 30m，双周期共振时在信号上打标记。
    """
    signals = []

    sig_15m = analyze_vpb(symbol, klines_15m, "15m")
    sig_30m = analyze_vpb(symbol, klines_30m, "30m")

    if sig_15m and sig_30m and sig_15m["trade_side"] == sig_30m["trade_side"]:
        # 双周期共振：两个信号都有效，标记共振
        sig_15m["vpb_resonance"] = True
        sig_30m["vpb_resonance"] = True
        sig_15m["vpb_score"] += 15
        sig_15m["net_score"] += 15 if sig_15m["trade_side"] == "long" else -15
        sig_30m["vpb_score"] += 15
        sig_30m["net_score"] += 15 if sig_30m["trade_side"] == "long" else -15
        sig_15m["reasons"].append("⚡VPB双周期共振")
        sig_30m["reasons"].append("⚡VPB双周期共振")
    elif sig_15m:
        sig_15m["vpb_resonance"] = False
    elif sig_30m:
        sig_30m["vpb_resonance"] = False

    if sig_15m:
        signals.append(sig_15m)
    if sig_30m:
        signals.append(sig_30m)

    return signals
