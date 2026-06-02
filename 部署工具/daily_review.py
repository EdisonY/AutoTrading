"""
每日自动复盘脚本 v2（daily_review.py）

功能：
  1. 盈亏统计：总盈亏、胜率、平均盈亏比、最大回撤
  2. 开单条件展示：每笔开仓的评分构成 + 开单原因拆解
  3. 盈利单 vs 亏损单深度对比分析（找规律）
  4. 前100市值币未开仓原因回测（K线回溯 + VPB评分）
  5. 策略不足总结 + 优化建议

用法：
    python daily_review.py                     # 复盘昨日
    python daily_review.py --date 2026-04-25   # 复盘指定日期
    python daily_review.py --local             # 使用本地已拉取日志（不重拉）

输出：
    reports/review_YYYY-MM-DD.md
"""

import json
import time
import argparse
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# Windows GBK编码修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from cloud.analyzer.auxiliary import (
    calc_rsi, calc_ema, calc_adx, calc_atr, calc_supertrend,
)
from strategy_breakout import analyze_vpb

import urllib.request
import urllib.error

from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request

CST = timezone(timedelta(hours=8))
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Binance 实盘行情
BINANCE_LIVE = "https://fapi.binance.com"
# 本地已拉取的日志目录
LOCAL_LOGS = ROOT / "server_logs"


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def fetch_json(url: str, timeout: int = 15) -> any:
    if api_queue_client_enabled():
        data = queued_api_request(scope="public", label="daily-review", method="GET", path=url, url=url, timeout_sec=timeout + 5)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("daily-review", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        record_public_response("daily-review", url, exc.code, body)
        raise


def fetch_top100_symbols() -> list:
    """拉取 Binance 永续合约 Top100（按24h成交额）"""
    raw = fetch_json(f"{BINANCE_LIVE}/fapi/v1/ticker/24hr")
    usdt_swaps = []
    for it in raw:
        sym = it.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            vol = float(it.get("quoteVolume", 0) or 0)
            price_change_pct = float(it.get("priceChangePercent", 0) or 0)
            last_price = float(it.get("lastPrice", 0) or 0)
            high_price = float(it.get("highPrice", 0) or 0)
            low_price = float(it.get("lowPrice", 0) or 0)
        except Exception:
            continue
        usdt_swaps.append({
            "symbol": sym,
            "volume_usdt": vol,
            "price_change_pct": price_change_pct,
            "last_price": last_price,
            "high_price": high_price,
            "low_price": low_price,
        })
    usdt_swaps.sort(key=lambda x: x["volume_usdt"], reverse=True)
    return usdt_swaps[:100]


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 500) -> list:
    url = (f"{BINANCE_LIVE}/fapi/v1/klines?"
           f"symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit={limit}")
    try:
        raw = fetch_json(url)
        return [[str(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in raw]
    except Exception:
        return []


def fetch_klines_for_backtest(symbol: str, date_str: str, bar: str = "15m") -> list:
    """拉取指定日期的K线用于回测（含前2天历史，保证指标有足够数据）"""
    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ms = int((start_dt - timedelta(days=2)).timestamp() * 1000)
        end_ms = int((start_dt + timedelta(days=1)).timestamp() * 1000)
        limits = {"5m": 864, "15m": 288, "1h": 72}
        return fetch_klines(symbol, bar, start_ms, end_ms, limits.get(bar, 288))
    except Exception:
        return []


def load_events(date_str: str, use_local: bool) -> list:
    """加载指定日期的开平仓事件"""
    paths = []
    if use_local:
        paths.append(LOCAL_LOGS / "scanner_data" / "events.jsonl")
    paths.append(ROOT / "scanner_data" / "events.jsonl")

    for p in paths:
        if p.exists():
            result = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line.strip())
                        if date_str in e.get("time", ""):
                            result.append(e)
                    except:
                        pass
            if result:
                return result
    return []


def load_signals(date_str: str, use_local: bool) -> list:
    """加载信号日志"""
    paths = []
    if use_local:
        paths.append(LOCAL_LOGS / "logs" / "signals.jsonl")
    paths.append(ROOT / "logs" / "signals.jsonl")

    for p in paths:
        if p.exists():
            result = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        s = json.loads(line.strip())
                        if date_str in s.get("ts", ""):
                            result.append(s)
                    except:
                        pass
            if result:
                return result
    return []


# ─────────────────────────────────────────────
# 分析函数
# ─────────────────────────────────────────────

def calc_pnl_stats(events: list) -> dict:
    """计算盈亏统计"""
    closes = [e for e in events if e.get("event") == "CLOSE"]
    opens  = [e for e in events if e.get("event") == "OPEN"]
    failed = [e for e in events if e.get("event") == "OPEN_FAILED"]

    if not closes:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_pnl_pct": 0, "total_pnl_usd": 0,
            "avg_win_pct": 0, "avg_loss_pct": 0, "profit_factor": 0,
            "max_win": None, "max_loss": None,
            "open_count": len(opens), "failed_count": len(failed),
            "by_reason": {}, "by_timeframe": {}, "by_strategy": {},
        }

    wins   = [c for c in closes if c.get("pnl_pct", 0) > 0]
    losses = [c for c in closes if c.get("pnl_pct", 0) <= 0]

    total_pnl_usd = sum(c.get("pnl_usd", 0) for c in closes)
    total_pnl_pct = sum(c.get("pnl_pct", 0) for c in closes)
    avg_win_pct   = sum(c["pnl_pct"] for c in wins) / len(wins) if wins else 0
    avg_loss_pct  = sum(c["pnl_pct"] for c in losses) / len(losses) if losses else 0
    total_win_usd = sum(c.get("pnl_usd", 0) for c in wins)
    total_loss_usd = abs(sum(c.get("pnl_usd", 0) for c in losses))
    profit_factor  = total_win_usd / total_loss_usd if total_loss_usd > 0 else 999

    max_win  = max(closes, key=lambda c: c.get("pnl_pct", 0)) if wins else None
    max_loss = min(closes, key=lambda c: c.get("pnl_pct", 0)) if losses else None

    # 按平仓原因分组
    by_reason = defaultdict(lambda: {"count": 0, "pnl_usd": 0.0})
    for c in closes:
        reason = c.get("reason", "未知")
        by_reason[reason]["count"] += 1
        by_reason[reason]["pnl_usd"] += c.get("pnl_usd", 0)

    # 按周期分组
    by_timeframe = defaultdict(lambda: {"count": 0, "wins": 0, "pnl_usd": 0.0})
    for c in closes:
        tf = c.get("timeframe", "?")
        by_timeframe[tf]["count"] += 1
        by_timeframe[tf]["pnl_usd"] += c.get("pnl_usd", 0)
        if c.get("pnl_pct", 0) > 0:
            by_timeframe[tf]["wins"] += 1

    return {
        "total_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closes) * 100,
        "total_pnl_pct": total_pnl_pct,
        "total_pnl_usd": total_pnl_usd,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "profit_factor": profit_factor,
        "max_win": max_win,
        "max_loss": max_loss,
        "open_count": len(opens),
        "failed_count": len(failed),
        "by_reason": dict(by_reason),
        "by_timeframe": dict(by_timeframe),
        "all_closes": closes,
        "all_opens": opens,
        "all_failed": failed,
    }


