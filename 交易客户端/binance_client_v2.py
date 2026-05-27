"""Binance Testnet USDM期货交易客户端 - Account B (v12专用)

直接使用 urllib + hmac 签名，不依赖 ccxt 复杂封装。

API: https://testnet.binancefuture.com
Account B API Key (v12策略专用)
"""

import json
import hmac
import hashlib
import time
import urllib.request
import urllib.parse
import logging
import os
from typing import Optional

from core.binance_order_rules import (
    SymbolRules,
    build_client_order_id,
    format_decimal,
    is_tradfi_perp_symbol,
    parse_symbols,
    rules_from_symbol,
    validate_market_price,
    validate_open_quantity,
)

logger = logging.getLogger("binance_client_v2")

# ═══════════════════════════════════════════════════════════════
# Account B 配置
# ═══════════════════════════════════════════════════════════════
API_KEY = os.environ.get("BINANCE_B_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_B_API_SECRET", "")
BASE_URL = "https://testnet.binancefuture.com"

# 合约乘数（qty=1时代表的基础币数量）
CTVAL_DEFAULTS = {
    "BTCUSDT": 1.0,
    "ETHUSDT": 0.01,
    "BNBUSDT": 1.0,
    "SOLUSDT": 1.0,
    "XRPUSDT": 1.0,
    "DOGEUSDT": 1.0,
    "ADAUSDT": 1.0,
    "AVAXUSDT": 1.0,
    "LINKUSDT": 1.0,
    "DOTUSDT": 1.0,
    "MATICUSDT": 1.0,
    "LTCUSDT": 1.0,
    "BCHUSDT": 1.0,
    "UNIUSDT": 1.0,
    "ATOMUSDT": 1.0,
    "XLMUSDT": 1.0,
    "APTUSDT": 1.0,
    "ARBUSDT": 1.0,
    "OPUSDT": 1.0,
    "NEARUSDT": 1.0,
    "INJUSDT": 1.0,
    "SUIUSDT": 1.0,
    "FTMUSDT": 1.0,
    "MASKUSDT": 1.0,
    "WIFUSDT": 1.0,
    "NEIROUSDT": 1.0,
    "STRKUSDT": 1.0,
    "ENAUSDT": 1.0,
    "WASUSDT": 1.0,
    "TNSUSDT": 1.0,
    "REZUSDT": 1.0,
    "GPSUSDT": 1.0,
    "STXUSDT": 1.0,
    "WALUSDT": 1.0,
    "XCUUSDT": 1.0,
    "KGENUSDT": 1.0,
    "ACUUSDT": 1.0,
    "XPDUSDT": 1.0,
    "WCTUSDT": 1.0,
    "ENJUSDT": 1.0,
    "NOTUSDT": 1.0,
    "LPTUSDT": 1.0,
    "ACTUSDT": 1.0,
    "SYRUPUSDT": 1.0,
    "PEPEUSDT": 1.0,
    "SHIBUSDT": 1.0,
    "FLOKIUSDT": 1.0,
    "1000FLOKIUSDT": 1.0,
}


_exchange_info_cache = None
_exchange_info_cache_time = 0.0
_symbol_precision_cache = {}

def _get_exchange_info_cached() -> dict:
    """带缓存的exchangeInfo查询（60秒内复用）"""
    global _exchange_info_cache, _exchange_info_cache_time
    now = time.time()
    if _exchange_info_cache is not None and (now - _exchange_info_cache_time) < 60:
        return _exchange_info_cache
    info = _request("GET", "/fapi/v1/exchangeInfo")
    _exchange_info_cache = info
    _exchange_info_cache_time = now
    return info

