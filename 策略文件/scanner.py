"""半木夏策略 Top100 扫描器（Binance Testnet 实单 + VPB双策略）

以 15 分钟和 30 分钟为双基准，对 Binance USDT 永续合约 Top100 运行半木夏+VPB信号检测，
通过 Binance Testnet API 真实下单开仓/平仓，记录所有事件，定时生成交易报告。

核心特性：
  - 双周期独立分析，各自拥有持仓池（最多6个）
  - 多周期共振检测：同币种在15m和30m同向信号时额外加分
  - 止盈止损追踪（只用浮动止损 + 止盈 + 最大亏损硬性限制，v11: 去掉固定止损）
  - Binance Testnet 真实下单（市价开仓 + 止盈止损追踪）
  - VPB量价突破策略：独立开仓信号，与半木夏互补
  - ATR=0异常币种自动跳过黑名单
  - 成交额异常监控：5x+放量币种自动加入扫描
  - 市场趋势过滤：BTC/ETH偏空时降多信号权重，偏多时降空信号权重
  - 固定止损冷静期：止损后60分钟不开同币种
  - 每笔固定100 USDT开仓
  - JSONL 日志 + Markdown 报告

用法：
    python scanner.py              # 前台运行（Ctrl+C 停止）
    python scanner.py --once       # 只跑一轮（调试用）

v10 改动（2026-04-26）:
  - 时间周期 5m+15m → 15m+30m（5m噪声太大，实盘应使用更长周期）
  - 开仓阈值 70→80（减少低质量信号开仓，降低手续费损耗）
  - 去掉早期动态扩容/EVICT功能（持仓只等止盈止损，不主动挤仓）
  - 2026-05-20: 恢复保守强信号替换弱仓，解决满仓冻结问题
  - 每笔固定100 USDT开仓（不再按张数，按USDT金额计算数量）
  - VPB 5m扫描去掉（同步到15m+30m）
  - REQUIRE_RESONANCE=False（30m信号较少，不强制共振）

v9 改动（2026-04-26）:
  - 动态扩容阈值 80→90（v8数据：80-84分占34%触发，大量挤出盈利仓位）
  - 持仓上限 5→8（给信号更多空间，减少挤出频率）
  - 保护盈利仓位：浮盈>2%的持仓不参与EVICT排序（防止赚的仓被挤掉）
  - 扩容冷却：被EVICT挤出的币种30分钟内不重复开仓
  - 固定止损冷静期：已移除（v11: 去掉固定止损，只用浮动止损）
  - 市场趋势过滤：BTC/ETH 15m EMA趋势偏空时做多扣15分，偏多时做空扣15分
  - OPEN_FAILED 错误信息细化：增加 reason 字段记录具体失败原因

v8 改动（2026-04-25）:
  - VPB 5m阈值 60→80（5m噪声大降权，15m保持60）
  - VPB 量能倍数上限 ≤5x（5x+胜率仅25%，极端放量封顶不计分）
  - 半木夏 MACD背离权重提升：三段 60→75, 二段 35→45, 一段 20→25（胜率50%+）
  - 半木夏 ST翻转权重提升：25→40（胜率50%+）
  - 半木夏 ADX权重降低：max 15→8, 乘数 0.4→0.2, 弱趋势惩罚 5→3（趋势末期信号）
  - 半木夏综合评分：等权平均→条件加权（同向等权增强，反向以主信号为准）
  - 新增最大亏损硬性限制：单笔亏损达50%（含杠杆）强制平仓
  - 删除影子持仓系统：不可交易的币种直接跳过+自动黑名单，简化逻辑

v7 改动（2026-04-25）:
  - 扫描范围 Top50→Top100 + 成交额异常监控（5x+放量自动加入）
  - 止损距离收紧：5m 2.5→2.0×ATR, 15m 2.0→1.5×ATR
  - 止盈距离放宽：5m 6.0→6.5×ATR, 15m 5.0→5.5×ATR
  - VPB止损收紧：1.5→1.2×ATR, VPB止盈放宽：4.0→4.5×ATR
  - 浮动止损收紧：5m激活2.0→1.5×ATR/回撤1.5→1.0×ATR, 15m激活1.5→1.2×ATR/回撤1.0→0.8×ATR
  - 增加趋势过滤：EMA方向与信号方向不一致时扣分（-10）
  - 动态扩容：强信号>80分时平最弱持仓腾位
  - 每笔开仓固定100 USDT（盈亏更清晰）
  - 每周期持仓上限提升到5（模拟盘仓位多一些）

v6 改动（2026-04-25）:
  - 开仓阈值提高到70（v5=60），过滤低质量信号
  - REQUIRE_RESONANCE=True：强制要求5m+15m共振才开仓（半木夏策略）
  - 开仓失败也进入冷却期15分钟，避免同币种反复失败
  - VPB量价突破策略集成（独立开仓逻辑）
  - ATR=0异常币种自动跳过黑名单（运行时动态发现并记录）
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
from strategy_breakout import analyze_symbol_vpb, VPB_SCORE_THRESHOLD_15M, VPB_VOL_MULT_MAX

from binance_client import get_client, BinanceClient as ExchangeClient, _delete
from core.audit_log import write_jsonl_with_daily_shard
from core.execution_engine import CloseRequest, ExecutionEngine, OpenRequest
from core.event_store import EventStoreWriter
from core.market_watchlist import load_sentinel_context
from core.market_data_cache import cached_available_symbols, cached_spike_symbols, cached_top_symbols
from core.kline_cache import load_cached_klines, save_cached_klines
from core.sentinel_scanner import fields_from_context, filter_context_by_available, merge_symbols_with_context
from core.risk_engine import RiskEngine, RiskLimits
from core.strategy_engine import StrategyEngine

def console_log_level() -> int:
    name = os.environ.get("SCANNER_CONSOLE_LOG_LEVEL") or os.environ.get("LOG_LEVEL", "INFO")
    return getattr(logging, name.strip().upper(), logging.INFO)


logging.basicConfig(
    level=console_log_level(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scanner")

CST = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
TIMEFRAMES = ["15m", "30m"]
MAX_POSITIONS_PER_TF = 2       # P0: 降低每周期新增持仓上限
MAX_TOTAL_POSITIONS = 30       # P0: 全账户总持仓上限，超过后只管理不新开
LEVERAGE = 4
SCORE_THRESHOLD = 85           # v11优化(2026-05-15): 80→85，只做最强信号
SCORE_THRESHOLDS = {"15m": 105, "30m": 90}  # P1: 降低15m频率，优先30m
SHORT_ENTRY_PENALTY = 20       # P1: v11历史空头拖累，空头额外提高门槛
SENTINEL_CONTEXT: dict[str, dict] = {}
RESONANCE_BONUS = 15           # 多周期共振额外加分
REQUIRE_RESONANCE = False      # v10: 不强制共振（30m信号较少，强制会错过太多机会）
REPLACEMENT_POLICY_VERSION = "small_live_v11_replacement_quality_v1"
STRONG_SIGNAL_THRESHOLD = 112  # P0小范围验证: 满仓时仅极强信号可释放弱仓
EVICT_SOFT_PROTECT_PNL_PCT = 2.0  # 实验口径: 小盈利仓只在新信号明显更强时释放
EVICT_HARD_PROTECT_PNL_PCT = 2.0  # P0小范围验证: 盈利>=2%硬保护，避免牺牲优质仓
EVICT_COOLDOWN_MINUTES = 90   # 被释放币种冷却90分钟，避免来回换仓
EVICT_MIN_AGE_MINUTES = 20    # 刚恢复/刚开的仓至少观察20分钟
EVICT_ELITE_SCORE = 120       # 极强信号允许更快替换弱仓
EVICT_ELITE_MIN_AGE_MINUTES = 10
EVICT_SCORE_GAP = 25          # P0小范围验证: 新信号至少比弱仓入场分高25分
EVICT_SOFT_PROTECT_SCORE_GAP = 25
STOP_LOSS_COOLDOWN_MINUTES = 60  # 固定止损后60分钟冷静期

# 分周期止损止盈参数（ATR倍数）——v10: 15m+30m
SL_MULT = {"15m": 1.5, "30m": 2.0}     # 止损：15m 1.5×ATR, 30m 2.0×ATR
TP_MULT = {"15m": 5.5, "30m": 6.5}     # 止盈：15m 5.5×ATR, 30m 6.5×ATR
TRAILING_ACTIVATE = {"15m": 1.0, "30m": 1.2}  # v11优化(2026-05-15): 1.2/1.5→1.0/1.2，加快止盈
TRAILING_PULLBACK = {"15m": 1.0, "30m": 0.8}  # 2026-05-29: 用户批准 A/v11 trailing pullback 0.8/1.0 候选全量开放，15m 采用更宽 1.0 ATR

COOLDOWN_MINUTES = 30          # 同币种平仓后冷却时间（分钟）
ATR_PRICE_MAX = 0.05           # ATR/Price > 5% 跳过（过滤极端波动币）

# ATR=0异常币种黑名单（运行时自动发现并记录）
# 这些币种的数据异常导致ATR计算为0，止盈止损价格无效
ATR_ZERO_BLACKLIST = {"RDNTUSDT"}

# 每笔交易固定保证金（USDT）——以保证金为风险口径，名义价值=保证金*杠杆
RISK_PER_TRADE_USDT = 100      # 每笔目标初始保证金100 USDT

# 安全限制
ORDER_MARGIN_TOLERANCE_PCT = 0.05  # 数量步长/行情微动容差；计算保证金偏离目标超过5%则拒单
MAX_LOSS_PCT = 30.0            # v8→v11优化(2026-05-08): 50%→30%，用户要求硬顶30%
MIN_AVAILABLE_BALANCE_PCT = 0.25   # P0: 开新仓后至少保留25%可用保证金
MIN_AVAILABLE_BALANCE_USDT = 300.0 # P0: 绝对可用余额保护线
PROFIT_PROTECT_MIN_USDT = 50.0     # P2: 盈利超过该值后启用回撤保护
PROFIT_PROTECT_RETRACE = 0.35      # P2: 从最高浮盈回撤35%则平仓保护利润
MAX_SL_PER_SYMBOL = 1              # P2: 同币种当日止损后暂停新开
SYMBOL_SL_BAN_HOURS = 72           # P2: 止损后跨周期冷却72小时
MAX_POS_PER_SIDE = 12              # P2: 单方向最大持仓数，降低单边暴露

# 数据目录
DATA_DIR = ROOT / "scanner_data"
DATA_DIR.mkdir(exist_ok=True)

TRADES_LOG = DATA_DIR / "trades.jsonl"
EVENTS_LOG = DATA_DIR / "events.jsonl"
REPORT_FILE = DATA_DIR / "report.md"

# 服务器版增强日志
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
SIGNAL_LOG = LOGS_DIR / "signals.jsonl"      # 每次扫描的信号记录（不论是否开仓）
DECISION_LOG = LOGS_DIR / "decisions.jsonl"  # 统一决策记录：信号/跳过/失败/开平仓
OPERATION_LOG = LOGS_DIR / "operations.jsonl"  # 操作日志（启动/停止/错误等）
SYSTEM_LOG = LOGS_DIR / "system.jsonl"         # 系统状态日志（心跳/余额/持仓数）
EVENT_STORE = EventStoreWriter(PROJECT_ROOT / "runtime" / "event_store.sqlite3")


def write_event_store(record: dict, source: str):
    EVENT_STORE.write_event(record, source=source)


# ═══════════════════════════════════════════════════════════════
# 模拟持仓
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
    timeframe: str = ""             # 所属时间框架
    resonance: bool = False         # 是否多周期共振
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_sl: float = 0.0
    trailing_active: bool = True   # v11: 开仓即激活，不再等盈利阈值
    pos_id: str = ""
    # 分周期参数（从全局配置注入）
    sl_mult: float = 2.0
    tp_mult: float = 4.0
    trail_activate: float = 1.5
    trail_pullback: float = 1.0
    # 交易所订单相关
    order_id: str = ""           # 交易所订单ID
    exchange_qty: float = 0.0     # 交易所合约数量

    def __post_init__(self):
        if not self.pos_id:
            self.pos_id = f"{self.symbol}_{self.timeframe}_{self.entry_time}"
        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price
        self.trailing_sl = self.stop_loss

    def check_exit(self, current_price: float) -> Optional[str]:
        """检查是否触发止盈/浮动止损/最大亏损限制，返回原因或 None
        v11: 只用浮动止损，开仓即激活，初始锚点=止损价，随最高/最低价向有利方向移动。
             固定止损已移除（胜率=0%，v10数据验证），只保留最大亏损硬限制兜底。
        """
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

        # v11: 浮动止损 — 激活阈值达到后开始跟踪最高/最低价，未激活前锚定 stop_loss
        # 去掉固定止损分支，激活后只用浮动止损（不再有 "固定止损" 回退）
        atr_move = self.atr_at_entry * self.trail_activate
        if self.side == "long":
            profit = current_price - self.entry_price
            if profit > atr_move and not self.trailing_active:
                self.trailing_active = True
            if self.trailing_active:
                new_trail = self.highest_price - self.atr_at_entry * self.trail_pullback
                if new_trail > self.trailing_sl:
                    self.trailing_sl = new_trail
            if current_price <= self.trailing_sl:
                return "浮动止损"
            if current_price >= self.take_profit:
                return "止盈"
        else:  # short
            profit = self.entry_price - current_price
            if profit > atr_move and not self.trailing_active:
                self.trailing_active = True
            if self.trailing_active:
                new_trail = self.lowest_price + self.atr_at_entry * self.trail_pullback
                if new_trail < self.trailing_sl:
                    self.trailing_sl = new_trail
            if current_price >= self.trailing_sl:
                return "浮动止损"
            if current_price <= self.take_profit:
                return "止盈"
        return None



# ═══════════════════════════════════════════════════════════════
# 数据拉取
# ═══════════════════════════════════════════════════════════════
_api_request_times: list[float] = []  # v11优化(2026-05-10): 请求频率追踪
_last_ban_until: float = 0


def fetch_json(url: str, timeout: int = 10) -> dict:
    """拉取JSON，内置限流和ban退避"""
    import urllib.request, urllib.error
    global _last_ban_until
    now = time.time()
    # IP ban退避 - 完全暂停直到解封
    if _last_ban_until > now:
        wait = _last_ban_until - now
        logger.warning(f"  ⏸️ IP仍被ban，等待{wait:.0f}秒后重试")
        time.sleep(wait)
        # 解封后重置标志
        _last_ban_until = 0
    # 限流: 最多200次/分钟
    _api_request_times.append(now)
    while _api_request_times and _api_request_times[0] < now - 60:
        _api_request_times.pop(0)
    if len(_api_request_times) > 200:
        time.sleep(0.5)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        if e.code == 418:
            body = str(e.read())
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
    """拉取 Top N（按 24h 成交额排序），默认 Top100（v7: 50→100）"""
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


# ═══════════════════════════════════════════════════════════════
# 成交额异常监控（v7新增）
# ═══════════════════════════════════════════════════════════════
# 记录上一轮各币种成交额，用于检测异常放量
VOL_SPIKE_MULT = 5.0  # 成交额暴增倍数阈值
_prev_volumes: dict[str, float] = {}  # {symbol: prev_quote_volume}


def fetch_volume_spikes(top_n: int = 100) -> list[str]:
    """检测成交额异常暴增的币种（5x+），即使不在常规扫描列表也返回。
    
    与 fetch_top_symbols 配合使用：
    - top_symbols: 常规 Top100 扫描
    - spike_symbols: 异常放量币种（可能在 Top100 之外）
    
    返回: 异常放量币种列表（不在 top_symbols 中的）
    """
    global _prev_volumes
    cached = cached_spike_symbols(PROJECT_ROOT / "runtime" / "market_data_cache.json", top_n)
    if cached:
        return cached
    
    try:
        raw = fetch_json("https://testnet.binancefuture.com/fapi/v1/ticker/24hr")
        current_volumes = {}
        for it in raw:
            sym = it.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                vol = float(it.get("quoteVolume", 0) or 0)
            except Exception:
                vol = 0
            current_volumes[sym] = vol
        
        spike_symbols = []
        for sym, cur_vol in current_volumes.items():
            prev_vol = _prev_volumes.get(sym, 0)
            if prev_vol > 0 and cur_vol > prev_vol * VOL_SPIKE_MULT:
                spike_symbols.append((sym, cur_vol / prev_vol, cur_vol))
        
        # 更新历史记录
        _prev_volumes = current_volumes
        
        # 按倍数排序，只返回不在 top_n 列表中的
        top_symbols = set(fetch_top_symbols(top_n))
        result = [s[0] for s in sorted(spike_symbols, key=lambda x: x[1], reverse=True)
                  if s[0] not in top_symbols]
        
        if result:
            logger.info(f"  🔥 成交额异常暴增: {len(result)} 个币种 ({', '.join(result[:5])}...)")
        
        return result
    except Exception as e:
        logger.debug(f"  成交额异常监控失败: {e}")
        return []


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


def fetch_klines(symbol: str, bar: str = "5m", limit: int = 100) -> list[list]:  # v11优化(2026-05-10): limit 200→100, weight 2→1
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


# ═══════════════════════════════════════════════════════════════
# 信号分析（核心：完整半木夏+老猫打分逻辑）
# ═══════════════════════════════════════════════════════════════
def analyze_symbol(symbol: str, bar: str = "5m") -> Optional[dict]:
    """对单个币种运行半木夏+老猫信号分析，返回信号结果或 None"""

    # 根据周期调整拉取数量
    limit_map = {"15m": 100, "30m": 100, "5m": 100, "1h": 100}  # v11优化(2026-05-10): limit 200→100, weight 2→1
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

    # ── MACD(13,34,9) ──
    ema13 = pd.Series(closes).ewm(span=13, adjust=False).mean()
    ema34 = pd.Series(closes).ewm(span=34, adjust=False).mean()
    macd_line = ema13 - ema34
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    macd_val = _last(macd_line)
    signal_val = _last(signal_line)
    hist_val = _last(histogram)
    hist_prev = float(histogram.iloc[-2]) if len(histogram) >= 2 else hist_val
    hist_delta = hist_val - hist_prev

    # ── RSI ──
    rsi_val = _last(calc_rsi(closes))

    # ── EMA ──
    ema9_val  = _last(calc_ema(closes, 9))
    ema21_val = _last(calc_ema(closes, 21))
    ema55_val = _last(calc_ema(closes, 55))
    ema_bull = ema9_val > ema21_val > ema55_val
    ema_bear = ema9_val < ema21_val < ema55_val

    # ── ADX ──
    adx_val = _last(calc_adx(highs, lows, closes))

    # ── Stoch ──
    sk, sd = calc_stochastic(highs, lows, closes)
    stoch_k = _last(sk)
    stoch_d = _last(sd)

    # ── BB ──
    bb_u, bb_m, bb_l = calc_bollinger_bands(closes)
    bb_upper = _last(bb_u)
    bb_lower = _last(bb_l)
    bb_pos = (price - bb_lower) / (bb_upper - bb_lower + 1e-9) * 100

    # ── ATR ──
    atr_val = _last(calc_atr(highs, lows, closes))

    # 高波动过滤：ATR/Price > 阈值则跳过
    atr_pct = atr_val / price if price > 0 else 0
    if atr_pct > ATR_PRICE_MAX:
        return None

    # ATR=0异常币种黑名单
    if symbol in ATR_ZERO_BLACKLIST:
        return None

    # ── OBV ──
    obv_s = calc_obv(closes, vols)
    obv_slope = float(obv_s.iloc[-1] - obv_s.iloc[-6]) if len(obv_s) >= 6 else 0.0

    # ═══ 半木夏打分 ═══
    score_long = 0.0
    score_short = 0.0
    reasons_long = []
    reasons_short = []

    # 1. MACD
    if macd_val > signal_val and hist_val > 0:
        score_long += 20; reasons_long.append("MACD多头")
    elif macd_val < signal_val and hist_val < 0:
        score_short += 20; reasons_short.append("MACD空头")

    if hist_val > 0 and hist_delta > 0:
        score_long += 5; reasons_long.append("MACD柱扩张")
    elif hist_val < 0 and hist_delta < 0:
        score_short += 5; reasons_short.append("MACD柱扩张")

    # 2. RSI
    if rsi_val < 30:
        score_long += 15; reasons_long.append("RSI超卖")
    elif rsi_val < 45:
        score_long += 7; reasons_long.append("RSI偏低")
    elif rsi_val > 70:
        score_short += 15; reasons_short.append("RSI超买")
    elif rsi_val > 55:
        score_short += 7; reasons_short.append("RSI偏高")

    # 3. EMA
    if ema_bull:
        score_long += 15; reasons_long.append("EMA多头排列")
    elif ema_bear:
        score_short += 15; reasons_short.append("EMA空头排列")

    # 4. ADX（v8: 降低权重，ADX强趋势胜率反而低，可能是趋势末期信号）
    if adx_val >= 25:
        bonus = min(8, adx_val * 0.2)   # v8: 15→8, 0.4→0.2
        if score_long >= score_short:
            score_long += bonus; reasons_long.append(f"ADX趋势{adx_val:.0f}")
        else:
            score_short += bonus; reasons_short.append(f"ADX趋势{adx_val:.0f}")
    elif adx_val < 20:
        # 弱趋势惩罚保持轻微
        score_long = max(0, score_long - 3)   # v8: 5→3
        score_short = max(0, score_short - 3)

    # 5. Stoch
    if stoch_k < 20 and stoch_k > stoch_d:
        score_long += 10; reasons_long.append("Stoch低位金叉")
    elif stoch_k > 80 and stoch_k < stoch_d:
        score_short += 10; reasons_short.append("Stoch高位死叉")

    # 6. BB — v5: 奖励从10提升至15（布林带边界更有效）
    if bb_pos < 10:
        score_long += 15; reasons_long.append("触及布林下轨")
    elif bb_pos > 90:
        score_short += 15; reasons_short.append("触及布林上轨")

    # 7. OBV
    if obv_slope > 0:
        score_long += 5; reasons_long.append("OBV上升")
    elif obv_slope < 0:
        score_short += 5; reasons_short.append("OBV下降")

    # 8. 趋势过滤（v7新增）：EMA方向与信号方向不一致时扣分
    # 如果做多但EMA空头排列，扣10分；做空但EMA多头排列，扣10分
    if score_long > score_short and ema_bear:
        score_long = max(0, score_long - 10)
        reasons_long.append("EMA逆势-10")
    elif score_short > score_long and ema_bull:
        score_short = max(0, score_short - 10)
        reasons_short.append("EMA逆势-10")

    # ═══ 半木夏三段背离检测 ═══
    hist_arr = np.array(histogram)

    def _detect_divergence(hist, closes, highs, lows, div_type):
        n = len(hist)
        result = {"type": div_type, "segments": 0, "pivot_prices": [],
                  "entry_signal": False, "entry_price": None, "sl_price": None,
                  "strength": 0, "description": ""}
        pivots = []
        for i in range(2, n - 2):
            h = hist[i]
            if div_type == "bearish":
                if h > 0 and h > hist[i-1] and h > hist[i-2] and h >= hist[i+1] and h >= hist[i+2]:
                    pivots.append((i, float(h), float(highs[i])))
            else:
                if h < 0 and h < hist[i-1] and h < hist[i-2] and h <= hist[i+1] and h <= hist[i+2]:
                    pivots.append((i, float(h), float(lows[i])))
        if len(pivots) < 2:
            return result

        def _has_opposite(i1, i2):
            seg = hist[i1:i2+1]
            return np.any(seg < 0) if div_type == "bearish" else np.any(seg > 0)

        valid_segs = []
        i = len(pivots) - 1
        while i >= 1 and len(valid_segs) < 3:
            p_cur, p_prev = pivots[i], pivots[i-1]
            if not _has_opposite(p_prev[0], p_cur[0]):
                i -= 1; continue
            if div_type == "bearish":
                seg_valid = p_cur[2] > p_prev[2] and p_cur[1] < p_prev[1]
            else:
                seg_valid = p_cur[2] < p_prev[2] and abs(p_cur[1]) < abs(p_prev[1])
            if seg_valid:
                valid_segs.insert(0, (p_prev, p_cur))
            i -= 1

        segs = len(valid_segs)
        result["segments"] = segs
        if segs == 0:
            return result

        all_pivots = [valid_segs[0][0]] + [s[1] for s in valid_segs]
        result["pivot_prices"] = [round(p[2], 4) for p in all_pivots]
        last_idx = valid_segs[-1][1][0]
        near_end = (n - 1 - last_idx) <= 5

        # v8: 三段背离权重从60提升至75（MACD背离胜率50%+，是最可靠的信号）
        if segs >= 3:
            result["strength"] = 75
            if near_end:
                result["entry_signal"] = True
                result["entry_price"] = round(float(closes[-1]), 4)
                result["sl_price"] = result["pivot_prices"][-1]
        elif segs == 2:
            result["strength"] = 45   # v8: 35→45（MACD背离胜率高，提高权重）
        else:
            result["strength"] = 25   # v8: 20→25

        result["description"] = f"{segs}段{'顶' if div_type=='bearish' else '底'}背离"
        return result

    div_bearish = _detect_divergence(hist_arr, closes, highs, lows, "bearish")
    div_bullish = _detect_divergence(hist_arr, closes, highs, lows, "bullish")

    primary_div = div_bearish if div_bearish["strength"] >= div_bullish["strength"] else div_bullish

    if primary_div["entry_signal"]:
        if primary_div["type"] == "bullish":
            score_long += 75; reasons_long.append("三段底背离确认")  # v8: 60→75，MACD背离胜率50%+
        else:
            score_short += 75; reasons_short.append("三段顶背离确认")  # v8: 60→75

    # ═══ 老猫 ST 打分 ═══
    st_result = calc_supertrend(highs, lows, closes, atr_val, period=10, multiplier=3.0)
    mfi_series = calc_mfi(closes, highs, lows, vols, period=14)
    st_direction = int(st_result['direction'].iloc[-1])
    prev_st_dir = int(st_result['direction'].iloc[-2]) if len(st_result['direction']) >= 2 else st_direction
    st_flipped = (st_direction != prev_st_dir)
    st_line_last = float(st_result['st_line'].iloc[-1])
    mfi_val = float(mfi_series.iloc[-1])

    st_score_long = 0.0
    st_score_short = 0.0

    if st_direction == 1:
        st_score_long += 40
    else:
        st_score_short += 40

    if st_flipped:
        if st_direction == 1:
            st_score_long += 40   # v8: 25→40，ST翻转胜率50%+，提高权重
        else:
            st_score_short += 40  # v8: 25→40

    if mfi_val < 20:
        st_score_long += 15
    elif mfi_val > 80:
        st_score_short += 15
    elif mfi_val < 35:
        st_score_long += 7
    elif mfi_val > 65:
        st_score_short += 7

    dist_pct = abs(price - st_line_last) / (st_line_last + 1e-9) * 100
    if st_direction == 1 and dist_pct > 1:
        st_score_long += min(10, dist_pct)
    elif st_direction == -1 and dist_pct > 1:
        st_score_short += min(10, dist_pct)

    # ═══ 综合评分（半木夏 + SuperTrend 条件加权）═══
    # 方案E：同向时等权增强，反向时以主信号为准
    if (score_long > score_short and st_score_long > st_score_short) or \
       (score_short > score_long and st_score_short > st_score_long):
        # 同向：等权平均（双确认增强）
        combined_long  = (score_long + st_score_long) / 2
        combined_short = (score_short + st_score_short) / 2
    else:
        # 反向：以分数更高的一方为主，另一方打折
        if score_long + st_score_long > score_short + st_score_short:
            combined_long  = max(score_long, st_score_long)
            combined_short = min(score_short, st_score_short) * 0.3
        else:
            combined_short = max(score_short, st_score_short)
            combined_long  = min(score_long, st_score_long) * 0.3
    net_score = combined_long - combined_short

    # 止损止盈（使用分周期ATR倍数）
    sl_m = SL_MULT.get(bar, 2.0)
    tp_m = TP_MULT.get(bar, 4.0)
    sl_long  = round(price - atr_val * sl_m, 4)
    sl_short = round(price + atr_val * sl_m, 4)
    tp_long  = round(price + atr_val * tp_m, 4)
    tp_short = round(price - atr_val * tp_m, 4)

    return {
        "symbol": symbol,
        "price": price,
        "atr": round(atr_val, 4),
        "net_score": round(net_score, 1),
        "score_long": round(score_long, 1),
        "score_short": round(score_short, 1),
        "st_score_long": round(st_score_long, 1),
        "st_score_short": round(st_score_short, 1),
        "reasons_long": reasons_long,
        "reasons_short": reasons_short,
        "divergence_primary": primary_div,
        "st_direction": st_direction,
        "st_flipped": st_flipped,
        "can_trade": abs(net_score) >= SCORE_THRESHOLD,
        "trade_side": "long" if net_score > 0 else "short",
        "sl_long": sl_long,
        "sl_short": sl_short,
        "tp_long": tp_long,
        "tp_short": tp_short,
        "rsi": round(rsi_val, 1),
        "adx": round(adx_val, 1),
        "timeframe": bar,
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
    if "atr" in text or "数量太小" in reason or "qty" in text:
        return "market_microstructure"
    if "失败" in reason or status == "OPEN_FAILED":
        return "order_failed"
    if status in ("SIGNAL", "SIGNAL_ONLY"):
        return "signal_candidate"
    return "other"

def log_decision(record: dict):
    try:
        write_jsonl_with_daily_shard(DECISION_LOG, record)
        write_event_store(record, "A/v11/decisions")
    except Exception as e:
        logger.debug(f"写入决策日志失败: {e}")

def _decision_from_event(event: dict) -> dict:
    status = str(event.get("event") or "EVENT").upper()
    reason = str(event.get("skip_reason") or event.get("reason") or event.get("msg") or "")
    side = str(event.get("side") or "").lower()
    score = event.get("score", event.get("raw_score", event.get("net_score", 0)))
    return {
        "time": event.get("time") or event.get("ts") or str(datetime.now(CST)),
        "strategy": "A/v11",
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
        "raw": event,
        "sentinel": event.get("sentinel", False),
        "sentinel_reason": event.get("sentinel_reason", ""),
        "sentinel_change_pct": event.get("sentinel_change_pct"),
        "sentinel_velocity_pct": event.get("sentinel_velocity_pct"),
        "sentinel_quote_volume": event.get("sentinel_quote_volume"),
        "sentinel_volume_delta": event.get("sentinel_volume_delta"),
        "sentinel_scan_result": event.get("sentinel_scan_result", ""),
    }

def _decision_from_signal(signal: dict) -> dict:
    side = str(signal.get("trade_side") or signal.get("side") or "").lower()
    score = signal.get("net_score", signal.get("score", signal.get("vpb_score", 0)))
    reasons = signal.get("reasons") or signal.get(f"reasons_{side}") or []
    if isinstance(reasons, list):
        reason = "+".join(str(x) for x in reasons[:6])
    else:
        reason = str(reasons)
    return {
        "time": signal.get("time") or signal.get("ts") or str(datetime.now(CST)),
        "strategy": "A/v11",
        "symbol": signal.get("symbol", ""),
        "status": "SIGNAL",
        "category": "signal_candidate",
        "side": side,
        "score": score,
        "raw_score": score,
        "timeframe": signal.get("timeframe") or signal.get("tf") or "",
        "reason": reason,
        "source": "signal",
        "event": "SIGNAL",
        "can_trade": signal.get("can_trade"),
        "raw": signal,
        "sentinel": signal.get("sentinel", False),
        "sentinel_reason": signal.get("sentinel_reason", ""),
        "sentinel_change_pct": signal.get("sentinel_change_pct"),
        "sentinel_velocity_pct": signal.get("sentinel_velocity_pct"),
        "sentinel_quote_volume": signal.get("sentinel_quote_volume"),
        "sentinel_volume_delta": signal.get("sentinel_volume_delta"),
    }

def log_event(event: dict):
    write_jsonl_with_daily_shard(EVENTS_LOG, event)
    write_event_store({**_decision_from_event(event), "raw_event": event}, "A/v11/events")
    log_decision(_decision_from_event(event))

def _log_sentinel_scan_event(event: dict):
    """Log sentinel scan to dedicated sentinel_scans table (not events)."""
    write_jsonl_with_daily_shard(EVENTS_LOG, event)
    EVENT_STORE.write_sentinel_scan(event, source="A/v11/events")
    log_decision(_decision_from_event(event))

def log_trade(trade: dict):
    write_jsonl_with_daily_shard(TRADES_LOG, trade)
    write_event_store({"strategy": "A/v11", "event": "TRADE", "category": "trade", **trade}, "A/v11/trades")

# ═══════════════════════════════════════════════════════════════
# 服务器版增强日志
# ═══════════════════════════════════════════════════════════════
def log_signal(signal: dict):
    """记录每次扫描产生的信号（不论是否触发开仓）。用于复盘分析信号质量。"""
    write_jsonl_with_daily_shard(SIGNAL_LOG, signal)
    write_event_store({**_decision_from_signal(signal), "raw_signal": signal}, "A/v11/signals")
    log_decision(_decision_from_signal(signal))

def log_operation(op: dict):
    """记录操作日志（启动/停止/错误/配置变更等）。"""
    write_jsonl_with_daily_shard(OPERATION_LOG, op)

def log_system(state: dict):
    """记录系统状态心跳（余额/持仓数/扫描币数等）。每N个周期记录一次。"""
    write_jsonl_with_daily_shard(SYSTEM_LOG, state)
    write_event_store({"strategy": "A/v11", "event": "SYSTEM", **state}, "A/v11/system")


# ═══════════════════════════════════════════════════════════════
# 扫描器主逻辑
# ═══════════════════════════════════════════════════════════════
class Scanner:
    def __init__(self):
        # 每个周期独立的持仓池: {tf: {symbol: SimPosition}}
        self.positions: dict[str, dict[tuple, SimPosition]] = {tf: {} for tf in TIMEFRAMES}
        self.closed_trades: list[dict] = []
        self.max_positions = MAX_POSITIONS_PER_TF
        self.leverage = LEVERAGE
        self.scan_count = 0
        self.start_time = datetime.now(CST).isoformat()
        # 冷却期：{tf: {symbol: cooldown_expire_time}}
        self.cooldowns: dict[str, dict[str, datetime]] = {tf: {} for tf in TIMEFRAMES}
        self.sl_counts: dict[str, int] = {}
        self._sl_counts_date: str = datetime.now(CST).strftime("%Y-%m-%d")
        # 交易所客户端
        self.client: ExchangeClient = get_client()
        self.strategy_engine = StrategyEngine("A/v11", analyze_symbol)
        self.execution = ExecutionEngine(self.client, "A/v11")
        self.risk_engine = RiskEngine(RiskLimits(
            max_total_positions=MAX_TOTAL_POSITIONS,
            max_positions_per_side=MAX_POS_PER_SIDE,
            min_available_balance_pct=MIN_AVAILABLE_BALANCE_PCT,
            min_available_balance_usdt=MIN_AVAILABLE_BALANCE_USDT,
        ))
        # 启动时同步交易所账户状态
        self._sync_exchange_state()

    def _pos_count(self, tf: str) -> int:
        return len(self.positions.get(tf, {}))

    def _total_local_positions(self) -> int:
        return sum(len(pos) for pos in self.positions.values())

    def _total_exchange_positions(self) -> int:
        try:
            positions = self.client.get_positions()
            return sum(1 for p in positions if abs(float(p.get("positionAmt", 0))) > 0.0001)
        except Exception as e:
            logger.debug(f"  交易所持仓数检查失败: {e}")
            return self._total_local_positions()

    def _exchange_side_count(self, side: str) -> int:
        try:
            positions = self.client.get_positions()
            count = 0
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) <= 0.0001:
                    continue
                pos_side = (p.get("positionSide") or "").lower()
                if not pos_side:
                    pos_side = "long" if amt > 0 else "short"
                if pos_side == side:
                    count += 1
            return count
        except Exception as e:
            logger.debug(f"  交易所方向持仓数检查失败: {e}")
            return sum(1 for tf_pos in self.positions.values() for (_, sd) in tf_pos if sd == side)

    def _exchange_symbol_position(self, symbol: str) -> dict:
        """Return an existing exchange position for symbol, if any."""
        try:
            for pos in self.client.get_positions():
                if str(pos.get("symbol") or "").upper() != symbol.upper():
                    continue
                amt = float(pos.get("positionAmt", 0) or 0)
                if abs(amt) <= 0.0001:
                    continue
                return pos
        except Exception as e:
            logger.debug(f"  交易所同币种持仓检查失败: {symbol} {e}")
        return {}

    def _account_balance_summary(self) -> tuple[float, float]:
        bal = self.client.get_balance()
        if isinstance(bal, dict):
            usdt_item = next((a for a in bal.get("assets", []) if a.get("asset") == "USDT"), None)
            if usdt_item:
                total = float(usdt_item.get("walletBalance", 0))
                available = float(usdt_item.get("availableBalance", 0))
                return total, available
        return 0.0, 0.0

    def _can_open_new_position(self, risk_usdt: float, now_str: str, tf: str, sym: str, side: str, score: float) -> bool:
        total_positions = max(self._total_local_positions(), self._total_exchange_positions())
        side_count = self._exchange_side_count(side)
        decision = self.risk_engine.check_entry(
            total_positions=total_positions,
            side_positions=side_count,
            balance=self.client.get_balance(),
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

    def _effective_signal_score(self, sig: dict, resonance: bool = False) -> float:
        score = float(sig.get("net_score", sig.get("vpb_score", 0)) or 0)
        side = sig.get("trade_side", "")
        if resonance:
            if side == "long" and score > 0:
                return round(score + RESONANCE_BONUS, 1)
            if side == "short" and score < 0:
                return round(score - RESONANCE_BONUS, 1)
        return score

    def _can_try_full_replacement(self, sig: dict, resonance: bool = False) -> bool:
        return abs(self._effective_signal_score(sig, resonance=resonance)) >= STRONG_SIGNAL_THRESHOLD

    def _passes_entry_threshold(self, sig: dict) -> bool:
        side = sig["trade_side"]
        score = abs(sig["net_score"])
        tf = sig["timeframe"]
        threshold = SCORE_THRESHOLDS.get(tf, SCORE_THRESHOLD)
        if side == "short":
            threshold += SHORT_ENTRY_PENALTY
        return score >= threshold

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

    def _get_market_trend(self) -> str:
        """v9: 判断市场整体趋势（基于BTC/ETH 15m EMA方向）。
        
        Returns: "bullish" / "bearish" / "neutral"
        如果BTC和ETH的15m EMA都看空→bearish，都看多→bullish，否则→neutral
        """
        try:
            from cloud.analyzer.auxiliary import compute_ema
            
            bearish_count = 0
            bullish_count = 0
            
            for benchmark in ["BTCUSDT", "ETHUSDT"]:
                klines = fetch_klines(benchmark, "15m", 50)
                if not klines or len(klines) < 20:
                    continue
                closes = [float(k[4]) for k in klines]
                
                # EMA7 vs EMA25 判断趋势方向
                ema_fast = compute_ema(closes, 7)
                ema_slow = compute_ema(closes, 25)
                
                if ema_fast < ema_slow * 0.998:  # 快线明显低于慢线
                    bearish_count += 1
                elif ema_fast > ema_slow * 1.002:  # 快线明显高于慢线
                    bullish_count += 1
            
            if bearish_count >= 2:
                return "bearish"
            elif bullish_count >= 2:
                return "bullish"
            return "neutral"
        except Exception as e:
            logger.debug(f"市场趋势判断失败: {e}")
            return "neutral"

    def _position_pnl_pct(self, pos: SimPosition) -> tuple[float, float, bool]:
        price = fetch_current_price(pos.symbol)
        if price <= 0 or pos.entry_price <= 0:
            return 0.0, 0.0, False
        if pos.side == "long":
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
            pnl_usd = (price - pos.entry_price) * pos.size
        else:
            pnl_pct = (pos.entry_price - price) / pos.entry_price * 100 * pos.leverage
            pnl_usd = (pos.entry_price - price) * pos.size
        return pnl_pct, pnl_usd, True

    def _position_age_minutes(self, pos: SimPosition, now_dt: datetime) -> int:
        try:
            entry_dt = datetime.strptime(pos.entry_time[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            return max(0, int((now_dt - entry_dt).total_seconds() // 60))
        except Exception:
            return 9999

    def _find_releasable_position(
        self,
        new_score: float,
        new_symbol: str,
        new_side: str,
        now_dt: datetime,
        same_side_required: bool = False,
        preferred_tf: Optional[str] = None,
        require_preferred_tf: bool = False,
    ):
        if abs(new_score) < STRONG_SIGNAL_THRESHOLD:
            return None
        candidates = []
        is_elite = abs(new_score) >= EVICT_ELITE_SCORE
        min_age = EVICT_ELITE_MIN_AGE_MINUTES if is_elite else EVICT_MIN_AGE_MINUTES
        for old_tf, tf_positions in self.positions.items():
            for key, pos in tf_positions.items():
                if pos.symbol == new_symbol:
                    continue
                if require_preferred_tf and preferred_tf and old_tf != preferred_tf:
                    continue
                if same_side_required and pos.side != new_side:
                    continue
                age_min = self._position_age_minutes(pos, now_dt)
                if age_min < min_age:
                    continue
                pnl_pct, pnl_usd, ok = self._position_pnl_pct(pos)
                if not ok:
                    continue
                if pnl_pct >= EVICT_HARD_PROTECT_PNL_PCT:
                    continue
                old_score = abs(float(pos.entry_score or 0))
                gap_required = 0 if pnl_pct <= 0 else EVICT_SCORE_GAP
                if pnl_pct >= EVICT_SOFT_PROTECT_PNL_PCT:
                    gap_required = EVICT_SOFT_PROTECT_SCORE_GAP
                    if old_score <= 0 and abs(new_score) < EVICT_ELITE_SCORE:
                        continue
                if old_score > 0 and abs(new_score) < old_score + gap_required:
                    continue
                tf_penalty = 0 if preferred_tf and old_tf == preferred_tf else 1
                # 优先释放同方向弱仓，避免替换后单方向暴露继续增加。
                side_penalty = 0 if pos.side == new_side else 1
                # 越亏、越低分、越老的仓越适合释放；恢复仓 entry_score=0 会自然排前。
                release_rank = (tf_penalty, side_penalty, pnl_pct, old_score, -age_min)
                candidates.append((release_rank, old_tf, key, pos, pnl_pct, pnl_usd, age_min, gap_required, min_age))
        if not candidates:
            return None
        return sorted(candidates, key=lambda x: x[0])[0][1:]

    def _release_position_for_strong_signal(
        self,
        sig: dict,
        now_str: str,
        now_dt: datetime,
        reason: str = "强信号替换弱仓",
        effective_score: Optional[float] = None,
        preferred_tf: Optional[str] = None,
        require_preferred_tf: bool = False,
    ) -> bool:
        new_score = abs(float(effective_score if effective_score is not None else self._effective_signal_score(sig)))
        new_side = sig.get("trade_side", "")
        same_side_required = self._exchange_side_count(new_side) >= MAX_POS_PER_SIDE
        found = self._find_releasable_position(
            new_score,
            sig.get("symbol", ""),
            new_side,
            now_dt,
            same_side_required=same_side_required,
            preferred_tf=preferred_tf,
            require_preferred_tf=require_preferred_tf,
        )
        if not found:
            return False
        old_tf, key, pos, pnl_pct, pnl_usd, age_min, gap_required, min_age = found
        close_exec = self.execution.close_position(CloseRequest(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.exchange_qty,
            cancel_open_orders=True,
        ))
        if not close_exec.success:
            log_event({
                "time": now_str, "event": "EVICT_FAILED", "symbol": pos.symbol,
                "side": pos.side, "timeframe": old_tf, "reason": close_exec.reason,
                "new_symbol": sig.get("symbol"), "new_score": new_score,
                "preferred_tf": preferred_tf or "",
                "require_preferred_tf": require_preferred_tf,
            })
            logger.warning(f"  释放弱仓失败: {pos.symbol} {pos.side} {close_exec.reason}")
            return False

        trade = {
            "symbol": pos.symbol, "side": pos.side,
            "entry_price": pos.entry_price, "exit_price": fetch_current_price(pos.symbol),
            "entry_time": pos.entry_time, "exit_time": now_str,
            "exit_reason": reason, "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 4), "leverage": pos.leverage,
            "score": pos.entry_score, "reason": pos.entry_reason,
            "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
            "timeframe": old_tf, "order_id": pos.order_id,
            "exchange_qty": pos.exchange_qty, "exchange_close_success": True,
            "released_for": sig.get("symbol"), "released_for_score": new_score,
            "replacement_policy": REPLACEMENT_POLICY_VERSION,
        }
        self.closed_trades.append(trade)
        log_trade(trade)
        log_event({
            "time": now_str, "event": "EVICT_CLOSE", "symbol": pos.symbol,
            "side": pos.side, "exit_price": trade["exit_price"],
            "reason": reason, "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 4), "entry_price": pos.entry_price,
            "entry_time": pos.entry_time, "timeframe": old_tf,
            "new_symbol": sig.get("symbol"), "new_score": new_score,
            "old_score": pos.entry_score, "age_min": age_min,
            "replacement_policy": REPLACEMENT_POLICY_VERSION,
            "preferred_tf": preferred_tf or "",
            "require_preferred_tf": require_preferred_tf,
            "gap_required": gap_required,
            "min_age_required": min_age,
            "soft_protect_pnl_pct": EVICT_SOFT_PROTECT_PNL_PCT,
            "hard_protect_pnl_pct": EVICT_HARD_PROTECT_PNL_PCT,
        })
        self.positions[old_tf].pop(key, None)
        self.cooldowns[old_tf][pos.symbol] = now_dt + timedelta(minutes=EVICT_COOLDOWN_MINUTES)
        logger.info(
            f"  ♻️ 释放弱仓: {pos.symbol} {pos.side} pnl={pnl_pct:+.2f}% "
            f"score={pos.entry_score:+.0f} -> {sig.get('symbol')} score={new_score:+.0f}"
        )
        return True

    def _sync_exchange_state(self):
        """启动时同步交易所账户状态：设置持仓模式，同步已有持仓到内存"""
        logger.info("同步交易所账户状态...")

        # 1. 确认持仓模式为双向
        config = self.client.get_account_config()
        if config.get("dualSidePosition"):
            logger.info("  持仓模式: 双向持仓 ✓")
        else:
            logger.warning(f"  获取账户配置失败: {config}")

        # 2. 查询现有持仓并同步到内存（重启后恢复）
        positions = self.client.get_positions()
        if positions:
            logger.info(f"  现有持仓: {len(positions)} 个")
            for pos in positions:
                symbol = pos.get("symbol", "")
                pos_side = pos.get("positionSide", "")
                sz = pos.get("positionAmt", "0")
                avg_px = pos.get("entryPrice", "0")
                logger.info(f"    {symbol} {pos_side} {sz} @ {avg_px}")

                # 恢复到内存：如果本地没有记录，则创建 SimPosition 跟踪
                pos_key = (symbol, pos_side.lower())
                already_tracked = any(
                    pos_key in self.positions.get(tf, {})
                    for tf in TIMEFRAMES
                )
                if not already_tracked and abs(float(sz)) > 0:
                    # 恢复持仓必须全量纳入本地管理；每周期上限只限制新开仓，
                    # 不能让重启后的真实交易所仓位脱离止损/尾部/弱仓替换监控。
                    target_tf = min(TIMEFRAMES, key=lambda tf: self._pos_count(tf))
                    if target_tf:
                        entry_price = float(avg_px) if avg_px else 0
                        # 使用保守的默认止损止盈参数
                        default_atr = entry_price * 0.02  # 假设2%作为默认ATR
                        if default_atr <= 0:
                            default_atr = entry_price * 0.01
                        sl_mult = SL_MULT.get(target_tf, 2.0)
                        tp_mult = TP_MULT.get(target_tf, 5.0)
                        if pos_side.lower() == "long":
                            sl = round(entry_price - default_atr * sl_mult, 6)
                            tp = round(entry_price + default_atr * tp_mult, 6)
                        else:
                            sl = round(entry_price + default_atr * sl_mult, 6)
                            tp = round(entry_price - default_atr * tp_mult, 6)

                        restored_pos = SimPosition(
                            symbol=symbol,
                            side="long" if pos_side.lower() == "long" else "short",
                            entry_price=entry_price,
                            size=float(abs(float(sz))),
                            leverage=self.leverage,
                            stop_loss=sl,
                            take_profit=tp,
                            atr_at_entry=default_atr,
                            entry_time=datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " [恢复]",
                            entry_score=0,
                            entry_reason="启动时从交易所恢复",
                            timeframe=target_tf,
                            resonance=False,
                            sl_mult=sl_mult,
                            tp_mult=tp_mult,
                            trail_activate=TRAILING_ACTIVATE.get(target_tf, 1.5),
                            trail_pullback=TRAILING_PULLBACK.get(target_tf, 1.0),
                            order_id="restored",
                            exchange_qty=float(abs(float(sz))),
                        )
                        self.positions[target_tf][pos_key] = restored_pos
                        logger.info(f"    ↩️ 已恢复到内存[{target_tf}]: {symbol} {pos_side} SL={sl} TP={tp}")
        else:
            logger.info("  当前无持仓 ✓")

        # 3. 查询余额（Binance /fapi/v2/account 返回dict，余额在assets列表中）
        bal = self.client.get_balance()
        if isinstance(bal, dict):
            assets = bal.get("assets", [])
            for item in assets:
                if item.get("asset") == "USDT":
                    logger.info(f"  余额: 可用={item.get('availableBalance', '0')} USDT, 总计={item.get('walletBalance', '0')} USDT")

        logger.info("交易所状态同步完成")

    def _sync_exchange_positions(self, now_str: str, now_dt: datetime = None):
        """同步交易所实际持仓状态。如果止盈止损已自动触发，本地也同步平仓。"""
        # 收集交易所上实际存在的持仓
        exchange_positions = self.client.get_positions()
        exchange_active = {}  # {(symbol, positionSide): pos_data}
        for p in exchange_positions:
            symbol = p.get("symbol", "")
            pos_side = p.get("positionSide", "")
            pos_amt = float(p.get("positionAmt", "0"))
            if pos_amt != 0:
                exchange_active[(symbol, pos_side.upper())] = p

        # 检查本地追踪的持仓是否在交易所上还存在
        for tf in list(self.positions.keys()):
            to_remove = []
            for key, pos in self.positions[tf].items():
                sym = pos.symbol
                if pos.exchange_qty <= 0.0001:
                    continue  # 没有在交易所下单的，跳过

                pos_side = "LONG" if pos.side == "long" else "SHORT"
                check_key = (sym, pos_side)

                if check_key not in exchange_active:
                    # 交易所上已不存在此持仓（可能被止盈止损自动平仓了）
                    logger.warning(f"  持仓已不存在: {sym} {pos.side}，可能是止盈止损自动触发")

                    # 尝试获取成交价格
                    exit_price = pos.entry_price  # 默认用开仓价
                    try:
                        current_price = fetch_current_price(sym)
                        exit_price = current_price
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
                        "exit_reason": "交易所止盈止损自动平仓", "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "leverage": pos.leverage, "score": pos.entry_score,
                        "reason": pos.entry_reason,
                        "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                        "timeframe": tf,
                        "resonance": pos.resonance,
                        "order_id": pos.order_id,
                        "exchange_qty": pos.exchange_qty,
                        "exchange_close_success": True,  # 交易所已自动平仓
                    }
                    self.closed_trades.append(trade)
                    log_trade(trade)

                    event = {
                        "time": now_str, "event": "CLOSE", "symbol": sym,
                        "side": pos.side, "exit_price": round(exit_price, 4),
                        "reason": "交易所止盈止损自动平仓", "pnl_pct": round(pnl, 2),
                        "pnl_usd": round(pnl_usd, 4),
                        "entry_price": pos.entry_price,
                        "entry_time": pos.entry_time,
                        "timeframe": tf,
                        "resonance": pos.resonance,
                        "exchange_close_success": True,
                    }
                    log_event(event)
                    logger.info(
                        f"  {'🟢' if pnl > 0 else '🔴'} 自动平仓[{tf}]: {sym} {pos.side} "
                        f"| PnL={pnl:+.2f}% (${pnl_usd:+.4f})"
                    )
                    to_remove.append(key)

                    # 记录冷却期
                    if now_dt:
                        cooldown_end = now_dt + timedelta(minutes=COOLDOWN_MINUTES)
                        self.cooldowns[tf][sym] = cooldown_end

            for key in to_remove:
                del self.positions[tf][key]

    def _enforce_hard_stop_on_exchange(self, now_str: str, now_dt: datetime = None):
        """交易所级硬顶兜底：直接扫描真实持仓，避免本地恢复遗漏导致超亏仓失管。"""
        try:
            exchange_positions = self.client.get_positions()
        except Exception as e:
            logger.warning(f"  交易所硬顶扫描失败: {e}")
            return

        for p in exchange_positions:
            try:
                amt = float(p.get("positionAmt", 0) or 0)
            except Exception:
                continue
            if abs(amt) <= 0.0001:
                continue

            symbol = p.get("symbol", "")
            pos_side = (p.get("positionSide") or "").upper()
            if not pos_side or pos_side == "BOTH":
                pos_side = "LONG" if amt > 0 else "SHORT"
            side = "long" if pos_side == "LONG" else "short"

            try:
                entry = float(p.get("entryPrice", 0) or 0)
                mark = float(p.get("markPrice", 0) or 0)
                leverage = float(p.get("leverage", self.leverage) or self.leverage)
            except Exception:
                continue
            if entry <= 0 or mark <= 0:
                continue

            adverse_pct = ((entry - mark) / entry * 100) if side == "long" else ((mark - entry) / entry * 100)
            loss_pct = max(0.0, adverse_pct * leverage)
            if loss_pct < MAX_LOSS_PCT:
                continue

            exchange_close_success = False
            result = {}
            qty = abs(amt)
            try:
                try:
                    _delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
                except Exception as ce:
                    logger.warning(f"  取消 {symbol} 未成交单失败（继续硬顶平仓）: {ce}")
                close_exec = self.execution.close_position(CloseRequest(
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    cancel_open_orders=True,
                ))
                exchange_close_success = close_exec.success
                result = close_exec.raw if isinstance(close_exec.raw, dict) else {}
            except Exception as e:
                logger.error(f"  交易所硬顶平仓异常: {symbol} {pos_side} {e}")

            pnl_pct = -round(loss_pct, 2)
            pnl_usd = (mark - entry) * qty if side == "long" else (entry - mark) * qty
            event = {
                "time": now_str,
                "event": "FORCED_CLOSE",
                "symbol": symbol,
                "side": side,
                "exit_price": round(mark, 8),
                "reason": f"交易所硬顶{MAX_LOSS_PCT:.0f}%",
                "pnl_pct": pnl_pct,
                "pnl_usd": round(pnl_usd, 4),
                "entry_price": entry,
                "timeframe": "exchange",
                "exchange_qty": qty,
                "exchange_close_success": exchange_close_success,
                "order_id": result.get("orderId") if isinstance(result, dict) else "",
            }
            log_event(event)
            logger.warning(
                f"  交易所硬顶平仓: {symbol} {side} qty={qty:g} "
                f"loss={loss_pct:.2f}% >= {MAX_LOSS_PCT:.2f}% "
                f"{'[交易所✓]' if exchange_close_success else '[交易所✗]'}"
            )

            # 同步清理本地追踪，避免下一步 check_exits 重复处理。
            for tf in list(self.positions.keys()):
                self.positions[tf].pop((symbol, side), None)
                if now_dt:
                    cooldown_end = now_dt + timedelta(hours=SYMBOL_SL_BAN_HOURS)
                    self.cooldowns[tf][symbol] = cooldown_end
            if now_dt:
                self.sl_counts[symbol] = self.sl_counts.get(symbol, 0) + 1

    def scan_cycle(self):
        """执行一轮完整扫描"""
        self.scan_count += 1
        now = datetime.now(CST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"═══ 第 {self.scan_count} 轮扫描 @ {now_str} ═══")

        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._sl_counts_date:
            self.sl_counts.clear()
            self._sl_counts_date = today_str

        # 0. 同步交易所持仓（检查是否有被止盈止损自动平仓的）
        self._sync_exchange_positions(now_str, now_dt=now)

        # 0.5 交易所级硬顶兜底（覆盖本地未恢复/满仓未纳入内存的持仓）
        self._enforce_hard_stop_on_exchange(now_str, now_dt=now)

        # 1. 检查所有周期的持仓止盈止损
        self._check_exits(now_str, now_dt=now)

        # 1.5 清理过期冷却期
        for tf in TIMEFRAMES:
            expired = [sym for sym, cd in self.cooldowns.get(tf, {}).items() if now >= cd]
            for sym in expired:
                del self.cooldowns[tf][sym]

        # 2. 获取 Top100 列表 + 异常放量币种
        try:
            top_symbols = fetch_top_symbols(100)
            spike_symbols = fetch_volume_spikes(100)
            # 合并：Top100 + 异常放量（去重）
            symbols = merge_sentinel_symbols(list(dict.fromkeys(top_symbols + spike_symbols)))
        except Exception as e:
            logger.error(f"获取扫描列表失败: {e}")
            return

        logger.info(f"扫描 {len(symbols)} 个币种 × {len(TIMEFRAMES)} 个周期 (Top{len(top_symbols)}+{len(spike_symbols)}异常放量)...")

        # 3. 逐周期扫描（半木夏策略）
        all_signals = {}  # {tf: [signals]}

        for tf in TIMEFRAMES:
            tf_signals = []
            for sym in symbols:
                # ATR=0异常币种黑名单
                if sym in ATR_ZERO_BLACKLIST:
                    log_sentinel_scan(sym, tf, "pre_filter_rejected", "ATR=0黑名单", decision_stage="pre_filter")
                    continue
                # 已持仓的跳过
                if self._has_position_in_tf(tf, sym):
                    log_sentinel_scan(sym, tf, "pre_filter_rejected", "本周期已有持仓", decision_stage="pre_filter")
                    continue
                if self.sl_counts.get(sym, 0) >= MAX_SL_PER_SYMBOL:
                    log_sentinel_scan(sym, tf, "cooldown_rejected", "同币种当日止损已达上限", decision_stage="cooldown", filter_layer="risk")
                    continue
                # 冷却期检查
                cd = self.cooldowns.get(tf, {}).get(sym)
                if cd and now < cd:
                    log_sentinel_scan(sym, tf, "cooldown_rejected", "冷却期内", decision_stage="cooldown", filter_layer="risk")
                    continue
                try:
                    result = self.strategy_engine.analyze(sym, tf)
                    if result and result["can_trade"] and self._passes_entry_threshold(result):
                        tf_signals.append(result)
                    elif result and result["can_trade"]:
                        log_sentinel_scan(
                            sym, tf, "score_rejected", "策略分数未达入场阈值",
                            side=result.get("trade_side", ""),
                            score=abs(float(result.get("net_score") or 0)),
                            decision_stage="threshold",
                        )
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

            # 按分数排序
            tf_signals.sort(key=lambda x: abs(x["net_score"]), reverse=True)
            all_signals[tf] = tf_signals

        # 3.5.1 市场趋势过滤（v9：BTC/ETH偏空时降多信号权重，偏多时降空信号权重）
        market_trend = self._get_market_trend()
        if market_trend != "neutral":
            trend_label = "偏空" if market_trend == "bearish" else "偏多"
            logger.info(f"  📊 市场趋势: {trend_label}，调整逆势信号权重")
            for tf in TIMEFRAMES:
                for sig in all_signals[tf]:
                    if market_trend == "bearish" and sig["trade_side"] == "long":
                        sig["net_score"] -= 15
                        sig.setdefault("reasons_long", []).append("市场偏空-15")
                    elif market_trend == "bullish" and sig["trade_side"] == "short":
                        sig["net_score"] += 15  # net_score负值，+15实际是减弱做空信号
                        sig.setdefault("reasons_short", []).append("市场偏多-15")
                # 过滤后重新排序
                all_signals[tf] = [s for s in all_signals[tf] if self._passes_entry_threshold(s)]
                all_signals[tf].sort(key=lambda x: abs(x["net_score"]), reverse=True)

        # 3.6 VPB 第二套策略扫描
        vpb_signals_all = {}  # {tf: [vpb_signals]}
        for tf in TIMEFRAMES:
            vpb_signals_all[tf] = []

        for sym in symbols:
            # ATR=0异常币种黑名单
            if sym in ATR_ZERO_BLACKLIST:
                log_sentinel_scan(sym, "vpb", "pre_filter_rejected", "ATR=0黑名单", decision_stage="pre_filter")
                continue
            if self.sl_counts.get(sym, 0) >= MAX_SL_PER_SYMBOL:
                log_sentinel_scan(sym, "vpb", "cooldown_rejected", "同币种当日止损已达上限", decision_stage="cooldown", filter_layer="risk")
                continue
            # 检查是否在所有周期都持仓/冷却
            in_cooldown_any = any(
                (sym in self.positions.get(tf, {})) or
                (self.cooldowns.get(tf, {}).get(sym) and now < self.cooldowns[tf][sym])
                for tf in TIMEFRAMES
            )
            if in_cooldown_any:
                log_sentinel_scan(sym, "vpb", "pre_filter_rejected", "已有持仓或冷却期内", decision_stage="pre_filter")
                continue
            try:
                klines_15m = fetch_klines(sym, "15m", 100)
                klines_30m = fetch_klines(sym, "30m", 100)
                vpb_sigs = analyze_symbol_vpb(sym, klines_15m, klines_30m)
                for vsig in vpb_sigs:
                    tf = vsig["timeframe"]
                    # VPB 不与半木夏信号重复（同币种同周期已有信号则跳过）
                    hm_syms = {s["symbol"] for s in all_signals.get(tf, [])}
                    if sym not in hm_syms:
                        vpb_signals_all[tf].append(vsig)
                        logger.info(
                            f"  🔥 VPB[{tf}] {sym}: score={vsig['vpb_score']:+.0f} "
                            f"side={vsig['trade_side']} vol={vsig['vol_mult']:.1f}x "
                            f"突破={vsig['breakout_type']} 形态={vsig['pattern']}"
                        )
                        # 服务器版：记录VPB信号日志
                        log_signal({
                            "ts": now_str,
                            "strategy": "vpb",
                            "timeframe": tf,
                            "symbol": sym,
                            "vpb_score": vsig["vpb_score"],
                            "trade_side": vsig["trade_side"],
                            "vol_mult": vsig["vol_mult"],
                            "breakout_type": vsig["breakout_type"],
                            "pattern": vsig["pattern"],
                            "funding_rate": vsig.get("funding_rate"),
                            **sentinel_fields(sym),
                        })
            except Exception as e:
                logger.debug(f"  {sym} VPB分析失败: {e}")
                log_sentinel_scan(sym, "vpb", "analysis_error", f"VPB分析异常: {str(e)[:120]}")

        # 4. 多周期共振检测
        resonance_map = {}  # {symbol: {"side": "long"/"short", "tfs": [tf1, tf2]}}
        for i, tf1 in enumerate(TIMEFRAMES):
            for tf2 in TIMEFRAMES[i+1:]:
                sigs1 = {s["symbol"]: s for s in all_signals.get(tf1, [])}
                sigs2 = {s["symbol"]: s for s in all_signals.get(tf2, [])}
                for sym in sigs1:
                    if sym in sigs2 and sigs1[sym]["trade_side"] == sigs2[sym]["trade_side"]:
                        resonance_map[sym] = {
                            "side": sigs1[sym]["trade_side"],
                            "tfs": [tf1, tf2],
                            "scores": {tf1: sigs1[sym]["net_score"], tf2: sigs2[sym]["net_score"]},
                        }

        # 5. 输出检测到的信号
        for tf in TIMEFRAMES:
            for sig in all_signals[tf]:
                side = sig["trade_side"]
                reasons = sig["reasons_long"] if side == "long" else sig["reasons_short"]
                res_flag = " ⚡共振" if sig["symbol"] in resonance_map and side == resonance_map[sig["symbol"]]["side"] else ""
                logger.info(
                    f"  📡 [{tf}] {sig['symbol']}: net={sig['net_score']:+.0f} "
                    f"side={side} {'|'.join(reasons)}{res_flag}"
                )
                # 服务器版：记录信号日志
                log_signal({
                    "ts": now_str,
                    "strategy": "hanmuxia",
                    "timeframe": tf,
                    "symbol": sig["symbol"],
                    "net_score": sig["net_score"],
                    "trade_side": side,
                    "reasons": reasons,
                    "resonance": sig["symbol"] in resonance_map and side == resonance_map[sig["symbol"]]["side"],
                    "price": sig.get("price", 0),
                    "atr": sig.get("atr", 0),
                    **sentinel_fields(sig["symbol"]),
                })

        # 6. 开仓：共振优先，高分优先
        # v11: 普通信号满仓不开；强信号可在 _open_position 内释放弱仓后等量替换
        opened_symbols = set()
        for sym, rinfo in resonance_map.items():
            # 在两个周期都开仓
            for tf in rinfo["tfs"]:
                if self._has_position_in_tf(tf, sym):
                    continue
                # 找到对应信号
                sig = next((s for s in all_signals.get(tf, []) if s["symbol"] == sym), None)
                if sig:
                    tf_full = self._pos_count(tf) >= self.max_positions
                    if tf_full and not self._can_try_full_replacement(sig, resonance=True):
                        log_sentinel_scan(
                            sym, tf, "position_rejected", "周期池满且未达到强信号替换条件",
                            side=sig.get("trade_side", ""),
                            score=abs(float(sig.get("net_score") or 0)),
                            decision_stage="position_replacement",
                            filter_layer="risk",
                        )
                        continue
                    self._open_position(sig, now_str, resonance=True, force_replacement=tf_full)
                    opened_symbols.add(sym)

        # 再处理单周期信号
        for tf in TIMEFRAMES:
            for sig in all_signals[tf]:
                if self._has_position_in_tf(tf, sig["symbol"]):
                    continue
                if sig["symbol"] in opened_symbols:
                    continue
                # REQUIRE_RESONANCE=False 时不强制共振
                if REQUIRE_RESONANCE and sig["symbol"] not in resonance_map:
                    logger.debug(f"  ⏭️ [{tf}] {sig['symbol']} 无共振，跳过（REQUIRE_RESONANCE=True）")
                    log_sentinel_scan(
                        sig["symbol"], tf, "strategy_rejected", "无共振，REQUIRE_RESONANCE=True",
                        side=sig.get("trade_side", ""),
                        score=abs(float(sig.get("net_score") or 0)),
                        decision_stage="resonance",
                    )
                    continue
                tf_full = self._pos_count(tf) >= self.max_positions
                if tf_full and not self._can_try_full_replacement(sig, resonance=False):
                    log_sentinel_scan(
                        sig["symbol"], tf, "position_rejected", "周期池满且未达到强信号替换条件",
                        side=sig.get("trade_side", ""),
                        score=abs(float(sig.get("net_score") or 0)),
                        decision_stage="position_replacement",
                        filter_layer="risk",
                    )
                    continue
                self._open_position(sig, now_str, resonance=False, force_replacement=tf_full)

        # 7. VPB 策略开仓（独立开仓，不受半木夏共振限制）
        for tf in TIMEFRAMES:
            for vsig in vpb_signals_all.get(tf, []):
                sym = vsig["symbol"]
                if self._has_position_in_tf(tf, sym):
                    continue
                cd = self.cooldowns.get(tf, {}).get(sym)
                if cd and now < cd:
                    continue
                tf_full = self._pos_count(tf) >= self.max_positions
                if tf_full and not self._can_try_full_replacement(vsig, resonance=vsig.get("vpb_resonance", False)):
                    log_sentinel_scan(
                        sym, tf, "position_rejected", "VPB周期池满且未达到强信号替换条件",
                        side=vsig.get("trade_side", ""),
                        score=abs(float(vsig.get("vpb_score") or vsig.get("net_score") or 0)),
                        decision_stage="position_replacement",
                        filter_layer="risk",
                    )
                    continue
                self._open_position(vsig, now_str, resonance=vsig.get("vpb_resonance", False), force_replacement=tf_full)

    def _open_position(self, sig: dict, now_str: str, resonance: bool = False, force_replacement: bool = False):
        """根据信号开仓，同时在交易所下真实订单。"""
        now_dt = datetime.now(CST)
        tf = sig["timeframe"]
        side = sig["trade_side"]

        # 共振加分
        net_score = self._effective_signal_score(sig, resonance=resonance)

        if side == "long":
            sl = sig["sl_long"]
            tp = sig["tp_long"]
            reasons = sig["reasons_long"]
        else:
            sl = sig["sl_short"]
            tp = sig["tp_short"]
            reasons = sig["reasons_short"]

        if resonance:
            reasons = list(reasons) + [f"⚡{'+'.join(TIMEFRAMES)}共振"]

        inst_id = sig["symbol"]
        price = sig["price"]
        atr = sig["atr"]
        risk_usdt = RISK_PER_TRADE_USDT

        # ── 前置安全校验 ──
        # 1. ATR=0 保护：ATR为0时止盈止损价格计算无效，直接跳过
        if atr <= 0:
            logger.warning(f"  ⚠️ {inst_id}({tf}) ATR=0，跳过开仓（避免止盈止损价格计算错误）")
            # v6: 自动将ATR=0币种加入黑名单
            if inst_id not in ATR_ZERO_BLACKLIST:
                ATR_ZERO_BLACKLIST.add(inst_id)
                logger.warning(f"  📛 {inst_id} 已加入ATR=0黑名单，后续扫描自动跳过")
            event = {
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "sl": sl, "tp": tp,
                "score": net_score, "reasons": reasons,
                "atr": atr, "leverage": self.leverage,
                "divergence": sig["divergence_primary"]["description"],
                "st_dir": "多" if sig["st_direction"] == 1 else "空",
                "st_flip": sig["st_flipped"],
                "timeframe": tf, "resonance": resonance,
                "skip_reason": "ATR=0，止损止盈计算无效",
                "decision_stage": "market_microstructure",
                "filter_layer": "market_data",
                **sentinel_fields(inst_id),
            }
            log_event(event)
            return

        # 2. 止损方向校验（防止止损价格方向错误）
        if side == "long" and sl >= price:
            logger.warning(f"  ⚠️ {inst_id}({tf}) 多单止损价{sl}>=开仓价{price}，跳过")
            event = {
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "sl": sl, "tp": tp,
                "score": net_score, "reasons": reasons,
                "atr": atr, "leverage": self.leverage,
                "divergence": sig["divergence_primary"]["description"],
                "st_dir": "多" if sig["st_direction"] == 1 else "空",
                "st_flip": sig["st_flipped"],
                "timeframe": tf, "resonance": resonance,
                "skip_reason": f"多单止损价{sl}>=开仓价{price}",
                "decision_stage": "market_microstructure",
                "filter_layer": "market_data",
                **sentinel_fields(inst_id),
            }
            log_event(event)
            return

        if side == "short" and sl <= price:
            logger.warning(f"  ⚠️ {inst_id}({tf}) 空单止损价{sl}<=开仓价{price}，跳过")
            event = {
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "sl": sl, "tp": tp,
                "score": net_score, "reasons": reasons,
                "atr": atr, "leverage": self.leverage,
                "divergence": sig["divergence_primary"]["description"],
                "st_dir": "多" if sig["st_direction"] == 1 else "空",
                "st_flip": sig["st_flipped"],
                "timeframe": tf, "resonance": resonance,
                "skip_reason": f"空单止损价{sl}<=开仓价{price}",
                "decision_stage": "market_microstructure",
                "filter_layer": "market_data",
                **sentinel_fields(inst_id),
            }
            log_event(event)
            return

        # ── 合约可交易性检查 ──
        tradable = self.client.is_tradable(inst_id)
        if not tradable["tradable"]:
            logger.info(f"  ⏭️ {inst_id}({tf}) 不可交易({tradable['reason']})，跳过开仓")
            event = {
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "sl": sl, "tp": tp,
                "score": net_score, "reasons": reasons,
                "atr": atr, "leverage": self.leverage,
                "divergence": sig["divergence_primary"]["description"],
                "st_dir": "多" if sig["st_direction"] == 1 else "空",
                "st_flip": sig["st_flipped"],
                "timeframe": tf, "resonance": resonance,
                "skip_reason": tradable["reason"],
                "decision_stage": "tradability",
                "filter_layer": "execution",
                **sentinel_fields(inst_id),
            }
            log_event(event)
            # 不可交易的币种自动加入黑名单
            if inst_id not in ATR_ZERO_BLACKLIST:
                ATR_ZERO_BLACKLIST.add(inst_id)
                logger.warning(f"  📛 {inst_id} 已加入黑名单（不可交易: {tradable['reason']}）")
            return

        existing_exchange_pos = self._exchange_symbol_position(inst_id)
        if existing_exchange_pos or self._has_position(inst_id):
            existing_qty = float(existing_exchange_pos.get("positionAmt") or 0) if existing_exchange_pos else 0.0
            existing_side = str(existing_exchange_pos.get("positionSide") or "")
            existing_entry = float(existing_exchange_pos.get("entryPrice") or 0) if existing_exchange_pos else 0.0
            logger.info(
                f"  ⏭️ {inst_id}({tf}) 交易所/本地已有持仓，禁止同币种叠仓 "
                f"side={existing_side or 'local'} qty={existing_qty:g}"
            )
            log_event({
                "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                "side": side, "price": price, "score": net_score,
                "timeframe": tf, "skip_reason": "同币种已有持仓，禁止叠仓导致保证金偏离100 USDT",
                "risk_category": "position_sizing",
                "decision_stage": "position_sizing",
                "filter_layer": "risk",
                "sizing_policy": "fixed_margin_v1",
                "target_margin_usdt": risk_usdt,
                "existing_exchange_qty": existing_qty,
                "existing_exchange_side": existing_side,
                "existing_entry_price": existing_entry,
                **sentinel_fields(inst_id),
            })
            return

        # ── 真实下单 ──
        order_id = ""
        exchange_qty = 0.0
        exchange_success = False

        try:
            # Binance U本位 quantity 是基础币数量，不能用固定张数截断；
            # 统一按目标保证金换算数量，再以计算保证金校验风险口径。
            exchange_qty = self.execution.calc_quantity(inst_id, price, risk_usdt, self.leverage)
            expected_notional_usdt = exchange_qty * price
            expected_margin_usdt = expected_notional_usdt / self.leverage
            min_notional_floor = 0.0
            if hasattr(self.client, "get_symbol_rules"):
                rules = self.client.get_symbol_rules(inst_id)
                min_notional_floor = float(getattr(rules, "min_notional", 0.0) or 0.0) if rules else 0.0
            min_margin_usdt = risk_usdt * (1 - ORDER_MARGIN_TOLERANCE_PCT)
            max_margin_usdt = risk_usdt * (1 + ORDER_MARGIN_TOLERANCE_PCT)
            min_notional_adjustment = min_notional_floor > risk_usdt * self.leverage and expected_notional_usdt <= min_notional_floor * 1.02
            if not min_notional_adjustment and not min_margin_usdt <= expected_margin_usdt <= max_margin_usdt:
                logger.warning(
                    f"  ⚠️ {inst_id}({tf}) 保证金校验失败: qty={exchange_qty:g}, "
                    f"预计保证金={expected_margin_usdt:.2f} USDT, 目标={risk_usdt:.2f} USDT"
                )
                log_event({
                    "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                    "side": side, "price": price, "score": net_score,
                    "timeframe": tf, "skip_reason": "计算保证金偏离100 USDT目标，拒绝开仓",
                    "risk_category": "position_sizing",
                    "decision_stage": "position_sizing",
                    "filter_layer": "risk",
                    "sizing_policy": "fixed_margin_v1",
                    "target_margin_usdt": risk_usdt,
                    "expected_margin_usdt": round(expected_margin_usdt, 4),
                    "expected_notional_usdt": round(expected_notional_usdt, 4),
                    "min_notional_usdt": round(min_notional_floor, 4),
                    "quantity": exchange_qty,
                    "margin_tolerance_pct": ORDER_MARGIN_TOLERANCE_PCT * 100,
                    **sentinel_fields(inst_id),
                })
                return

            released_for_replacement = False
            if force_replacement:
                if self._release_position_for_strong_signal(
                    sig,
                    now_str,
                    now_dt,
                    reason="周期池满，强信号替换弱仓",
                    effective_score=net_score,
                    preferred_tf=tf,
                    require_preferred_tf=True,
                ):
                    released_for_replacement = True
                    log_event({
                        "time": now_str, "event": "OPEN_RETRY_AFTER_EVICT",
                        "symbol": inst_id, "side": side, "score": net_score,
                        "timeframe": tf, "reason": "周期池满，强信号释放弱仓后重试开仓",
                        "decision_stage": "position_replacement",
                        "filter_layer": "risk",
                        **sentinel_fields(inst_id),
                    })
                else:
                    log_event({
                        "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                        "side": side, "score": net_score, "timeframe": tf,
                        "skip_reason": "周期池满且无可释放弱仓",
                        "risk_category": "position_replacement",
                        "decision_stage": "position_replacement",
                        "filter_layer": "risk",
                        "replacement_policy": REPLACEMENT_POLICY_VERSION,
                        "replacement_min_score": STRONG_SIGNAL_THRESHOLD,
                        "replacement_elite_score": EVICT_ELITE_SCORE,
                        "tf_positions": self._pos_count(tf),
                        "max_positions_per_tf": self.max_positions,
                        **sentinel_fields(inst_id),
                    })
                    return

            if not released_for_replacement and not self._can_open_new_position(risk_usdt, now_str, tf, inst_id, side, net_score):
                total_positions = max(self._total_local_positions(), self._total_exchange_positions())
                if total_positions >= MAX_TOTAL_POSITIONS and self._release_position_for_strong_signal(sig, now_str, now_dt, effective_score=net_score):
                    log_event({
                        "time": now_str, "event": "OPEN_RETRY_AFTER_EVICT",
                        "symbol": inst_id, "side": side, "score": net_score,
                        "timeframe": tf, "reason": "强信号释放弱仓后重试开仓",
                        "decision_stage": "position_replacement",
                        "filter_layer": "risk",
                        **sentinel_fields(inst_id),
                    })
                else:
                    return

            # 下单
            if side == "long":
                exec_result = self.execution.open_position(OpenRequest(
                    symbol=inst_id, side="long", price=price,
                    risk_usdt=risk_usdt, leverage=self.leverage,
                    take_profit=tp, stop_loss=sl,
                    quantity=exchange_qty,
                    confirm_position=True,
                ))
            else:
                exec_result = self.execution.open_position(OpenRequest(
                    symbol=inst_id, side="short", price=price,
                    risk_usdt=risk_usdt, leverage=self.leverage,
                    take_profit=tp, stop_loss=sl,
                    quantity=exchange_qty,
                    confirm_position=True,
                ))
            result = exec_result.raw if isinstance(exec_result.raw, dict) else {}

            # Binance 成功返回包含 orderId
            if exec_result.success:
                exchange_success = True
                order_id = exec_result.order_id
                planned_exchange_qty = exchange_qty
                exchange_qty = float(exec_result.quantity or exchange_qty)
                actual_margin_usdt = exchange_qty * price / self.leverage
                if not min_notional_adjustment and not min_margin_usdt <= actual_margin_usdt <= max_margin_usdt:
                    close_exec = self.execution.close_position(CloseRequest(
                        symbol=inst_id,
                        side=side,
                        quantity=exchange_qty,
                        cancel_open_orders=True,
                    ))
                    log_event({
                        "time": now_str,
                        "event": "OPEN_SIZING_MISMATCH_CLOSED" if close_exec.success else "OPEN_SIZING_MISMATCH_FAILED",
                        "symbol": inst_id,
                        "side": side,
                        "price": price,
                        "score": net_score,
                        "timeframe": tf,
                        "reason": "成交后确认保证金偏离100 USDT目标，自动撤销该仓",
                        "sizing_policy": "fixed_margin_v1_confirmed",
                        "target_margin_usdt": risk_usdt,
                        "planned_exchange_qty": planned_exchange_qty,
                        "confirmed_exchange_qty": exchange_qty,
                        "confirmed_margin_usdt": round(actual_margin_usdt, 4),
                        "planned_margin_usdt": round(planned_exchange_qty * price / self.leverage, 4),
                        "exchange_close_success": close_exec.success,
                        "close_failure_reason": close_exec.reason,
                        "decision_stage": "post_open_sizing_confirm",
                        "filter_layer": "execution",
                        **sentinel_fields(inst_id),
                    })
                    now_dt = datetime.now(CST)
                    self.cooldowns[tf][inst_id] = now_dt + timedelta(minutes=15)
                    logger.warning(
                        f"  开仓后保证金确认失败并已处理: {inst_id} planned={planned_exchange_qty:g} "
                        f"actual={exchange_qty:g} margin={actual_margin_usdt:.2f} close={close_exec.success}"
                    )
                    return
                logger.info(f"  下单成功: orderId={order_id} qty={exchange_qty}")
            elif exec_result.preflight_rejected:
                err_code = exec_result.code or "preflight_rejected"
                err_msg = exec_result.message or exec_result.reason
                logger.info(f"  执行预检跳过: code={err_code} msg={err_msg}")
                event = {
                    "time": now_str, "event": "OPEN_SKIPPED", "symbol": inst_id,
                    "side": side, "price": price, "sl": sl, "tp": tp,
                    "score": net_score, "reasons": reasons,
                    "atr": sig["atr"], "leverage": self.leverage,
                    "divergence": sig["divergence_primary"]["description"],
                    "st_dir": "多" if sig["st_direction"] == 1 else "空",
                    "st_flip": sig["st_flipped"],
                    "timeframe": tf,
                    "resonance": resonance,
                    "err_code": err_code,
                    "err_msg": err_msg,
                    "skip_reason": f"执行预检拒绝({err_code}): {err_msg[:80]}",
                    "reason": f"执行预检拒绝({err_code}): {err_msg[:80]}",
                    "preflight": exec_result.preflight_detail,
                    "risk_category": "execution_preflight",
                    "decision_stage": "execution_preflight",
                    "filter_layer": "execution",
                    **sentinel_fields(inst_id),
                }
                log_event(event)
                now_dt = datetime.now(CST)
                self.cooldowns[tf][inst_id] = now_dt + timedelta(minutes=5)
                logger.info(f"  ⏳ {inst_id}({tf}) 预检跳过，冷却5分钟")
                return
            elif exec_result.code == "-1001":
                # 字典式错误响应（如 Binance Testnet）
                err_code = exec_result.code or "?"
                err_msg = exec_result.message or "?"
                logger.error(f"  下单失败: code={err_code} msg={err_msg}")
                event = {
                    "time": now_str, "event": "OPEN_FAILED", "symbol": inst_id,
                    "side": side, "price": price, "sl": sl, "tp": tp,
                    "score": net_score, "reasons": reasons,
                    "atr": sig["atr"], "leverage": self.leverage,
                    "divergence": sig["divergence_primary"]["description"],
                    "st_dir": "多" if sig["st_direction"] == 1 else "空",
                    "st_flip": sig["st_flipped"],
                    "timeframe": tf,
                    "resonance": resonance,
                    "err_code": err_code,
                    "err_msg": err_msg,
                    "reason": f"下单失败({err_code}): {err_msg[:80]}",
                    "decision_stage": "execution",
                    "filter_layer": "execution",
                    **sentinel_fields(inst_id),
                }
                log_event(event)
                # v6: 开仓失败也进入冷却期（15分钟），避免同币种反复失败
                now_dt = datetime.now(CST)
                self.cooldowns[tf][inst_id] = now_dt + timedelta(minutes=15)
                logger.info(f"  ⏳ {inst_id}({tf}) 开仓失败，冷却15分钟")
                return
            else:
                # 其他格式的错误
                err_code = exec_result.code or "?"
                err_msg = exec_result.message or str(exec_result.raw)[:200]
                logger.error(f"  下单失败: code={err_code} msg={err_msg}")
                event = {
                    "time": now_str, "event": "OPEN_FAILED", "symbol": inst_id,
                    "side": side, "price": price, "sl": sl, "tp": tp,
                    "score": net_score, "reasons": reasons,
                    "atr": sig["atr"], "leverage": self.leverage,
                    "divergence": sig["divergence_primary"]["description"],
                    "st_dir": "多" if sig["st_direction"] == 1 else "空",
                    "st_flip": sig["st_flipped"],
                    "timeframe": tf,
                    "resonance": resonance,
                    "err_code": err_code,
                    "err_msg": err_msg,
                    "reason": f"下单失败({err_code}): {err_msg[:80]}",
                    "decision_stage": "execution",
                    "filter_layer": "execution",
                    **sentinel_fields(inst_id),
                }
                log_event(event)
                now_dt = datetime.now(CST)
                self.cooldowns[tf][inst_id] = now_dt + timedelta(minutes=15)
                logger.info(f"  ⏳ {inst_id}({tf}) 开仓失败，冷却15分钟")
                return
        except Exception as e:
            logger.error(f"  下单异常: {e}")
            event = {
                "time": now_str, "event": "OPEN_FAILED", "symbol": inst_id,
                "side": side, "price": price, "sl": sl, "tp": tp,
                "score": net_score, "reasons": reasons,
                "atr": sig["atr"], "leverage": self.leverage,
                "timeframe": tf,
                "resonance": resonance,
                "err_msg": str(e),
                "reason": f"下单异常: {str(e)[:80]}",
                "decision_stage": "execution",
                "filter_layer": "execution",
                **sentinel_fields(inst_id),
            }
            log_event(event)
            # v6: 异常下单也进入冷却期
            now_dt = datetime.now(CST)
            self.cooldowns[tf][inst_id] = now_dt + timedelta(minutes=15)
            return

        # 创建本地追踪持仓
        pos = SimPosition(
            symbol=sig["symbol"],
            side=side,
            entry_price=sig["price"],
            size=float(exchange_qty),
            leverage=self.leverage,
            stop_loss=sl,
            take_profit=tp,
            atr_at_entry=sig["atr"],
            entry_time=now_str,
            entry_score=net_score,
            entry_reason=" + ".join(reasons),
            timeframe=tf,
            resonance=resonance,
            sl_mult=SL_MULT.get(tf, 2.0),
            tp_mult=TP_MULT.get(tf, 5.0),
            trail_activate=TRAILING_ACTIVATE.get(tf, 1.5),
            trail_pullback=TRAILING_PULLBACK.get(tf, 1.0),
            order_id=order_id,
            exchange_qty=exchange_qty,
        )
        self.positions[tf][(sig["symbol"], side)] = pos

        event = {
            "time": now_str, "event": "OPEN", "symbol": sig["symbol"],
            "side": side, "price": sig["price"], "sl": sl, "tp": tp,
            "score": net_score, "reasons": reasons,
            "atr": sig["atr"], "leverage": self.leverage,
            "divergence": sig["divergence_primary"]["description"],
            "st_dir": "多" if sig["st_direction"] == 1 else "空",
            "st_flip": sig["st_flipped"],
            "timeframe": tf,
            "resonance": resonance,
            "order_id": order_id,
            "exchange_qty": exchange_qty,
            "planned_exchange_qty": locals().get("planned_exchange_qty", exchange_qty),
            "sizing_policy": "fixed_margin_v1",
            "target_margin_usdt": risk_usdt,
            "expected_margin_usdt": round(exchange_qty * price / self.leverage, 4),
            "expected_notional_usdt": round(exchange_qty * price, 4),
            "min_notional_adjusted": min_notional_adjustment,
            "exchange_success": exchange_success,
            "decision_stage": "open",
            "filter_layer": "execution",
            **sentinel_fields(sig["symbol"]),
        }
        log_event(event)
        res_str = " ⚡共振" if resonance else ""
        exchange_str = f" [{exchange_qty}]" if exchange_success else " [本地模拟]"
        logger.info(f"  ✅ 开仓[{tf}]: {sig['symbol']} {side} @ {sig['price']}{exchange_str} | SL={sl} TP={tp} | {pos.entry_reason}{res_str}")

    def _check_exits(self, now_str: str, now_dt: datetime = None):
        """检查所有周期持仓的止盈止损，触发平仓时在交易所平仓"""
        for tf in list(self.positions.keys()):
            to_close = []
            for key, pos in self.positions[tf].items():
                try:
                    current_price = fetch_current_price(pos.symbol)
                except Exception:
                    continue
                reason = pos.check_exit(current_price)
                # v11优化(2026-05-08): 震荡仓清理
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
                                    reason = f"震荡平仓（持仓{age_hours:.0f}h，波动仅{pnl_pct_abs:.1f}%，信号获取失败）"
                    except Exception:
                        pass
                if reason:
                    to_close.append((key, pos, current_price, reason))

            for key, pos, exit_price, reason in to_close:
                sym = pos.symbol
                # ── 真实平仓 ──
                exchange_close_success = False
                if pos.exchange_qty > 0:
                    try:
                        pos_side = "long" if pos.side == "long" else "short"
                        close_exec = self.execution.close_position(CloseRequest(
                            symbol=sym,
                            side=pos_side,
                            quantity=pos.exchange_qty,
                            cancel_open_orders=True,
                        ))
                        if close_exec.success:
                            exchange_close_success = True
                            logger.info(f"  平仓成功: {sym} {pos_side} {pos.exchange_qty}")
                        else:
                            logger.error(f"  平仓失败: {close_exec.reason}")
                    except Exception as e:
                        logger.error(f"  平仓异常: {e}")

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
                    "exit_reason": reason, "pnl_pct": round(pnl, 2),
                    "pnl_usd": round(pnl_usd, 4),
                    "leverage": pos.leverage, "score": pos.entry_score,
                    "reason": pos.entry_reason,
                    "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                    "trailing_sl": round(pos.trailing_sl, 4),
                    "highest": round(pos.highest_price, 4),
                    "lowest": round(pos.lowest_price, 4),
                    "timeframe": tf,
                    "resonance": pos.resonance,
                    "order_id": pos.order_id,
                    "exchange_qty": pos.exchange_qty,
                    "exchange_close_success": exchange_close_success,
                }
                self.closed_trades.append(trade)
                log_trade(trade)

                event = {
                    "time": now_str, "event": "CLOSE", "symbol": sym,
                    "side": pos.side, "exit_price": round(exit_price, 4),
                    "reason": reason, "pnl_pct": round(pnl, 2),
                    "pnl_usd": round(pnl_usd, 4),
                    "entry_price": pos.entry_price,
                    "entry_time": pos.entry_time,
                    "timeframe": tf,
                    "resonance": pos.resonance,
                    "exchange_close_success": exchange_close_success,
                }
                log_event(event)
                exchange_str = " [交易所✓]" if exchange_close_success else (" [交易所✗]" if pos.exchange_qty > 0 else "")
                logger.info(
                    f"  {'🟢' if pnl > 0 else '🔴'} 平仓[{tf}]: {sym} {pos.side} @ {exit_price:.4f} "
                    f"| {reason} | PnL={pnl:+.2f}% (${pnl_usd:+.4f}){exchange_str}"
                )
                del self.positions[tf][key]
                # v11: 只用浮动止损，所有平仓统一用 COOLDOWN_MINUTES 冷却期
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

    def generate_report(self) -> str:
        """生成交易报告"""
        now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        # 读取所有事件和交易日志
        events = []
        trades = []
        if EVENTS_LOG.exists():
            with open(EVENTS_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    try: events.append(json.loads(line.strip()))
                    except: pass
        if TRADES_LOG.exists():
            with open(TRADES_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    try: trades.append(json.loads(line.strip()))
                    except: pass

        opens = [e for e in events if e["event"] == "OPEN"]

        # ── 总体统计 ──
        total_trades = len(trades)
        wins = sum(1 for t in trades if t["pnl_pct"] > 0)
        losses = sum(1 for t in trades if t["pnl_pct"] <= 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum(t["pnl_pct"] for t in trades)
        avg_pnl = (total_pnl / total_trades) if total_trades > 0 else 0

        lines = [
            f"# 半木夏策略双周期扫描报告（Binance Testnet 实单 + VPB双策略 v10）",
            f"",
            f"**生成时间**: {now}",
            f"**扫描开始**: {self.start_time}",
            f"**扫描周期**: {' + '.join(TIMEFRAMES)}",
            f"**扫描轮数**: {self.scan_count}",
            f"**扫描币种**: Binance USDT永续合约 Top100 + 异常放量",
            f"**开仓阈值**: |net_score| >= {SCORE_THRESHOLD} | VPB 15m>=60 / 30m>=55",
            f"**共振加分**: {RESONANCE_BONUS} | 共振硬条件: {'是' if REQUIRE_RESONANCE else '否'}",
            f"**止损模式**: 只用浮动止损（开仓即激活，初始锚点=止损价）+ 最大亏损{MAX_LOSS_PCT:.0f}%兜底 | v11: 固定止损已移除",
            f"**止盈倍数**: " + " / ".join(f"{tf}={TP_MULT[tf]}×ATR" for tf in TIMEFRAMES),
            f"**浮动止损回撤**: " + " / ".join(f"{tf}={TRAILING_PULLBACK[tf]}×ATR" for tf in TIMEFRAMES),
            f"**冷却期**: {COOLDOWN_MINUTES}分钟 | 高波动过滤: ATR/Price>{ATR_PRICE_MAX*100:.0f}%",
            f"**持仓管理**: 普通信号满仓不开；强信号>= {STRONG_SIGNAL_THRESHOLD} 可释放弱仓等量替换 | 异常放量: {VOL_SPIKE_MULT}x+",
            f"**安全限制**: ATR=0跳过 | 止损方向校验 | 开仓保证金目标{RISK_PER_TRADE_USDT} USDT±{ORDER_MARGIN_TOLERANCE_PCT*100:.0f}% | 最大亏损{MAX_LOSS_PCT:.0f}%强制平仓 | 止损冷静{STOP_LOSS_COOLDOWN_MINUTES}min",
            f"",
            f"---",
            f"",
            f"## 📊 交易统计",
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总交易次数 | {total_trades} |",
            f"| 盈利次数 | {wins} |",
            f"| 亏损次数 | {losses} |",
            f"| 胜率 | {win_rate:.1f}% |",
            f"| 总盈亏% | {total_pnl:+.2f}% |",
            f"| 平均盈亏% | {avg_pnl:+.2f}% |",
            f"| 当前持仓 | {sum(len(p) for p in self.positions.values())} |",
            f"",
        ]

        # ── 分周期统计 ──
        lines.extend(["---", "", "## 📊 分周期统计", ""])
        lines.append("| 周期 | 交易数 | 盈利 | 亏损 | 胜率 | 总盈亏% | 平均盈亏% | 当前持仓 |")
        lines.append("|------|--------|------|------|------|---------|-----------|----------|")

        for tf in TIMEFRAMES:
            tf_trades = [t for t in trades if t.get("timeframe") == tf]
            tf_total = len(tf_trades)
            tf_wins = sum(1 for t in tf_trades if t["pnl_pct"] > 0)
            tf_losses = sum(1 for t in tf_trades if t["pnl_pct"] <= 0)
            tf_win_rate = (tf_wins / tf_total * 100) if tf_total > 0 else 0
            tf_pnl = sum(t["pnl_pct"] for t in tf_trades)
            tf_avg = (tf_pnl / tf_total) if tf_total > 0 else 0
            tf_open = len(self.positions.get(tf, {}))
            lines.append(
                f"| {tf} | {tf_total} | {tf_wins} | {tf_losses} | "
                f"{tf_win_rate:.1f}% | {tf_pnl:+.2f}% | {tf_avg:+.2f}% | {tf_open} |"
            )

        # ── 共振交易统计 ──
        res_trades = [t for t in trades if t.get("resonance")]
        if res_trades:
            res_wins = sum(1 for t in res_trades if t["pnl_pct"] > 0)
            res_pnl = sum(t["pnl_pct"] for t in res_trades)
            lines.extend(["", "### ⚡ 共振交易统计", ""])
            lines.append(f"- 共振交易: {len(res_trades)} 笔 | 胜率: {res_wins/len(res_trades)*100:.1f}% | 总盈亏: {res_pnl:+.2f}%")

        # ── 开仓记录 ──
        lines.extend(["", "---", "", f"## 📋 开仓记录（共 {len(opens)} 笔）", ""])
        if opens:
            lines.append("| # | 时间 | 周期 | 币种 | 方向 | 价格 | 综合分 | 止损 | 止盈 | 背离 | ST | 共振 |")
            lines.append("|---|------|------|------|------|------|--------|------|------|------|------|-----|------|")
            for i, o in enumerate(opens, 1):
                div_desc = o.get("divergence", "-")
                st_dir = o.get("st_dir", "-")
                st_flip = "翻转!" if o.get("st_flip") else ""
                tf = o.get("timeframe", "?")
                res = "⚡" if o.get("resonance") else ""
                lines.append(
                    f"| {i} | {o['time']} | {tf} | {o['symbol'].split('-')[0]} | "
                    f"{'🟢多' if o['side']=='long' else '🔴空'} | "
                    f"{o['price']:.4f} | {o.get('score',0):+.0f} | "
                    f"{o['sl']:.4f} | {o['tp']:.4f} | "
                    f"{div_desc} | {st_dir}{st_flip} | {res} |"
                )
        else:
            lines.append("*暂无开仓记录*")

        # ── 平仓记录 ──
        lines.extend(["", "---", "", f"## 📋 平仓记录（共 {len(trades)} 笔）", ""])
        if trades:
            lines.append("| # | 开仓时间 | 平仓时间 | 周期 | 币种 | 方向 | 开仓价 | 平仓价 | 原因 | 盈亏% | 盈亏$ |")
            lines.append("|---|----------|----------|------|------|------|--------|--------|------|-------|-------|")
            for i, t in enumerate(trades, 1):
                emoji = "✅" if t["pnl_pct"] > 0 else "❌"
                tf = t.get("timeframe", "?")
                lines.append(
                    f"| {i} | {t['entry_time']} | {t['exit_time']} | {tf} | "
                    f"{t['symbol'].split('-')[0]} | {'多' if t['side']=='long' else '空'} | "
                    f"{t['entry_price']:.4f} | {t['exit_price']:.4f} | "
                    f"{t['exit_reason']} | {emoji}{t['pnl_pct']:+.2f}% | ${t['pnl_usd']:+.4f} |"
                )
        else:
            lines.append("*暂无平仓记录*")

        # ── 当前持仓 ──
        total_open = sum(len(p) for p in self.positions.values())
        if total_open > 0:
            lines.extend(["", "---", "", "## 🔄 当前持仓", ""])
            lines.append("| 周期 | 币种 | 方向 | 开仓价 | 开仓时间 | 综合分 | 条件 |")
            lines.append("|------|------|------|--------|----------|--------|------|")
            for tf in TIMEFRAMES:
                for sym, pos in self.positions.get(tf, {}).items():
                    res = "⚡" if pos.resonance else ""
                    sym_text = sym[0] if isinstance(sym, tuple) else str(sym)
                    lines.append(
                        f"| {tf} | {sym_text.split('-')[0]} | {'多' if pos.side=='long' else '空'} | "
                        f"{pos.entry_price:.4f} | {pos.entry_time} | "
                        f"{pos.entry_score:+.0f} | {pos.entry_reason} {res} |"
                    )
        else:
            lines.extend(["", "---", "", "## 🔄 当前持仓", ""])
            lines.append("*当前无持仓*")

        report = "\n".join(lines)
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(report)
        return report


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="半木夏+VPB 双策略 Top100 双周期扫描器 (Binance Testnet)")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--interval", type=int, default=120, help="扫描间隔（秒），默认120=2分钟")
    args = parser.parse_args()

    scanner = Scanner()

    logger.info("=" * 60)
    logger.info("  半木夏策略 v10 — Top100 双周期扫描器 启动")
    logger.info("  ✓ Binance Testnet 实单 + 🔥 VPB双策略 + ♻️ 强信号替换弱仓")
    logger.info(f"  周期: {' + '.join(TIMEFRAMES)} | 杠杆: {scanner.leverage}x | 每周期最大持仓: {scanner.max_positions}")
    logger.info(f"  共振加分: {RESONANCE_BONUS} | 共振硬条件: {'是' if REQUIRE_RESONANCE else '否'} | 开仓阈值: |net_score| >= {SCORE_THRESHOLD}")
    logger.info(f"  止盈: 15m={TP_MULT['15m']}×ATR  30m={TP_MULT['30m']}×ATR | 止损: 只用浮动止损(回撤 15m={TRAILING_PULLBACK['15m']}×ATR  30m={TRAILING_PULLBACK['30m']}×ATR)")
    logger.info(f"  VPB阈值: 15m>=60 / 30m>=55 | 量能上限: {VPB_VOL_MULT_MAX}x | 最大亏损: {MAX_LOSS_PCT:.0f}%强制平仓")
    logger.info(f"  每笔开仓: {RISK_PER_TRADE_USDT} USDT | 强信号>={STRONG_SIGNAL_THRESHOLD}满仓可替换弱仓 | 止损冷静: {STOP_LOSS_COOLDOWN_MINUTES}min")
    logger.info(f"  数据目录: {DATA_DIR}")
    logger.info(f"  报告将保存至: {REPORT_FILE}")
    logger.info("=" * 60)

    # 服务器版：记录启动操作日志
    log_operation({
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "type": "STARTUP",
        "version": "v10",
        "config": {
            "score_threshold": SCORE_THRESHOLD,
            "require_resonance": REQUIRE_RESONANCE,
            "resonance_bonus": RESONANCE_BONUS,
            "leverage": scanner.leverage,
            "max_positions": scanner.max_positions,
            "timeframes": TIMEFRAMES,
            "interval": args.interval,
        },
    })

    heartbeat_counter = 0
    try:
        while True:
            try:
                scanner.scan_cycle()
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)
                log_operation({
                    "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "SCAN_ERROR",
                    "error": str(e),
                })

            if args.once:
                break

            scanner.generate_report()
            
            # 服务器版：每10个周期记录一次系统心跳
            heartbeat_counter += 1
            if heartbeat_counter % 10 == 0:
                try:
                    bal = scanner.client.get_balance()
                    # Binance /fapi/v2/account 返回dict，余额在assets列表中
                    if isinstance(bal, dict):
                        usdt_bal = next((b for b in bal.get("assets", []) if b.get("asset") == "USDT"), {})
                    elif isinstance(bal, list):
                        usdt_bal = next((b for b in bal if b.get("asset") == "USDT"), {})
                    else:
                        usdt_bal = {}
                    log_system({
                        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                        "type": "HEARTBEAT",
                        "scan_count": scanner.scan_count,
                        "balance": usdt_bal.get("walletBalance", usdt_bal.get("balance", "0")),
                        "avail_bal": usdt_bal.get("availableBalance", "0"),
                        "open_positions": sum(len(p) for p in scanner.positions.values()),
                        "closed_trades": len(scanner.closed_trades),
                        "atr_blacklist": list(ATR_ZERO_BLACKLIST),
                    })
                except Exception:
                    pass
            
            logger.info(f"  ⏳ 等待 {args.interval}s 后下一轮扫描...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("用户中断，生成最终报告...")
        log_operation({
            "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "type": "SHUTDOWN",
            "scan_count": scanner.scan_count,
            "closed_trades": len(scanner.closed_trades),
        })

    report = scanner.generate_report()
    logger.info(f"报告已保存至: {REPORT_FILE}")
    logger.info(f"共扫描 {scanner.scan_count} 轮，完成 {len(scanner.closed_trades)} 笔交易")


if __name__ == "__main__":
    main()
