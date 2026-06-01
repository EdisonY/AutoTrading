"""Binance Testnet USDM期货交易客户端

直接使用 urllib + hmac 签名，不依赖 ccxt 复杂封装。

API: https://testnet.binancefuture.com
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
    rules_from_symbol,
    validate_market_price,
    validate_open_quantity,
)

logger = logging.getLogger("binance_client")

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
API_KEY = os.environ.get("BINANCE_A_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_A_API_SECRET", "")
BASE_URL = "https://testnet.binancefuture.com"

# 合约乘数（qty=1时代表的基础币数量）
# BTCUSDT: 1 qty = 1 BTC; ETHUSDT: 1 qty = 0.01 ETH; 其他多数: 1 qty = 1 USDT面值
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


def _sign(params: dict) -> str:
    """HMAC SHA256 签名"""
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing BINANCE_A_API_KEY / BINANCE_A_API_SECRET")
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
            # 只有 dict 类型才检查 code 字段；list 响应（如公共接口）直接返回
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


def _get(path: str, params: dict = None) -> dict:
    return _request("GET", path, params)


def _post(path: str, params: dict = None) -> dict:
    return _request("POST", path, params)

def _delete(path: str, params: dict = None) -> dict:
    return _request("DELETE", path, params)


# ═══════════════════════════════════════════════════════════════
# 市场数据（公共接口，无需签名）
# ═══════════════════════════════════════════════════════════════
_markets_cache = None


def get_markets() -> dict:
    """获取所有交易对信息（带缓存）"""
    global _markets_cache
    if _markets_cache:
        return _markets_cache

    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
            markets = {}
            for s in data.get("symbols", []):
                if s.get("status") != "TRADING":
                    continue
                symbol = s["symbol"]
                # 从 filters 提取 LOT_SIZE（stepSize = 每份合约面值）
                step_size = 1.0
                min_qty = 0.0
                max_qty = 0.0
                min_notional = 0.0
                tick_size = 0.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step_size = float(f.get("stepSize", 1.0))
                        min_qty = float(f.get("minQty", 0.0))
                        max_qty = float(f.get("maxQty", 0.0))
                    elif f.get("filterType") == "PRICE_FILTER":
                        tick_size = float(f.get("tickSize", 0.0))
                    elif f.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                        min_notional = float(f.get("notional") or f.get("minNotional") or 0.0)

                markets[symbol] = {
                    "symbol": symbol,
                    "base": s["baseAsset"],
                    "quote": s["quoteAsset"],
                    "contractSize": step_size,  # stepSize = 每份面值（BTC系用BTC单位）
                    "pricePrecision": s.get("pricePrecision", 8),
                    "qtyPrecision": s.get("quantityPrecision", 8),
                    "minQty": min_qty,
                    "maxQty": max_qty,
                    "tickSize": tick_size,
                    "stepSize": step_size,
                    "minNotional": min_notional,
                    "status": s.get("status", ""),
                    "contractType": s.get("contractType", ""),
                    "active": True,
                    "info": s,
                }
            _markets_cache = markets
            logger.info(f"市场数据加载: {len(markets)} 个交易对")
            return markets
    except Exception as e:
        logger.error(f"加载市场数据失败: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
# 客户端类
# ═══════════════════════════════════════════════════════════════
class BinanceClient:
    """币安 Testnet USDM 期货交易客户端"""

    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Missing BINANCE_A_API_KEY / BINANCE_A_API_SECRET")
        self._ctval_cache: dict[str, float] = {}
        self._markets = get_markets()
        self._account_cache_ttl = 2.0
        self._balance_cache = None
        self._positions_cache = None
        self._last_balance_error = None
        self._last_positions_error = None
        logger.info("BinanceClient 初始化完成")

    def _cache_valid(self, cached) -> bool:
        return cached is not None and (time.time() - cached[0]) <= self._account_cache_ttl

    def invalidate_account_snapshot(self) -> None:
        self._balance_cache = None
        self._positions_cache = None

    # ═══════════════════════════════════════════════════════════
    # 账户相关
    # ═══════════════════════════════════════════════════════════
    def get_balance(self) -> dict:
        """获取账户余额，直接返回Binance原生响应"""
        if self._cache_valid(self._balance_cache):
            return self._balance_cache[1]
        data = _get("/fapi/v2/account")
        self._last_balance_error = data if isinstance(data, dict) and str(data.get("code") or "") not in {"", "0", "200"} else None
        self._balance_cache = (time.time(), data)
        return data

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置杠杆"""
        data = _post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        if str(data.get("code", "")) in ("200", "0", ""):
            logger.info(f"杠杆设置成功: {symbol} {leverage}x")
        else:
            logger.warning(f"杠杆设置失败: {data}")
        return data

    # ═══════════════════════════════════════════════════════════
    # 合约信息
    # ═══════════════════════════════════════════════════════════
    def get_account_config(self) -> dict:
        """获取账户配置

        查询双向持仓模式：/fapi/v1/positionSide/dual
        """
        data = _get("/fapi/v1/positionSide/dual")
        if isinstance(data, dict) and "code" in data:
            logger.warning(f"查询持仓模式失败: {data}")
        return data

    def set_position_mode(self, mode: str):
        """设置持仓模式

        mode: "long_short_mode" = 双向持仓（Binance Hedge Mode）
              "net_mode"         = 单向持仓（Binance One-way Mode）
        """
        dual = (mode == "long_short_mode")
        data = _post("/fapi/v1/positionSide/dual", {
            "dualSidePosition": "true" if dual else "false",
        })
        if isinstance(data, dict) and data.get("code", 0) == 0:
            logger.info(f"持仓模式设置成功: {mode}")
        else:
            logger.warning(f"持仓模式设置失败: {data}")
        return data

    def get_ct_val(self, symbol: str) -> float:
        """获取合约面值（多数 = 1 USDT/张）"""
        if symbol in self._ctval_cache:
            return self._ctval_cache[symbol]

        if symbol in CTVAL_DEFAULTS:
            self._ctval_cache[symbol] = CTVAL_DEFAULTS[symbol]
            return CTVAL_DEFAULTS[symbol]

        market = self._markets.get(symbol, {})
        ct_val = float(market.get("contractSize", 1))
        self._ctval_cache[symbol] = ct_val
        logger.info(f"合约面值 {symbol}: {ct_val}")
        return ct_val

    def calc_size(self, symbol: str, price: float, usdt_amount: float,
                  leverage: int = 4) -> float:
        """计算合约数量（API中的quantity字段）

        Binance USDM 期货 quantity = 基础币数量，没有"合约面值"倍数概念：
        - BTCUSDT: quantity=0.001 → 0.001 BTC
        - XRPUSDT: quantity=100.0 → 100 XRP
        - 其他: quantity=N → N 个基础币

        公式: quantity = (usdt_amount × leverage) / price
        再按 LOT_SIZE.stepSize 向下取整。

        注意：get_ct_val 返回的是 stepSize 而非真正的合约面值，
        不要在这里使用 ctVal，否则会导致数量计算错误（如把0.1的stepSize当乘数）。
        """
        raw_qty = (usdt_amount * leverage) / price
        check = self.validate_order_quantity(symbol, raw_qty, price, usdt_amount, leverage)
        return check["quantity"] if check["ok"] else 0.0

    def get_symbol_rules(self, symbol: str) -> SymbolRules | None:
        market = self._markets.get(symbol)
        if not market:
            global _markets_cache
            _markets_cache = None
            self._markets = get_markets()
            market = self._markets.get(symbol)
        if not market:
            return None
        return rules_from_symbol(market.get("info") or {
            "symbol": symbol,
            "status": market.get("status") or "TRADING",
            "contractType": market.get("contractType") or "PERPETUAL",
            "baseAsset": market.get("base") or "",
            "quantityPrecision": market.get("qtyPrecision", 8),
            "pricePrecision": market.get("pricePrecision", 8),
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": market.get("stepSize", 1),
                    "minQty": market.get("minQty", 0),
                    "maxQty": market.get("maxQty", 0),
                },
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": market.get("tickSize", 0),
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "notional": market.get("minNotional", 0),
                },
            ],
        })

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
        precision = rules.quantity_precision if rules else 8
        return format_decimal(quantity, step, precision)

    def is_tradable(self, symbol: str) -> dict:
        """检查合约是否可交易"""
        market = self._markets.get(symbol)
        if not market:
            # 尝试重新加载市场数据
            global _markets_cache
            _markets_cache = None
            self._markets = get_markets()
            market = self._markets.get(symbol)

        if not market:
            return {"tradable": False, "reason": "合约不存在"}

        if is_tradfi_perp_symbol(symbol, market.get("base", "")):
            return {"tradable": False, "reason": "TradFi-Perps agreement symbol blocked"}

        if not market.get("active"):
            return {"tradable": False, "reason": "合约已下线"}

        ct_val = float(market.get("contractSize", 1))
        self._ctval_cache[symbol] = ct_val
        return {"tradable": True, "reason": "ok", "ctVal": ct_val}

    # ═══════════════════════════════════════════════════════════
    # 下单
    # ═══════════════════════════════════════════════════════════
    def place_order(self, symbol: str, side: str, sz: float,
                    tp: float = None, sl: float = None,
                    pos_side: str = None, cl_ord_id: str = None) -> dict:
        """下单（市价 + 止盈止损）

        币安需要两条单：
        1. 市价开仓
        2. 止盈止损（stop_market）

        双向持仓模式下必须传 positionSide: LONG/SHORT
        """
        try:
            # 先下主市价单
            main_params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": "MARKET",
                "quantity": self.format_quantity(symbol, sz),
                "newOrderRespType": "FULL",
            }
            # 双向持仓模式需传 positionSide
            if pos_side:
                main_params["positionSide"] = pos_side.upper()
            if cl_ord_id:
                main_params["newClientOrderId"] = cl_ord_id

            logger.info(f"主下单: {symbol} {side} qty={sz} TP={tp} SL={sl}")
            main_data = _post("/fapi/v1/order", main_params)

            if str(main_data.get("code", "")) not in ("200", "0", ""):
                logger.error(f"主下单失败: {main_data}")
                return main_data

            main_ord_id = main_data.get("orderId", "?")
            logger.info(f"主单成功: orderId={main_ord_id}")

            if tp is not None and sl is not None:
                logger.info(f"止盈止损将由scanner自动追踪: TP={tp} SL={sl}")

            return main_data
        except Exception as e:
            logger.error(f"下单异常 {symbol} {side} {sz}: {e}")
            return {"code": "-1", "msg": str(e)}

    def open_long(self, symbol: str, quantity: float = None,
                  leverage: int = 4, tp: float = None, sl: float = None,
                  risk_usdt: float = None, price: float = None) -> dict:
        """开多仓"""
        self.set_leverage(symbol, leverage)
        if risk_usdt and price and quantity is None:
            quantity = self.calc_size(symbol, price, risk_usdt, leverage)
        if quantity is None:
            quantity = 1.0

        result = self.place_order(
            symbol=symbol,
            side="BUY",
            sz=quantity,
            tp=tp,
            sl=sl,
            pos_side="long",
            cl_ord_id=build_client_order_id("olong", symbol, "long")
        )
        self.invalidate_account_snapshot()
        return result

    def open_short(self, symbol: str, quantity: float = None,
                   leverage: int = 4, tp: float = None, sl: float = None,
                   risk_usdt: float = None, price: float = None) -> dict:
        """开空仓"""
        self.set_leverage(symbol, leverage)
        if risk_usdt and price and quantity is None:
            quantity = self.calc_size(symbol, price, risk_usdt, leverage)
        if quantity is None:
            quantity = 1.0

        result = self.place_order(
            symbol=symbol,
            side="SELL",
            sz=quantity,
            tp=tp,
            sl=sl,
            pos_side="short",
            cl_ord_id=build_client_order_id("oshort", symbol, "short")
        )
        self.invalidate_account_snapshot()
        return result

    # ═══════════════════════════════════════════════════════════
    # 平仓
    # ═══════════════════════════════════════════════════════════
    def close_position(self, symbol: str, pos_side: str = None,
                       quantity: float = None, order_side: str = "", position_side: str = "") -> dict:
        """平仓（市价）

        pos_side: "LONG" 或 "SHORT"（Binance双向持仓模式下的positionSide值）
        """
        positions = self.get_positions(symbol)
        target = None
        for p in positions:
            pos_amt = float(p.get("positionAmt", 0))
            if pos_amt == 0:
                continue
            target_position_side = (position_side or pos_side or "").upper()
            if target_position_side and p.get("positionSide", "").upper() != target_position_side:
                continue
            target = p
            break

        if not target:
            logger.info(f"无持仓可平: {symbol}")
            return {"code": "0", "msg": "no position"}

        current_sz = abs(float(target.get("positionAmt", 0)))
        close_sz = quantity if quantity else current_sz
        target_side = target.get("positionSide", "").upper()
        target_amt = float(target.get("positionAmt", 0))
        close_side = (order_side or ("SELL" if target_amt > 0 else "BUY")).upper()

        logger.info(f"平仓: {symbol} {target_side} qty={close_sz}")
        close_params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": str(close_sz),
        }
        if target_side:
            close_params["positionSide"] = target_side
        else:
            close_params["reduceOnly"] = True
        result = _post("/fapi/v1/order", close_params)

        if str(result.get("code", "")) in ("200", "0", ""):
            logger.info(f"平仓成功: orderId={result.get('orderId', '?')}")
        else:
            logger.error(f"平仓失败: {result}")

        self.invalidate_account_snapshot()
        return result

    # ═══════════════════════════════════════════════════════════
    # 持仓查询
    # ═══════════════════════════════════════════════════════════
    def get_positions(self, symbol: str = None) -> list:
        """查询持仓，返回Binance原生格式（过滤零仓位）

        Returns:
            list of Binance position fields: symbol, positionAmt, positionSide,
            entryPrice, unrealizedProfit, notionalValue, etc.
        """
        if self._cache_valid(self._positions_cache):
            positions = self._positions_cache[1]
            if symbol:
                return [pos for pos in positions if pos.get("symbol") == symbol]
            return positions

        data = _get("/fapi/v2/positionRisk", {})
        if isinstance(data, list):
            positions = data
        elif isinstance(data, dict) and "code" in data and str(data.get("code")) not in ("200", "0", ""):
            logger.error(f"持仓查询失败: {data}")
            self._last_positions_error = data
            return []
        else:
            positions = data if isinstance(data, list) else []

        open_positions = [pos for pos in positions if float(pos.get("positionAmt", 0)) != 0]
        self._last_positions_error = None
        self._positions_cache = (time.time(), open_positions)
        if symbol:
            return [pos for pos in open_positions if pos.get("symbol") == symbol]
        return open_positions

    def get_position(self, symbol: str, pos_side: str) -> Optional[dict]:
        """查询特定持仓

        pos_side: "LONG" 或 "SHORT"（Binance双向持仓模式下的positionSide值）
        """
        positions = self.get_positions(symbol)
        for pos in positions:
            if pos.get("positionSide", "").upper() == pos_side.upper():
                return pos
        return None


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════
_client = None


def get_client() -> BinanceClient:
    global _client
    if _client is None:
        _client = BinanceClient()
    return _client