def _get_step_size(symbol: str) -> tuple:
    """获取指定币种的stepSize和minQty，返回(float, float)，失败返回(1.0, 0.0)。结果缓存60秒"""
    if symbol in _symbol_precision_cache:
        cached_step, cached_min, cached_tick, cached_ts = _symbol_precision_cache[symbol]
        if time.time() - cached_ts < 60:
            return cached_step, cached_min
    info = _get_exchange_info_cached()
    symbols = info if isinstance(info, list) else (info.get("data") or info.get("symbols") or [])
    step, min_qty, tick = 1.0, 0.0, 0.0001
    for s in symbols:
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f["stepSize"]); min_qty = float(f["minQty"])
                elif f.get("filterType") == "PRICE_FILTER":
                    tick = float(f.get("tickSize", "0.0001"))
            break
    _symbol_precision_cache[symbol] = (step, min_qty, tick, time.time())
    return step, min_qty

def _get_symbol_rules(symbol: str) -> SymbolRules | None:
    info = _get_exchange_info_cached()
    for s in parse_symbols(info):
        if s.get("symbol") == symbol:
            return rules_from_symbol(s)
    return None

def _get_tick_size(symbol: str) -> float:
    """获取指定币种的tickSize（价格精度）。先查缓存，没有则填充"""
    if symbol in _symbol_precision_cache:
        _, _, tick, ts = _symbol_precision_cache[symbol]
        if time.time() - ts < 60:
            return tick
    _get_step_size(symbol)
    _, _, tick, _ = _symbol_precision_cache.get(symbol, (1.0, 0.0, 0.0001, 0))
    return tick

def _format_quantity(qty: float, step: float) -> str:
    """将数量格式化为符合stepSize的字符串，去掉多余小数"""
    return format_decimal(qty, step, 8)

def _format_price(price: float, symbol: str) -> str:
    """将价格格式化为符合tickSize的字符串"""
    tick = _get_tick_size(symbol)
    if tick <= 0:
        tick = 0.0001
    decimals = max(0, -int(round(__import__('math').log10(tick))))
    formatted = f"{price:.{decimals}f}"
    return formatted.rstrip('0').rstrip('.')


def _sign(params: dict) -> str:
    """HMAC SHA256 签名"""
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing BINANCE_B_API_KEY / BINANCE_B_API_SECRET")
    query = urllib.parse.urlencode(params)
    mac = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256)
    return mac.hexdigest()


