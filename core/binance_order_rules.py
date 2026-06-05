"""Shared Binance USDM order-rule helpers."""

from __future__ import annotations

import math
import re
import time
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.binance_api_guard import record_public_response, wait_before_public_request
from core.binance_api_queue_client import api_queue_client_enabled, queued_api_request


TRADFI_PERP_BASES = {
    "XAU",
    "XAG",
    "XPD",
    "XPT",
    "XCU",
    "CRC",
    "CRCL",
}

BINANCE_USDM_MIN_NOTIONAL_FLOOR = 5.05


@dataclass(slots=True)
class SymbolRules:
    symbol: str
    status: str = ""
    contract_type: str = ""
    step_size: float = 1.0
    min_qty: float = 0.0
    max_qty: float = 0.0
    tick_size: float = 0.0
    min_notional: float = 0.0
    market_step_size: float = 0.0
    market_min_qty: float = 0.0
    market_max_qty: float = 0.0
    percent_multiplier_up: float = 0.0
    percent_multiplier_down: float = 0.0
    quantity_precision: int = 8
    price_precision: int = 8
    base_asset: str = ""


@dataclass(slots=True)
class QuantityCheck:
    ok: bool
    quantity: float = 0.0
    reason: str = ""
    code: str = ""
    min_notional: float = 0.0
    notional: float = 0.0
    max_qty: float = 0.0
    min_qty: float = 0.0
    step_size: float = 0.0


def parse_symbols(exchange_info: Any) -> list[dict[str, Any]]:
    if isinstance(exchange_info, dict):
        symbols = exchange_info.get("symbols")
        if symbols is None:
            symbols = exchange_info.get("data")
        return symbols if isinstance(symbols, list) else []
    return exchange_info if isinstance(exchange_info, list) else []


def is_tradfi_perp_symbol(symbol: str, base_asset: str = "") -> bool:
    symbol_u = str(symbol or "").upper()
    base_u = str(base_asset or "").upper()
    if base_u in TRADFI_PERP_BASES:
        return True
    return any(symbol_u.startswith(base) for base in TRADFI_PERP_BASES)


def rules_from_symbol(raw: dict[str, Any]) -> SymbolRules:
    filters = raw.get("filters") or []
    rules = SymbolRules(
        symbol=str(raw.get("symbol") or ""),
        status=str(raw.get("status") or ""),
        contract_type=str(raw.get("contractType") or ""),
        quantity_precision=int(raw.get("quantityPrecision") or 8),
        price_precision=int(raw.get("pricePrecision") or 8),
        base_asset=str(raw.get("baseAsset") or ""),
    )
    for item in filters:
        ftype = item.get("filterType")
        if ftype == "LOT_SIZE":
            rules.step_size = float(item.get("stepSize") or rules.step_size or 1.0)
            rules.min_qty = float(item.get("minQty") or 0.0)
            rules.max_qty = float(item.get("maxQty") or 0.0)
        elif ftype == "PRICE_FILTER":
            rules.tick_size = float(item.get("tickSize") or 0.0)
        elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
            rules.min_notional = float(item.get("notional") or item.get("minNotional") or 0.0)
        elif ftype == "MARKET_LOT_SIZE":
            rules.market_step_size = float(item.get("stepSize") or 0.0)
            rules.market_min_qty = float(item.get("minQty") or 0.0)
            rules.market_max_qty = float(item.get("maxQty") or 0.0)
        elif ftype == "PERCENT_PRICE":
            rules.percent_multiplier_up = float(item.get("multiplierUp") or 0.0)
            rules.percent_multiplier_down = float(item.get("multiplierDown") or 0.0)
    return rules


def decimals_from_step(step: float, fallback: int = 8) -> int:
    if step <= 0:
        return fallback
    text = f"{step:.16f}".rstrip("0")
    if "." in text:
        return min(16, max(0, len(text.split(".", 1)[1])))
    if "e-" in f"{step:e}":
        return min(16, int(f"{step:e}".split("e-")[-1]))
    return 0


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor((value + step * 1e-9) / step) * step


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil((value - step * 1e-9) / step) * step


def format_decimal(value: float, step: float, precision: int = 8) -> str:
    decimals = decimals_from_step(step, precision)
    text = f"{float(value):.{decimals}f}"
    return text.rstrip("0").rstrip(".") or "0"


def format_decimal_down(value: float, step: float, precision: int = 8) -> str:
    decimals = decimals_from_step(step, precision)
    if step > 0:
        value = floor_to_step(float(value), step)
    else:
        factor = 10 ** max(0, decimals)
        value = math.floor(float(value) * factor) / factor
    text = f"{float(value):.{decimals}f}"
    return text.rstrip("0").rstrip(".") or "0"


def build_client_order_id(prefix: str, symbol: str, side: str) -> str:
    safe_symbol = re.sub(r"[^A-Z0-9]", "", str(symbol).upper())[:18]
    safe_side = re.sub(r"[^A-Z]", "", str(side).upper())[:5]
    millis = int(time.time() * 1000)
    return f"{prefix}_{safe_symbol}_{safe_side}_{millis}"[:36]


