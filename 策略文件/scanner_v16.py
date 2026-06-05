"""v16 订单流策略 - Account B（复用v13 API Key）
信号：订单流不平衡(OFI) + RSI背离 + 资金费率确认，极简高效
Author: v16 | Date: 2026-05-12
"""

import json, time, logging, argparse, os, sys, bisect, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
PROJECT_ROOT = ROOT.parent if ROOT.name == "策略文件" else ROOT
sys.path.insert(0, str(PROJECT_ROOT))

from cloud.analyzer.auxiliary import calc_rsi, calc_ema, calc_atr
from binance_client_v2 import get_client, BinanceClientV2 as ExchangeClient
from core.audit_log import write_jsonl_with_daily_shard
from core.account_state_cache import load_cached_account_state
from core.execution_engine import CloseRequest, ExecutionEngine, OpenRequest
from core.exchange_state import count_active_positions, count_side_positions, find_symbol_position, usdt_balance_summary
from core.external_market_data import fetch_okx_cvd, fetch_okx_funding_rate, fetch_okx_klines, fetch_okx_ofi
from core.event_store import EventStoreWriter
from core.market_watchlist import load_sentinel_context
from core.market_data_cache import cached_available_symbols, cached_top_symbols, market_data_network_enabled
from core.kline_cache import kline_network_enabled, kline_request_url, load_cached_klines, save_cached_klines
from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request
from core.position_utils import infer_position_side, leveraged_loss_pct
from core.sentinel_scanner import fields_from_context, filter_context_by_available, merge_symbols_with_context
from core.risk_engine import RiskEngine, RiskLimits
from core.strategy_gates import (
    evaluate_account_state_available_gate,
    evaluate_active_position_limit_gate,
    evaluate_b_v16_confirmation_gate,
    evaluate_b_v16_entry_threshold,
    evaluate_b_v16_small_live_stage_guard,
    evaluate_entry_risk_gate,
    evaluate_execution_result_gate,
    evaluate_no_same_symbol_position_gate,
    evaluate_positive_quantity_gate,
    evaluate_score_max_gate,
    evaluate_symbol_blacklist_gate,
    evaluate_symbol_scan_cooldown_gate,
    evaluate_symbol_stop_loss_gate,
    evaluate_timeframe_position_gate,
    evaluate_watchlist_score_adjustment,
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


logging.basicConfig(level=console_log_level(), format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("scanner_v16")

CST = timezone(timedelta(hours=8))

# ═══ 配置 ═══
TIMEFRAMES = ["1h", "15m"]      # 保留历史持仓管理周期
ENTRY_TIMEFRAMES = ["1h"]       # 15m不再独立开仓，仅作为1h信号确认
CONFIRM_TIMEFRAME = "15m"
CONFIRM_MIN_SCORE = 15
CONFIRM_OPPOSITE_REJECT_SCORE = 25
MAX_POS_PER_TF = 2              # v16优化(2026-05-15): 4→2，极简持仓
MAX_TOTAL_POSITIONS = 20        # P0: 全账户总持仓上限，超过后只管理不新开
MAX_ACTIVE_NEW_POSITIONS = 12   # 低仓位恢复利用率，12仓后只管理不新开
LEVERAGE = 4
STAGE_GUARD_SMALL_LIVE_ENABLED = os.environ.get("V16_STAGE_GUARD_SMALL_LIVE", "0").strip().lower() not in {"0", "false", "no"}
SMALL_LIVE_TRADE_SIZE = float(os.environ.get("V16_SMALL_LIVE_TRADE_SIZE", "35"))
TRADE_SIZE = SMALL_LIVE_TRADE_SIZE if STAGE_GUARD_SMALL_LIVE_ENABLED else 100.0
SCORE_MIN = 35                  # v16优化(2026-05-15): 30→35，减少假信号
SCORE_THRESHOLDS = {"1h": 38, "15m": 55}  # P1: 1h轻微放宽，15m仍只做强确认
SHORT_ENTRY_PENALTY = 8         # P1: 小币空头额外提高门槛
SCORE_MAX = 85                  # P0 approved 2026-05-31: EXP-20260527-v16-overheat-cap-85
SL_MULT = 2.0                   # 默认止损；实盘使用 sl_mult_for_atr_pct 分档
SL_MULT_LOW_VOL = 2.5           # P0 approved 2026-05-31: EXP-20260527-v16-atr-stop-bands
SL_MULT_NORMAL_VOL = 2.0
SL_MULT_HIGH_VOL = 1.5
ATR_STOP_LOW_VOL_THRESHOLD = 0.02
ATR_STOP_HIGH_VOL_THRESHOLD = 0.03
APPROVED_FULL_LIVE_CANDIDATE_IDS = [
    "EXP-20260527-v16-atr-stop-bands",
    "EXP-20260527-v16-overheat-cap-85",
]
TP_MULT = 4.0
TRAIL_ACTIVATE = 1.0            # P1: 更早进入浮动保护
TRAIL_PULLBACK = 1.0            # v16优化(2026-05-15): 1.5→1.0，更快止损
MAX_LOSS_PCT = 10.0             # P0/P1: 硬底止损收紧，控制单票尾部亏损
COOLDOWN_MIN = 60
ATR_PRICE_MAX = 0.05
MIN_VOL_RATIO = 0.6
MIN_VOL_RATIO_MAJOR = 0.45
MIN_VOL_RATIO_HIGH_VOL = 0.8
MIN_AVAILABLE_BALANCE_PCT = 0.25   # P0: 开新仓后至少保留25%可用保证金
MIN_AVAILABLE_BALANCE_USDT = 300.0 # P0: 绝对可用余额保护线
PROFIT_PROTECT_MIN_USDT = 30.0     # P2: 盈利超过该值后启用回撤保护
PROFIT_PROTECT_RETRACE = 0.25      # P2: 从最高浮盈回撤25%则平仓保护利润
MAX_SL_PER_SYMBOL = 1              # P2: 同币种当日止损后暂停新开
SYMBOL_SL_BAN_CYCLES = 72          # P2: 止损后按扫描周期冷却，约8小时
MAX_POS_PER_SIDE = 12              # P2: 单方向最大持仓数
WATCHLIST_SCORE_PENALTY = 10       # P2: 观察名单只降权，不拉黑
WATCHLIST_SYMBOLS = {
    "BUSDT", "SAGAUSDT", "EIGENUSDT", "BEATUSDT", "ESPORTSUSDT",
    "VELVETUSDT", "SKYAIUSDT", "COLLECTUSDT", "PHBUSDT", "B2USDT",
}
SENTINEL_CONTEXT: dict[str, dict] = {}
MAJOR_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "BCHUSDT", "LTCUSDT"}
NO_CONFIRM_THRESHOLD_PENALTY = 5
WEAK_OPPOSITE_CONFIRM_PENALTY = 8
CONFIRM_BONUS = 5
CONFIRM_STRONG_BONUS = 8
NO_CONFIRM_HIGH_SCORE_PASS = 50
WEAK_CONFIRM_PASS_SCORE = 44
OPPOSITE_HIGH_SCORE_PASS = 65
LOW_POSITION_THRESHOLD_DISCOUNT = 3
STAGE_GUARD_MIN_SCORE = float(os.environ.get("V16_STAGE_GUARD_MIN_SCORE", "55"))
STAGE_GUARD_REVERSE_PASS_SCORE = float(os.environ.get("V16_STAGE_GUARD_REVERSE_PASS_SCORE", "65"))

# P2: 降低单指标叠分导致的高分虚胖
SCORE_OFI_STRONG = 20
SCORE_OFI_WEAK = 10
SCORE_RSI_DIVERGENCE = 15
SCORE_RSI_EXTREME = 10
SCORE_FUNDING = 10
SCORE_EMA = 8
SCORE_CVD_STRONG = 15
SCORE_CVD_WEAK = 8


def sl_mult_for_atr_pct(atr_pct: float) -> float:
    """Return the approved v16 ATR stop band for the current volatility regime."""
    if atr_pct >= ATR_STOP_HIGH_VOL_THRESHOLD:
        return SL_MULT_HIGH_VOL
    if atr_pct < ATR_STOP_LOW_VOL_THRESHOLD:
        return SL_MULT_LOW_VOL
    return SL_MULT_NORMAL_VOL

# OFI参数
OFI_STRONG = 0.30   # 极端失衡
OFI_WEAK = 0.15     # 确认信号
OFI_WINDOW = 20     # 滚动窗口

# CVD参数：新加坡服务器可直连实盘 Binance，用实盘成交主动买卖量差，避免Testnet成交稀疏失真。
LIVE_BASE_URL = os.environ.get("BINANCE_LIVE_BASE_URL", "https://fapi.binance.com")
MARKET_BASE_URL = os.environ.get("BINANCE_MARKET_BASE_URL", "https://fapi.binance.com").strip().rstrip("/")
CVD_LIMIT = 120
CVD_STRONG = 0.18
CVD_WEAK = 0.08
CVD_CACHE_SECONDS = 60

# 黑名单
LOSS_BLACKLIST = {
    "BASUSDT", "LABUSDT", "LUMIAUSDT", "WIFUSDT", "FARTCOINUSDT",
    "SPORTFUNUSDT", "BTCDOMUSDT", "RDNTUSDT", "RAVEUSDT", "DEXEUSDT",
}

# 数据目录
DATA_DIR = ROOT / "scanner_data_v16"
DATA_DIR.mkdir(exist_ok=True)
TRADES_LOG = DATA_DIR / "trades.jsonl"
EVENTS_LOG = DATA_DIR / "events.jsonl"
LOGS_DIR = ROOT / "logs_v16"
LOGS_DIR.mkdir(exist_ok=True)
SIGNAL_LOG = LOGS_DIR / "signals.jsonl"
DECISION_LOG = LOGS_DIR / "decisions.jsonl"
SYSTEM_LOG = LOGS_DIR / "system.jsonl"
EVENT_STORE = EventStoreWriter(PROJECT_ROOT / "runtime" / "event_store.sqlite3")

def market_url(path: str) -> str:
    return f"{MARKET_BASE_URL}{path}"

def log_jsonl(path: Path, item: dict):
    write_jsonl_with_daily_shard(path, item)

def write_event_store(record: dict, source: str):
    EVENT_STORE.write_event(record, source=source)

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

def log_decision(record: dict, *, persist_event_store: bool = True):
    try:
        log_jsonl(DECISION_LOG, record)
        if persist_event_store:
            write_event_store(record, "B/v16/decisions")
    except Exception as e:
        logger.debug(f"写入决策日志失败: {e}")

def _decision_from_event(event: dict) -> dict:
    status = str(event.get("event") or "EVENT").upper()
    reason = str(event.get("skip_reason") or event.get("reason") or event.get("msg") or "")
    side = str(event.get("side") or "").lower()
    score = event.get("score", event.get("raw_score", event.get("net_score", 0)))
    return {
        "time": event.get("time") or event.get("ts") or str(datetime.now(CST)),
        "strategy": "B/v16",
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
        "cvd": event.get("cvd"),
        "ofi": event.get("ofi"),
        "rsi": event.get("rsi"),
        "funding_rate": event.get("funding_rate"),
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
    side = str(signal.get("trade_side") or signal.get("side") or "").lower()
    score = signal.get("net_score", signal.get("score", signal.get("vpb_score", 0)))
    reasons = signal.get("reasons") or signal.get(f"reasons_{side}") or []
    if isinstance(reasons, list):
        reason = "+".join(str(x) for x in reasons[:6])
    else:
        reason = str(reasons)
    return {
        "time": signal.get("time") or signal.get("ts") or str(datetime.now(CST)),
        "strategy": "B/v16",
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
        "decision_stage": "candidate",
        "filter_layer": "strategy",
        "reasons": reasons,
        "cvd": signal.get("cvd"),
        "ofi": signal.get("ofi"),
        "rsi": signal.get("rsi"),
        "funding_rate": signal.get("funding_rate"),
        "sentinel": signal.get("sentinel", False),
        "sentinel_reason": signal.get("sentinel_reason", ""),
        "sentinel_change_pct": signal.get("sentinel_change_pct"),
        "sentinel_velocity_pct": signal.get("sentinel_velocity_pct"),
        "sentinel_quote_volume": signal.get("sentinel_quote_volume"),
        "sentinel_volume_delta": signal.get("sentinel_volume_delta"),
        "raw": signal,
    }

def log_event(event: dict):
    log_jsonl(EVENTS_LOG, event)
    write_event_store({**_decision_from_event(event), "raw_event": event}, "B/v16/events")
    log_decision(_decision_from_event(event), persist_event_store=False)

def _execution_result_case(name: str, exec_result, *, timeframe: str = "", phase: str = "") -> dict:
    execution_gate = evaluate_execution_result_gate(
        success=exec_result.success,
        preflight_rejected=exec_result.preflight_rejected,
        code=exec_result.code,
        reason=exec_result.reason,
        message=exec_result.message,
    )
    meta = {"strategy": "B/v16", "timeframe": timeframe}
    if phase:
        meta["phase"] = phase
    return strategy_gate_case(
        name=name,
        gate="execution_result",
        inputs={
            "success": exec_result.success,
            "preflight_rejected": exec_result.preflight_rejected,
            "code": exec_result.code,
            "reason": exec_result.reason,
            "message": exec_result.message,
        },
        decision=execution_gate,
        meta=meta,
    )

def _execution_exception_case(name: str, exc: Exception | str, *, timeframe: str = "", phase: str = "") -> dict:
    message = str(exc)
    execution_gate = evaluate_execution_result_gate(
        success=False,
        preflight_rejected=False,
        code="exception",
        reason=message,
        message=message,
    )
    meta = {"strategy": "B/v16", "timeframe": timeframe}
    if phase:
        meta["phase"] = phase
    return strategy_gate_case(
        name=name,
        gate="execution_result",
        inputs={
            "success": False,
            "preflight_rejected": False,
            "code": "exception",
            "reason": message,
            "message": message,
        },
        decision=execution_gate,
        meta=meta,
    )

def log_signal(signal: dict):
    log_jsonl(SIGNAL_LOG, signal)
    write_event_store({**_decision_from_signal(signal), "raw_signal": signal}, "B/v16/signals")
    log_decision(_decision_from_signal(signal), persist_event_store=False)

def log_system(state: dict):
    log_jsonl(SYSTEM_LOG, state)
    write_event_store({"strategy": "B/v16", "event": "SYSTEM", **state}, "B/v16/system")

def log_trade(trade: dict):
    log_jsonl(TRADES_LOG, trade)
    write_event_store({"strategy": "B/v16", "event": "TRADE", "category": "trade", **trade}, "B/v16/trades")

def dynamic_min_vol_ratio(symbol: str, atr_pct: float) -> float:
    if symbol in MAJOR_SYMBOLS:
        return MIN_VOL_RATIO_MAJOR
    if atr_pct >= 0.04:
        return MIN_VOL_RATIO_HIGH_VOL
    return MIN_VOL_RATIO

# ═══ 全局限流 ═══
_api_request_times = []  # v16优化(2026-05-14): 请求频率追踪
_last_ban_until = 0.0   # IP封禁时间戳
_cvd_cache = {}

def _safe_sleep(seconds):
    """安全的sleep，不阻塞主线程"""
    start = time.time()
    while time.time() - start < seconds:
        time.sleep(min(0.1, seconds - (time.time() - start)))

def fetch_json(url: str, timeout: int = 10) -> dict:
    """拉取JSON，内置限流和ban退避"""
    global _last_ban_until
    import urllib.request, urllib.error
    if api_queue_client_enabled():
        queue_timeout = max(timeout + 5, int(float(os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", "180"))))
        data = queued_api_request(scope="public", label="B/v16", method="GET", path=url, url=url, timeout_sec=queue_timeout)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("B/v16", url)
    now = time.time()
    # IP ban退避 - 完全暂停直到解封
    if _last_ban_until > now:
        wait = _last_ban_until - now
        logger.warning(f"  ⏸️ IP仍被ban，等待{wait:.0f}秒后重试")
        _safe_sleep(wait)
        _last_ban_until = 0
    # 限流: 最多200次/分钟
    _api_request_times.append(now)
    while _api_request_times and _api_request_times[0] < now - 60:
        _api_request_times.pop(0)
    if len(_api_request_times) > 200:
        _safe_sleep(0.5)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code in {418, 429}:
            record_public_response("B/v16", url, e.code, body)
        if e.code == 418:
            if "banned until" in body:
                import re
                m = re.search(r"banned until (\d+)", body)
                if m:
                    _last_ban_until = int(m.group(1)) / 1000
                    wait = _last_ban_until - time.time()
                    logger.warning(f"  ⏸️ IP被ban，等待{wait:.0f}秒")
                    if wait > 0:
                        _safe_sleep(wait)
                        _last_ban_until = 0
            raise
        raise

# ═══ 持仓类 ═══
@dataclass
class SimPosition:
    symbol: str; side: str; entry_price: float; size: float; leverage: int
    atr: float; entry_time: datetime; score: float; tf: str
    stop_loss: float; take_profit: float
    trailing_sl: float = 0.0; highest: float = 0.0; lowest: float = 0.0
    trailing_active: bool = False; cost: float = TRADE_SIZE
    order_id: str = ""; exchange_qty: float = 0.0
    entry_reason: str = ""
    confirm_reason: str = ""
    cvd: float = 0.0
    ofi: float = 0.0
    sl_mult: float = SL_MULT
    current_price: float = 0.0
    recovery_close_reason: str = ""
    recovery_close_attempts: int = 0

    def calc_pnl(self, ep):
        d = (ep - self.entry_price) / self.entry_price if self.side == "long" else (self.entry_price - ep) / self.entry_price
        pnl_usd = d * self.cost * self.leverage
        fee = self.cost * 0.0004
        return pnl_usd - fee, pnl_usd, fee

    def check_exit(self, high, low, close):
        if high > self.highest: self.highest = high
        if low < self.lowest: self.lowest = low
        if self.side == "long":
            loss = max(0.0, (self.entry_price - close) / self.entry_price * 100 * self.leverage)
        else:
            loss = max(0.0, (close - self.entry_price) / self.entry_price * 100 * self.leverage)
        if loss >= MAX_LOSS_PCT: return f"硬底{MAX_LOSS_PCT:.0f}%", True
        if self.side == "long":
            best_pnl_usd = max(0.0, (self.highest - self.entry_price) / self.entry_price * self.cost * self.leverage)
            cur_pnl_usd = (close - self.entry_price) / self.entry_price * self.cost * self.leverage
        else:
            best_pnl_usd = max(0.0, (self.entry_price - self.lowest) / self.entry_price * self.cost * self.leverage)
            cur_pnl_usd = (self.entry_price - close) / self.entry_price * self.cost * self.leverage
        if best_pnl_usd >= PROFIT_PROTECT_MIN_USDT and cur_pnl_usd <= best_pnl_usd * (1 - PROFIT_PROTECT_RETRACE):
            return f"盈利回撤保护{PROFIT_PROTECT_RETRACE*100:.0f}%", True
        hit, reason = False, None
        if self.side == "long":
            if low <= self.stop_loss: hit, reason = True, "止损"
            elif close >= self.take_profit: hit, reason = True, "止盈"
            elif self.trailing_active:
                new_sl = self.highest - self.atr * TRAIL_PULLBACK
                if new_sl > self.trailing_sl: self.trailing_sl = new_sl
                if low <= self.trailing_sl: hit, reason = True, "浮动止损"
            elif close - self.entry_price > self.atr * TRAIL_ACTIVATE:
                self.trailing_active = True
                self.trailing_sl = self.highest - self.atr * TRAIL_PULLBACK
        else:
            if high >= self.stop_loss: hit, reason = True, "止损"
            elif close <= self.take_profit: hit, reason = True, "止盈"
            elif self.trailing_active:
                new_sl = self.lowest + self.atr * TRAIL_PULLBACK
                if new_sl < self.trailing_sl: self.trailing_sl = new_sl
                if high >= self.trailing_sl: hit, reason = True, "浮动止损"
            elif self.entry_price - close > self.atr * TRAIL_ACTIVATE:
                self.trailing_active = True
                self.trailing_sl = self.lowest + self.atr * TRAIL_PULLBACK
        return reason, hit

# ═══ 工具函数 ═══
def klines_to_df(rows):
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts","o","h","l","c","v","ct","qv"])
    df["ts"] = df["ts"].astype(np.int64)
    df["o"] = df["o"].astype(float); df["h"] = df["h"].astype(float)
    df["l"] = df["l"].astype(float); df["c"] = df["c"].astype(float)
    df["v"] = df["v"].astype(float)
    return df

def fetch_klines(symbol, bar="15m", limit=100):
    """获取K线数据，使用fetch_json统一限流"""
    cached = load_cached_klines(PROJECT_ROOT, symbol, bar, limit)
    if cached:
        return cached
    try:
        rows = fetch_okx_klines(symbol, bar, limit)
        if rows:
            save_cached_klines(PROJECT_ROOT, symbol, bar, limit, rows)
            return rows
    except Exception as e:
        logger.debug(f"fetch_okx_klines {symbol}: {e}")
    if not kline_network_enabled():
        logger.warning(f"fetch_klines {symbol}: staged cache-only mode, no cached {bar}/{limit} rows")
        return []
    url = kline_request_url(symbol, bar, limit)
    try:
        raw = fetch_json(url)
        if not raw: return []
        rows = []
        for k in raw:
            rows.append([str(k[0]), str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5]), str(k[6]), str(k[7])])
        save_cached_klines(PROJECT_ROOT, symbol, bar, limit, rows)
        return rows
    except Exception as e:
        logger.warning(f"fetch_klines {symbol}: {e}")
        return []

def fetch_ofi(symbol):
    """计算订单流不平衡 - 使用统一fetch_json"""
    try:
        ofi = fetch_okx_ofi(symbol)
        if ofi is not None:
            return float(ofi)
    except Exception as e:
        logger.debug(f"fetch_okx_ofi {symbol}: {e}")
    url = market_url(f"/fapi/v1/depth?symbol={symbol}&limit=20")
    try:
        raw = fetch_json(url)
        bids = raw.get("bids", []) or []
        asks = raw.get("asks", []) or []
        bid_q = sum(float(q) for _, q in bids[:10])
        ask_q = sum(float(q) for _, q in asks[:10])
        total = bid_q + ask_q
        return (bid_q - ask_q) / total if total > 0 else 0.0
    except Exception as e:
        logger.warning(f"fetch_ofi {symbol}: {e}")
        return 0.0

def fetch_funding_rate(symbol):
    """获取资金费率 - 使用统一fetch_json"""
    try:
        funding_rate = fetch_okx_funding_rate(symbol)
        if funding_rate is not None:
            return float(funding_rate)
    except Exception as e:
        logger.debug(f"fetch_okx_funding_rate {symbol}: {e}")
    url = market_url(f"/fapi/v1/fundingRate?symbol={symbol}&limit=1")
    try:
        data = fetch_json(url)
        return float(data[-1]["fundingRate"]) * 100 if data else 0.0
    except Exception as e:
        logger.warning(f"fetch_funding_rate {symbol}: {e}")
        return 0.0

def fetch_top_symbols(n=50):
    """获取Top N交易对 - 使用统一fetch_json"""
    cached = cached_top_symbols(PROJECT_ROOT / "runtime" / "market_data_cache.json", n)
    if cached:
        return cached
    if not market_data_network_enabled():
        logger.warning("fetch_top_symbols: staged cache-only mode, no cached market symbols")
        return []
    url = market_url("/fapi/v1/ticker/24hr")
    try:
        raw = fetch_json(url)
        pairs = [t for t in raw if t.get("symbol","").endswith("USDT")]
        pairs.sort(key=lambda x: float(x.get("quoteVolume",0) or 0), reverse=True)
        return [t["symbol"] for t in pairs[:n]]
    except Exception as e:
        logger.warning(f"fetch_top_symbols: {e}")
        return ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT"]


def fetch_available_symbols():
    """当前扫描市场可用的 USDT 合约池，用于过滤实盘哨兵名单。"""
    cached = cached_available_symbols(PROJECT_ROOT / "runtime" / "market_data_cache.json")
    if cached:
        return cached
    if not market_data_network_enabled():
        logger.warning("fetch_available_symbols: staged cache-only mode, no cached available symbols")
        return set()
    url = market_url("/fapi/v1/ticker/24hr")
    try:
        raw = fetch_json(url)
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
    event = {
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
    }
    log_jsonl(EVENTS_LOG, event)
    EVENT_STORE.write_sentinel_scan(event, source="B/v16/events")
    log_decision(_decision_from_event(event), persist_event_store=False)


def merge_sentinel_symbols(symbols, limit=30):
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
    return merge_symbols_with_context(list(symbols), SENTINEL_CONTEXT)


def fetch_live_agg_trades(symbol: str, limit: int = CVD_LIMIT):
    """获取实盘 aggTrades。腾讯云新加坡节点直连 fapi.binance.com，阿里云则自动降级为空结果。"""
    import urllib.request, urllib.error
    url = f"{LIVE_BASE_URL}/fapi/v1/aggTrades?symbol={symbol}&limit={limit}"
    try:
        if api_queue_client_enabled():
            queue_timeout = max(12, int(float(os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", "180"))))
            data = queued_api_request(scope="public", label="B/v16-live", method="GET", path=url, url=url, timeout_sec=queue_timeout)
            return data if isinstance(data, list) else []
        wait_before_public_request("B/v16-live", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else []
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code in {418, 429}:
            record_public_response("B/v16-live", url, e.code, body)
        logger.debug(f"fetch_live_agg_trades {symbol} failed: HTTP {e.code}: {body[:200]}")
        return []
    except Exception as e:
        logger.debug(f"fetch_live_agg_trades {symbol} failed: {e}")
        return []

def fetch_cvd(symbol: str):
    """实盘 CVD：用主动买/主动卖成交量差计算，缓存 60 秒。"""
    now = time.time()
    cached = _cvd_cache.get(symbol)
    if cached and now - cached[0] < CVD_CACHE_SECONDS:
        return cached[1]

    try:
        okx_cvd = fetch_okx_cvd(symbol, CVD_LIMIT)
        if okx_cvd is not None:
            _cvd_cache[symbol] = (now, float(okx_cvd))
            return float(okx_cvd)
    except Exception as e:
        logger.debug(f"fetch_okx_cvd {symbol} failed: {e}")

    trades = fetch_live_agg_trades(symbol, CVD_LIMIT)
    if not trades:
        _cvd_cache[symbol] = (now, 0.0)
        return 0.0

    buy_vol = 0.0
    sell_vol = 0.0
    for t in trades:
        qty = float(t.get("q", 0) or 0)
        if t.get("m"):
            sell_vol += qty
        else:
            buy_vol += qty
    total = buy_vol + sell_vol
    cvd = (buy_vol - sell_vol) / total if total > 0 else 0.0
    _cvd_cache[symbol] = (now, cvd)
    return cvd

# ═══ 信号分析 ═══
def analyze_symbol(symbol, bar="15m"):
    score_long = score_short = 0.0
    reasons_long = reasons_short = []

    rows = fetch_klines(symbol, bar, 200)
    if len(rows) < 60: return None
    df = klines_to_df(rows)
    closes = df["c"].values; highs = df["h"].values
    lows = df["l"].values; vols = df["v"].values
    price = closes[-1]
    ts_arr = df["ts"].values

    # ATR
    atr = float(calc_atr(highs, lows, closes).iloc[-1])
    atr_pct = atr / price if price > 0 else 0
    if atr_pct > ATR_PRICE_MAX: return None
    if symbol in LOSS_BLACKLIST: return None

    # RSI
    rsi = float(calc_rsi(closes).iloc[-1])
    rsi_prev = float(calc_rsi(closes)[:-1].iloc[-1])

    # EMA
    ema9 = float(pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1])

    # 成交量
    vol_ma = float(pd.Series(vols).rolling(20).mean().iloc[-1])
    vol_ratio = vols[-1] / vol_ma if vol_ma > 0 else 1.0
    min_vol_ratio = dynamic_min_vol_ratio(symbol, atr_pct)
    if vol_ratio < min_vol_ratio: return None

    # ═══ CVD / OFI ═══
    cvd = fetch_cvd(symbol)
    ofi = fetch_ofi(symbol)
    if cvd > CVD_STRONG and ofi > OFI_STRONG:
        score_long += SCORE_CVD_STRONG + SCORE_OFI_STRONG
        reasons_long.append(f"CVD+OFI强势买入{cvd:+.2f}/{ofi:+.2f}")
    elif cvd < -CVD_STRONG and ofi < -OFI_STRONG:
        score_short += SCORE_CVD_STRONG + SCORE_OFI_STRONG
        reasons_short.append(f"CVD+OFI强势卖出{cvd:+.2f}/{ofi:+.2f}")
    else:
        if cvd > CVD_WEAK:
            score_long += SCORE_CVD_WEAK
            reasons_long.append(f"CVD买入{cvd:+.2f}")
        elif cvd < -CVD_WEAK:
            score_short += SCORE_CVD_WEAK
            reasons_short.append(f"CVD卖出{cvd:+.2f}")

        if ofi > OFI_WEAK:
            score_long += SCORE_OFI_WEAK
            reasons_long.append(f"OFI买入{ofi:+.2f}")
        elif ofi < -OFI_WEAK:
            score_short += SCORE_OFI_WEAK
            reasons_short.append(f"OFI卖出{ofi:+.2f}")

    if (cvd > CVD_WEAK and ofi < -OFI_WEAK) or (cvd < -CVD_WEAK and ofi > OFI_WEAK):
        score_long = max(0.0, score_long - 5)
        score_short = max(0.0, score_short - 5)
        reasons_long.append("CVD/OFI背离")
        reasons_short.append("CVD/OFI背离")

    # ═══ RSI背离检测 ═══
    rsi_trend = rsi - float(calc_rsi(closes)[:-5].iloc[-1])
    price_trend = closes[-1] - closes[-5]
    # 底背离：价格新低但RSI没有新低
    if price_trend < 0 and rsi_trend > 2:
        score_long += SCORE_RSI_DIVERGENCE
        reasons_long.append("RSI底背离")
    elif price_trend > 0 and rsi_trend < -2:
        score_short += SCORE_RSI_DIVERGENCE
        reasons_short.append("RSI顶背离")
    # RSI超卖超买
    if rsi < 30:
        score_long += SCORE_RSI_EXTREME
        reasons_long.append(f"RSI超卖{rsi:.0f}")
    elif rsi > 70:
        score_short += SCORE_RSI_EXTREME
        reasons_short.append(f"RSI超买{rsi:.0f}")

    # ═══ 资金费率确认 ═══
    fr = fetch_funding_rate(symbol)
    if fr > 0.08:
        score_short += SCORE_FUNDING
        reasons_short.append(f"资金费率{fr:+.3f}%")
    elif fr < -0.08:
        score_long += SCORE_FUNDING
        reasons_long.append(f"资金费率{fr:+.3f}%")

    # ═══ EMA趋势确认 ═══
    if ema9 > ema21:
        score_long += SCORE_EMA
        reasons_long.append("EMA多头")
    elif ema9 < ema21:
        score_short += SCORE_EMA
        reasons_short.append("EMA空头")

    net_score = score_long - score_short

    sl_mult = sl_mult_for_atr_pct(atr_pct)
    sl_long = round(price - atr * sl_mult, 6)
    sl_short = round(price + atr * sl_mult, 6)
    tp_long = round(price + atr * TP_MULT, 6)
    tp_short = round(price - atr * TP_MULT, 6)

    return {
        "symbol": symbol, "price": price, "atr": round(atr, 6),
        "timeframe": bar,
        "net_score": round(net_score, 1),
        "score_long": round(score_long, 1), "score_short": round(score_short, 1),
        "reasons_long": reasons_long, "reasons_short": reasons_short,
        "can_trade": abs(net_score) >= SCORE_MIN,
        "trade_side": "long" if net_score > 0 else "short",
        "sl_long": sl_long, "sl_short": sl_short,
        "tp_long": tp_long, "tp_short": tp_short,
        "sl_mult": sl_mult,
        "rsi": round(rsi, 1), "ofi": round(ofi, 3),
        "cvd": round(cvd, 3),
        "atr_pct": round(atr_pct, 4), "vol_ratio": round(vol_ratio, 2),
        "funding_rate": round(fr, 4),
    }

# ═══ 主扫描器 ═══
class ScannerV16:
    def __init__(self):
        self.client: ExchangeClient = get_client()
        self.strategy_engine = StrategyEngine("B/v16", analyze_symbol)
        self.execution = ExecutionEngine(self.client, "B/v16")
        self.risk_engine = RiskEngine(RiskLimits(
            max_total_positions=MAX_TOTAL_POSITIONS,
            max_positions_per_side=MAX_POS_PER_SIDE,
            min_available_balance_pct=MIN_AVAILABLE_BALANCE_PCT,
            min_available_balance_usdt=MIN_AVAILABLE_BALANCE_USDT,
        ))
        self.positions: dict = {tf: {} for tf in TIMEFRAMES}
        self.cooldowns: dict = {tf: {} for tf in TIMEFRAMES}
        self.sl_counts: dict = {}
        self.residual_close_attempted_symbols: set[str] = set()
        self.capital = 1000.0

    def has_position(self, tf, symbol):
        return symbol in self.positions.get(tf, {})

    def _total_local_positions(self):
        return sum(len(pos) for pos in self.positions.values())

    def _total_exchange_positions(self):
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
            if cached_state:
                return count_active_positions(cached_state.positions)
        except Exception as e:
            logger.debug(f"中心账户状态持仓数检查失败: {e}")
        return self._total_local_positions()

    def _exchange_side_count(self, side):
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
            if cached_state:
                return count_side_positions(cached_state.positions, side)
        except Exception as e:
            logger.debug(f"中心账户状态方向持仓数检查失败: {e}")
        return sum(1 for tf_pos in self.positions.values() for pos in tf_pos.values() if pos.side == side)

    def _exchange_symbol_position(self, symbol):
        """Return an existing exchange position for symbol, if any."""
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
            if cached_state:
                return find_symbol_position(cached_state.positions, symbol)
        except Exception as e:
            logger.debug(f"中心账户状态同币种持仓检查失败: {symbol} {e}")
        return {}

    def _latest_symbol_position_event(self, symbol: str) -> dict:
        try:
            with sqlite3.connect(EVENT_STORE.path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    select event_type, reason, payload_json
                    from events
                    where strategy='B/v16'
                      and symbol=?
                      and event_type in ('OPEN','OPEN_FAILED','CLOSE','CLOSE_FAILED','FORCED_CLOSE','FORCED_CLOSE_FAILED')
                    order by id desc
                    limit 1
                    """,
                    (symbol,),
                ).fetchone()
            return dict(row) if row else {}
        except Exception as e:
            logger.debug(f"读取最新持仓事件失败: {symbol} {e}")
            return {}

    def _residual_exchange_close_reason(self, symbol: str) -> str:
        value = os.environ.get("B_V16_CLOSE_RESIDUAL_EXCHANGE_POSITIONS_ENABLED", "1").strip().lower()
        if value in {"0", "false", "no", "off"}:
            return ""
        latest = self._latest_symbol_position_event(symbol)
        event_type = str(latest.get("event_type") or "").upper()
        if event_type == "CLOSE":
            return "交易所残留仓清理"
        if event_type in {"CLOSE_FAILED", "FORCED_CLOSE_FAILED"}:
            return "交易所残留仓重试平仓"
        return ""

    def _sync_exchange_positions(self):
        """把交易所现有持仓同步进本地内存，避免重启/恢复遗漏导致硬止损失效。"""
        cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
        if not cached_state:
            logger.debug("中心账户状态不可用，跳过本轮交易所持仓同步")
            return
        raw_positions = cached_state.positions

        for p in raw_positions:
            try:
                amt = float(p.get("positionAmt", 0))
            except Exception:
                continue
            if abs(amt) <= 0.0001:
                continue
            symbol = p.get("symbol")
            side = infer_position_side(p)[0].lower()
            if any(symbol in tf_pos for tf_pos in self.positions.values()):
                continue
            entry_price = float(p.get("entryPrice", 0) or 0)
            mark_price = float(p.get("markPrice", 0) or 0)
            leverage = int(float(p.get("leverage", LEVERAGE) or LEVERAGE))
            tf = "1h"
            if symbol in self.cooldowns.get("15m", {}):
                tf = "15m"
            elif symbol in self.cooldowns.get("1h", {}):
                tf = "1h"
            recovery_close_reason = self._residual_exchange_close_reason(symbol)
            if recovery_close_reason and symbol in getattr(self, "residual_close_attempted_symbols", set()):
                continue
            pos = SimPosition(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                size=abs(amt),
                leverage=leverage,
                atr=max(1e-12, abs(entry_price - mark_price) / max(leverage, 1)),
                entry_time=datetime.now(CST),
                score=0.0,
                tf=tf,
                stop_loss=entry_price,
                take_profit=entry_price,
                trailing_sl=entry_price,
                highest=max(entry_price, mark_price),
                lowest=min(entry_price, mark_price),
                order_id=str(p.get("orderId", "")),
                exchange_qty=abs(amt),
                current_price=mark_price,
                recovery_close_reason=recovery_close_reason,
            )
            self.positions.setdefault(tf, {})[symbol] = pos

    def _account_balance_summary(self):
        cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
        if not cached_state:
            logger.debug("中心账户状态不可用，余额摘要返回 0，避免 signed REST")
            return 0.0, 0.0
        return usdt_balance_summary(cached_state.balance)

    def _can_open_new_position(self, risk_usdt, tf, sym, side, score, return_cases=False):
        cases = []
        try:
            cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
            state_gate = evaluate_account_state_available_gate(account_state_available=bool(cached_state))
            state_case = strategy_gate_case(
                name="b_v16_account_state_available",
                gate="account_state_available",
                inputs={"account_state_available": bool(cached_state)},
                decision=state_gate,
                meta={"strategy": "B/v16", "timeframe": tf},
            )
            cases.append(state_case)
            if not state_gate.allowed:
                logger.info(f"  跳过[{tf}]: {sym} 中心账户状态不可用，暂停新开仓以避免 signed REST 压力")
                log_event({
                    "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                    "symbol": sym, "side": side, "score": score, "timeframe": tf,
                    "skip_reason": "中心账户状态不可用，暂停新开仓以避免 signed REST 压力",
                    "risk_category": "account_state_unavailable",
                    "decision_stage": "risk_gate",
                    "filter_layer": "risk",
                    "strategy_gate_case": state_case,
                    **sentinel_fields(sym),
                })
                return (False, cases) if return_cases else False
            exchange_positions = cached_state.positions
            total_positions = max(self._total_local_positions(), count_active_positions(exchange_positions))
            side_count = count_side_positions(exchange_positions, side)
        except Exception as e:
            logger.debug(f"开仓风控快照读取失败: {e}")
            state_gate = evaluate_account_state_available_gate(account_state_available=False, read_error=True)
            state_case = strategy_gate_case(
                name="b_v16_account_state_read_failed",
                gate="account_state_available",
                inputs={"account_state_available": False, "read_error": True},
                decision=state_gate,
                meta={"strategy": "B/v16", "timeframe": tf, "error": str(e)[:160]},
            )
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                "symbol": sym, "side": side, "score": score, "timeframe": tf,
                "skip_reason": "中心账户状态读取失败，暂停新开仓以避免 signed REST 压力",
                "risk_category": "account_state_unavailable",
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                "strategy_gate_case": state_case,
                **sentinel_fields(sym),
            })
            return (False, [state_case]) if return_cases else False
        balance = cached_state.balance
        decision = self.risk_engine.check_entry(
            total_positions=total_positions,
            side_positions=side_count,
            balance=balance,
            risk_usdt=risk_usdt,
        )
        if not decision.allowed:
            risk_gate = evaluate_entry_risk_gate(
                total_positions=decision.total_positions,
                side_positions=decision.side_positions,
                total_balance=decision.total_balance,
                available_balance=decision.available_balance,
                risk_usdt=risk_usdt,
                max_total_positions=self.risk_engine.limits.max_total_positions,
                max_positions_per_side=self.risk_engine.limits.max_positions_per_side,
                min_available_balance_pct=self.risk_engine.limits.min_available_balance_pct,
                min_available_balance_usdt=self.risk_engine.limits.min_available_balance_usdt,
            )
            risk_case = strategy_gate_case(
                name="b_v16_entry_risk",
                gate="entry_risk",
                inputs={
                    "total_positions": decision.total_positions,
                    "side_positions": decision.side_positions,
                    "total_balance": decision.total_balance,
                    "available_balance": decision.available_balance,
                    "risk_usdt": risk_usdt,
                    "max_total_positions": self.risk_engine.limits.max_total_positions,
                    "max_positions_per_side": self.risk_engine.limits.max_positions_per_side,
                    "min_available_balance_pct": self.risk_engine.limits.min_available_balance_pct,
                    "min_available_balance_usdt": self.risk_engine.limits.min_available_balance_usdt,
                },
                decision=risk_gate,
                meta={"strategy": "B/v16", "timeframe": tf},
            )
            logger.info(f"  跳过[{tf}]: {sym} {decision.reason}")
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                "symbol": sym, "side": side, "score": score, "timeframe": tf,
                "skip_reason": decision.reason,
                "risk_category": decision.category,
                "total_positions": decision.total_positions,
                "side_positions": decision.side_positions,
                "available": round(decision.available_balance, 4),
                "reserve": round(decision.reserve_required, 4),
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                "strategy_gate_case": risk_case,
                **sentinel_fields(sym),
            })
            return (False, [*cases, risk_case]) if return_cases else False
        risk_gate = evaluate_entry_risk_gate(
            total_positions=decision.total_positions,
            side_positions=decision.side_positions,
            total_balance=decision.total_balance,
            available_balance=decision.available_balance,
            risk_usdt=risk_usdt,
            max_total_positions=self.risk_engine.limits.max_total_positions,
            max_positions_per_side=self.risk_engine.limits.max_positions_per_side,
            min_available_balance_pct=self.risk_engine.limits.min_available_balance_pct,
            min_available_balance_usdt=self.risk_engine.limits.min_available_balance_usdt,
        )
        risk_case = strategy_gate_case(
            name="b_v16_entry_risk",
            gate="entry_risk",
            inputs={
                "total_positions": decision.total_positions,
                "side_positions": decision.side_positions,
                "total_balance": decision.total_balance,
                "available_balance": decision.available_balance,
                "risk_usdt": risk_usdt,
                "max_total_positions": self.risk_engine.limits.max_total_positions,
                "max_positions_per_side": self.risk_engine.limits.max_positions_per_side,
                "min_available_balance_pct": self.risk_engine.limits.min_available_balance_pct,
                "min_available_balance_usdt": self.risk_engine.limits.min_available_balance_usdt,
            },
            decision=risk_gate,
            meta={"strategy": "B/v16", "timeframe": tf},
        )
        return (True, [*cases, risk_case]) if return_cases else True

    def _passes_entry_threshold(self, tf, side, score, sym=None, open_positions=None, confirm_reason=""):
        decision = evaluate_b_v16_entry_threshold(
            timeframe=tf,
            side=side,
            score=score,
            symbol=sym,
            open_positions=open_positions,
            confirm_reason=confirm_reason,
            score_thresholds=SCORE_THRESHOLDS,
            score_min=SCORE_MIN,
            short_entry_penalty=SHORT_ENTRY_PENALTY,
            major_symbols=MAJOR_SYMBOLS,
            low_position_threshold_discount=LOW_POSITION_THRESHOLD_DISCOUNT,
            no_confirm_threshold_penalty=NO_CONFIRM_THRESHOLD_PENALTY,
            weak_opposite_confirm_penalty=WEAK_OPPOSITE_CONFIRM_PENALTY,
            confirm_bonus=CONFIRM_BONUS,
            confirm_strong_bonus=CONFIRM_STRONG_BONUS,
        )
        return decision.allowed

    def _passes_small_live_stage_guard(self, sym, tf, sig, side, score):
        """Small-live approval for HYP-2026-05-22-B-v16-reverse_trade-stage-guard."""
        decision = evaluate_b_v16_small_live_stage_guard(
            enabled=STAGE_GUARD_SMALL_LIVE_ENABLED,
            signal=sig,
            side=side,
            score=score,
            min_score=STAGE_GUARD_MIN_SCORE,
            reverse_pass_score=STAGE_GUARD_REVERSE_PASS_SCORE,
        )
        return decision.allowed, decision.reason

    def _adjusted_score(self, sym, score):
        decision = evaluate_watchlist_score_adjustment(
            symbol=sym,
            score=score,
            watchlist_symbols=WATCHLIST_SYMBOLS,
            penalty=WATCHLIST_SCORE_PENALTY,
        )
        return decision.adjusted_score if decision.adjusted_score is not None else score

    def _passes_15m_confirmation(self, sym, side, raw_score, open_positions):
        sig = self.strategy_engine.analyze(sym, CONFIRM_TIMEFRAME)
        decision = evaluate_b_v16_confirmation_gate(
            side=side,
            raw_score=raw_score,
            confirm_signal=sig,
            open_positions=open_positions,
            max_active_new_positions=MAX_ACTIVE_NEW_POSITIONS,
            no_confirm_high_score_pass=NO_CONFIRM_HIGH_SCORE_PASS,
            confirm_opposite_reject_score=CONFIRM_OPPOSITE_REJECT_SCORE,
            opposite_high_score_pass=OPPOSITE_HIGH_SCORE_PASS,
            weak_confirm_pass_score=WEAK_CONFIRM_PASS_SCORE,
            confirm_min_score=CONFIRM_MIN_SCORE,
            confirm_bonus=CONFIRM_BONUS,
            confirm_strong_bonus=CONFIRM_STRONG_BONUS,
        )
        return decision.allowed, decision.reason

    def scan(self):
        top_limit = env_int("SCANNER_B_TOP_SYMBOLS", 50)
        sentinel_limit = env_int("SCANNER_B_SENTINEL_LIMIT", 30)
        symbols = merge_sentinel_symbols(fetch_top_symbols(top_limit), limit=sentinel_limit)
        logger.info(f"v16扫描 {len(symbols)} 币 × {len(ENTRY_TIMEFRAMES)} 入场周期...")
        open_positions = self._total_exchange_positions()
        scan_stats = {
            "loss_blacklist": 0, "has_position": 0, "symbol_sl_cooldown": 0,
            "cooldown": 0, "no_signal": 0, "score_low": 0,
            "active_limit": 0, "score_gt_max": 0, "confirm_fail": 0,
            "threshold_fail": 0, "stage_guard_fail": 0, "opened": 0,
            "small_live_stage_guard": STAGE_GUARD_SMALL_LIVE_ENABLED,
            "trade_size_usdt": TRADE_SIZE,
        }

        for tf in ENTRY_TIMEFRAMES:
            for sym in symbols:
                blacklist_gate = evaluate_symbol_blacklist_gate(symbol=sym, blacklisted_symbols=LOSS_BLACKLIST, reason="loss_blacklist")
                if not blacklist_gate.allowed:
                    scan_stats["loss_blacklist"] += 1
                    log_sentinel_scan(
                        sym, tf, "pre_filter_rejected", "loss_blacklist",
                        decision_stage="pre_filter",
                        strategy_gate_case=strategy_gate_case(
                            name="b_v16_symbol_blacklist",
                            gate="symbol_blacklist",
                            inputs={"symbol": sym, "blacklisted_symbols": LOSS_BLACKLIST, "reason": "loss_blacklist"},
                            decision=blacklist_gate,
                            meta={"strategy": "B/v16", "timeframe": tf},
                        ),
                    )
                    continue
                timeframe_position_gate = evaluate_timeframe_position_gate(has_timeframe_position=self.has_position(tf, sym))
                if not timeframe_position_gate.allowed:
                    scan_stats["has_position"] += 1
                    continue
                symbol_sl_gate = evaluate_symbol_stop_loss_gate(
                    stop_loss_count=self.sl_counts.get(sym, 0),
                    max_stop_loss_per_symbol=MAX_SL_PER_SYMBOL,
                )
                if not symbol_sl_gate.allowed:
                    scan_stats["symbol_sl_cooldown"] += 1
                    continue
                cd = self.cooldowns.get(tf, {}).get(sym, 0)
                cooldown_gate = evaluate_symbol_scan_cooldown_gate(cooldown_ticks=cd)
                if not cooldown_gate.allowed:
                    self.cooldowns[tf][sym] = cd - 1
                    scan_stats["cooldown"] += 1
                    continue

                sig = self.strategy_engine.analyze(sym, tf)
                if not sig:
                    scan_stats["no_signal"] += 1
                    log_sentinel_scan(sym, tf, "no_signal", "策略分析无信号")
                    continue
                if not sig["can_trade"]:
                    scan_stats["score_low"] += 1
                    log_sentinel_scan(
                        sym, tf, "strategy_rejected", "策略层 can_trade=False",
                        side=sig.get("trade_side", ""),
                        score=abs(float(sig.get("net_score") or 0)),
                    )
                    continue
                active_gate = evaluate_active_position_limit_gate(
                    open_positions=open_positions,
                    max_active_positions=MAX_ACTIVE_NEW_POSITIONS,
                )
                if not active_gate.allowed:
                    scan_stats["active_limit"] += 1
                    log_event({
                        "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                        "symbol": sym, "side": sig["trade_side"], "score": abs(sig["net_score"]),
                        "timeframe": tf, "skip_reason": active_gate.reason,
                        "decision_stage": "risk_gate",
                        "filter_layer": "risk",
                        "strategy_gate_case": strategy_gate_case(
                            name="b_v16_active_position_limit",
                            gate="active_position_limit",
                            inputs={
                                "open_positions": open_positions,
                                "max_active_positions": MAX_ACTIVE_NEW_POSITIONS,
                            },
                            decision=active_gate,
                            meta={"strategy": "B/v16", "timeframe": tf},
                        ),
                        **sentinel_fields(sym),
                    })
                    continue
                log_signal({
                    "ts": str(datetime.now(CST)), "strategy": "v16_orderflow",
                    "timeframe": tf, "symbol": sym, "net_score": sig["net_score"],
                    "trade_side": sig["trade_side"], "reasons": sig.get(f"reasons_{sig['trade_side']}", []),
                    "price": sig.get("price"), "atr": sig.get("atr"),
                    "cvd": sig.get("cvd"), "ofi": sig.get("ofi"),
                    "rsi": sig.get("rsi"), "funding_rate": sig.get("funding_rate"),
                    **sentinel_fields(sym),
                })

                side = sig["trade_side"]
                raw_score = abs(sig["net_score"])
                score = self._adjusted_score(sym, raw_score)
                score_max_gate = evaluate_score_max_gate(score=score, score_max=SCORE_MAX)
                if not score_max_gate.allowed:
                    scan_stats["score_gt_max"] += 1
                    log_sentinel_scan(
                        sym, tf, "score_rejected", score_max_gate.reason,
                        side=side, score=score, raw_score=raw_score,
                        decision_stage="score_gate",
                        strategy_gate_case=strategy_gate_case(
                            name="b_v16_score_max",
                            gate="score_max",
                            inputs={"score": score, "score_max": SCORE_MAX},
                            decision=score_max_gate,
                            meta={"strategy": "B/v16", "timeframe": tf},
                        ),
                    )
                    continue
                stage_gate = evaluate_b_v16_small_live_stage_guard(
                    enabled=STAGE_GUARD_SMALL_LIVE_ENABLED,
                    signal=sig,
                    side=side,
                    score=score,
                    min_score=STAGE_GUARD_MIN_SCORE,
                    reverse_pass_score=STAGE_GUARD_REVERSE_PASS_SCORE,
                )
                if not stage_gate.allowed:
                    scan_stats["stage_guard_fail"] += 1
                    log_event({
                        "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                        "symbol": sym, "side": side, "score": score,
                        "raw_score": raw_score, "timeframe": tf,
                        "skip_reason": stage_gate.reason,
                        "decision_stage": "small_live_stage_guard",
                        "filter_layer": "strategy",
                        "approved_candidate_id": "HYP-2026-05-22-B-v16-reverse_trade-stage-guard",
                        "strategy_gate_case": strategy_gate_case(
                            name="b_v16_small_live_stage_guard",
                            gate="b_v16_small_live_stage_guard",
                            inputs={
                                "enabled": STAGE_GUARD_SMALL_LIVE_ENABLED,
                                "signal": sig,
                                "side": side,
                                "score": score,
                                "min_score": STAGE_GUARD_MIN_SCORE,
                                "reverse_pass_score": STAGE_GUARD_REVERSE_PASS_SCORE,
                            },
                            decision=stage_gate,
                            meta={"strategy": "B/v16", "timeframe": tf},
                        ),
                        **sentinel_fields(sym),
                    })
                    continue
                confirm_signal = self.strategy_engine.analyze(sym, CONFIRM_TIMEFRAME)
                confirmation_gate = evaluate_b_v16_confirmation_gate(
                    side=side,
                    raw_score=raw_score,
                    confirm_signal=confirm_signal,
                    open_positions=open_positions,
                    max_active_new_positions=MAX_ACTIVE_NEW_POSITIONS,
                    no_confirm_high_score_pass=NO_CONFIRM_HIGH_SCORE_PASS,
                    confirm_opposite_reject_score=CONFIRM_OPPOSITE_REJECT_SCORE,
                    opposite_high_score_pass=OPPOSITE_HIGH_SCORE_PASS,
                    weak_confirm_pass_score=WEAK_CONFIRM_PASS_SCORE,
                    confirm_min_score=CONFIRM_MIN_SCORE,
                    confirm_bonus=CONFIRM_BONUS,
                    confirm_strong_bonus=CONFIRM_STRONG_BONUS,
                )
                confirm_reason = confirmation_gate.reason
                confirmation_gate_case = strategy_gate_case(
                    name="b_v16_confirmation",
                    gate="b_v16_confirmation",
                    inputs={
                        "side": side,
                        "raw_score": raw_score,
                        "confirm_signal": confirm_signal,
                        "open_positions": open_positions,
                        "max_active_new_positions": MAX_ACTIVE_NEW_POSITIONS,
                        "no_confirm_high_score_pass": NO_CONFIRM_HIGH_SCORE_PASS,
                        "confirm_opposite_reject_score": CONFIRM_OPPOSITE_REJECT_SCORE,
                        "opposite_high_score_pass": OPPOSITE_HIGH_SCORE_PASS,
                        "weak_confirm_pass_score": WEAK_CONFIRM_PASS_SCORE,
                        "confirm_min_score": CONFIRM_MIN_SCORE,
                        "confirm_bonus": CONFIRM_BONUS,
                        "confirm_strong_bonus": CONFIRM_STRONG_BONUS,
                    },
                    decision=confirmation_gate,
                    meta={"strategy": "B/v16", "timeframe": tf, "chain_step": "confirmation"},
                )
                if not confirmation_gate.allowed:
                    scan_stats["confirm_fail"] += 1
                    log_event({
                        "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                        "symbol": sym, "side": side, "score": score,
                        "raw_score": raw_score, "timeframe": tf,
                        "skip_reason": confirm_reason,
                        "decision_stage": "confirmation",
                        "filter_layer": "confirmation",
                        "strategy_gate_case": confirmation_gate_case,
                        **sentinel_fields(sym),
                    })
                    continue
                threshold_gate = evaluate_b_v16_entry_threshold(
                    timeframe=tf,
                    side=side,
                    score=score,
                    symbol=sym,
                    open_positions=open_positions,
                    confirm_reason=confirm_reason,
                    score_thresholds=SCORE_THRESHOLDS,
                    score_min=SCORE_MIN,
                    short_entry_penalty=SHORT_ENTRY_PENALTY,
                    major_symbols=MAJOR_SYMBOLS,
                    low_position_threshold_discount=LOW_POSITION_THRESHOLD_DISCOUNT,
                    no_confirm_threshold_penalty=NO_CONFIRM_THRESHOLD_PENALTY,
                    weak_opposite_confirm_penalty=WEAK_OPPOSITE_CONFIRM_PENALTY,
                    confirm_bonus=CONFIRM_BONUS,
                    confirm_strong_bonus=CONFIRM_STRONG_BONUS,
                )
                threshold_gate_case = strategy_gate_case(
                    name="b_v16_entry_threshold",
                    gate="b_v16_entry_threshold",
                    inputs={
                        "timeframe": tf,
                        "side": side,
                        "score": score,
                        "symbol": sym,
                        "open_positions": open_positions,
                        "confirm_reason": confirm_reason,
                        "score_thresholds": SCORE_THRESHOLDS,
                        "score_min": SCORE_MIN,
                        "short_entry_penalty": SHORT_ENTRY_PENALTY,
                        "major_symbols": MAJOR_SYMBOLS,
                        "low_position_threshold_discount": LOW_POSITION_THRESHOLD_DISCOUNT,
                        "no_confirm_threshold_penalty": NO_CONFIRM_THRESHOLD_PENALTY,
                        "weak_opposite_confirm_penalty": WEAK_OPPOSITE_CONFIRM_PENALTY,
                        "confirm_bonus": CONFIRM_BONUS,
                        "confirm_strong_bonus": CONFIRM_STRONG_BONUS,
                    },
                    decision=threshold_gate,
                    meta={"strategy": "B/v16", "timeframe": tf, "chain_step": "entry_threshold"},
                )
                if not threshold_gate.allowed:
                    scan_stats["threshold_fail"] += 1
                    log_event({
                        "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                        "symbol": sym, "side": side, "score": score,
                        "raw_score": raw_score, "timeframe": tf,
                        "skip_reason": f"阈值未达:{confirm_reason}",
                        "decision_stage": "threshold",
                        "filter_layer": "strategy",
                        "strategy_gate_cases": [confirmation_gate_case, threshold_gate_case],
                        **sentinel_fields(sym),
                    })
                    continue

                open_chain_cases = [confirmation_gate_case, threshold_gate_case]
                if self._open_position(tf, sym, sig, side, score, confirm_reason, open_chain_cases=open_chain_cases):
                    open_positions += 1
                    scan_stats["opened"] += 1

        log_system({"ts": str(datetime.now(CST)), "event": "SCAN_STATS", **scan_stats})

    def _open_position(self, tf, sym, sig, side, score, confirm_reason="", open_chain_cases=None):
        open_chain_cases = list(open_chain_cases or [])
        runtime_chain_cases = []
        price = sig["price"]
        atr = sig["atr"]
        sl = sig["sl_long"] if side == "long" else sig["sl_short"]
        tp = sig["tp_long"] if side == "long" else sig["tp_short"]

        existing_exchange_pos = self._exchange_symbol_position(sym)
        local_holding = any(sym in tf_pos for tf_pos in self.positions.values())
        position_gate = evaluate_no_same_symbol_position_gate(
            has_exchange_position=bool(existing_exchange_pos),
            has_local_position=local_holding,
        )
        if not position_gate.allowed:
            existing_qty = float(existing_exchange_pos.get("positionAmt") or 0) if existing_exchange_pos else 0.0
            existing_side = infer_position_side(existing_exchange_pos)[0] if existing_exchange_pos else ""
            existing_entry = float(existing_exchange_pos.get("entryPrice") or 0) if existing_exchange_pos else 0.0
            logger.info(
                f"  跳过[{tf}]: {sym} 交易所/本地已有持仓，禁止同币种重复开仓 "
                f"side={existing_side or 'local'} qty={existing_qty:g}"
            )
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                "symbol": sym, "side": side, "score": score, "timeframe": tf,
                "skip_reason": "同币种已有持仓，禁止重复开仓以避免聚合仓位偏离策略风险预算",
                "risk_category": "position_duplicate",
                "decision_stage": "risk_gate",
                "filter_layer": "risk",
                "existing_exchange_qty": existing_qty,
                "existing_exchange_side": existing_side,
                "existing_entry_price": existing_entry,
                "trade_size_usdt": TRADE_SIZE,
                "strategy_gate_case": strategy_gate_case(
                    name="b_v16_no_same_symbol_position",
                    gate="no_same_symbol_position",
                    inputs={
                        "has_exchange_position": bool(existing_exchange_pos),
                        "has_local_position": local_holding,
                    },
                    decision=position_gate,
                    meta={"strategy": "B/v16", "timeframe": tf},
                ),
                **sentinel_fields(sym),
            })
            return False
        position_case = strategy_gate_case(
            name="b_v16_no_same_symbol_position",
            gate="no_same_symbol_position",
            inputs={
                "has_exchange_position": bool(existing_exchange_pos),
                "has_local_position": local_holding,
            },
            decision=position_gate,
            meta={"strategy": "B/v16", "timeframe": tf, "chain_step": "position_duplicate"},
        )
        runtime_chain_cases.append(position_case)

        risk_allowed, risk_cases = self._can_open_new_position(TRADE_SIZE, tf, sym, side, score, return_cases=True)
        if not risk_allowed:
            return False
        runtime_chain_cases.extend(risk_cases)

        size_qty = self.execution.calc_quantity(sym, price, TRADE_SIZE, LEVERAGE)
        quantity_gate = evaluate_positive_quantity_gate(quantity=size_qty)
        if not quantity_gate.allowed:
            logger.warning(f"  跳过: {sym} 数量太小 (price={price}, minQty不满足)")
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED",
                "symbol": sym, "side": side, "timeframe": tf,
                "skip_reason": "qty<=0", "decision_stage": "execution",
                "filter_layer": "execution",
                "strategy_gate_case": strategy_gate_case(
                    name="b_v16_positive_quantity",
                    gate="positive_quantity",
                    inputs={"quantity": size_qty},
                    decision=quantity_gate,
                    meta={"strategy": "B/v16", "timeframe": tf},
                ),
                **sentinel_fields(sym),
            })
            return False
        quantity_case = strategy_gate_case(
            name="b_v16_positive_quantity",
            gate="positive_quantity",
            inputs={"quantity": size_qty},
            decision=quantity_gate,
            meta={"strategy": "B/v16", "timeframe": tf, "chain_step": "positive_quantity"},
        )
        runtime_chain_cases.append(quantity_case)
        logger.info(f"  开仓[{tf}]: {sym} {side} @{price:.4f} 分数={score} 原因={'+'.join(sig[f'reasons_{side}'][:2])}")

        try:
            exec_result = self.execution.open_position(OpenRequest(
                symbol=sym,
                side=side,
                price=price,
                risk_usdt=TRADE_SIZE,
                leverage=LEVERAGE,
                take_profit=tp,
                stop_loss=sl,
                quantity=size_qty,
                confirm_position=True,
            ))
            r = exec_result.raw if isinstance(exec_result.raw, dict) else {}

            if not exec_result.success:
                execution_gate = evaluate_execution_result_gate(
                    success=exec_result.success,
                    preflight_rejected=exec_result.preflight_rejected,
                    code=exec_result.code,
                    reason=exec_result.reason,
                    message=exec_result.message,
                )
                if execution_gate.gate == "execution_preflight":
                    detail = exec_result.preflight_detail
                    logger.info(f"  执行预检跳过: {sym} {exec_result.reason}")
                    log_event({
                        "time": str(datetime.now(CST)), "event": "OPEN_SKIPPED", "symbol": sym,
                        "side": side, "price": price, "score": score, "timeframe": tf,
                        "skip_reason": exec_result.reason,
                        "reason": exec_result.reason,
                        "code": exec_result.code,
                        "msg": exec_result.message,
                        "preflight": detail,
                        "risk_category": "execution_preflight",
                        "decision_stage": "execution_preflight",
                        "filter_layer": "execution",
                        "trade_size_usdt": TRADE_SIZE,
                        "small_live_stage_guard": STAGE_GUARD_SMALL_LIVE_ENABLED,
                        "strategy_gate_case": strategy_gate_case(
                            name="b_v16_execution_result",
                            gate="execution_result",
                            inputs={
                                "success": exec_result.success,
                                "preflight_rejected": exec_result.preflight_rejected,
                                "code": exec_result.code,
                                "reason": exec_result.reason,
                                "message": exec_result.message,
                            },
                            decision=execution_gate,
                            meta={"strategy": "B/v16", "timeframe": tf},
                        ),
                        **sentinel_fields(sym),
                    })
                    self.cooldowns[tf][sym] = max(self.cooldowns[tf].get(sym, 0), 5)
                    return False
                logger.warning(f"  开仓失败: {exec_result.reason}")
                log_event({
                    "time": str(datetime.now(CST)), "event": "OPEN_FAILED", "symbol": sym,
                    "side": side, "price": price, "score": score, "timeframe": tf,
                    "reason": exec_result.reason, "code": exec_result.code, "msg": exec_result.message,
                    "decision_stage": "execution",
                    "filter_layer": "execution",
                    "trade_size_usdt": TRADE_SIZE,
                    "small_live_stage_guard": STAGE_GUARD_SMALL_LIVE_ENABLED,
                    "strategy_gate_case": strategy_gate_case(
                        name="b_v16_execution_result",
                        gate="execution_result",
                        inputs={
                            "success": exec_result.success,
                            "preflight_rejected": exec_result.preflight_rejected,
                            "code": exec_result.code,
                            "reason": exec_result.reason,
                            "message": exec_result.message,
                        },
                        decision=execution_gate,
                        meta={"strategy": "B/v16", "timeframe": tf},
                    ),
                    **sentinel_fields(sym),
                })
                self.cooldowns[tf][sym] = max(self.cooldowns[tf].get(sym, 0), 20)
                return False
            exec_qty = exec_result.quantity or size_qty
            pos = SimPosition(
                symbol=sym, side=side, entry_price=price, size=exec_qty, leverage=LEVERAGE,
                atr=atr, entry_time=datetime.now(CST), score=score, tf=tf,
                stop_loss=sl, take_profit=tp, trailing_sl=sl,
                highest=price, lowest=price,
                order_id=str(r.get("orderId","")) if isinstance(r, dict) else "",
                exchange_qty=exec_qty,
                entry_reason="+".join(sig.get(f"reasons_{side}", [])[:6]),
                confirm_reason=confirm_reason,
                cvd=float(sig.get("cvd") or 0),
                ofi=float(sig.get("ofi") or 0),
                sl_mult=float(sig.get("sl_mult") or SL_MULT),
            )
            self.positions[tf][sym] = pos
            logger.info(f"  ✅ 开仓成功: {sym} qty={exec_qty}")
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN", "symbol": sym,
                "side": side, "price": price, "qty": exec_qty, "leverage": LEVERAGE,
                "sl": sl, "tp": tp, "score": score, "timeframe": tf,
                "sl_mult": sig.get("sl_mult"),
                "entry_reason": "+".join(sig.get(f"reasons_{side}", [])[:6]),
                "confirm_reason": confirm_reason,
                "reasons": sig.get(f"reasons_{side}", []),
                "decision_stage": "open",
                "cvd": sig.get("cvd"),
                "ofi": sig.get("ofi"),
                "rsi": sig.get("rsi"),
                "funding_rate": sig.get("funding_rate"),
                "order_id": str(r.get("orderId","")) if isinstance(r, dict) else "",
                "trade_size_usdt": TRADE_SIZE,
                "small_live_stage_guard": STAGE_GUARD_SMALL_LIVE_ENABLED,
                "approved_candidate_id": (
                    "HYP-2026-05-22-B-v16-reverse_trade-stage-guard"
                    if STAGE_GUARD_SMALL_LIVE_ENABLED
                    else ",".join(APPROVED_FULL_LIVE_CANDIDATE_IDS)
                ),
                "approved_candidate_ids": APPROVED_FULL_LIVE_CANDIDATE_IDS,
                "strategy_gate_cases": [
                    *open_chain_cases,
                    *runtime_chain_cases,
                    _execution_result_case(
                        "b_v16_open_execution_result",
                        exec_result,
                        timeframe=tf,
                        phase="open",
                    ),
                ],
                **sentinel_fields(sym),
            })
            return True
        except Exception as e:
            logger.error(f"  开仓异常: {e}")
            execution_gate = evaluate_execution_result_gate(
                success=False,
                preflight_rejected=False,
                code="exception",
                reason=f"开仓异常: {str(e)[:120]}",
                message=str(e),
            )
            log_event({
                "time": str(datetime.now(CST)), "event": "OPEN_FAILED", "symbol": sym,
                "side": side, "price": price, "score": score, "timeframe": tf,
                "reason": f"开仓异常: {str(e)[:120]}",
                "decision_stage": "execution",
                "filter_layer": "execution",
                "trade_size_usdt": TRADE_SIZE,
                "small_live_stage_guard": STAGE_GUARD_SMALL_LIVE_ENABLED,
                "strategy_gate_case": strategy_gate_case(
                    name="b_v16_execution_exception",
                    gate="execution_result",
                    inputs={
                        "success": False,
                        "preflight_rejected": False,
                        "code": "exception",
                        "reason": f"开仓异常: {str(e)[:120]}",
                        "message": str(e),
                    },
                    decision=execution_gate,
                    meta={"strategy": "B/v16", "timeframe": tf},
                ),
                **sentinel_fields(sym),
            })
            self.cooldowns[tf][sym] = max(self.cooldowns[tf].get(sym, 0), 20)
            return False

    def check_exits(self):
        now = datetime.now(CST)
        self._sync_exchange_positions()
        for tf in TIMEFRAMES:
            for sym, pos in list(self.positions[tf].items()):
                if pos.recovery_close_reason:
                    if pos.recovery_close_attempts <= 0:
                        pos.recovery_close_attempts += 1
                        self.residual_close_attempted_symbols.add(sym)
                        exit_price = pos.current_price or pos.entry_price
                        self._close_position(tf, sym, pos, exit_price, pos.recovery_close_reason)
                    continue
                rows = fetch_klines(sym, tf, 2)
                if not rows: continue
                df = klines_to_df(rows)
                high, low, close = df["h"].iloc[-1], df["l"].iloc[-1], df["c"].iloc[-1]
                reason, hit = pos.check_exit(high, low, close)

                if not hit:
                    # 浮动止损激活检测
                    if not pos.trailing_active:
                        profit = (close - pos.entry_price) / pos.entry_price * 100 * pos.leverage if pos.side == "long" else (pos.entry_price - close) / pos.entry_price * 100 * pos.leverage
                        if profit > 0:
                            pos.trailing_active = True
                    continue

                self._close_position(tf, sym, pos, close, reason)

    def _close_position(self, tf, sym, pos, exit_price, reason):
        try:
            close_exec = self.execution.close_position(CloseRequest(
                symbol=sym,
                side=pos.side,
                quantity=pos.exchange_qty,
                cancel_open_orders=True,
            ))
            if not close_exec.success:
                logger.error(f"  平仓失败: {close_exec.reason}")
                log_event({
                    "time": str(datetime.now(CST)), "event": "CLOSE_FAILED", "symbol": sym,
                    "side": pos.side, "exit_price": exit_price, "reason": reason,
                    "failure_reason": close_exec.reason, "exchange_status": close_exec.status,
                    "exchange_close_success": False, "timeframe": tf,
                    "decision_stage": "execution",
                    "filter_layer": "execution",
                    "strategy_gate_case": _execution_result_case(
                        "b_v16_close_execution_result",
                        close_exec,
                        timeframe=tf,
                        phase="normal_close",
                    ),
                })
                return
            logger.info(f"  平仓[{tf}]: {sym} {pos.side} {reason} @{exit_price:.4f}")
        except Exception as e:
            logger.error(f"  平仓失败: {e}")
            log_event({
                "time": str(datetime.now(CST)), "event": "CLOSE_FAILED", "symbol": sym,
                "side": pos.side, "exit_price": exit_price, "reason": reason,
                "failure_reason": str(e), "exchange_close_success": False, "timeframe": tf,
                "decision_stage": "execution",
                "filter_layer": "execution",
                "strategy_gate_case": _execution_exception_case(
                    "b_v16_close_exception",
                    e,
                    timeframe=tf,
                    phase="normal_close",
                ),
            })
            return

        net_pnl, gross_pnl, fee = pos.calc_pnl(exit_price)
        self.capital += pos.cost + net_pnl
        trade = {
            "symbol": sym, "side": pos.side, "entry_price": pos.entry_price,
            "exit_price": exit_price, "entry_time": str(pos.entry_time),
            "exit_time": str(datetime.now(CST)), "exit_reason": reason,
            "pnl_pct": round(gross_pnl / pos.cost * 100, 2),
            "pnl_usd": round(net_pnl, 4), "leverage": pos.leverage,
            "score": pos.score, "reason": f"{'+'.join(pos.tf)}开仓",
            "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
            "trailing_sl": pos.trailing_sl, "highest": pos.highest, "lowest": pos.lowest,
            "timeframe": tf, "cvd": 0, "ofi": 0, "rsi": 0, "funding_rate": 0,
        }
        log_trade(trade)
        log_event({
            "time": str(datetime.now(CST)), "event": "CLOSE", "symbol": sym,
            "side": pos.side, "exit_price": exit_price, "reason": reason,
            "pnl_pct": trade["pnl_pct"], "pnl_usd": trade["pnl_usd"],
            "entry_price": pos.entry_price, "entry_time": str(pos.entry_time),
            "timeframe": tf,
        })
        risk_exit = ("止损" in reason) or ("硬底" in reason)
        if risk_exit:
            self.sl_counts[sym] = self.sl_counts.get(sym, 0) + 1
        self.cooldowns[tf][sym] = SYMBOL_SL_BAN_CYCLES if risk_exit else COOLDOWN_MIN
        del self.positions[tf][sym]

    def _enforce_hard_stop_on_exchange(self):
        """交易所级硬顶兜底：即使本地状态丢失，也直接扫交易所持仓。"""
        cached_state = load_cached_account_state(PROJECT_ROOT, "B/v16")
        if not cached_state:
            logger.debug("中心账户状态不可用，跳过本轮交易所硬顶扫描")
            return
        raw_positions = cached_state.positions
        for p in raw_positions:
            try:
                amt = float(p.get("positionAmt", 0))
            except Exception:
                continue
            if abs(amt) <= 0.0001:
                continue
            symbol = p.get("symbol")
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
                    symbol=symbol,
                    side=side,
                    quantity=abs(amt),
                    cancel_open_orders=True,
                ))
                logger.warning(f"  交易所硬顶平仓: {symbol} {side} loss={loss_pct:.2f}% >= {MAX_LOSS_PCT:.2f}%")
                log_event({
                    "time": str(datetime.now(CST)),
                    "event": "FORCED_CLOSE" if close_exec.success else "FORCED_CLOSE_FAILED",
                    "symbol": symbol,
                    "side": side,
                    "reason": f"交易所硬顶{MAX_LOSS_PCT:.0f}%",
                    "loss_pct": round(loss_pct, 2),
                    "entry_price": entry,
                    "mark_price": mark,
                    "qty": abs(amt),
                    "side_source": side_source,
                    "raw_position_side": p.get("positionSide", ""),
                    "failure_reason": "" if close_exec.success else close_exec.reason,
                    "exchange_status": close_exec.status,
                    "exchange_close_success": close_exec.success,
                    **({} if close_exec.success else {
                        "decision_stage": "execution",
                        "filter_layer": "execution",
                        "strategy_gate_case": _execution_result_case(
                            "b_v16_forced_close_execution_result",
                            close_exec,
                            timeframe="exchange",
                            phase="hard_stop",
                        ),
                    }),
                })
            except Exception as e:
                logger.error(f"  交易所硬顶平仓失败: {symbol} {side} {e}")
                log_event({
                    "time": str(datetime.now(CST)),
                    "event": "FORCED_CLOSE_FAILED",
                    "symbol": symbol,
                    "side": side,
                    "reason": f"交易所硬顶{MAX_LOSS_PCT:.0f}%",
                    "loss_pct": round(loss_pct, 2),
                    "entry_price": entry,
                    "mark_price": mark,
                    "qty": abs(amt),
                    "side_source": side_source,
                    "raw_position_side": p.get("positionSide", ""),
                    "failure_reason": str(e),
                    "exchange_close_success": False,
                    "decision_stage": "execution",
                    "filter_layer": "execution",
                    "strategy_gate_case": _execution_exception_case(
                        "b_v16_forced_close_exception",
                        e,
                        timeframe="exchange",
                        phase="hard_stop",
                    ),
                })

    def run_cycle(self):
        self._enforce_hard_stop_on_exchange()
        self.scan()
        self.check_exits()
        total_positions = sum(len(p) for p in self.positions.values())
        logger.info(f"v16资金: ${self.capital:.2f} | 持仓: {total_positions}")
        log_system({"ts": str(datetime.now(CST)), "capital": round(self.capital, 4), "positions": total_positions, "status": "running"})

    def run(self, interval: int | None = None):
        logger.info(
            f"v16启动 | 阈值{SCORE_MIN} | 过热封顶{SCORE_MAX} | "
            f"分档止损low/normal/high={SL_MULT_LOW_VOL}/{SL_MULT_NORMAL_VOL}/{SL_MULT_HIGH_VOL}×ATR | "
            f"止盈{TP_MULT}×ATR | OFI窗口{OFI_WINDOW}"
        )
        if interval is None:
            interval = env_int("SCANNER_B_INTERVAL_SEC", 120)
        while True:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"扫描异常: {e}")
            time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="单次扫描后退出")
    parser.add_argument("--interval", type=int, default=None, help="扫描间隔秒数（默认120=2分钟）")
    args = parser.parse_args()
    scanner = ScannerV16()
    if args.once:
        scanner.run_cycle()
    else:
        scanner.run(args.interval)