def _request(method: str, path: str, params: dict = None) -> dict:
    """发送带签名的请求"""
    timestamp = int(time.time() * 1000)
    params = params or {}
    params["timestamp"] = timestamp
    params["recvWindow"] = 5000

    query = urllib.parse.urlencode(params)
    signature = _sign(params)
    full_url = f"{BASE_URL}{path}?{query}&signature={signature}"

    headers = {
        "X-MBX-APIKEY": API_KEY,
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(full_url, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
                logger.error(f"Binance API错误: {data}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code}: {body[:500]}")
        return {"code": str(e.code), "msg": body[:200]}
    except Exception as e:
        logger.error(f"请求异常: {e}")
        return {"code": "-1", "msg": str(e)}


class BinanceClientV2:
    """Account B 专用 Binance Testnet 客户端"""

    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Missing BINANCE_B_API_KEY / BINANCE_B_API_SECRET")
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.base_url = BASE_URL
        self._account_cache_ttl = 2.0
        self._balance_cache = None
        self._positions_cache = None

    def _cache_valid(self, cached) -> bool:
        return cached is not None and (time.time() - cached[0]) <= self._account_cache_ttl

    def invalidate_account_snapshot(self) -> None:
        self._balance_cache = None
        self._positions_cache = None

    # ── 账户信息 ────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """查询账户余额"""
        if self._cache_valid(self._balance_cache):
            return self._balance_cache[1]
        data = _request("GET", "/fapi/v2/balance")
        self._balance_cache = (time.time(), data)
        return data

    def get_account_config(self) -> dict:
        """查询账户持仓模式"""
        return _request("GET", "/fapi/v1/positionSide/dual")

    def set_position_mode(self, mode: str = "long_short_mode") -> dict:
        """设置持仓模式：long_short_mode（双向持仓）"""
        return _request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})

    def get_positions(self) -> list:
        """查询所有持仓（过滤零仓位，返回Binance原始格式）

        Returns:
            list of Binance positionRisk items (symbol, positionAmt, positionSide,
            entryPrice, unrealizedProfit, notionalValue, etc.)
        """
        if self._cache_valid(self._positions_cache):
            return self._positions_cache[1]
        data = _request("GET", "/fapi/v2/positionRisk")
        if isinstance(data, list):
            positions = data
        elif isinstance(data, dict) and "code" in data and str(data.get("code")) not in ("200", "0", ""):
            logger.error(f"持仓查询失败: {data}")
            return []
        else:
            positions = data if isinstance(data, list) else data.get("data", [])

        result = []
        for pos in positions:
            pos_amt = float(pos.get("positionAmt", 0))
            if pos_amt == 0:
                continue
            result.append(pos)
        self._positions_cache = (time.time(), result)
        return result

    def get_exchange_info(self) -> dict:
        """查询交易对信息（合约规模、最小下单量等）"""
        return _request("GET", "/fapi/v1/exchangeInfo")

    # ── 合约下单 ────────────────────────────────────────────────

    def calc_size(self, symbol: str, price: float, usdt: float, leverage: int) -> float:
        """计算合约数量，按stepSize取整。price<=0返回0"""
        if price <= 0:
            return 0
        try:
            raw_qty = (usdt * leverage) / price
            check = self.validate_order_quantity(symbol, raw_qty, price, usdt, leverage)
            if not check["ok"]:
                logger.warning(f"calc_size: {symbol} {check['code']} {check['reason']}")
                return 0
            logger.debug(f"calc_size: {symbol} price={price} usdt={usdt} lev={leverage} -> qty={check['quantity']}")
            return check["quantity"]
        except Exception as e:
            logger.debug(f"calc_size失败: {e}")
            qty_fb = (usdt * leverage) / price
            return qty_fb

    def get_symbol_rules(self, symbol: str) -> SymbolRules | None:
        return _get_symbol_rules(symbol)

    def validate_order_quantity(self, symbol: str, quantity: float, price: float, risk_usdt: float, leverage: int) -> dict:
        check = validate_open_quantity(self.get_symbol_rules(symbol), quantity, price, risk_usdt, leverage)
        return {
            "ok": check.ok,
            "quantity": check.quantity,
            "reason": check.reason,
            "code": check.code,
            "notional": check.notional,
            "min_notional": check.min_notional,
            "max_qty": check.max_qty,
            "min_qty": check.min_qty,
            "step_size": check.step_size,
        }

    def validate_market_order_price(self, symbol: str, side: str) -> dict:
        check = validate_market_price(BASE_URL, self.get_symbol_rules(symbol), symbol, side)
        return {"ok": check.ok, "reason": check.reason, "code": check.code, "counterparty_price": check.notional}

    def format_quantity(self, symbol: str, quantity: float) -> str:
        rules = self.get_symbol_rules(symbol)
        step = (rules.market_step_size or rules.step_size) if rules else 1.0
        return format_decimal(quantity, step, rules.quantity_precision if rules else 8)

    def open_long(self, symbol: str, quantity: float, leverage: int, tp: float, sl: float) -> dict:
        self.set_leverage(symbol, leverage)
        params = {
            "symbol": symbol,
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": self.format_quantity(symbol, quantity),
            "newOrderRespType": "FULL",
            "newClientOrderId": build_client_order_id("bopen", symbol, "long"),
        }
        result = _request("POST", "/fapi/v1/order", params)
        self.invalidate_account_snapshot()
        return result

    def open_short(self, symbol: str, quantity: float, leverage: int, tp: float, sl: float) -> dict:
        self.set_leverage(symbol, leverage)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "MARKET",
            "quantity": self.format_quantity(symbol, quantity),
            "newOrderRespType": "FULL",
            "newClientOrderId": build_client_order_id("bopen", symbol, "short"),
        }
        result = _request("POST", "/fapi/v1/order", params)
        self.invalidate_account_snapshot()
        return result

    def _add_tp_sl(self, symbol: str, pos_side: str, sz: int, tp: float, sl: float):
        """下一个止盈一个止损（市价）"""
        is_long = pos_side == "LONG"
        # 止盈止损用 STOP_MARKET（不能下单时设TP/SL，改用独立追踪）
        for endpoint, side, price in [
            ("/fapi/v1/order", "SELL" if is_long else "BUY", tp),
            ("/fapi/v1/order", "SELL" if is_long else "BUY", sl),
        ]:
            params = {
                "symbol": symbol,
                "side": side,
                "positionSide": pos_side,
                "type": "STOP_MARKET",
                "quantity": sz,
                "stopPrice": price,
                # Hedge Mode 下不传 reduceOnly
                "newOrderRespType": "FULL",
            }
            try:
                _request("POST", endpoint, params)
            except Exception as e:
                logger.debug(f"_add_tp_sl失败: {e}")

    def close_position(self, symbol: str, pos_side: str, quantity: float = 0) -> dict:
        """平仓（市价全平）

        Args:
            symbol: 交易对，如 BTCUSDT
            pos_side: 持仓方向，long 或 short
            quantity: 平仓数量，0表示全平（不传quantity由交易所自动全平）
        注意：Hedge Mode 下不能传 reduceOnly，只用 positionSide 区分方向
        """
        is_long = pos_side.lower() == "long"
        params = {
            "symbol": symbol,
            "side": "SELL" if is_long else "BUY",
            "positionSide": pos_side.upper(),
            "type": "MARKET",
            # Hedge Mode 下禁止 reduceOnly，移除该参数
            "newOrderRespType": "FULL",
        }
        if quantity > 0:
            params["quantity"] = _format_quantity(quantity, _get_step_size(symbol)[0])

        result = _request("POST", "/fapi/v1/order", params)
        self.invalidate_account_snapshot()
        return result

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置杠杆倍数（Account B 版本）"""
        result = _request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        if isinstance(result, dict) and result.get("leverage") is not None:
            logger.info(f"杠杆设置成功: {symbol} {leverage}x")
        return result

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """设置保证金类型：CROSSED(全仓) / ISOLATED(逐仓)"""
        result = _request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        # 如果已经是目标类型，Binance 返回 -4046，视为成功
        if isinstance(result, dict):
            code = str(result.get("code", ""))
            if code == "-4046":
                logger.info(f"保证金类型已是 {margin_type}，无需修改: {symbol}")
            elif result.get("code") == 200 or not result.get("code"):
                logger.info(f"保证金类型设置成功: {symbol} {margin_type}")
            else:
                logger.warning(f"保证金类型设置失败: {symbol} {margin_type} → {result}")
        return result

    def is_tradable(self, symbol: str) -> dict:
        """检查合约是否可交易"""
        try:
            info = _request("GET", "/fapi/v1/exchangeInfo")
            symbols = info.get("data", info.get("symbols", []))
            for s in symbols:
                if s.get("symbol") == symbol:
                    status = s.get("status", "")
                    contract = s.get("contractType", "")
                    if is_tradfi_perp_symbol(symbol, s.get("baseAsset", "")):
                        return {"tradable": False, "reason": "TradFi-Perps agreement symbol blocked"}
                    if status == "TRADING" and contract == "PERPETUAL":
                        return {"tradable": True, "reason": ""}
                    return {"tradable": False, "reason": f"status={status}, contract={contract}"}
            return {"tradable": False, "reason": "symbol not found"}
        except Exception as e:
            return {"tradable": False, "reason": str(e)}

    def _delete(self, symbol: str = None) -> dict:
        """撤销所有未成交订单（或指定symbol的）"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return _request("DELETE", "/fapi/v1/allOpenOrders", params)


def _delete(path: str, params: dict = None) -> dict:
    """模块级删除函数（兼容scanner导入）"""
    return _request("DELETE", path, params or {})


_client = None


def get_client() -> BinanceClientV2:
    global _client
    if _client is None:
        _client = BinanceClientV2()
    return _client