def analyze_trade_patterns(closes: list) -> dict:
    """深度分析盈利单和亏损单的共同特征"""
    wins   = [c for c in closes if c.get("pnl_pct", 0) > 0]
    losses = [c for c in closes if c.get("pnl_pct", 0) <= 0]

    def avg_score(items):
        scores = [abs(i.get("score", 0)) for i in items if i.get("score")]
        return sum(scores) / len(scores) if scores else 0

    def resonance_rate(items):
        return sum(1 for i in items if i.get("resonance")) / max(len(items), 1) * 100

    def side_dist(items):
        longs  = sum(1 for i in items if i.get("side") == "long")
        shorts = sum(1 for i in items if i.get("side") == "short")
        return longs, shorts

    def hold_time(items):
        """计算平均持仓时间（分钟）"""
        durations = []
        for c in items:
            entry_t = c.get("entry_time", "")
            close_t = c.get("time", "")
            try:
                t1 = datetime.strptime(entry_t[:19], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(close_t[:19], "%Y-%m-%d %H:%M:%S")
                durations.append((t2 - t1).total_seconds() / 60)
            except:
                pass
        return sum(durations) / len(durations) if durations else 0

    w_longs, w_shorts = side_dist(wins)
    l_longs, l_shorts = side_dist(losses)

    return {
        "win_avg_score": avg_score(wins),
        "loss_avg_score": avg_score(losses),
        "win_resonance_rate": resonance_rate(wins),
        "loss_resonance_rate": resonance_rate(losses),
        "win_longs": w_longs, "win_shorts": w_shorts,
        "loss_longs": l_longs, "loss_shorts": l_shorts,
        "win_avg_hold_min": hold_time(wins),
        "loss_avg_hold_min": hold_time(losses),
    }


def analyze_missed_reason(symbol: str, date_str: str, price_change_pct: float) -> dict:
    """回测K线，分析为什么没有开仓"""
    result = {
        "max_hm_score": 0,
        "best_tf": "",
        "best_time": "",
        "best_side": "",
        "vpb_triggered": False,
        "vpb_score": 0,
        "vpb_side": "",
        "miss_reason": "信号不足",
        "opportunity_level": "D",
    }

    direction = "long" if price_change_pct > 0 else "short"

    for bar in ["15m", "5m"]:
        try:
            klines = fetch_klines_for_backtest(symbol, date_str, bar)
            if len(klines) < 50:
                continue

            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = start_ms + 24 * 3600 * 1000

            best_score = 0
            best_idx = -1

            for i in range(50, len(klines)):
                ts = int(klines[i][0])
                if ts < start_ms or ts >= end_ms:
                    continue
                sub = klines[:i+1]
                closes_arr = np.array([float(r[4]) for r in sub])
                highs_arr  = np.array([float(r[2]) for r in sub])
                lows_arr   = np.array([float(r[3]) for r in sub])

                import pandas as pd
                ema13 = pd.Series(closes_arr).ewm(span=13, adjust=False).mean()
                ema34 = pd.Series(closes_arr).ewm(span=34, adjust=False).mean()
                hist  = ema13 - ema34 - (ema13 - ema34).ewm(span=9, adjust=False).mean()

                rsi_arr = calc_rsi(closes_arr)
                rsi_val = float(pd.Series(rsi_arr).dropna().iloc[-1]) if len(rsi_arr) > 0 else 50
                ema9  = float(pd.Series(calc_ema(closes_arr, 9)).dropna().iloc[-1])  if len(closes_arr) >= 9 else 0
                ema21 = float(pd.Series(calc_ema(closes_arr, 21)).dropna().iloc[-1]) if len(closes_arr) >= 21 else 0
                ema55 = float(pd.Series(calc_ema(closes_arr, 55)).dropna().iloc[-1]) if len(closes_arr) >= 55 else 0
                adx_arr = calc_adx(highs_arr, lows_arr, closes_arr)
                adx_val = float(pd.Series(adx_arr).dropna().iloc[-1]) if len(adx_arr) > 0 else 0

                score_dir = 0
                if direction == "long":
                    if ema13.iloc[-1] > ema34.iloc[-1] and float(hist.iloc[-1]) > 0:
                        score_dir += 20
                    if rsi_val < 45:
                        score_dir += 10
                    if ema9 > ema21 > ema55:
                        score_dir += 15
                    if adx_val >= 25:
                        score_dir += min(15, adx_val * 0.4)
                else:
                    if ema13.iloc[-1] < ema34.iloc[-1] and float(hist.iloc[-1]) < 0:
                        score_dir += 20
                    if rsi_val > 55:
                        score_dir += 10
                    if ema9 < ema21 < ema55:
                        score_dir += 15
                    if adx_val >= 25:
                        score_dir += min(15, adx_val * 0.4)

                if score_dir > best_score:
                    best_score = score_dir
                    best_idx = i

            if best_score > result["max_hm_score"]:
                result["max_hm_score"] = best_score
                result["best_tf"] = bar
                if best_idx >= 0:
                    ts_ms = int(klines[best_idx][0])
                    result["best_time"] = datetime.fromtimestamp(ts_ms / 1000, tz=CST).strftime("%H:%M")
                result["best_side"] = direction

            # VPB 回测
            day_klines = [k for k in klines if start_ms <= int(k[0]) < end_ms]
            if len(day_klines) >= 25:
                try:
                    vsig = analyze_vpb(symbol, day_klines, bar)
                    if vsig and abs(vsig.get("vpb_score", 0)) > result["vpb_score"]:
                        result["vpb_triggered"] = True
                        result["vpb_score"] = abs(vsig["vpb_score"])
                        result["vpb_side"] = vsig.get("trade_side", "")
                except Exception:
                    pass

        except Exception:
            pass
        time.sleep(0.05)

    # 综合评级
    abs_change = abs(price_change_pct)

    if result["max_hm_score"] >= 60:
        result["miss_reason"] = f"半木夏{result['max_hm_score']:.0f}分（有信号但未触发ST翻转）"
        result["opportunity_level"] = "B"
    elif result["max_hm_score"] >= 40:
        result["miss_reason"] = f"半木夏{result['max_hm_score']:.0f}分（指标方向不一致）"
        result["opportunity_level"] = "C"
    else:
        result["miss_reason"] = "震荡行情，指标无法提前识别"
        result["opportunity_level"] = "D"

    if result["vpb_triggered"] and result["vpb_score"] >= 60:
        side_str = "多" if result["vpb_side"] == "long" else "空"
        result["miss_reason"] += f" | VPB{result['vpb_score']:.0f}分{side_str}向"
        result["opportunity_level"] = "A" if abs_change >= 8 else "B"

    if abs_change >= 15:
        upgrade = {"D": "C", "C": "B", "B": "A", "A": "A"}
        result["opportunity_level"] = upgrade.get(result["opportunity_level"], "B")

    return result


# ─────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────

def build_report(date_str: str, stats: dict, patterns: dict,
                 symbols_data: list, analysis_results: list,
                 signals: list) -> str:
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines = []

    def h(text): lines.append(f"\n{text}\n")
    def hr(): lines.append("\n---\n")

    # ── 标题
    pnl_emoji = "✅" if stats["total_pnl_usd"] >= 0 else "❌"
    lines.append(f"# 📊 每日复盘报告 — {date_str}")
    lines.append(f"\n**生成时间**: {now_str}  |  **数据来源**: Binance 永续合约 Testnet\n")

    # ── 一、盈亏统计摘要
    hr()
    h("## 一、📈 盈亏统计")

    tc = stats["total_trades"]
    if tc == 0:
        lines.append("> ⚠️ 今日无已平仓交易记录\n")
    else:
        wr  = stats["win_rate"]
        pnl = stats["total_pnl_usd"]
        pf  = stats["profit_factor"]
        lines.append(f"| 指标 | 数值 | 说明 |")
        lines.append(f"|------|------|------|")
        lines.append(f"| 总交易笔数 | **{tc} 笔** | 已平仓 |")
        lines.append(f"| 胜率 | **{wr:.1f}%** ({stats['wins']}胜/{stats['losses']}负) | {'>50% 达标' if wr >= 50 else '<50% 需优化'} |")
        lines.append(f"| 总盈亏(USD) | **{pnl_emoji} ${pnl:+.2f}** | 含杠杆 |")
        lines.append(f"| 总盈亏(%) | **{stats['total_pnl_pct']:+.2f}%** | 各单累加 |")
        lines.append(f"| 平均盈利 | +{stats['avg_win_pct']:.2f}% | 盈利单均值 |")
        lines.append(f"| 平均亏损 | {stats['avg_loss_pct']:.2f}% | 亏损单均值 |")
        lines.append(f"| 盈亏比 | **{pf:.2f}x** | {'良好≥1.5' if pf >= 1.5 else '需优化<1.5'} |")
        lines.append(f"| 成功开仓 | {stats['open_count']} 笔 | 含当前持仓 |")
        lines.append(f"| 开仓失败 | {stats['failed_count']} 笔 | 含黑名单/限制 |")

        # 按平仓原因
        lines.append(f"\n**平仓原因分布：**\n")
        lines.append(f"| 平仓原因 | 次数 | 盈亏(USD) |")
        lines.append(f"|----------|------|-----------|")
        for reason, d in stats["by_reason"].items():
            emoji = "✅" if d["pnl_usd"] >= 0 else "❌"
            lines.append(f"| {reason} | {d['count']} | {emoji} ${d['pnl_usd']:+.2f} |")

        # 按周期
        if stats["by_timeframe"]:
            lines.append(f"\n**各周期绩效：**\n")
            lines.append(f"| 周期 | 交易数 | 胜率 | 盈亏(USD) |")
            lines.append(f"|------|--------|------|-----------|")
            for tf, d in stats["by_timeframe"].items():
                wr_tf = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
                emoji = "✅" if d["pnl_usd"] >= 0 else "❌"
                lines.append(f"| {tf} | {d['count']} | {wr_tf:.0f}% | {emoji} ${d['pnl_usd']:+.2f} |")

    # ── 二、开单条件展示
    hr()
    h("## 二、📋 开单条件记录")

    all_opens = stats.get("all_opens", [])
    if not all_opens:
        lines.append("> 今日无开仓记录\n")
    else:
        lines.append(f"| 时间 | 周期 | 币种 | 方向 | 分数 | 共振 | 价格 | 止损 | 止盈 | 开仓原因 |")
        lines.append(f"|------|------|------|------|------|------|------|------|------|----------|")
        for o in sorted(all_opens, key=lambda x: x.get("time", "")):
            side_emoji = "🟢多" if o.get("side") == "long" else "🔴空"
            resonance = "⚡是" if o.get("resonance") else "否"
            reasons   = o.get("reasons", [])
            reason_str = " / ".join(str(r) for r in reasons[:3]) if isinstance(reasons, list) else str(reasons)
            lines.append(
                f"| {o['time'][11:16]} | {o.get('timeframe','?')} | **{o['symbol']}** "
                f"| {side_emoji} | {o.get('score',0):+.0f} | {resonance} "
                f"| {o.get('price',0)} | {o.get('sl','-')} | {o.get('tp','-')} "
                f"| {reason_str[:60]} |"
            )

    # ── 三、盈亏单深度对比
    hr()
    h("## 三、🔬 盈利单 vs 亏损单深度分析")

    all_closes = stats.get("all_closes", [])
    wins_list   = [c for c in all_closes if c.get("pnl_pct", 0) > 0]
    losses_list = [c for c in all_closes if c.get("pnl_pct", 0) <= 0]

    if all_closes:
        lines.append(f"| 维度 | 盈利单 ({len(wins_list)}笔) | 亏损单 ({len(losses_list)}笔) | 结论 |")
        lines.append(f"|------|----------------------|----------------------|------|")

        p = patterns
        lines.append(f"| 平均信号分数 | {p['win_avg_score']:.0f}分 | {p['loss_avg_score']:.0f}分 | {'高分更可靠' if p['win_avg_score'] > p['loss_avg_score'] else '分数无明显区分'} |")
        lines.append(f"| 共振率 | {p['win_resonance_rate']:.0f}% | {p['loss_resonance_rate']:.0f}% | {'共振更有效' if p['win_resonance_rate'] > p['loss_resonance_rate'] else '共振无明显优势'} |")
        lines.append(f"| 多空分布 | {p['win_longs']}多/{p['win_shorts']}空 | {p['loss_longs']}多/{p['loss_shorts']}空 | - |")
        lines.append(f"| 平均持仓 | {p['win_avg_hold_min']:.0f}分钟 | {p['loss_avg_hold_min']:.0f}分钟 | {'长持更优' if p['win_avg_hold_min'] > p['loss_avg_hold_min'] else '短持更优'} |")

        # 单笔明细
        lines.append(f"\n**✅ 盈利单明细：**\n")
        lines.append(f"| 时间 | 周期 | 币种 | 方向 | 开仓价 | 平仓价 | 原因 | 盈亏% | 盈亏USD |")
        lines.append(f"|------|------|------|------|--------|--------|------|-------|---------|")
        for c in sorted(wins_list, key=lambda x: x.get("pnl_usd", 0), reverse=True):
            entry_info = _find_open(all_opens, c["symbol"], c.get("timeframe", ""))
            score_str = f"{entry_info.get('score', 0):+.0f}" if entry_info else "-"
            lines.append(
                f"| {c['time'][11:16]} | {c.get('timeframe','?')} | **{c['symbol']}** "
                f"| {'多' if c.get('side')=='long' else '空'} | {c.get('entry_price','-')} "
                f"| {c.get('exit_price','-')} | {c.get('reason','-')} "
                f"| **+{c['pnl_pct']:.2f}%** | **+${c.get('pnl_usd',0):.2f}** |"
            )

        lines.append(f"\n**❌ 亏损单明细：**\n")
        lines.append(f"| 时间 | 周期 | 币种 | 方向 | 开仓价 | 平仓价 | 原因 | 亏损% | 亏损USD |")
        lines.append(f"|------|------|------|------|--------|--------|------|-------|---------|")
        for c in sorted(losses_list, key=lambda x: x.get("pnl_usd", 0)):
            lines.append(
                f"| {c['time'][11:16]} | {c.get('timeframe','?')} | **{c['symbol']}** "
                f"| {'多' if c.get('side')=='long' else '空'} | {c.get('entry_price','-')} "
                f"| {c.get('exit_price','-')} | {c.get('reason','-')} "
                f"| **{c['pnl_pct']:.2f}%** | **-${abs(c.get('pnl_usd',0)):.2f}** |"
            )

    # ── 四、前100市值未开仓分析
    hr()
    h("## 四、🔍 前100市值行情复盘")

    if symbols_data:
        up_count   = sum(1 for x in symbols_data if x["price_change_pct"] > 0)
        down_count = sum(1 for x in symbols_data if x["price_change_pct"] < 0)
        avg_change = sum(abs(x["price_change_pct"]) for x in symbols_data) / max(len(symbols_data), 1)
        max_up   = max(symbols_data, key=lambda x: x["price_change_pct"])
        max_down = min(symbols_data, key=lambda x: x["price_change_pct"])

        lines.append(f"| 市场概况 | 数值 |")
        lines.append(f"|----------|------|")
        lines.append(f"| 涨跌分布 | 🟢 {up_count} 涨 / 🔴 {down_count} 跌 |")
        lines.append(f"| 平均振幅 | {avg_change:.2f}% |")
        lines.append(f"| 最大涨幅 | **{max_up['symbol']}** {max_up['price_change_pct']:+.2f}% |")
        lines.append(f"| 最大跌幅 | **{max_down['symbol']}** {max_down['price_change_pct']:+.2f}% |")

        # 涨跌前20
        lines.append(f"\n**涨跌幅Top20 + 机会评级：**\n")
        lines.append(f"| # | 币种 | 涨跌幅 | 振幅 | 成交额(亿) | 评级 | 未开仓原因 |")
        lines.append(f"|---|------|--------|------|------------|------|------------|")
        sorted_by_abs = sorted(symbols_data, key=lambda x: abs(x["price_change_pct"]), reverse=True)
        analyzed_set  = {a["symbol"]: a for a in analysis_results}
        for i, item in enumerate(sorted_by_abs[:20], 1):
            sym  = item["symbol"]
            pct  = item["price_change_pct"]
            high = item.get("high_price", 0)
            low  = item.get("low_price", 0)
            vol_y = item.get("volume_usdt", 0) / 1e8
            amp  = (high - low) / low * 100 if low > 0 else 0
            info = analyzed_set.get(sym, {})
            level = info.get("opportunity_level", "-")
            reason = info.get("miss_reason", "未分析")[:50]
            level_str = {"A": "🔴A错过", "B": "🟡B信号弱", "C": "🟢C弱信号", "D": "⚪D无信号"}.get(level, f"⚪{level}")
            pct_str = f"{'📈' if pct > 0 else '📉'} {pct:+.2f}%"
            lines.append(f"| {i} | **{sym}** | {pct_str} | {amp:.1f}% | {vol_y:.1f}亿 | {level_str} | {reason} |")

    # A/B级专项
    a_level = [x for x in analysis_results if x.get("opportunity_level") == "A"]
    b_level = [x for x in analysis_results if x.get("opportunity_level") == "B"]

    if a_level:
        lines.append(f"\n### 🔴 A级错过机会（大行情+有信号）\n")
        for item in a_level:
            sym = item["symbol"]
            pct = item["price_change_pct"]
            vpb_str = f"VPB{item['vpb_score']:.0f}分({item['vpb_side']})" if item.get("vpb_triggered") else "VPB未触发"
            lines.append(f"**{sym}** {pct:+.2f}%")
            lines.append(f"- 最佳入场: {item.get('best_time','?')} | 周期: {item.get('best_tf','?')}")
            lines.append(f"- 半木夏: {item.get('max_hm_score',0):.0f}分 | {vpb_str}")
            lines.append(f"- 原因: {item.get('miss_reason','-')}")
            lines.append("")
    else:
        lines.append(f"\n> ✅ 无A级错过（大行情均已捕捉或信号确实不足）\n")

    if b_level:
        lines.append(f"\n### 🟡 B级错过（有信号但分数不够）\n")
        for item in b_level[:5]:
            vpb_str = f"VPB{item['vpb_score']:.0f}" if item.get("vpb_triggered") else ""
            lines.append(
                f"- **{item['symbol']}** {item['price_change_pct']:+.2f}% "
                f"| 半木夏{item.get('max_hm_score',0):.0f}分 {vpb_str} "
                f"| {item.get('miss_reason','-')[:60]}"
            )

    # ── 五、今日信号流水
    hr()
    h("## 五、📡 今日信号流水")
    if signals:
        lines.append(f"共产生 **{len(signals)}** 条信号\n")
        lines.append(f"| 时间 | 周期 | 币种 | 方向 | VPB分 | 量比 | 突破 | 形态 |")
        lines.append(f"|------|------|------|------|-------|------|------|------|")
        for s in signals[-30:]:  # 最后30条
            side_str = "🟢多" if s.get("trade_side") == "long" else "🔴空"
            lines.append(
                f"| {s['ts'][11:16]} | {s.get('timeframe','?')} | {s['symbol']} "
                f"| {side_str} | {s.get('vpb_score',0):+.0f} | {s.get('vol_mult',0):.1f}x "
                f"| {s.get('breakout_type','-')} | {s.get('pattern','-')} |"
            )

    # ── 六、策略总结与优化建议
    hr()
    h("## 六、💡 策略不足与优化建议")

    suggestions = []

    if all_closes:
        p = patterns
        # 分析亏损规律
        if losses_list:
            loss_reasons = defaultdict(int)
            for c in losses_list:
                loss_reasons[c.get("reason", "未知")] += 1
            main_loss_reason = max(loss_reasons, key=loss_reasons.get)
            suggestions.append(
                f"**亏损主因**: 最多亏损来自「{main_loss_reason}」({loss_reasons[main_loss_reason]}次)，"
                f"{'建议优化止损距离' if '止损' in main_loss_reason else '建议检查信号质量'}"
            )

        if p["win_avg_score"] > p["loss_avg_score"] + 10:
            suggestions.append(
                f"**信号质量**: 盈利单平均分({p['win_avg_score']:.0f})明显高于亏损单({p['loss_avg_score']:.0f})，"
                f"建议进一步提高开仓阈值到 {max(70, int(p['loss_avg_score']) + 10)} 分"
            )

        if p["win_resonance_rate"] > p["loss_resonance_rate"] + 20:
            suggestions.append(
                f"**共振过滤**: 盈利单共振率({p['win_resonance_rate']:.0f}%)远高于亏损单({p['loss_resonance_rate']:.0f}%)，"
                f"建议对 VPB 策略也启用共振要求"
            )

        if stats["win_rate"] < 40:
            suggestions.append("**胜率偏低**: 胜率不足40%，信号质量需提升，考虑增加趋势过滤条件")

        if stats["profit_factor"] < 1.5:
            suggestions.append(
                f"**盈亏比不足**: 盈亏比 {stats['profit_factor']:.2f}x < 1.5x，"
                f"建议放宽止盈距离（当前TP倍数可增大0.5x）或减小止损距离"
            )

    # 错过大机会
    if a_level:
        a_syms = ", ".join(x["symbol"] for x in a_level[:3])
        suggestions.append(
            f"**错过大机会**: {a_syms} 有明显信号但未开仓，"
            f"建议逐个分析当时冷却期/持仓上限是否阻碍了开仓"
        )

    # 失败率
    if stats["failed_count"] > stats["open_count"]:
        suggestions.append(
            f"**开仓失败率高**: 失败{stats['failed_count']}次 > 成功{stats['open_count']}次，"
            f"检查是否有过多不可交易合约（如TradFi类）进入扫描池"
        )

    if not suggestions:
        suggestions.append("✅ 今日策略表现正常，继续积累数据")

    for idx, s in enumerate(suggestions, 1):
        lines.append(f"\n**{idx}.** {s}\n")

    # 尾注
    hr()
    lines.append(f"*报告由 daily_review.py v2 自动生成 | {now_str}*")

    return "\n".join(lines)


def _find_open(all_opens: list, symbol: str, timeframe: str) -> dict:
    for o in all_opens:
        if o.get("symbol") == symbol and o.get("timeframe") == timeframe:
            return o
    return {}


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def run_daily_review(date_str: str = None, use_local: bool = False):
    if not date_str:
        yesterday = datetime.now(CST) - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  📊 每日复盘 — {date_str}")
    print(f"  {'使用本地日志' if use_local else '拉取 Binance 实盘数据'}...")
    print(f"{'='*60}\n")

    # ── 1. 加载事件日志
    print("⏳ 加载交易事件...")
    events  = load_events(date_str, use_local)
    signals = load_signals(date_str, use_local)
    print(f"  开平仓事件: {len(events)} 条 | 信号: {len(signals)} 条")

    # ── 2. 盈亏统计
    stats    = calc_pnl_stats(events)
    patterns = analyze_trade_patterns(stats.get("all_closes", []))
    print(f"  成交: {stats['total_trades']} 笔 | 胜率: {stats['win_rate']:.1f}% | "
          f"盈亏: ${stats['total_pnl_usd']:+.2f}")

    # ── 3. 拉取市场行情
    print("\n⏳ 拉取 Top100 市场行情...")
    try:
        symbols_data = fetch_top100_symbols()
        print(f"  获取 {len(symbols_data)} 个币种")
    except Exception as e:
        print(f"  ⚠️ 获取行情失败: {e}")
        symbols_data = []

    # ── 4. 回测前30大行情
    analysis_results = []
    if symbols_data:
        sorted_by_abs = sorted(symbols_data, key=lambda x: abs(x["price_change_pct"]), reverse=True)
        print(f"\n⏳ 回测前20大行情未开仓原因（约需 1-2 分钟）...\n")
        for i, item in enumerate(sorted_by_abs[:20]):
            sym = item["symbol"]
            pct = item["price_change_pct"]
            print(f"  [{i+1:2d}/20] {sym:15s} {pct:+7.2f}%  分析中...", end="", flush=True)
            try:
                info = analyze_missed_reason(sym, date_str, pct)
                item.update(info)
                level = info["opportunity_level"]
                emoji = {"A": "🔴", "B": "🟡", "C": "🟢", "D": "⚪"}.get(level, "⚪")
                print(f" {emoji}{level} | HM:{info['max_hm_score']:.0f} VPB:{info['vpb_score']:.0f} | {info['miss_reason'][:40]}")
            except Exception as e:
                print(f" ⚠️ {e}")
                item.update({"max_hm_score": 0, "opportunity_level": "D", "miss_reason": str(e),
                             "vpb_triggered": False, "vpb_score": 0, "vpb_side": ""})
            analysis_results.append(item)
            time.sleep(0.3)
        # 其余80个直接加入（不回测）
        for item in sorted_by_abs[20:]:
            item.update({"max_hm_score": 0, "opportunity_level": "D",
                         "miss_reason": "未回测", "vpb_triggered": False,
                         "vpb_score": 0, "vpb_side": ""})
            analysis_results.append(item)

    # ── 5. 生成报告
    print("\n⏳ 生成复盘报告...")
    report_content = build_report(date_str, stats, patterns,
                                  symbols_data, analysis_results, signals)
    report_path = REPORTS_DIR / f"review_{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    print(f"\n{'='*60}")
    print(f"✅ 报告已生成: {report_path}")
    print(f"{'='*60}")
    print(f"\n📊 快速摘要:")
    if symbols_data:
        max_up   = max(symbols_data, key=lambda x: x["price_change_pct"])
        max_down = min(symbols_data, key=lambda x: x["price_change_pct"])
        print(f"  市场: 涨{sum(1 for x in symbols_data if x['price_change_pct']>0)}"
              f"/跌{sum(1 for x in symbols_data if x['price_change_pct']<0)}")
        print(f"  最大涨: {max_up['symbol']} {max_up['price_change_pct']:+.2f}%")
        print(f"  最大跌: {max_down['symbol']} {max_down['price_change_pct']:+.2f}%")
    print(f"  实盘: {stats['total_trades']} 笔成交，胜率 {stats['win_rate']:.1f}%，"
          f"盈亏 ${stats['total_pnl_usd']:+.2f}")
    a_level = [x for x in analysis_results if x.get("opportunity_level") == "A"]
    if a_level:
        print(f"\n  🔴 A级错过 ({len(a_level)} 个):")
        for item in a_level[:3]:
            print(f"    {item['symbol']:15s} {item['price_change_pct']:+7.2f}%  {item['miss_reason'][:50]}")

    return str(report_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每日复盘分析 v2")
    parser.add_argument("--date", type=str, default=None, help="复盘日期 YYYY-MM-DD，默认昨日")
    parser.add_argument("--local", action="store_true", help="使用本地已拉取日志（server_logs/）")
    args = parser.parse_args()
    run_daily_review(args.date, args.local)
