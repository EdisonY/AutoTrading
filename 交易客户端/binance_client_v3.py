"""Binance Testnet USDM期货交易客户端 - Account C (v14专用)

直接使用 urllib + hmac 签名，不依赖 ccxt 复杂封装。

API: https://testnet.binancefuture.com
Account C API Key (v14策略专用)
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
from core.binance_api_guard import (
    record_public_response,
    record_response,
    wait_before_public_request,
    wait_before_request,
)
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request

logger = logging.getLogger("binance_client_v3")

# ═══════════════════════════════════════════════════════════════
# Account C 配置
# ═══════════════════════════════════════════════════════════════
API_KEY = os.environ.get("BINANCE_C_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_C_API_SECRET", "")
BASE_URL = "https://testnet.binancefuture.com"
_exchange_info_cache = None
_exchange_info_cache_time = 0.0


def _sign(params: dict) -> str:
    """HMAC SHA256 签名"""
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing BINANCE_C_API_KEY / BINANCE_C_API_SECRET")
    query = urllib.parse.urlencode(params)
    mac = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256)
    return mac.hexdigest()


def _request(method: str, path: str, params: dict = None) -> dict:
    """发送带签名的请求"""
    if api_queue_client_enabled():
        data = queued_api_request(
            scope="signed",
            account="C",
            label="C/v14",
            method=method,
            path=path,
            body=dict(params or {}),
        )
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            logger.error(f"Binance queued API错误: {data}")
        return data
    wait_before_request("C/v14", method, path)
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
                record_response("C/v14", method, path, data.get("code"), json.dumps(data, ensure_ascii=False))
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code}: {body[:500]}")
        record_response("C/v14", method, path, e.code, body)
        return {"code": str(e.code), "msg": body[:500]}
    except Exception as e:
        logger.error(f"请求异常: {e}")
        return {"code": "-1", "msg": str(e)}


def _public_request(path: str) -> dict:
    """发送公共 REST 请求，不占 signed 账户预算。"""
    url = f"{BASE_URL}{path}"
    try:
        if api_queue_client_enabled():
            return queued_api_request(scope="public", label="C/v14-client", method="GET", path=path)
        wait_before_public_request("C/v14-client", url)
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code}: {body[:500]}")
        record_public_response("C/v14-client", url, e.code, body)
        return {"code": str(e.code), "msg": body[:500]}
    except Exception as e:
        logger.error(f"公共请求异常: {e}")
        return {"code": "-1", "msg": str(e)}


def _is_reduce_only_rejected(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    msg = str(result.get("msg", "")).lower()
    return str(result.get("code", "")) == "-2022" or "reduceonly order is rejected" in msg


def _get_exchange_info_cached() -> dict:
    global _exchange_info_cache, _exchange_info_cache_time
    now = time.time()
    if _exchange_info_cache is not None and (now - _exchange_info_cache_time) < 60:
        return _exchange_info_cache
    info = _public_request("/fapi/v1/exchangeInfo")
    _exchange_info_cache = info
    _exchange_info_cache_time = now
    return info


def _get_symbol_rules(symbol: str) -> SymbolRules | None:
    info = _get_exchange_info_cached()
    for s in parse_symbols(info):
        if s.get("symbol") == symbol:
            return rules_from_symbol(s)
    return None


class BinanceClientV3:
    """Account C 专用 Binance Testnet 客户端"""

    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Missing BINANCE_C_API_KEY / BINANCE_C_API_SECRET")
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.base_url = BASE_URL
        self._account_cache_ttl = float(os.environ.get("BINANCE_ACCOUNT_CACHE_TTL_SEC", "5"))
        self._balance_cache = None
        self._positions_cache = None
        self._last_balance_error = None
        self._last_positions_error = None

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
        self._last_balance_error = data if isinstance(data, dict) and str(data.get("code") or "") not in {"", "0", "200"} else None
        self._balance_cache = (time.time(), data)
        return data

    def get_account_config(self) -> dict:
        """查询账户持仓模式"""
        return _request("GET", "/fapi/v1/positionSide/dual")

    def set_position_mode(self, mode: str = "long_short_mode") -> dict:
        """设置持仓模式：long_short_mode（双向持仓）"""
        return _request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})

    def get_positions(self) -> list:
        """查询所有持仓（过滤零仓位，返回Binance原始格式）"""
        if self._cache_valid(self._positions_cache):
            return self._positions_cache[1]
        data = _request("GET", "/fapi/v2/positionRisk")
        if isinstance(data, list):
            positions = data
        elif isinstance(data, dict) and "code" in data and str(data.get("code")) not in ("200", "0", ""):
            logger.error(f"持仓查询失败: {data}")
            self._last_positions_error = data
            return []
        else:
            positions = data if isinstance(data, list) else data.get("data", [])

        result = []
        for pos in positions:
            pos_amt = float(pos.get("positionAmt", 0))
            if pos_amt == 0:
                continue
            result.append(pos)
        self._last_positions_error = None
        self._positions_cache = (time.time(), result)
        return result

    def get_exchange_info(self) -> dict:
        """查询交易对信息（合约规模、最小下单量等）"""
        return _get_exchange_info_cached()

    # ── 合约下单 ────────────────────────────────────────────────

    def calc_size(self, symbol: str, price: float, usdt: float, leverage: int) -> float:
        """计算合约数量。公式：qty = (usdt * leverage) / price，按 stepSize 向下取整。"""
        try:
            raw_qty = (usdt * leverage) / price
            check = self.validate_order_quantity(symbol, raw_qty, price, usdt, leverage)
            if not check["ok"]:
                logger.warning(f"calc_size: {symbol} {check['code']} {check['reason']}")
                return 0
            logger.debug(f"calc_size: {symbol} price={price} usdt={usdt} lev={leverage} -> qty={check['quantity']}")
            return check["quantity"]
        except Exception as e:
            logger.debug(f"calc_size获取合约信息失败: {e}")

        qty_fb = (usdt * leverage) / price
        logger.debug(f"calc_size fallback: {symbol} → qty={qty_fb:.4f}")
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
        """开多单（市价入场）"""
        self.set_leverage(symbol, leverage)
        params = {
            "symbol": symbol,
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": self.format_quantity(symbol, quantity),
            "newOrderRespType": "FULL",
            "newClientOrderId": build_client_order_id("copen", symbol, "long"),
        }
        result = _request("POST", "/fapi/v1/order", params)
        self.invalidate_account_snapshot()
        return result

    def open_short(self, symbol: str, quantity: float, leverage: int, tp: float, sl: float) -> dict:
        """开空单（市价入场）"""
        self.set_leverage(symbol, leverage)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "MARKET",
            "quantity": self.format_quantity(symbol, quantity),
            "newOrderRespType": "FULL",
            "newClientOrderId": build_client_order_id("copen", symbol, "short"),
        }
        result = _request("POST", "/fapi/v1/order", params)
        self.invalidate_account_snapshot()
        return result

    def close_position(self, symbol: str, pos_side: str, quantity: float = 0, order_side: str = "", position_side: str = "") -> dict:
        """平仓（市价全平）"""
        api_position_side = (position_side or pos_side).upper()
        api_order_side = (order_side or ("SELL" if pos_side.lower() == "long" else "BUY")).upper()
        params = {
            "symbol": symbol,
            "side": api_order_side,
            "positionSide": api_position_side,
            "type": "MARKET",
            "newOrderRespType": "FULL",
        }
        if quantity > 0:
            params["quantity"] = f"{quantity:.8f}".rstrip('0').rstrip('.') if isinstance(quantity, float) else quantity

        result = _request("POST", "/fapi/v1/order", params)
        if _is_reduce_only_rejected(result):
            fallback = dict(params)
            fallback.pop("positionSide", None)
            fallback["reduceOnly"] = "true"
            result = _request("POST", "/fapi/v1/order", fallback)
        self.invalidate_account_snapshot()
        return result

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置杠杆倍数"""
        result = _request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        if isinstance(result, dict) and result.get("leverage") is not None:
            logger.info(f"杠杆设置成功: {symbol} {leverage}x")
        return result

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """设置保证金类型：CROSSED(全仓) / ISOLATED(逐仓)"""
        result = _request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
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


def get_client() -> BinanceClientV3:
    global _client
    if _client is None:
        _client = BinanceClientV3()
    return _client
