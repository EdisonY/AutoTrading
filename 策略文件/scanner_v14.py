"""四维度评分策略扫描器 - Account C (v14)

全新策略，与半木夏(v11/v13)完全不同：
  - 四维度独立评分（趋势/动量/量价/结构，各0-25分，满分100）
  - 系统性否决机制（7条硬性否决规则）
  - 阶梯式移动止损（2.5/4.0/6.0×ATR 三阶段）
  - 15m+1h 双周期扫描
  - 不依赖半木夏MACD背离，也不依赖VPB

v14 核心理念：
  - 简单规则 + 严格风控 > 复杂信号 + 松散控制
  - 每个维度独立可解释，不搞综合加权黑箱
  - 否决机制比信号更重要

扫描周期：15m（快速响应）+ 1h（过滤噪声，信号更稳定）

Author: Auto-generated based on v13 architecture + data-driven design
Date: 2026-05-02
"""

import json
import time
import logging
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# Windows GBK编码修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

# ── 路径 ──
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
PROJECT_ROOT = ROOT.parent if ROOT.name == "策略文件" else ROOT
sys.path.insert(0, str(PROJECT_ROOT))

from cloud.analyzer.auxiliary import (
    calc_rsi, calc_ema, calc_adx, calc_stochastic,
    calc_bollinger_bands, calc_atr, calc_obv,
    calc_mfi, calc_supertrend,
)
# 交易交易所: Binance Account C
from binance_client_v3 import get_client, BinanceClientV3 as ExchangeClient, _delete
from core.audit_log import write_jsonl_with_daily_shard
from core.account_state_cache import load_cached_account_state
from core.execution_engine import CloseRequest, ExecutionEngine, OpenRequest
from core.exchange_state import count_active_positions, count_side_positions, find_symbol_position, usdt_balance_summary
from core.event_store import EventStoreWriter
from core.market_watchlist import load_sentinel_context
from core.market_data_cache import cached_available_symbols, cached_top_symbols
from core.kline_cache import load_cached_klines, save_cached_klines
from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request
from core.position_utils import infer_position_side, leveraged_loss_pct
from core.sentinel_scanner import fields_from_context, filter_context_by_available, merge_symbols_with_context
from core.risk_engine import RiskEngine, RiskLimits
from core.strategy_gates import (
    evaluate_account_state_available_gate,
    evaluate_c_v14_confirmation_gate,
    evaluate_c_v14_entry_threshold,
    evaluate_c_v14_market_microstructure_gate,
    evaluate_c_v14_stale_entry_price_gate,
    evaluate_c_v14_tail_guard,
    evaluate_consecutive_loss_cooldown_gate,
    evaluate_execution_result_gate,
    evaluate_no_same_symbol_position_gate,
    evaluate_positive_quantity_gate,
    evaluate_same_side_position_gate,
    evaluate_score_max_gate,
    evaluate_sector_position_gate,
    evaluate_symbol_blacklist_gate,
    evaluate_symbol_cooldown_gate,
    evaluate_symbol_stop_loss_gate,
    evaluate_timeframe_position_gate,
)
from core.strategy_gate_cases import strategy_gate_case
from core.strategy_engine import StrategyEngine

def console_log_level() -> int:
    name = os.environ.get("SCANNER_CONSOLE_LOG_LEVEL") or os.environ.get("LOG_LEVEL", "INFO")
    return getattr(logging, name.strip().upper(), logging.INFO)


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