def validate_open_quantity(
    rules: SymbolRules | None,
    quantity: float,
    price: float,
    risk_usdt: float,
    leverage: int,
    allow_raise_to_min_notional: bool = True,
) -> QuantityCheck:
    if rules is None:
        return QuantityCheck(False, code="symbol_rules_missing", reason="exchangeInfo symbol rules missing")
    if rules.status and rules.status != "TRADING":
        return QuantityCheck(False, code="symbol_not_trading", reason=f"status={rules.status}")
    if rules.contract_type and rules.contract_type != "PERPETUAL":
        return QuantityCheck(False, code="contract_not_perpetual", reason=f"contractType={rules.contract_type}")
    if is_tradfi_perp_symbol(rules.symbol, rules.base_asset):
        return QuantityCheck(False, code="tradfi_perp_blocked", reason="TradFi-Perps agreement symbol is disabled")
    if price <= 0:
        return QuantityCheck(False, code="price_invalid", reason="price<=0")

    step = rules.market_step_size or rules.step_size or 1.0
    min_qty = rules.market_min_qty or rules.min_qty
    max_qty = rules.market_max_qty or rules.max_qty
    qty = floor_to_step(float(quantity), step)
    if max_qty > 0 and qty > max_qty:
        qty = floor_to_step(max_qty, step)
    if min_qty > 0 and qty < min_qty:
        qty = ceil_to_step(min_qty, step)

    effective_min_notional = max(float(rules.min_notional or 0.0), BINANCE_USDM_MIN_NOTIONAL_FLOOR)

    notional = qty * price
    if effective_min_notional > 0 and notional < effective_min_notional:
        if not allow_raise_to_min_notional:
            return QuantityCheck(
                False,
                quantity=qty,
                code="notional_too_small",
                reason=f"notional {notional:.6g} < minNotional {effective_min_notional:.6g}",
                min_notional=effective_min_notional,
                notional=notional,
                max_qty=rules.max_qty,
                min_qty=rules.min_qty,
                step_size=step,
            )
        qty = ceil_to_step(effective_min_notional / price, step)
        if min_qty > 0 and qty < min_qty:
            qty = ceil_to_step(min_qty, step)
        if max_qty > 0 and qty > max_qty:
            return QuantityCheck(
                False,
                quantity=qty,
                code="min_notional_exceeds_max_qty",
                reason="minNotional-required quantity exceeds maxQty",
                min_notional=effective_min_notional,
                notional=qty * price,
                max_qty=max_qty,
                min_qty=min_qty,
                step_size=step,
            )
        notional = qty * price

    if qty <= 0:
        return QuantityCheck(False, quantity=qty, code="qty<=0", reason="quantity too small")
    if max_qty > 0 and qty > max_qty:
        return QuantityCheck(
            False,
            quantity=qty,
            code="quantity_over_max",
            reason=f"quantity {qty:g} > maxQty {max_qty:g}",
            notional=notional,
            max_qty=max_qty,
            min_qty=min_qty,
            step_size=step,
        )

    max_notional = risk_usdt * max(leverage, 1)
    if max_notional > 0 and notional > max_notional * 1.25:
        return QuantityCheck(
            False,
            quantity=qty,
            code="risk_notional_exceeded",
            reason=f"notional {notional:.6g} exceeds risk budget {max_notional:.6g}",
            min_notional=effective_min_notional,
            notional=notional,
            max_qty=rules.max_qty,
            min_qty=rules.min_qty,
            step_size=step,
        )
    return QuantityCheck(
        True,
        quantity=round(qty, decimals_from_step(step, rules.quantity_precision)),
        reason="ok",
        min_notional=effective_min_notional,
        notional=notional,
        max_qty=max_qty,
        min_qty=min_qty,
        step_size=step,
    )


def public_get_json(base_url: str, path: str, params: dict[str, Any], timeout: int = 5) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{base_url}{path}?{query}" if query else f"{base_url}{path}"
    if api_queue_client_enabled():
        data = queued_api_request(scope="public", label="order-rules", method="GET", path=path, url=url, timeout_sec=timeout + 5)
        if isinstance(data, dict) and data.get("code") is not None and str(data.get("code")) != "200":
            raise RuntimeError(str(data.get("msg") or data))
        return data
    wait_before_public_request("order-rules", url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {418, 429}:
            record_public_response("order-rules", url, exc.code, body)
        raise


def validate_market_price(base_url: str, rules: SymbolRules | None, symbol: str, side: str) -> QuantityCheck:
    if rules is None:
        return QuantityCheck(False, code="symbol_rules_missing", reason="exchangeInfo symbol rules missing")
    up = rules.percent_multiplier_up
    down = rules.percent_multiplier_down
    if up <= 0 or down <= 0:
        return QuantityCheck(True, reason="no_percent_price_rule")
    try:
        book = public_get_json(base_url, "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        premium = public_get_json(base_url, "/fapi/v1/premiumIndex", {"symbol": symbol})
        mark = float(premium.get("markPrice") or 0.0)
        bid = float(book.get("bidPrice") or 0.0)
        ask = float(book.get("askPrice") or 0.0)
    except Exception as exc:
        return QuantityCheck(True, code="market_price_check_unavailable", reason=str(exc)[:120])
    if mark <= 0:
        return QuantityCheck(True, code="mark_price_missing", reason="mark price missing")
    side_l = str(side).lower()
    counterparty = ask if side_l == "long" else bid
    lower = mark * down
    upper = mark * up
    if counterparty <= 0:
        return QuantityCheck(False, code="book_price_missing", reason="book counterparty price missing")
    if counterparty < lower or counterparty > upper:
        return QuantityCheck(
            False,
            code="percent_price_filter",
            reason=(
                f"counterparty {counterparty:.8g} outside percent-price range "
                f"{lower:.8g}-{upper:.8g} mark={mark:.8g}"
            ),
            notional=counterparty,
        )
    return QuantityCheck(True, reason="ok", notional=counterparty)