logging.basicConfig(
    level=console_log_level(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scanner_v14")

CST = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
TIMEFRAMES = ["1h", "15m"]          # 保留历史持仓管理周期
ENTRY_TIMEFRAMES = ["1h"]           # 15m不再独立开仓，仅作为1h确认
CONFIRM_TIMEFRAME = "15m"
CONFIRM_MIN_SCORE = 25              # 2026-05-31 sample expansion: 35→25
MAX_POSITIONS_PER_TF = 3            # 2026-05-31 sample expansion: 2→3
MAX_TOTAL_POSITIONS = 20            # P0: 全账户总持仓上限，超过后只管理不新开
LEVERAGE = 4

# ── 四维度评分系统 ──
SCORE_MIN = 25                      # 最低开仓阈值，v14优化(2026-05-06): 20→25
SCORE_MAX = 85                      # 2026-05-31 sample expansion: allow more high-conviction samples
SCORE_THRESHOLDS = {"15m": 60, "1h": 50}  # 2026-05-31 sample expansion
SIGNAL_SCHEMA_VERSION = "v14_entry_candidate_v2"
SAMPLE_EXPANSION_POLICY = "v14_sample_expansion_2026_05_31"
TAIL_GUARD_MIN_SCORE = 55
TAIL_GUARD_LONG_BB_POS = 72
TAIL_GUARD_SHORT_BB_POS = 28
TAIL_GUARD_MIN_VOL_RATIO = 1.2
TAIL_GUARD_MAX_ATR_PCT = 0.045
LONG_PENALTY = 0                    # v14: 暂不扣分，等数据验证后再调整
SHORT_ENTRY_PENALTY = 10            # 2026-05-31 sample expansion: 15→10
NO_CONFIRM_HIGH_SCORE_PASS = 64
WEAK_CONFIRM_MIN_SCORE = 20
SENTINEL_CONTEXT: dict[str, dict] = {}


def entry_threshold_for(tf: str, side: str, trend_dir: str = "neutral", trend_strength: float = 0.0) -> tuple[float, int]:
    """Return the real entry threshold used by the v14 open gate."""
    decision = evaluate_c_v14_entry_threshold(
        timeframe=tf,
        side=side,
        trend_dir=trend_dir,
        trend_strength=trend_strength,
        score_thresholds=SCORE_THRESHOLDS,
        score_min=SCORE_MIN,
        long_penalty=LONG_PENALTY,
        short_entry_penalty=SHORT_ENTRY_PENALTY,
    )
    return float(decision.threshold or 0), int((decision.evidence or {}).get("trend_penalty") or 0)

# ── 止损止盈参数（ATR倍数）──
# v14优化(2026-05-06): 分档ATR止损，替代固定倍数
# 低波动（ATR/Price < 2%）：止损 2.3×ATR（给更多空间）
# 中波动（ATR/Price 2%-4%）：止损 2.0×ATR
# 高波动（ATR/Price > 4%）：止损 1.5×ATR（快速止损）
SL_MULT = {"15m": 1.8, "1h": 2.0}   # 默认值（备用，实际用 calc_sl_mult_v14）
SL_TIER_LOW   = 2.3   # ATR/Price < 2%
SL_TIER_MID   = 2.0   # ATR/Price 2%-4%
SL_TIER_HIGH  = 1.5   # ATR/Price > 4%
SL_TIER_LOW_THRESH  = 0.02   # < 2%
SL_TIER_HIGH_THRESH = 0.04   # > 4%

def calc_sl_mult_v14(atr_pct: float) -> float:
    """根据ATR/Price比率返回对应的止损ATR倍数（分档ATR止损）"""
    if atr_pct < SL_TIER_LOW_THRESH:
        return SL_TIER_LOW
    elif atr_pct <= SL_TIER_HIGH_THRESH:
        return SL_TIER_MID
    else:
        return SL_TIER_HIGH

# 阶梯式移动止损参数
TRAILING_ACTIVATE = {"15m": 1.0, "1h": 1.2}   # 更早进入浮动保护
TRAILING_PULLBACK = {"15m": 0.8, "1h": 1.0}   # 降低盈利回吐和尾部亏损

# 阶梯式止盈配置
# (触发ATR倍数, 止损移到的ATR倍数) — 第一阶段移至成本价
TP_STAGES = [
    {"trigger_atr": 1.8, "trail_to": "breakeven"},  # 到1.8ATR → 止损移到成本价
    {"trigger_atr": 3.0, "trail_to_atr": 1.5},      # 到3.0ATR → 止损移到1.5ATR
    {"trigger_atr": 5.0, "trail_to_atr": 2.2},      # 到5.0ATR → 止损移到2.2ATR
]
TP_MAX_MULT = 10.0                  # 绝对止盈（安全网）：10×ATR

# ── 冷却与风控 ──
COOLDOWN_MINUTES = 120              # 同币种冷却120分钟
COOLDOWN_CONSECUTIVE = 120          # 连续亏损冷却120分钟，v14优化(2026-05-06): 240→120
ATR_PRICE_MAX = 0.04                # ATR/Price > 4% 跳过

# ATR=0异常币种黑名单
ATR_ZERO_BLACKLIST = {"RDNTUSDT", "RAVEUSDT", "DEXEUSDT"}

# 动态投入金额
RISK_PER_TRADE_USDT = 100           # 默认
RISK_PER_TRADE_HIGH = 150           # 90-100分
RISK_PER_TRADE_LOW = 75             # 70-89分（保留但不使用，与v13区分）

# 安全限制
MAX_LOSS_PCT = 12.0                 # 更快止损保护本金，控制单票尾部亏损
MIN_AVAILABLE_BALANCE_PCT = 0.25    # P0: 开新仓后至少保留25%可用保证金
MIN_AVAILABLE_BALANCE_USDT = 300.0  # P0: 绝对可用余额保护线
MAX_SL_PER_SYMBOL = 2               # 同币种当日最大止损次数
SYMBOL_SL_BAN_HOURS = 72            # P2: 止损后跨周期冷却72小时
MAX_POS_PER_SECTOR = 3              # 2026-05-31 sample expansion: 2→3
MAX_POS_PER_SIDE = 12               # P2: 单方向最大持仓数
PROFIT_PROTECT_MIN_USDT = 30.0      # 盈利超过该值后启用回撤保护
PROFIT_PROTECT_RETRACE = 0.25       # 从最高浮盈回撤25%则平仓保护利润

# 赛道映射（用于同赛道分散限制）
SECTOR_MAP = {
    # L1
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "BNB": "L1",
    "AVAX": "L1", "NEAR": "L1", "ATOM": "L1", "DOT": "L1",
    "ADA": "L1", "TRX": "L1", "XRP": "L1", "DOGE": "L1",
    # L2
    "MATIC": "L2", "ARB": "L2", "OP": "L2", "IMX": "L2",
    "MNT": "L2", "STRK": "L2", "ZK": "L2",
    # DeFi
    "UNI": "DeFi", "AAVE": "DeFi", "COMP": "DeFi", "MKR": "DeFi",
    "LDO": "DeFi", "CRV": "DeFi", "SUSHI": "DeFi", "SNX": "DeFi",
    "PENDLE": "DeFi", "1INCH": "DeFi",
    # Meme
    "SHIB": "Meme", "PEPE": "Meme", "WIF": "Meme", "BONK": "Meme",
    "FLOKI": "Meme", "MEME": "Meme", "TURBO": "Meme",
    # AI
    "FET": "AI", "RNDR": "AI", "TAO": "AI", "AKT": "AI",
    "AR": "AI", "WLD": "AI", "NEURAL": "AI",
    # GameFi
    "AXS": "GameFi", "SAND": "GameFi", "MANA": "GameFi", "GALA": "GameFi",
    "IMX": "GameFi",
    # RWA
    "LINK": "RWA", "MANA": "RWA", "GRT": "RWA", "FIL": "RWA",
}
# 未知币种默认归为 "Other"，不计入分散限制（宽松处理）


def get_sector(symbol: str) -> str:
    """返回币种所属赛道；无法识别时返回 'Other'"""
    base = symbol.replace("USDT", "").upper()
    return SECTOR_MAP.get(base, "Other")


def count_positions_by_sector(positions: dict) -> dict:
    """统计当前各赛道持仓数（跨所有timeframe）"""
    counts = {}
    for tf, pos_dict in positions.items():
        for key in pos_dict:
            sym = key[0] if isinstance(key, tuple) else key
            sec = get_sector(sym)
            if sec != "Other":
                counts[sec] = counts.get(sec, 0) + 1
    return counts


# 数据目录
DATA_DIR = ROOT / "scanner_data_v14"
DATA_DIR.mkdir(exist_ok=True)

TRADES_LOG = DATA_DIR / "trades.jsonl"
EVENTS_LOG = DATA_DIR / "events.jsonl"
REPORT_FILE = DATA_DIR / "report.md"

LOGS_DIR = ROOT / "logs_v14"
LOGS_DIR.mkdir(exist_ok=True)
SIGNAL_LOG = LOGS_DIR / "signals.jsonl"
DECISION_LOG = LOGS_DIR / "decisions.jsonl"
OPERATION_LOG = LOGS_DIR / "operations.jsonl"
SYSTEM_LOG = LOGS_DIR / "system.jsonl"
EVENT_STORE = EventStoreWriter(PROJECT_ROOT / "runtime" / "event_store.sqlite3")


def write_event_store(record: dict, source: str):
    EVENT_STORE.write_event(record, source=source)


# ═══════════════════════════════════════════════════════════════
# 模拟持仓（与v13结构一致）
# ═══════════════════════════════════════════════════════════════
@dataclass
class SimPosition:
    symbol: str
    side: str           # "long" / "short"
    entry_price: float
    size: float
    leverage: int
    stop_loss: float
    take_profit: float
    atr_at_entry: float
    entry_time: str
    entry_score: float
    entry_reason: str
    timeframe: str = ""
    resonance: bool = False
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_sl: float = 0.0
    trailing_active: bool = True
    pos_id: str = ""
    sl_mult: float = 1.8
    tp_mult: float = 10.0       # v14: 用阶梯止损，TP设大一点
    trail_activate: float = 1.5
    trail_pullback: float = 1.5
    order_id: str = ""
    exchange_qty: float = 0.0
    # v14: 阶梯止损追踪
    tp_stage: int = 0           # 当前阶梯阶段（0=未激活）

    def __post_init__(self):
        if not self.pos_id:
            self.pos_id = f"{self.symbol}_{self.timeframe}_{self.entry_time}"
        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price
        self.trailing_sl = self.stop_loss

    def check_exit(self, current_price: float) -> Optional[str]:
        """检查是否触发止盈/浮动止损/阶梯止损/最大亏损限制"""
        if current_price > self.highest_price:
            self.highest_price = current_price
        if current_price < self.lowest_price:
            self.lowest_price = current_price

        # 最大亏损%硬性限制（含杠杆），极端行情兜底
        if self.side == "long":
            loss_pct = (self.entry_price - current_price) / self.entry_price * 100 * self.leverage
        else:
            loss_pct = (current_price - self.entry_price) / self.entry_price * 100 * self.leverage
        if loss_pct >= MAX_LOSS_PCT:
            return f"最大亏损限制-{MAX_LOSS_PCT:.0f}%"

        if self.side == "long":
            best_pnl_usd = max(0.0, (self.highest_price - self.entry_price) * self.size)
            cur_pnl_usd = (current_price - self.entry_price) * self.size
        else:
            best_pnl_usd = max(0.0, (self.entry_price - self.lowest_price) * self.size)
            cur_pnl_usd = (self.entry_price - current_price) * self.size
        if best_pnl_usd >= PROFIT_PROTECT_MIN_USDT and cur_pnl_usd <= best_pnl_usd * (1 - PROFIT_PROTECT_RETRACE):
            return f"盈利回撤保护-{PROFIT_PROTECT_RETRACE*100:.0f}%"

        # ── v14: 阶梯式移动止损 ──
        profit_atr = self._profit_in_atr(current_price)

        for i, stage in enumerate(TP_STAGES):
            if i <= self.tp_stage:
                continue  # 已处理过的阶段
            if profit_atr >= stage["trigger_atr"]:
                self.tp_stage = i
                if stage.get("trail_to") == "breakeven":
                    # 移到成本价
                    new_sl = self.entry_price
                else:
                    trail_atr = stage.get("trail_to_atr", 2.0)
                    if self.side == "long":
                        new_sl = self.entry_price + self.atr_at_entry * (trail_atr - self.sl_mult)
                    else:
                        new_sl = self.entry_price - self.atr_at_entry * (trail_atr - self.sl_mult)

                if self.side == "long":
                    if new_sl > self.trailing_sl:
                        self.trailing_sl = new_sl
                else:
                    if new_sl < self.trailing_sl or self.trailing_sl == self.stop_loss:
                        self.trailing_sl = new_sl

                logger.info(f"    📈 {self.symbol} 阶梯止损[{i+1}] 激活: 盈利{profit_atr:.1f}×ATR → SL={self.trailing_sl:.4f}")

        # 浮动止损追踪（在阶梯之上叠加）
        atr_move = self.atr_at_entry * self.trail_activate
        if self.side == "long":
            profit = current_price - self.entry_price
            if profit > atr_move and not self.trailing_active:
                self.trailing_active = True
            if self.trailing_active:
                new_trail = self.highest_price - self.atr_at_entry * self.trail_pullback
                if new_trail > self.trailing_sl:
                    self.trailing_sl = new_trail
            if current_price <= self.trailing_sl and self.trailing_sl != self.stop_loss:
                return "浮动止损"
            if current_price >= self.entry_price + self.atr_at_entry * TP_MAX_MULT:
                return "绝对止盈"
        else:  # short
            profit = self.entry_price - current_price
            if profit > atr_move and not self.trailing_active:
                self.trailing_active = True
            if self.trailing_active:
                new_trail = self.lowest_price + self.atr_at_entry * self.trail_pullback
                if new_trail < self.trailing_sl or self.trailing_sl == self.stop_loss:
                    self.trailing_sl = new_trail
            if current_price >= self.trailing_sl and self.trailing_sl != self.stop_loss:
                return "浮动止损"
            if current_price <= self.entry_price - self.atr_at_entry * TP_MAX_MULT:
                return "绝对止盈"
        return None

    def _profit_in_atr(self, current_price: float) -> float:
        """计算当前盈利是ATR的多少倍"""
        if self.atr_at_entry <= 0:
            return 0
        if self.side == "long":
            return (current_price - self.entry_price) / self.atr_at_entry
        else:
            return (self.entry_price - current_price) / self.atr_at_entry


# ═══════════════════════════════════════════════════════════════
# 数据拉取（复用v13模式）
# ═══════════════════════════════════════════════════════════════
_api_request_times: list[float] = []  # v14优化(2026-05-10): 请求频率追踪
_last_ban_until: float = 0


def fetch_json(url: str, timeout: int = 10) -> dict:
    """拉取JSON，内置限流和ban退避"""
    import urllib.request, urllib.error
    global _last_ban_until
    if api_queue_client_enabled():
        queue_timeout = max(timeout + 5, int(float(os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", "180"))))
        data = queued_api_request(scope="public", label="C/v14", method="GET", path=url, url=url, timeout_sec=queue_timeout)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("C/v14", url)
    now = time.time()
    if _last_ban_until > now:
        wait = _last_ban_until - now
        logger.warning(f"  ⏸️ IP仍被ban，等待{wait:.0f}秒后重试")
        time.sleep(wait)
        _last_ban_until = 0
    _api_request_times.append(now)
    while _api_request_times and _api_request_times[0] < now - 60:
        _api_request_times.pop(0)
    if len(_api_request_times) > 200:
        time.sleep(0.5)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code in {418, 429}:
            record_public_response("C/v14", url, e.code, body)
        if e.code == 418:
            if "banned until" in body:
                import re
                m = re.search(r"banned until (\d+)", body)
                if m:
                    _last_ban_until = int(m.group(1)) / 1000
                    wait = _last_ban_until - time.time()
                    logger.warning(f"  ⏸️ IP被ban，等待{wait:.0f}秒")
                    if wait > 0:
                        time.sleep(wait)
                        _last_ban_until = 0
            raise
        raise


def fetch_top_symbols(top_n: int = 100) -> list[str]:
    """拉取 Top N（按 24h 成交额排序）"""
    cached = cached_top_symbols(PROJECT_ROOT / "runtime" / "market_data_cache.json", top_n)
    if cached:
        return cached
    raw = fetch_json("https://testnet.binancefuture.com/fapi/v1/ticker/24hr")
    usdt_swaps = []
    for it in raw:
        sym = it.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            vol = float(it.get("quoteVolume", 0) or 0)
        except Exception:
            vol = 0
        usdt_swaps.append((sym, vol))
    usdt_swaps.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in usdt_swaps[:top_n]]


def fetch_available_symbols() -> set[str]:
    """当前扫描市场可用的 USDT 合约池，用于过滤实盘哨兵名单。"""
    cached = cached_available_symbols(PROJECT_ROOT / "runtime" / "market_data_cache.json")
    if cached:
        return cached
    try:
        raw = fetch_json("https://testnet.binancefuture.com/fapi/v1/ticker/24hr")
    except Exception as e:
        logger.warning(f"  哨兵市场池校验失败，保留原始哨兵名单: {e}")
        return set()
    return {
        str(it.get("symbol", "")).upper()
        for it in raw
        if str(it.get("symbol", "")).upper().endswith("USDT")
    }


def sentinel_fields(symbol: str) -> dict:
    return fields_from_context(SENTINEL_CONTEXT, symbol)


def log_sentinel_scan(symbol: str, tf: str, result: str, reason: str, **extra):
    fields = sentinel_fields(symbol)
    if not fields:
        return
    _log_sentinel_scan_event({
        "time": str(datetime.now(CST)),
        "event": "SENTINEL_SCANNED",
        "symbol": symbol,
        "timeframe": tf,
        "reason": reason,
        "category": f"sentinel_{result}",
        "decision_stage": extra.pop("decision_stage", "strategy_scan"),
        "filter_layer": extra.pop("filter_layer", "strategy"),
        "sentinel_scan_result": result,
        **fields,
        **extra,
    })


def merge_sentinel_symbols(symbols: list[str], limit: int = 40) -> list[str]:
    """把涨跌榜哨兵名单合进扫描列表。哨兵只加关注，不直接开仓。"""
    global SENTINEL_CONTEXT
    context = load_sentinel_context(limit=limit)
    available = fetch_available_symbols()
    context, skipped = filter_context_by_available(context, available)
    if skipped:
        logger.info(f"  哨兵过滤 {len(skipped)} 个当前扫描市场不可用币种: {', '.join(skipped[:8])}")
    SENTINEL_CONTEXT = context
    sentinel = list(SENTINEL_CONTEXT)
    if sentinel:
        logger.info(f"  👀 涨跌榜哨兵补充 {len(sentinel)} 个币种: {', '.join(sentinel[:8])}")
    return merge_symbols_with_context(symbols, SENTINEL_CONTEXT)


def fetch_klines(symbol: str, bar: str = "15m", limit: int = 100) -> list[list]:  # v14优化(2026-05-10): limit 200→100, weight 2→1
    """拉取 K 线"""
    cached = load_cached_klines(PROJECT_ROOT, symbol, bar, limit)
    if cached:
        return cached
    url = "https://testnet.binancefuture.com/fapi/v1/klines?symbol=%s&interval=%s&limit=%d" % (symbol, bar, limit)
    raw = fetch_json(url)
    rows = []
    for r in raw:
        rows.append([str(r[0]), r[1], r[2], r[3], r[4], r[5], r[7], r[8]])
    save_cached_klines(PROJECT_ROOT, symbol, bar, limit, rows)
    return rows


def fetch_current_price(symbol: str) -> float:
    """拉取最新价"""
    url = "https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol=%s" % symbol
    raw = fetch_json(url)
    return float(raw.get("price", 0))


# ── v14优化(2026-05-08): BTC大盘趋势判断 ──
def fetch_btc_trend() -> dict:
    """获取BTC 4h趋势，返回趋势方向和强度
    
    用于全局趋势过滤：
    - 大趋势向上 → 降低空头开仓权重/提高空头门槛
    - 大趋势向下 → 降低多头开仓权重/提高多头门槛
    - 震荡 → 不调整
    
    返回: {"direction": "bull"/"bear"/"neutral", "strength": 0-100, "ema20": float, "ema50": float}
    """
    try:
        rows = fetch_klines("BTCUSDT", "4h", 100)
        if len(rows) < 60:
            return {"direction": "neutral", "strength": 0, "ema20": 0, "ema50": 0}
        closes = np.array([float(r[4]) for r in rows])
        highs = np.array([float(r[2]) for r in rows])
        lows = np.array([float(r[3]) for r in rows])
        
        ema20_s = pd.Series(closes).ewm(span=20, adjust=False).mean()
        ema50_s = pd.Series(closes).ewm(span=50, adjust=False).mean()
        ema20 = float(ema20_s.iloc[-1])
        ema50 = float(ema50_s.iloc[-1])
        price = float(closes[-1])
        
        # ADX趋势强度
        adx_val = float(pd.Series(calc_adx(highs, lows, closes)).dropna().iloc[-1])
        
        # 5根EMA20斜率
        slope = float(ema20_s.iloc[-1] - ema20_s.iloc[-6]) / ema20 * 100 if ema20 > 0 else 0
        
        if ema20 > ema50 and slope > 0.3:
            direction = "bull"
            strength = min(100, adx_val * 2)
        elif ema20 < ema50 and slope < -0.3:
            direction = "bear"
            strength = min(100, adx_val * 2)
        else:
            direction = "neutral"
            strength = 0
        
        return {"direction": direction, "strength": strength, "ema20": ema20, "ema50": ema50, "adx": adx_val, "slope": slope}
    except Exception as e:
        logger.debug(f"BTC趋势判断失败: {e}")
        return {"direction": "neutral", "strength": 0}


# ═══════════════════════════════════════════════════════════════
# v14: 四维度评分系统
# ═══════════════════════════════════════════════════════════════
def analyze_symbol_v14(symbol: str, bar: str = "15m") -> Optional[dict]:
    """v14 四维度独立评分系统

    维度1: 趋势识别 (0-25分) — EMA排列 + ADX强度
    维度2: 动量确认 (0-25分) — RSI + MACD + MACD柱状图
    维度3: 量价验证 (0-25分) — OBV + 成交量 + MFI
    维度4: 市场结构 (0-25分) — 布林带 + Stochastic + SuperTrend

    返回: 信号结果 dict 或 None
    """

    limit_map = {"15m": 100, "1h": 100, "30m": 100}  # v14优化(2026-05-10): weight减半
    limit = limit_map.get(bar, 200)

    rows = fetch_klines(symbol, bar, limit)
    if len(rows) < 50:
        return None

    closes = np.array([float(r[4]) for r in rows])
    highs  = np.array([float(r[2]) for r in rows])
    lows   = np.array([float(r[3]) for r in rows])
    opens  = np.array([float(r[1]) for r in rows])
    vols   = np.array([float(r[5]) for r in rows])
    price  = float(closes[-1])

    def _last(s):
        s = pd.Series(s)
        vals = s.dropna()
        return float(vals.iloc[-1]) if not vals.empty else 0.0

    # ── 指标计算 ──
    # ATR
    atr_val = _last(calc_atr(highs, lows, closes))
    atr_pct = atr_val / price if price > 0 else 0

    # 否决条件检查（提前，避免不必要的计算）
    if atr_pct > ATR_PRICE_MAX:
        return None
    if symbol in ATR_ZERO_BLACKLIST:
        return None

    # EMA (9/21/55)
    ema9_val  = _last(calc_ema(closes, 9))
    ema21_val = _last(calc_ema(closes, 21))
    ema55_val = _last(calc_ema(closes, 55))

    # ADX
    adx_val = _last(calc_adx(highs, lows, closes))

    # RSI
    rsi_val = _last(calc_rsi(closes))

    # MACD (12,26,9)
    ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    macd_val = _last(macd_line)
    macd_signal = _last(signal_line)
    hist_val = _last(histogram)
    hist_prev = float(histogram.iloc[-2]) if len(histogram) >= 2 else hist_val
    hist_delta = hist_val - hist_prev

    # OBV
    obv_s = calc_obv(closes, vols)
    obv_slope = float(obv_s.iloc[-1] - obv_s.iloc[-6]) if len(obv_s) >= 6 else 0.0

    # 成交量 MA(20)
    vol_ma20 = float(pd.Series(vols).rolling(20).mean().iloc[-1]) if len(vols) >= 20 else float(np.mean(vols))
    vol_ratio = float(vols[-1]) / vol_ma20 if vol_ma20 > 0 else 1.0

    # MFI
    mfi_series = calc_mfi(closes, highs, lows, vols, period=14)
    mfi_val = float(mfi_series.iloc[-1])

    # 布林带
    bb_u, bb_m, bb_l = calc_bollinger_bands(closes)
    bb_upper = _last(bb_u)
    bb_lower = _last(bb_l)
    bb_pos = (price - bb_lower) / (bb_upper - bb_lower + 1e-9) * 100

    # Stochastic
    sk, sd = calc_stochastic(highs, lows, closes)
    stoch_k = _last(sk)
    stoch_d = _last(sd)

    # SuperTrend
    st_result = calc_supertrend(highs, lows, closes, atr_val, period=10, multiplier=3.0)
    st_direction = int(st_result['direction'].iloc[-1])
    prev_st_dir = int(st_result['direction'].iloc[-2]) if len(st_result['direction']) >= 2 else st_direction
    st_flipped = (st_direction != prev_st_dir)

    # ═══════════════════════════════════════════════════════════
    # 四维度评分
    # ═══════════════════════════════════════════════════════════
    score_long = 0.0
    score_short = 0.0
    reasons_long = []
    reasons_short = []

    # ── 维度1: 趋势识别 (0-25分) ──
    # EMA排列 (0-15分)
    if ema9_val > ema21_val > ema55_val:
        score_long += 15
        reasons_long.append("EMA三线多头排列")
    elif ema9_val > ema21_val:
        score_long += 10
        reasons_long.append("EMA两线多头")
    elif ema9_val < ema21_val < ema55_val:
        score_short += 15
        reasons_short.append("EMA三线空头排列")
    elif ema9_val < ema21_val:
        score_short += 10
        reasons_short.append("EMA两线空头")

    # ADX趋势强度 (0-10分)
    if adx_val >= 25:
        bonus = min(10, adx_val * 0.2)
        if score_long > score_short:
            score_long += bonus
            reasons_long.append(f"ADX趋势{adx_val:.0f}")
        elif score_short > score_long:
            score_short += bonus
            reasons_short.append(f"ADX趋势{adx_val:.0f}")
    elif adx_val < 20:
        score_long = max(0, score_long - 3)
        score_short = max(0, score_short - 3)

    # ── 维度2: 动量确认 (0-25分) ──
    # RSI (0-10分)
    if rsi_val < 30:
        score_long += 10
        reasons_long.append("RSI超卖")
    elif rsi_val < 45:
        score_long += 5
        reasons_long.append("RSI偏低")
    elif rsi_val > 70:
        score_short += 10
        reasons_short.append("RSI超买")
    elif rsi_val > 55:
        score_short += 5
        reasons_short.append("RSI偏高")

    # MACD (0-10分)
    if macd_val > macd_signal and hist_val > 0:
        score_long += 10
        reasons_long.append("MACD金叉")
    elif macd_val < macd_signal and hist_val < 0:
        score_short += 10
        reasons_short.append("MACD死叉")

    # MACD柱状图方向 (0-5分)
    if hist_val > 0 and hist_delta > 0:
        score_long += 5
        reasons_long.append("MACD柱扩张")
    elif hist_val < 0 and hist_delta < 0:
        score_short += 5
        reasons_short.append("MACD柱扩张")

    # ── 维度3: 量价验证 (0-25分) ──
    # OBV斜率 (0-10分)
    if obv_slope > 0:
        score_long += 10
        reasons_long.append("OBV上升")
    elif obv_slope < 0:
        score_short += 10
        reasons_short.append("OBV下降")

    # 成交量放大 (0-10分)
    if vol_ratio >= 1.5:
        if score_long > score_short:
            score_long += 10
            reasons_long.append(f"放量{vol_ratio:.1f}x")
        elif score_short > score_long:
            score_short += 10
            reasons_short.append(f"放量{vol_ratio:.1f}x")
    elif vol_ratio >= 1.2:
        if score_long > score_short:
            score_long += 5
            reasons_long.append(f"温和放量{vol_ratio:.1f}x")
        elif score_short > score_long:
            score_short += 5
            reasons_short.append(f"温和放量{vol_ratio:.1f}x")

    # MFI资金流 (0-5分)
    if mfi_val < 20:
        score_long += 5
        reasons_long.append("MFI超卖")
    elif mfi_val > 80:
        score_short += 5
        reasons_short.append("MFI超买")

    # ── 维度4: 市场结构 (0-25分) ──
    # 布林带位置 (0-10分)
    if bb_pos < 10:
        score_long += 10
        reasons_long.append("触及布林下轨")
    elif bb_pos > 90:
        score_short += 10
        reasons_short.append("触及布林上轨")

    # Stochastic (0-10分)
    if stoch_k < 20 and stoch_k > stoch_d:
        score_long += 10
        reasons_long.append("Stoch低位金叉")
    elif stoch_k > 80 and stoch_k < stoch_d:
        score_short += 10
        reasons_short.append("Stoch高位死叉")

    # SuperTrend (0-5+5分)
    if st_direction == 1:
        score_long += 5
        reasons_long.append("ST看多")
        if st_flipped:
            score_long += 5
            reasons_long.append("ST翻转+5")
    else:
        score_short += 5
        reasons_short.append("ST看空")
        if st_flipped:
            score_short += 5
            reasons_short.append("ST翻转+5")

    # ── 趋势过滤：EMA方向与信号方向不一致时扣分 ──
    ema_bull = ema9_val > ema21_val > ema55_val
    ema_bear = ema9_val < ema21_val < ema55_val
    if score_long > score_short and ema_bear:
        score_long = max(0, score_long - 10)
        reasons_long.append("EMA逆势-10")
    elif score_short > score_long and ema_bull:
        score_short = max(0, score_short - 10)
        reasons_short.append("EMA逆势-10")

    # 净分数
    net_score = score_long - score_short

    # 止损止盈（v14优化：分档ATR止损）
    sl_m = calc_sl_mult_v14(atr_pct)
    sl_long  = round(price - atr_val * sl_m, 4)
    sl_short = round(price + atr_val * sl_m, 4)
    # v14: 用阶梯止损，TP设一个安全网
    tp_long  = round(price + atr_val * TP_MAX_MULT, 4)
    tp_short = round(price - atr_val * TP_MAX_MULT, 4)

    return {
        "symbol": symbol,
        "price": price,
        "atr": round(atr_val, 4),
        "net_score": round(net_score, 1),
        "score_long": round(score_long, 1),
        "score_short": round(score_short, 1),
        "reasons_long": reasons_long,
        "reasons_short": reasons_short,
        "can_trade": abs(net_score) >= SCORE_MIN,
        "trade_side": "long" if net_score > 0 else "short",
        "sl_long": sl_long,
        "sl_short": sl_short,
        "tp_long": tp_long,
        "tp_short": tp_short,
        "rsi": round(rsi_val, 1),
        "adx": round(adx_val, 1),
        "atr_pct": round(atr_pct, 4),
        "vol_ratio": round(vol_ratio, 2),
        "mfi": round(mfi_val, 1),
        "bb_pos": round(bb_pos, 1),
        "st_direction": st_direction,
        "st_flipped": st_flipped,
        "timeframe": bar,
        # 维度分拆（用于日志和分析）
        "dim_trend": round(max(
            (15 if ema9_val > ema21_val > ema55_val else 10 if ema9_val > ema21_val else 0),
            (15 if ema9_val < ema21_val < ema55_val else 10 if ema9_val < ema21_val else 0)
        ) + min(10, adx_val * 0.2) if adx_val >= 25 else 0, 1),
    }


# ═══════════════════════════════════════════════════════════════
# 日志写入
# ═══════════════════════════════════════════════════════════════
def _decision_category(status: str, reason: str) -> str:
    text = f"{status} {reason}".lower()
    if status == "SENTINEL_SCANNED":
        return "sentinel_scanned"
    if status == "OPEN":
        return "opened"
    if status in ("CLOSE", "FORCED_CLOSE"):
        return "closed"
    if "总持仓" in reason or "活跃持仓" in reason:
        return "position_limit"
    if "方向持仓" in reason or "单方向" in reason:
        return "side_limit"
    if "余额" in reason or "available" in text or "reserve" in text:
        return "capital_guard"
    if "15m" in reason or "确认" in reason:
        return "confirmation"
    if "阈值" in reason or "score" in text or "分" in reason:
        return "score_threshold"
    if "止损" in reason or "冷却" in reason or "cooldown" in text:
        return "cooldown"
    if "赛道" in reason:
        return "sector_limit"
    if "atr" in text or "数量太小" in reason or "qty" in text:
        return "market_microstructure"
    if "失败" in reason or status == "OPEN_FAILED":
        return "order_failed"
    if status in ("SIGNAL", "SIGNAL_ONLY"):
        return "signal_candidate"
    return "other"

def log_decision(record: dict, *, persist_event_store: bool = True):
    try:
        write_jsonl_with_daily_shard(DECISION_LOG, record)
        if persist_event_store:
            write_event_store(record, "C/v14/decisions")
    except Exception as e:
        logger.debug(f"写入决策日志失败: {e}")

def _decision_from_event(event: dict) -> dict:
    status = str(event.get("event") or "EVENT").upper()
    reason = str(event.get("skip_reason") or event.get("reason") or event.get("msg") or "")
    side = str(event.get("side") or "").lower()
    score = event.get("score", event.get("raw_score", event.get("net_score", 0)))
    return {
        "time": event.get("time") or event.get("ts") or str(datetime.now(CST)),
        "strategy": "C/v14",
        "symbol": event.get("symbol", ""),
        "status": status,
        "category": event.get("category") or _decision_category(status, reason),
        "side": side,
        "score": score,
        "raw_score": event.get("raw_score", score),
        "timeframe": event.get("timeframe") or event.get("tf") or "",
        "reason": reason,
        "source": "event",
        "event": status,
        "decision_stage": event.get("decision_stage") or event.get("stage") or "",
        "filter_layer": event.get("filter_layer") or event.get("risk_category") or "",
        "confirm_reason": event.get("confirm_reason") or event.get("skip_reason") or "",
        "entry_reason": event.get("entry_reason") or event.get("reason") or "",
        "reasons": event.get("reasons") or [],
        "atr_pct": event.get("atr_pct"),
        "bb_pos": event.get("bb_pos"),
        "rsi": event.get("rsi"),
        "adx": event.get("adx"),
        "vol_ratio": event.get("vol_ratio"),
        "mfi": event.get("mfi"),
        "st_flipped": event.get("st_flipped"),
        "sentinel": event.get("sentinel", False),
        "sentinel_reason": event.get("sentinel_reason", ""),
        "sentinel_change_pct": event.get("sentinel_change_pct"),
        "sentinel_velocity_pct": event.get("sentinel_velocity_pct"),
        "sentinel_quote_volume": event.get("sentinel_quote_volume"),
        "sentinel_volume_delta": event.get("sentinel_volume_delta"),
        "sentinel_scan_result": event.get("sentinel_scan_result", ""),
        "raw": event,
    }

def _decision_from_signal(signal: dict) -> dict:
    status = str(signal.get("event") or signal.get("status") or "SIGNAL").upper()
    side = str(signal.get("trade_side") or signal.get("side") or "").lower()
    score = signal.get("net_score", signal.get("score", signal.get("vpb_score", 0)))
    reasons = signal.get("reasons") or signal.get(f"reasons_{side}") or []
    if isinstance(reasons, list):
        reason = "+".join(str(x) for x in reasons[:6])
    else:
        reason = str(reasons)
    return {
        "time": signal.get("time") or signal.get("ts") or str(datetime.now(CST)),
        "strategy": "C/v14",
        "symbol": signal.get("symbol", ""),
        "status": status,
        "category": signal.get("category") or "entry_candidate",
        "side": side,
        "score": score,
        "raw_score": score,
        "timeframe": signal.get("timeframe") or signal.get("tf") or "",
        "reason": reason,
        "source": "signal",
        "event": status,
        "can_trade": signal.get("can_trade"),
        "entry_threshold": signal.get("entry_threshold"),
        "raw_signal_counted": signal.get("raw_signal_counted", False),
        "signal_schema": signal.get("signal_schema") or SIGNAL_SCHEMA_VERSION,
        "decision_stage": signal.get("decision_stage") or "entry_candidate",
        "filter_layer": signal.get("filter_layer") or "entry_gate",
        "reasons": reasons,
        "atr_pct": signal.get("atr_pct"),
        "bb_pos": signal.get("bb_pos"),
        "rsi": signal.get("rsi"),
        "adx": signal.get("adx"),
        "vol_ratio": signal.get("vol_ratio"),
        "mfi": signal.get("mfi"),
        "st_flipped": signal.get("st_flipped"),
        "sentinel": signal.get("sentinel", False),
        "sentinel_reason": signal.get("sentinel_reason", ""),
        "sentinel_change_pct": signal.get("sentinel_change_pct"),
        "sentinel_velocity_pct": signal.get("sentinel_velocity_pct"),
        "sentinel_quote_volume": signal.get("sentinel_quote_volume"),
        "sentinel_volume_delta": signal.get("sentinel_volume_delta"),
        "raw": signal,
    }

def log_event(event: dict):
    write_jsonl_with_daily_shard(EVENTS_LOG, event)
    write_event_store({**_decision_from_event(event), "raw_event": event}, "C/v14/events")
    log_decision(_decision_from_event(event), persist_event_store=False)

def _log_sentinel_scan_event(event: dict):
    """Log sentinel scan to dedicated sentinel_scans table (not events)."""
    write_jsonl_with_daily_shard(EVENTS_LOG, event)
    EVENT_STORE.write_sentinel_scan(event, source="C/v14/events")
    log_decision(_decision_from_event(event), persist_event_store=False)

def log_trade(trade: dict):
    write_jsonl_with_daily_shard(TRADES_LOG, trade)
    write_event_store({"strategy": "C/v14", "event": "TRADE", "category": "trade", **trade}, "C/v14/trades")

def log_signal(signal: dict):
    write_jsonl_with_daily_shard(SIGNAL_LOG, signal)
    write_event_store({**_decision_from_signal(signal), "raw_signal": signal}, "C/v14/signals")
    log_decision(_decision_from_signal(signal), persist_event_store=False)

def log_operation(op: dict):
    write_jsonl_with_daily_shard(OPERATION_LOG, op)

def log_system(state: dict):
    write_jsonl_with_daily_shard(SYSTEM_LOG, state)
    write_event_store({"strategy": "C/v14", "event": "SYSTEM", **state}, "C/v14/system")


# ═══════════════════════════════════════════════════════════════
# 扫描器主逻辑
# ═══════════════════════════════════════════════════════════════
class Scanner:
    def __init__(self):
        # v14优化(2026-05-08): key改为(sym, side)支持双向持仓独立监控
        self.positions: dict[str, dict[tuple, SimPosition]] = {tf: {} for tf in TIMEFRAMES}
        self.closed_trades: list[dict] = []
        self.max_positions = MAX_POSITIONS_PER_TF
        self.leverage = LEVERAGE
        self.scan_count = 0
        self.start_time = datetime.now(CST).isoformat()
        self.cooldowns: dict[str, dict[str, datetime]] = {tf: {} for tf in TIMEFRAMES}
        # 连续亏损追踪
        self.consecutive_losses: int = 0
        self.last_loss_time: Optional[datetime] = None
        # 同币种止损计数
        self.sl_counts: dict[str, int] = {}
        self._sl_counts_date: str = datetime.now(CST).strftime("%Y-%m-%d")
        # 价格停滞检测
        self.recent_entry_prices: dict[str, list[float]] = {}
        # 交易所客户端
        self.client: ExchangeClient = get_client()
        self.strategy_engine = StrategyEngine("C/v14", analyze_symbol_v14)
        self.execution = ExecutionEngine(self.client, "C/v14")
        self.risk_engine = RiskEngine(RiskLimits(
            max_total_positions=MAX_TOTAL_POSITIONS,
            max_positions_per_side=MAX_POS_PER_SIDE,
            min_available_balance_pct=MIN_AVAILABLE_BALANCE_PCT,
            min_available_balance_usdt=MIN_AVAILABLE_BALANCE_USDT,
        ))
        # 启动时同步交易所状态
        self._sync_exchange_state()

    def _pos_count(self, tf: str) -> int:
        return len(self.positions.get(tf, {}))

    def _total_local_positions(self) -> int:
        return sum(len(pos) for pos in self.positions.values())

    def _total_exchange_positions(self) -> int:
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
            if cached_state:
                return count_active_positions(cached_state.positions)
        except Exception as e:
            logger.debug(f"  中心账户状态持仓数检查失败: {e}")
        return self._total_local_positions()

    def _exchange_side_count(self, side: str) -> int:
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
            if cached_state:
                return count_side_positions(cached_state.positions, side)
        except Exception as e:
            logger.debug(f"  中心账户状态方向持仓数检查失败: {e}")
        return sum(1 for tf_pos in self.positions.values() for (_, sd) in tf_pos if sd == side)

    def _exchange_symbol_position(self, symbol: str) -> dict:
        """Return an existing exchange position for symbol, if any."""
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
            if cached_state:
                return find_symbol_position(cached_state.positions, symbol)
        except Exception as e:
            logger.debug(f"  中心账户状态同币种持仓检查失败: {symbol} {e}")
        return {}

    def _account_balance_summary(self) -> tuple[float, float]:
        cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
        if not cached_state:
            logger.debug("  中心账户状态不可用，余额摘要返回 0，避免 signed REST")
            return 0.0, 0.0
        return usdt_balance_summary(cached_state.balance)

    def _can_open_new_position(self, risk_usdt: float, now_str: str, tf: str, sym: str, side: str, score: float) -> bool:
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
            state_gate = evaluate_account_state_available_gate(account_state_available=bool(cached_state))
            if not state_gate.allowed:
                logger.info(f"  ⏸️ [{tf}] {sym} 中心账户状态不可用，暂停新开仓以避免 signed REST 压力")
                log_event({
                    "time": now_str, "event": "OPEN_SKIPPED", "symbol": sym,
                    "side": side, "score": score, "timeframe": tf,
                    "skip_reason": "中心账户状态不可用，暂停新开仓以避免 signed REST 压力",
                    "risk_category": "account_state_unavailable",
                    "decision_stage": "risk_gate",
                    "filter_layer": "risk",
                    "strategy_gate_case": strategy_gate_case(
                        name="c_v14_account_state_available",
                        gate="account_state_available",
                        inputs={"account_state_available": False},
                        decision=state_gate,
                        meta={"strategy": "C/v14", "timeframe": tf},
                    ),
                    **sentinel_fields(sym),
                })
                return False
            exchange_positions = cached_state.positions
            total_positions = max(self._total_local_positions(), count_active_positions(exchange_positions))
            side_count = count_side_positions(exchange_positions, side)
        except Exception as e:
            logger.debug(f"  开仓风控快照读取失败: {e}")
            state_gate = evaluate_account_state_available_gate(account_state_available=False, read_error=True)
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": sym,
                "side": side, "score": score, "timeframe": tf,
                "skip_reason": "中心账户状态读取失败，暂停新开仓以避免 signed REST 压力",
                "risk_category": "account_state_unavailable",
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                "strategy_gate_case": strategy_gate_case(
                    name="c_v14_account_state_read_failed",
                    gate="account_state_available",
                    inputs={"account_state_available": False, "read_error": True},
                    decision=state_gate,
                    meta={"strategy": "C/v14", "timeframe": tf, "error": str(e)[:160]},
                ),
                **sentinel_fields(sym),
            })
            return False
        balance = cached_state.balance
        decision = self.risk_engine.check_entry(
            total_positions=total_positions,
            side_positions=side_count,
            balance=balance,
            risk_usdt=risk_usdt,
        )
        if not decision.allowed:
            logger.info(f"  ⏭️ [{tf}] {sym} {decision.reason}，暂停新开仓")
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": sym,
                "side": side, "score": score, "timeframe": tf,
                "skip_reason": decision.reason,
                "risk_category": decision.category,
                "total_positions": decision.total_positions,
                "side_positions": decision.side_positions,
                "available": round(decision.available_balance, 4),
                "reserve": round(decision.reserve_required, 4),
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                **sentinel_fields(sym),
            })
            return False
        return True

    def _has_position(self, sym: str, side: str = None) -> bool:
        """检查是否已有某币种/方向的持仓（跨周期）"""
        for tf in TIMEFRAMES:
            if side:
                if (sym, side) in self.positions.get(tf, {}):
                    return True
            else:
                for (s, sd) in self.positions.get(tf, {}):
                    if s == sym:
                        return True
        return False

    def _has_position_in_tf(self, tf: str, sym: str, side: str = None) -> bool:
        """检查指定周期是否已有某币种/方向的持仓"""
        if side:
            return (sym, side) in self.positions.get(tf, {})
        for (s, sd) in self.positions.get(tf, {}):
            if s == sym:
                return True
        return False

    def _passes_15m_confirmation(self, sym: str, side: str, entry_score: float) -> tuple[bool, str]:
        sig = self.strategy_engine.analyze(sym, CONFIRM_TIMEFRAME)
        decision = evaluate_c_v14_confirmation_gate(
            side=side,
            entry_score=entry_score,
            confirm_signal=sig,
            confirm_timeframe=CONFIRM_TIMEFRAME,
            no_confirm_high_score_pass=NO_CONFIRM_HIGH_SCORE_PASS,
            weak_confirm_min_score=WEAK_CONFIRM_MIN_SCORE,
            confirm_min_score=CONFIRM_MIN_SCORE,
        )
        return decision.allowed, decision.reason

    def _passes_tail_guard(self, sig: dict, side: str) -> tuple[bool, str]:
        """Reduce low-score entries that are likely chasing the tail of a move."""
        decision = evaluate_c_v14_tail_guard(
            signal=sig,
            side=side,
            tail_guard_min_score=TAIL_GUARD_MIN_SCORE,
            tail_guard_long_bb_pos=TAIL_GUARD_LONG_BB_POS,
            tail_guard_short_bb_pos=TAIL_GUARD_SHORT_BB_POS,
            tail_guard_min_vol_ratio=TAIL_GUARD_MIN_VOL_RATIO,
            tail_guard_max_atr_pct=TAIL_GUARD_MAX_ATR_PCT,
        )
        return decision.allowed, decision.reason

    def _sync_exchange_state(self):
        """启动时同步交易所账户状态"""
        logger.info("同步交易所账户状态...")
        cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
        if not cached_state:
            logger.warning("  中心账户状态不可用，跳过启动同步，避免 signed REST 探测")
            return

        logger.info("  使用中心账户状态恢复本地持仓；跳过启动持仓模式 REST 检查")

        positions = cached_state.positions
        active_positions = []
        if isinstance(positions, list):
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    active_positions.append(pos)
        if active_positions:
            logger.info(f"  现有持仓: {len(active_positions)} 个")
            for pos in active_positions:
                sym = pos.get("symbol", "")
                pos_side = infer_position_side(pos)[0].lower()
                amt = pos.get("positionAmt", "0")
                entry_px = pos.get("entryPrice", "0")
                logger.info(f"    {sym} {pos_side} {amt} @ {entry_px}")

                pos_key = (sym, pos_side)
                already_tracked = any(
                    pos_key in self.positions.get(tf, {})
                    for tf in TIMEFRAMES
                )
                if not already_tracked and float(amt) != 0:
                    target_tf = None
                    for tf in TIMEFRAMES:
                        if self._pos_count(tf) < self.max_positions:
                            target_tf = tf
                            break
                    if target_tf:
                        entry_price = float(entry_px) if entry_px else 0
                        default_atr = entry_price * 0.02
                        if default_atr <= 0:
                            default_atr = entry_price * 0.01
                        # v14优化：分档ATR止损
                        atr_pct_sync = (default_atr / entry_price) if entry_price > 0 else 0.02
                        sl_m = calc_sl_mult_v14(atr_pct_sync)
                        if pos_side == "long":
                            sl = round(entry_price - default_atr * sl_m, 6)
                        else:
                            sl = round(entry_price + default_atr * sl_m, 6)
                        # v14: TP用阶梯，设一个大的安全网
                        tp = round(entry_price + default_atr * TP_MAX_MULT * (1 if pos_side == "long" else -1), 6)

                        restored_pos = SimPosition(
                            symbol=sym, side=pos_side,
                            entry_price=entry_price,
                            size=abs(float(amt)),
                            leverage=self.leverage,
                            stop_loss=sl, take_profit=tp,
                            atr_at_entry=default_atr,
                            entry_time=datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " [恢复]",
                            entry_score=0,
                            entry_reason="启动时从交易所恢复",
                            timeframe=target_tf,
                            sl_mult=sl_m, tp_mult=TP_MAX_MULT,
                            trail_activate=TRAILING_ACTIVATE.get(target_tf, 1.5),
                            trail_pullback=TRAILING_PULLBACK.get(target_tf, 1.5),
                            order_id="restored",
                            exchange_qty=abs(float(amt)),
                        )
                        self.positions[target_tf][pos_key] = restored_pos
                        logger.info(f"    ↩️ 已恢复到内存[{target_tf}]: {sym} {pos_side} SL={sl}")
        else:
            logger.info("  当前无持仓 ✓")

        bal = cached_state.balance
        if isinstance(bal, list):
            for item in bal:
                if item.get("asset") == "USDT":
                    logger.info(f"  余额: 总计={item.get('walletBalance', '0')} USDT, 可用={item.get('availableBalance', '0')} USDT")
        elif isinstance(bal, dict):
            for item in bal.get("assets", []):
                if item.get("asset") == "USDT":
                    logger.info(f"  余额: 总计={item.get('walletBalance', '0')} USDT, 可用={item.get('availableBalance', '0')} USDT")

        logger.info("交易所状态同步完成")

    def _sync_exchange_positions(self, now_str: str, now_dt: datetime = None):
        """同步交易所持仓状态"""
        cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
        if not cached_state:
            logger.debug("  中心账户状态不可用，跳过本轮交易所持仓同步")
            return
        exchange_positions = cached_state.positions
        exchange_active = {}
        if isinstance(exchange_positions, list):
            for p in exchange_positions:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    sym = p.get("symbol", "")
                    pos_side = infer_position_side(p)[0].lower()
                    exchange_active[(sym, pos_side)] = p

        for tf in list(self.positions.keys()):
            to_remove = []
            for key, pos in self.positions[tf].items():
                sym = pos.symbol
                if pos.exchange_qty <= 0:
                    continue
                if key not in exchange_active:
                    exit_price = pos.entry_price
                    try:
                        exit_price = fetch_current_price(sym)
                    except Exception:
                        pass

                    if pos.side == "long":
                        pnl = (exit_price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
                        pnl_usd = (exit_price - pos.entry_price) * pos.size
                    else:
                        pnl = (pos.entry_price - exit_price) / pos.entry_price * 100 * pos.leverage
                        pnl_usd = (pos.entry_price - exit_price) * pos.size

                    trade = {
                        "symbol": sym, "side": pos.side,
                        "entry_price": pos.entry_price, "exit_price": round(exit_price, 4),
                        "entry_time": pos.entry_time, "exit_time": now_str,
                        "exit_reason": "交易所自动平仓", "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "leverage": pos.leverage, "score": pos.entry_score,
                        "reason": pos.entry_reason,
                        "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                        "timeframe": tf,
                        "order_id": pos.order_id,
                        "exchange_qty": pos.exchange_qty,
                    }
                    self.closed_trades.append(trade)
                    log_trade(trade)
                    log_event({
                        "time": now_str, "event": "CLOSE", "symbol": sym,
                        "side": pos.side, "exit_price": round(exit_price, 4),
                        "reason": "交易所自动平仓", "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "entry_price": pos.entry_price, "timeframe": tf,
                    })
                    logger.info(f"  {'🟢' if pnl > 0 else '🔴'} 自动平仓[{tf}]: {sym} {pos.side} | PnL={pnl:+.2f}% (${pnl_usd:+.4f})")
                    to_remove.append(key)

                    if now_dt:
                        cooldown_end = now_dt + timedelta(minutes=COOLDOWN_MINUTES)
                        self.cooldowns[tf][sym] = cooldown_end

            for key in to_remove:
                del self.positions[tf][key]

    def _enforce_hard_stop_on_exchange(self, now_str: str, now_dt: datetime = None):
        """交易所级硬顶兜底：本地未恢复的仓位也必须被风控覆盖。"""
        cached_state = load_cached_account_state(PROJECT_ROOT, "C/v14")
        if not cached_state:
            logger.debug("  中心账户状态不可用，跳过本轮交易所硬顶扫描")
            return
        exchange_positions = cached_state.positions

        for p in exchange_positions:
            try:
                amt = float(p.get("positionAmt", 0))
            except Exception:
                continue
            if abs(amt) <= 0.0001:
                continue
            sym = p.get("symbol", "")
            side, side_source = infer_position_side(p)
            side = side.lower()
            entry = float(p.get("entryPrice", 0) or 0)
            mark = float(p.get("markPrice", 0) or 0)
            if entry <= 0 or mark <= 0:
                continue
            loss_pct = leveraged_loss_pct(p, side)
            if loss_pct < MAX_LOSS_PCT:
                continue
            try:
                close_exec = self.execution.close_position(CloseRequest(
                    symbol=sym,
                    side=side,
                    quantity=abs(amt),
                    cancel_open_orders=True,
                ))
                exchange_ok = close_exec.success
                logger.warning(f"  交易所硬顶平仓: {sym} {side} loss={loss_pct:.2f}% >= {MAX_LOSS_PCT:.2f}%")
                log_event({
                    "time": now_str, "event": "FORCED_CLOSE" if exchange_ok else "FORCED_CLOSE_FAILED", "symbol": sym,
                    "side": side, "reason": f"交易所硬顶{MAX_LOSS_PCT:.0f}%",
                    "loss_pct": round(loss_pct, 2), "entry_price": entry,
                    "mark_price": mark, "qty": abs(amt),
                    "side_source": side_source,
                    "raw_position_side": p.get("positionSide", ""),
                    "failure_reason": "" if exchange_ok else close_exec.reason,
                    "exchange_status": close_exec.status,
                    "exchange_close_success": exchange_ok,
                })
                if exchange_ok and now_dt:
                    self.sl_counts[sym] = self.sl_counts.get(sym, 0) + 1
                    cooldown_end = now_dt + timedelta(hours=SYMBOL_SL_BAN_HOURS)
                    for cd_tf in TIMEFRAMES:
                        self.cooldowns[cd_tf][sym] = cooldown_end
            except Exception as e:
                logger.error(f"  交易所硬顶平仓失败: {sym} {side} {e}")

    def _check_exits(self, now_str: str, now_dt: datetime = None):
        """检查所有持仓的止盈止损"""
        for tf in TIMEFRAMES:
            to_remove = []
            for key, pos in self.positions[tf].items():
                try:
                    current_price = fetch_current_price(pos.symbol)
                    if current_price <= 0:
                        continue

                    reason = pos.check_exit(current_price)

                    # v14优化(2026-05-08): 震荡仓清理
                    # 持仓超过48h且盈亏在±3%以内 → 重新评估信号方向
                    # 只有当前信号方向与持仓方向相反时才平仓，防止底部被刷出去
                    if reason is None and now_dt and "[恢复]" not in pos.entry_time:
                        try:
                            entry_dt = datetime.strptime(pos.entry_time[:19], "%Y-%m-%d %H:%M:%S")
                            age_hours = (now_dt - entry_dt.replace(tzinfo=CST)).total_seconds() / 3600
                            if age_hours > 48:
                                if pos.side == "long":
                                    pnl_pct_abs = abs((current_price - pos.entry_price) / pos.entry_price * 100)
                                else:
                                    pnl_pct_abs = abs((pos.entry_price - current_price) / pos.entry_price * 100)
                                if pnl_pct_abs <= 3.0:
                                    # 重新评估：当前信号是否还支持持仓方向
                                    try:
                                        sig = self.strategy_engine.analyze(pos.symbol, tf)
                                        if sig is not None:
                                            current_direction = sig.get("trade_side", "")
                                            if current_direction != pos.side:
                                                reason = f"震荡平仓（持仓{age_hours:.0f}h，波动仅{pnl_pct_abs:.1f}%，信号已反转→{current_direction}）"
                                            else:
                                                logger.debug(f"  ⏸️ {pos.symbol} {pos.side} 震荡{age_hours:.0f}h但信号仍支持，继续持有")
                                    except Exception:
                                        # 无法获取信号时，保守平仓
                                        reason = f"震荡平仓（持仓{age_hours:.0f}h，波动仅{pnl_pct_abs:.1f}%，信号获取失败）"
                        except Exception:
                            pass

                    if reason is None:
                        continue

                    # 触发平仓
                    if pos.side == "long":
                        pnl = (current_price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
                        pnl_usd = (current_price - pos.entry_price) * pos.size
                    else:
                        pnl = (pos.entry_price - current_price) / pos.entry_price * 100 * pos.leverage
                        pnl_usd = (pos.entry_price - current_price) * pos.size

                    # 交易所平仓
                    close_exec = self.execution.close_position(CloseRequest(
                        symbol=pos.symbol,
                        side=pos.side,
                        quantity=pos.exchange_qty,
                        cancel_open_orders=True,
                    ))
                    exchange_ok = close_exec.success
                    if not exchange_ok:
                        logger.error(f"  平仓失败: {pos.symbol} {pos.side} {close_exec.reason}")
                        log_event({
                            "time": now_str, "event": "CLOSE_FAILED", "symbol": pos.symbol,
                            "side": pos.side, "exit_price": round(current_price, 4),
                            "reason": reason, "failure_reason": close_exec.reason,
                            "exchange_status": close_exec.status,
                            "exchange_close_success": False,
                            "timeframe": tf,
                        })
                        continue

                    trade = {
                        "symbol": pos.symbol, "side": pos.side,
                        "entry_price": pos.entry_price, "exit_price": round(current_price, 4),
                        "entry_time": pos.entry_time, "exit_time": now_str,
                        "exit_reason": reason, "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "leverage": pos.leverage, "score": pos.entry_score,
                        "reason": pos.entry_reason,
                        "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                        "timeframe": tf,
                        "order_id": pos.order_id, "exchange_qty": pos.exchange_qty,
                        "exchange_close_success": exchange_ok,
                    }
                    self.closed_trades.append(trade)
                    log_trade(trade)

                    event = {
                        "time": now_str, "event": "CLOSE", "symbol": pos.symbol,
                        "side": pos.side, "exit_price": round(current_price, 4),
                        "reason": reason, "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "entry_price": pos.entry_price, "entry_time": pos.entry_time,
                        "timeframe": tf,
                        "exchange_close_success": exchange_ok,
                    }
                    log_event(event)

                    emoji = '🟢' if pnl > 0 else '🔴'
                    logger.info(
                        f"  {emoji} 平仓[{tf}]: {pos.symbol} {pos.side} | {reason} "
                        f"| PnL={pnl:+.2f}% (${pnl_usd:+.4f})"
                    )

                    # 连续亏损追踪
                    if pnl < 0:
                        self.consecutive_losses += 1
                        self.last_loss_time = now_dt
                        # 止损计数
                        if "止损" in reason:
                            self.sl_counts[pos.symbol] = self.sl_counts.get(pos.symbol, 0) + 1
                    else:
                        self.consecutive_losses = 0

                    # 冷却期
                    if now_dt:
                        risk_exit = ("止损" in reason) or ("最大亏损" in reason)
                        if risk_exit:
                            self.sl_counts[pos.symbol] = self.sl_counts.get(pos.symbol, 0) + 1
                        cooldown_minutes = SYMBOL_SL_BAN_HOURS * 60 if risk_exit else COOLDOWN_MINUTES
                        cooldown_end = now_dt + timedelta(minutes=cooldown_minutes)
                        self.cooldowns[tf][pos.symbol] = cooldown_end
                        if risk_exit:
                            for cd_tf in TIMEFRAMES:
                                self.cooldowns[cd_tf][pos.symbol] = cooldown_end

                    to_remove.append(key)
                except Exception as e:
                    logger.error(f"  检查平仓异常 {pos.symbol}: {e}")

            for key in to_remove:
                del self.positions[tf][key]

    def scan_cycle(self):
        """执行一轮完整扫描"""
        self.scan_count += 1
        now = datetime.now(CST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"═══ [v14] 第 {self.scan_count} 轮扫描 @ {now_str} ═══")

        # 每日重置止损计数
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._sl_counts_date:
            self.sl_counts.clear()
            self._sl_counts_date = today_str
            self.consecutive_losses = 0
            logger.info("  📅 新的一天，计数已重置")

        # 0. 同步交易所持仓
        self._sync_exchange_positions(now_str, now_dt=now)

        # 0.5 交易所级硬顶兜底
        self._enforce_hard_stop_on_exchange(now_str, now_dt=now)

        # 1. 检查止盈止损
        self._check_exits(now_str, now_dt=now)

        # 1.5 清理过期冷却期
        for tf in TIMEFRAMES:
            expired = [sym for sym, cd in self.cooldowns.get(tf, {}).items() if now >= cd]
            for sym in expired:
                del self.cooldowns[tf][sym]

        # 否决条件: 连续亏损冷却
        cooldown_gate = evaluate_consecutive_loss_cooldown_gate(
            consecutive_losses=self.consecutive_losses,
            last_loss_time=self.last_loss_time,
            now=now,
            min_consecutive_losses=5,
            cooldown_minutes=COOLDOWN_CONSECUTIVE,
        )
        if not cooldown_gate.allowed:
            remaining = int((cooldown_gate.evidence or {}).get("remaining_minutes") or 0)
            logger.info(f"  ⏸️ 连续亏损{self.consecutive_losses}次，冷却中（剩余{remaining}分钟）")
            log_system({
                "ts": now_str, "scan_count": self.scan_count,
                "positions": sum(self._pos_count(tf) for tf in TIMEFRAMES),
                "consecutive_losses": self.consecutive_losses,
                "status": "cooldown",
            })
            return

        # 1.8 BTC大盘趋势判断（v14优化 2026-05-08）
        btc_trend = fetch_btc_trend()
        self._btc_trend = btc_trend
        trend_dir = btc_trend.get("direction", "neutral")
        trend_str = btc_trend.get("strength", 0)
        if trend_dir != "neutral":
            logger.info(f"  📊 BTC趋势: {trend_dir.upper()} 强度={trend_str:.0f} 斜率={btc_trend.get('slope',0):.2f}%")

        # 2. 获取扫描列表
        try:
            top_limit = env_int("SCANNER_C_TOP_SYMBOLS", 100)
            sentinel_limit = env_int("SCANNER_C_SENTINEL_LIMIT", 40)
            symbols = merge_sentinel_symbols(fetch_top_symbols(top_limit), limit=sentinel_limit)
        except Exception as e:
            logger.error(f"获取扫描列表失败: {e}")
            return

        logger.info(f"扫描 {len(symbols)} 个币种 × {len(TIMEFRAMES)} 个周期...")

        # 3. 逐周期扫描
        all_signals = {}
        for tf in TIMEFRAMES:
            tf_signals = []
            for sym in symbols:
                blacklist_gate = evaluate_symbol_blacklist_gate(symbol=sym, blacklisted_symbols=ATR_ZERO_BLACKLIST, reason="ATR=0黑名单")
                if not blacklist_gate.allowed:
                    log_sentinel_scan(sym, tf, "pre_filter_rejected", "ATR=0黑名单", decision_stage="pre_filter")
                    continue
                # v14修复(2026-05-08): 双向持仓独立检查
                timeframe_position_gate = evaluate_timeframe_position_gate(has_timeframe_position=self._has_position_in_tf(tf, sym))
                if not timeframe_position_gate.allowed:
                    log_sentinel_scan(sym, tf, "pre_filter_rejected", "本周期已有持仓", decision_stage="pre_filter")
                    continue
                cd = self.cooldowns.get(tf, {}).get(sym)
                cooldown_gate = evaluate_symbol_cooldown_gate(cooldown_until=cd, now=now)
                if not cooldown_gate.allowed:
                    log_sentinel_scan(sym, tf, "pre_filter_rejected", "冷却期内", decision_stage="pre_filter")
                    continue
                try:
                    result = self.strategy_engine.analyze(sym, tf)
                    if result and result["can_trade"]:
                        tf_signals.append(result)
                    elif result:
                        log_sentinel_scan(
                            sym, tf, "strategy_rejected", "策略层 can_trade=False",
                            side=result.get("trade_side", ""),
                            score=abs(float(result.get("net_score") or 0)),
                        )
                    else:
                        log_sentinel_scan(sym, tf, "no_signal", "策略分析无信号")
                except Exception as e:
                    logger.debug(f"  {sym}({tf}) 分析失败: {e}")
                    log_sentinel_scan(sym, tf, "analysis_error", f"策略分析异常: {str(e)[:120]}")

            tf_signals.sort(key=lambda x: abs(x["net_score"]), reverse=True)
            all_signals[tf] = tf_signals

        # 4. 输出入场候选信号
        # 15m 是确认周期，低于真实入场门槛的 1h 结果只是原始分析候选；
        # 只有能进入开仓门控的 1h 候选才写 SIGNAL，避免入口页把噪声当成可交易信号。
        raw_signal_count = sum(len(s) for s in all_signals.values())
        entry_signal_count = 0
        for tf in ENTRY_TIMEFRAMES:
            for sig in all_signals[tf]:
                side = sig["trade_side"]
                abs_score = abs(float(sig.get("net_score") or 0))
                threshold, trend_penalty = entry_threshold_for(tf, side, trend_dir, trend_str)
                if abs_score > SCORE_MAX or abs_score < threshold:
                    continue
                reasons = sig["reasons_long"] if side == "long" else sig["reasons_short"]
                logger.info(
                    f"  📡 入场候选[{tf}] {sig['symbol']}: score={sig['net_score']:+.0f} "
                    f"threshold={threshold:.0f} side={side} | {'|'.join(reasons)}"
                )
                log_signal({
                    "ts": now_str,
                    "event": "SIGNAL",
                    "category": "entry_candidate",
                    "decision_stage": "entry_candidate",
                    "filter_layer": "entry_gate",
                    "strategy": "v14_multi_dim",
                    "signal_schema": SIGNAL_SCHEMA_VERSION,
                    "sample_expansion_policy": SAMPLE_EXPANSION_POLICY,
                    "timeframe": tf,
                    "symbol": sig["symbol"],
                    "net_score": sig["net_score"],
                    "trade_side": side,
                    "entry_threshold": threshold,
                    "trend_penalty": trend_penalty,
                    "raw_signal_counted": False,
                    "reasons": reasons,
                    "price": sig.get("price", 0),
                    "atr": sig.get("atr", 0),
                    "atr_pct": sig.get("atr_pct", 0),
                    "vol_ratio": sig.get("vol_ratio", 0),
                    "rsi": sig.get("rsi", 0),
                    "adx": sig.get("adx", 0),
                    "bb_pos": sig.get("bb_pos", 0),
                    "mfi": sig.get("mfi", 0),
                    "st_flipped": sig.get("st_flipped", False),
                    **sentinel_fields(sig["symbol"]),
                })
                entry_signal_count += 1

        # 5. 开仓（1h优先，15m只做确认）
        opened_symbols = set()

        for tf in ENTRY_TIMEFRAMES:
            for sig in all_signals[tf]:
                if self._pos_count(tf) >= self.max_positions:
                    break
                sym = sig["symbol"]
                side = sig["trade_side"]
                if self._has_position_in_tf(tf, sym, side):
                    continue
                if sym in opened_symbols:
                    continue

                # 否决: 同方向持仓已存在（跨周期检查）
                already_holding = self._has_position(sym, side)
                same_side_gate = evaluate_same_side_position_gate(has_same_side_position=already_holding)
                if not same_side_gate.allowed:
                    log_sentinel_scan(
                        sym, tf, "position_rejected", "同方向持仓已存在",
                        side=side, score=abs(sig["net_score"]),
                        decision_stage="position_gate", filter_layer="risk",
                    )
                    continue

                # 否决: 评分超过上限
                abs_score = abs(sig["net_score"])
                score_max_gate = evaluate_score_max_gate(score=abs_score, score_max=SCORE_MAX)
                if not score_max_gate.allowed:
                    logger.info(f"  ⏭️ [{tf}] {sym} {score_max_gate.reason}，跳过（过热）")
                    log_sentinel_scan(
                        sym, tf, "score_rejected", score_max_gate.reason,
                        side=side, score=abs_score, decision_stage="score_gate",
                    )
                    continue

                # 否决: 同币种止损保护
                sl_cnt = self.sl_counts.get(sym, 0)
                symbol_sl_gate = evaluate_symbol_stop_loss_gate(
                    stop_loss_count=sl_cnt,
                    max_stop_loss_per_symbol=MAX_SL_PER_SYMBOL,
                )
                if not symbol_sl_gate.allowed:
                    logger.info(f"  ⏭️ [{tf}] {sym} 当日止损{sl_cnt}次已达上限，跳过")
                    log_sentinel_scan(
                        sym, tf, "cooldown_rejected", symbol_sl_gate.reason,
                        side=side, score=abs_score, decision_stage="cooldown",
                        filter_layer="risk",
                    )
                    continue

                # 否决: 同赛道分散（v14优化）
                sec = get_sector(sym)
                sector_counts = count_positions_by_sector(self.positions) if sec != "Other" else {}
                sector_gate = evaluate_sector_position_gate(
                    sector=sec,
                    sector_position_count=sector_counts.get(sec, 0),
                    max_positions_per_sector=MAX_POS_PER_SECTOR,
                )
                if not sector_gate.allowed:
                    logger.info(f"  ⏭️ [{tf}] {sym} {sector_gate.reason}，跳过")
                    log_sentinel_scan(
                        sym, tf, "risk_rejected", sector_gate.reason,
                        side=side, score=abs_score, decision_stage="sector_guard",
                        filter_layer="risk",
                    )
                    continue

                # 多头额外门槛 + 趋势惩罚
                threshold, trend_penalty = entry_threshold_for(tf, side, trend_dir, trend_str)
                if abs_score < threshold:
                    if trend_penalty > 0:
                        logger.info(f"  ⏭️ [{tf}] {sym} {side} 趋势门槛+{trend_penalty} → 需{threshold}分，实际{abs_score:.0f}分，跳过")
                    log_sentinel_scan(
                        sym, tf, "score_rejected", f"阈值未达:需{threshold}分，实际{abs_score:.0f}分",
                        side=side, score=abs_score, decision_stage="threshold",
                    )
                    continue

                confirmed, confirm_reason = self._passes_15m_confirmation(sym, side, abs_score)
                if not confirmed:
                    logger.info(f"  ⏭️ [{tf}] {sym} {side} {confirm_reason}")
                    log_event({
                        "time": now_str, "event": "OPEN_SKIPPED", "symbol": sym,
                        "side": side, "score": abs_score, "timeframe": tf,
                        "skip_reason": confirm_reason,
                        "decision_stage": "confirmation",
                        "filter_layer": "confirmation",
                        "sample_expansion_policy": SAMPLE_EXPANSION_POLICY,
                        **sentinel_fields(sym),
                    })
                    continue

                tail_ok, tail_reason = self._passes_tail_guard(sig, side)
                if not tail_ok:
                    logger.info(f"  skip [{tf}] {sym} {side} {tail_reason}")
                    log_event({
                        "time": now_str, "event": "OPEN_SKIPPED", "symbol": sym,
                        "side": side, "score": abs_score, "timeframe": tf,
                        "skip_reason": tail_reason,
                        "decision_stage": "tail_guard",
                        "filter_layer": "risk",
                        "sample_expansion_policy": SAMPLE_EXPANSION_POLICY,
                        "confirm_reason": confirm_reason,
                        "atr_pct": sig.get("atr_pct"),
                        "bb_pos": sig.get("bb_pos"),
                        "rsi": sig.get("rsi"),
                        "adx": sig.get("adx"),
                        "vol_ratio": sig.get("vol_ratio"),
                        "mfi": sig.get("mfi"),
                        "st_flipped": sig.get("st_flipped"),
                        **sentinel_fields(sym),
                    })
                    continue

                self._open_position(sig, now_str)
                opened_symbols.add(sym)

        # 心跳日志
        total_pos = sum(self._pos_count(tf) for tf in TIMEFRAMES)
        log_system({
            "ts": now_str, "scan_count": self.scan_count,
            "positions": total_pos,
            "consecutive_losses": self.consecutive_losses,
            "symbols_scanned": len(symbols),
            "signals_found": entry_signal_count,
            "entry_signals_found": entry_signal_count,
            "raw_signals_found": raw_signal_count,
            "signal_schema": SIGNAL_SCHEMA_VERSION,
            "status": "running",
        })

    def _open_position(self, sig: dict, now_str: str):
        """开仓"""
        tf = sig["timeframe"]
        side = sig["trade_side"]
        net_score = sig["net_score"]

        # 动态投入
        abs_score = abs(net_score)
        if 90 <= abs_score <= 100:
            risk_usdt = RISK_PER_TRADE_HIGH
        else:
            risk_usdt = RISK_PER_TRADE_USDT

        if side == "long":
            sl = sig["sl_long"]
            tp = sig["tp_long"]
            reasons = sig["reasons_long"]
        else:
            sl = sig["sl_short"]
            tp = sig["tp_short"]
            reasons = sig["reasons_short"]

        inst_id = sig["symbol"]
        price = sig["price"]
        atr = sig["atr"]

        existing_exchange_pos = self._exchange_symbol_position(inst_id)
        position_gate = evaluate_no_same_symbol_position_gate(
            has_exchange_position=bool(existing_exchange_pos),
            has_local_position=self._has_position(inst_id),
        )
        if not position_gate.allowed:
            existing_qty = float(existing_exchange_pos.get("positionAmt") or 0) if existing_exchange_pos else 0.0
            existing_side = infer_position_side(existing_exchange_pos)[0] if existing_exchange_pos else ""
            existing_entry = float(existing_exchange_pos.get("entryPrice") or 0) if existing_exchange_pos else 0.0
            logger.info(
                f"  ⏭️ [{tf}] {inst_id} 交易所/本地已有持仓，禁止同币种重复开仓 "
                f"side={existing_side or 'local'} qty={existing_qty:g}"
            )
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "score": abs_score, "timeframe": tf,
                "skip_reason": "同币种已有持仓，禁止重复开仓以避免聚合仓位偏离策略风险预算",
                "risk_category": "position_duplicate",
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                "existing_exchange_qty": existing_qty,
                "existing_exchange_side": existing_side,
                "existing_entry_price": existing_entry,
                "strategy_gate_case": strategy_gate_case(
                    name="c_v14_no_same_symbol_position",
                    gate="no_same_symbol_position",
                    inputs={
                        "has_exchange_position": bool(existing_exchange_pos),
                        "has_local_position": self._has_position(inst_id),
                    },
                    decision=position_gate,
                    meta={"strategy": "C/v14", "timeframe": tf},
                ),
                **sentinel_fields(inst_id),
            })
            return

        if not self._can_open_new_position(risk_usdt, now_str, tf, inst_id, side, abs_score):
            return

        # 价格停滞检测
        recent_prices = self.recent_entry_prices.get(inst_id, [])
        stale_price_gate = evaluate_c_v14_stale_entry_price_gate(recent_prices=recent_prices)
        if not stale_price_gate.allowed:
            logger.warning(f"  ⚠️ [{tf}] {inst_id} {stale_price_gate.reason}")
            ATR_ZERO_BLACKLIST.add(inst_id)
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "score": abs_score, "timeframe": tf,
                "skip_reason": stale_price_gate.reason,
                "decision_stage": "market_data_guard",
                "filter_layer": "market_data",
                "strategy_gate_case": strategy_gate_case(
                    name="c_v14_stale_entry_price",
                    gate="c_v14_stale_entry_price",
                    inputs={"recent_prices": recent_prices},
                    decision=stale_price_gate,
                    meta={"strategy": "C/v14", "timeframe": tf},
                ),
                **sentinel_fields(inst_id),
            })
            return

        # ATR保护
        market_gate = evaluate_c_v14_market_microstructure_gate(atr=atr)
        if not market_gate.allowed:
            logger.warning(f"  ⚠️ {inst_id}({tf}) ATR=0，跳过")
            ATR_ZERO_BLACKLIST.add(inst_id)
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "reason": market_gate.reason, "timeframe": tf,
                "decision_stage": "market_microstructure",
                "filter_layer": "market_data",
                "strategy_gate_case": strategy_gate_case(
                    name="c_v14_market_microstructure",
                    gate="c_v14_market_microstructure",
                    inputs={"atr": atr},
                    decision=market_gate,
                    meta={"strategy": "C/v14", "timeframe": tf},
                ),
                **sentinel_fields(inst_id),
            })
            return

        # 设置杠杆和保证金类型
        self.client.set_leverage(inst_id, self.leverage)
        self.client.set_margin_type(inst_id, "CROSSED")

        # 统一执行层计算数量并下单
        qty = self.execution.calc_quantity(inst_id, price, risk_usdt, self.leverage)
        quantity_gate = evaluate_positive_quantity_gate(quantity=qty)
        if not quantity_gate.allowed:
            logger.warning(f"  ⚠️ [{tf}] {inst_id} 计算数量为0，跳过")
            preflight = {}
            if hasattr(self.client, "validate_order_quantity"):
                raw_qty = (risk_usdt * self.leverage / price) if price else 0.0
                preflight = self.client.validate_order_quantity(inst_id, raw_qty, price, risk_usdt, self.leverage)
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "skip_reason": preflight.get("reason") or "qty=0",
                "reason": preflight.get("reason") or "qty=0",
                "code": preflight.get("code") or "qty=0",
                "score": abs_score,
                "risk_usdt": risk_usdt,
                "preflight": preflight,
                "timeframe": tf,
                "risk_category": "execution_preflight",
                "decision_stage": "execution_preflight",
                "filter_layer": "execution",
                "strategy_gate_case": strategy_gate_case(
                    name="c_v14_positive_quantity",
                    gate="positive_quantity",
                    inputs={"quantity": qty},
                    decision=quantity_gate,
                    meta={"strategy": "C/v14", "timeframe": tf},
                ),
                **sentinel_fields(inst_id),
            })
            return

        exec_result = self.execution.open_position(OpenRequest(
            symbol=inst_id,
            side=side,
            price=price,
            risk_usdt=risk_usdt,
            leverage=self.leverage,
            take_profit=tp,
            stop_loss=sl,
            quantity=qty,
            confirm_position=True,
        ))
        if not exec_result.success:
            execution_gate = evaluate_execution_result_gate(
                success=exec_result.success,
                preflight_rejected=exec_result.preflight_rejected,
                code=exec_result.code,
                reason=exec_result.reason,
                message=exec_result.message,
            )
            if execution_gate.gate == "execution_preflight":
                logger.info(f"  [{tf}] {inst_id} 执行预检跳过: {exec_result.reason}")
                log_event({
                    "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                    "side": side, "skip_reason": exec_result.reason,
                    "reason": exec_result.reason,
                    "code": exec_result.code,
                    "msg": exec_result.message,
                    "score": abs_score,
                    "risk_usdt": risk_usdt,
                    "preflight": exec_result.preflight_detail,
                    "timeframe": tf,
                    "risk_category": "execution_preflight",
                    "decision_stage": "execution_preflight",
                    "filter_layer": "execution",
                    "strategy_gate_case": strategy_gate_case(
                        name="c_v14_execution_result",
                        gate="execution_result",
                        inputs={
                            "success": exec_result.success,
                            "preflight_rejected": exec_result.preflight_rejected,
                            "code": exec_result.code,
                            "reason": exec_result.reason,
                            "message": exec_result.message,
                        },
                        decision=execution_gate,
                        meta={"strategy": "C/v14", "timeframe": tf},
                    ),
                    **sentinel_fields(inst_id),
                })
                return
            logger.error(f"  ❌ [{tf}] {inst_id} 开仓失败: {exec_result.reason}")
            log_event({
                "time": now_str, "event": "OPEN_FAILED", "symbol": inst_id,
                "side": side, "reason": exec_result.reason,
                "code": exec_result.code,
                "msg": exec_result.message,
                "score": abs_score,
                "risk_usdt": risk_usdt,
                "timeframe": tf,
                "decision_stage": "execution",
                "filter_layer": "execution",
                **sentinel_fields(inst_id),
            })
            return

        order_id = exec_result.order_id
        exchange_qty = exec_result.quantity or qty

        # 记录持仓（v14优化：分档ATR止损）
        sl_m = calc_sl_mult_v14(sig.get("atr_pct", 0.02))
        pos = SimPosition(
            symbol=inst_id, side=side,
            entry_price=price, size=exchange_qty,
            leverage=self.leverage,
            stop_loss=sl, take_profit=tp,
            atr_at_entry=atr,
            entry_time=now_str,
            entry_score=abs_score,
            entry_reason="|".join(reasons),
            timeframe=tf,
            sl_mult=sl_m, tp_mult=TP_MAX_MULT,
            trail_activate=TRAILING_ACTIVATE.get(tf, 1.5),
            trail_pullback=TRAILING_PULLBACK.get(tf, 1.5),
            order_id=order_id,
            exchange_qty=exchange_qty,
        )
        pos_key = (inst_id, side)
        self.positions[tf][pos_key] = pos

        # 更新价格记录
        self.recent_entry_prices.setdefault(inst_id, []).append(price)
        if len(self.recent_entry_prices[inst_id]) > 5:
            self.recent_entry_prices[inst_id] = self.recent_entry_prices[inst_id][-5:]

        # 设置冷却
        cooldown_end = datetime.now(CST) + timedelta(minutes=COOLDOWN_MINUTES)
        self.cooldowns[tf][inst_id] = cooldown_end

        logger.info(
            f"  ✅ 开仓[{tf}] {inst_id} {side} @ {price} | "
            f"score={abs_score:.0f} qty={exchange_qty} SL={sl} | {'|'.join(reasons)}"
        )
        log_event({
            "time": now_str, "event": "OPEN", "symbol": inst_id,
            "side": side, "price": price, "qty": exchange_qty,
            "leverage": self.leverage,
            "sl": sl, "tp": tp, "score": abs_score,
            "reasons": reasons, "risk_usdt": risk_usdt,
            "atr": atr, "timeframe": tf,
            "entry_reason": "|".join(reasons),
            "sample_expansion_policy": SAMPLE_EXPANSION_POLICY,
            "decision_stage": "open",
            "atr_pct": sig.get("atr_pct"),
            "bb_pos": sig.get("bb_pos"),
            "rsi": sig.get("rsi"),
            "adx": sig.get("adx"),
            "vol_ratio": sig.get("vol_ratio"),
            "mfi": sig.get("mfi"),
            "st_flipped": sig.get("st_flipped"),
            "order_id": order_id,
            **sentinel_fields(inst_id),
        })


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="v14 四维度策略扫描器 - Account C")
    parser.add_argument("--interval", type=int, default=120, help="扫描间隔秒数（默认120=2分钟）")
    parser.add_argument("--once", action="store_true", help="只扫描一次然后退出")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("v14 四维度策略扫描器启动")
    logger.info(f"  周期: {TIMEFRAMES}")
    logger.info(f"  杠杆: {LEVERAGE}x")
    logger.info(f"  阈值: {SCORE_MIN} (趋势惩罚+15)")
    logger.info(f"  浮动止损: activate={TRAILING_ACTIVATE} pullback={TRAILING_PULLBACK}")
    logger.info(f"  阶梯止损: {TP_STAGES}")
    logger.info(f"  冷却: {COOLDOWN_MINUTES}min / 连续亏损: {COOLDOWN_CONSECUTIVE}min")
    logger.info(f"  最大亏损: {MAX_LOSS_PCT}%")
    logger.info(f"  BTC趋势过滤: bull→空头+15 / bear→多头+15")
    logger.info(f"  震荡平仓: >48h 且 ±3%以内")
    logger.info("=" * 60)

    log_operation({
        "ts": datetime.now(CST).isoformat(),
        "event": "START",
        "config": {
            "timeframes": TIMEFRAMES, "leverage": LEVERAGE,
            "score_min": SCORE_MIN, "score_max": SCORE_MAX,
            "sl_mult": SL_MULT, "tp_stages": TP_STAGES,
            "cooldown": COOLDOWN_MINUTES, "max_loss": MAX_LOSS_PCT,
        },
    })

    scanner = Scanner()

    if args.once:
        scanner.scan_cycle()
        return

    while True:
        try:
            scanner.scan_cycle()
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出...")
            log_operation({"ts": datetime.now(CST).isoformat(), "event": "STOP", "reason": "keyboard_interrupt"})
            break
        except Exception as e:
            logger.error(f"扫描异常: {e}", exc_info=True)
            log_operation({"ts": datetime.now(CST).isoformat(), "event": "ERROR", "error": str(e)})

        logger.info(f"下次扫描: {args.interval}秒后...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
