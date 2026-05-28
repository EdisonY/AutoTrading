"""Unified live execution wrapper for scanner clients."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from core.position_utils import infer_position_side


Side = Literal["long", "short"]


@dataclass(slots=True)
class OpenRequest:
    symbol: str
    side: Side
    price: float
    risk_usdt: float
    leverage: int
    take_profit: float
    stop_loss: float
    quantity: float | None = None
    max_quantity: float | None = None
    confirm_position: bool = True
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CloseRequest:
    symbol: str
    side: Side
    quantity: float = 0.0
    cancel_open_orders: bool = False
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionResult:
    success: bool
    action: str
    symbol: str
    side: str
    quantity: float = 0.0
    order_id: str = ""
    status: str = ""
    code: str = ""
    message: str = ""
    raw: Any = None

    @property
    def reason(self) -> str:
        if self.success:
            return ""
        parts = [x for x in (self.code, self.message or str(self.raw)[:160]) if x]
        return ": ".join(parts) if parts else "execution_failed"

    @property
    def preflight_rejected(self) -> bool:
        return isinstance(self.raw, dict) and (
            "preflight" in self.raw or "preflight_market_price" in self.raw
        )

    @property
    def preflight_detail(self) -> dict[str, Any]:
        if not isinstance(self.raw, dict):
            return {}
        detail = self.raw.get("preflight") or self.raw.get("preflight_market_price") or {}
        return detail if isinstance(detail, dict) else {}


class ExecutionEngine:
    def __init__(self, client: Any, name: str = ""):
        self.client = client
        self.name = name

    def calc_quantity(self, symbol: str, price: float, risk_usdt: float, leverage: int, max_quantity: float | None = None) -> float:
        qty = float(self.client.calc_size(symbol, price, risk_usdt, leverage))
        if max_quantity is not None and qty > max_quantity:
            qty = float(max_quantity)
        if hasattr(self.client, "validate_order_quantity"):
            check = self.client.validate_order_quantity(symbol, qty, price, risk_usdt, leverage)
            if not check.get("ok"):
                return 0.0
            qty = float(check.get("quantity") or qty)
        return qty

    def open_position(self, req: OpenRequest) -> ExecutionResult:
        qty = req.quantity
        if qty is None:
            qty = self.calc_quantity(req.symbol, req.price, req.risk_usdt, req.leverage, req.max_quantity)
        if qty <= 0:
            return ExecutionResult(False, "open", req.symbol, req.side, qty, code="qty<=0", message="quantity too small")
        if hasattr(self.client, "validate_order_quantity"):
            check = self.client.validate_order_quantity(req.symbol, qty, req.price, req.risk_usdt, req.leverage)
            if not check.get("ok"):
                return ExecutionResult(
                    False,
                    "open",
                    req.symbol,
                    req.side,
                    float(check.get("quantity") or qty),
                    code=str(check.get("code") or "preflight_rejected"),
                    message=str(check.get("reason") or "order rule rejected"),
                    raw={"preflight": check},
                )
            qty = float(check.get("quantity") or qty)
        if hasattr(self.client, "validate_market_order_price"):
            price_check = self.client.validate_market_order_price(req.symbol, req.side)
            if not price_check.get("ok"):
                return ExecutionResult(
                    False,
                    "open",
                    req.symbol,
                    req.side,
                    qty,
                    code=str(price_check.get("code") or "market_price_rejected"),
                    message=str(price_check.get("reason") or "market order price rejected"),
                    raw={"preflight_market_price": price_check},
                )
        try:
            if req.side == "long":
                raw = self.client.open_long(req.symbol, qty, req.leverage, req.take_profit, req.stop_loss)
            else:
                raw = self.client.open_short(req.symbol, qty, req.leverage, req.take_profit, req.stop_loss)
        except Exception as exc:
            return ExecutionResult(False, "open", req.symbol, req.side, qty, code="exception", message=str(exc))

        ok = self._is_success(raw)
        if not ok:
            if self._is_status_unknown(raw):
                exec_qty = self._confirm_position_qty(req.symbol, req.side, attempts=3)
                if exec_qty > 0:
                    return ExecutionResult(
                        True,
                        "open",
                        req.symbol,
                        req.side,
                        exec_qty,
                        order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                        status="UNKNOWN_CONFIRMED_POSITION",
                        code=self._raw_code(raw),
                        message=self._raw_message(raw),
                        raw=raw,
                    )
            return ExecutionResult(
                False,
                "open",
                req.symbol,
                req.side,
                qty,
                status=str(raw.get("status", "")) if isinstance(raw, dict) else "",
                code=self._raw_code(raw),
                message=self._raw_message(raw),
                raw=raw,
            )

        exec_qty = self._executed_quantity(raw)
        if exec_qty <= 0 and req.confirm_position:
            exec_qty = self._confirm_position_qty(req.symbol, req.side)
        if exec_qty <= 0:
            exec_qty = qty
        return ExecutionResult(
            True,
            "open",
            req.symbol,
            req.side,
            exec_qty,
            order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
            status=str(raw.get("status", "")) if isinstance(raw, dict) else "",
            raw=raw,
        )

    def close_position(self, req: CloseRequest) -> ExecutionResult:
        if req.cancel_open_orders:
            self._cancel_open_orders(req.symbol)
        try:
            raw = self.client.close_position(req.symbol, req.side, quantity=req.quantity)
        except Exception as exc:
            return ExecutionResult(False, "close", req.symbol, req.side, req.quantity, code="exception", message=str(exc))
        if self._is_success(raw):
            return ExecutionResult(
                True,
                "close",
                req.symbol,
                req.side,
                req.quantity,
                order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                status=str(raw.get("status", "")) if isinstance(raw, dict) else "",
                raw=raw,
            )
        if req.quantity > 0:
            try:
                raw2 = self.client.close_position(req.symbol, req.side)
            except Exception as exc:
                return ExecutionResult(False, "close", req.symbol, req.side, req.quantity, code="exception", message=str(exc), raw=raw)
            if self._is_success(raw2):
                return ExecutionResult(
                    True,
                    "close",
                    req.symbol,
                    req.side,
                    req.quantity,
                    order_id=str(raw2.get("orderId", "")) if isinstance(raw2, dict) else "",
                    status=str(raw2.get("status", "")) if isinstance(raw2, dict) else "",
                    raw=raw2,
                )
        return ExecutionResult(
            False,
            "close",
            req.symbol,
            req.side,
            req.quantity,
            status=str(raw.get("status", "")) if isinstance(raw, dict) else "",
            code=str(raw.get("code", "")) if isinstance(raw, dict) else "",
            message=str(raw.get("msg", raw))[:240] if isinstance(raw, dict) else str(raw)[:240],
            raw=raw,
        )

    def _cancel_open_orders(self, symbol: str) -> None:
        try:
            if hasattr(self.client, "_delete"):
                self.client._delete(symbol)
        except Exception:
            pass

    def _confirm_position_qty(self, symbol: str, side: str, attempts: int = 1) -> float:
        try:
            for _ in range(max(1, attempts)):
                time.sleep(2)
                if hasattr(self.client, "invalidate_account_snapshot"):
                    self.client.invalidate_account_snapshot()
                positions = self.client.get_positions()
                for p in positions:
                    if p.get("symbol") != symbol:
                        continue
                    amt = float(p.get("positionAmt", 0) or 0)
                    pos_side = infer_position_side(p)[0]
                    if pos_side == side.upper():
                        qty = abs(amt)
                    else:
                        qty = 0.0
                    if qty > 0:
                        return qty
        except Exception:
            return 0.0
        return 0.0

    @staticmethod
    def _is_success(raw: Any) -> bool:
        if not isinstance(raw, dict):
            return False
        if raw.get("orderId"):
            return True
        status = str(raw.get("status", "")).upper()
        if status in ("NEW", "FILLED", "PARTIALLY_FILLED"):
            return True
        code = raw.get("code")
        return code in (None, "", 0, "0", 200, "200") and "msg" not in raw

    @staticmethod
    def _executed_quantity(raw: Any) -> float:
        if not isinstance(raw, dict):
            return 0.0
        qty = 0.0
        for fill in raw.get("fills", []) or []:
            qty += float(fill.get("qty", fill.get("executedQty", 0)) or 0)
        if qty > 0:
            return qty
        return float(raw.get("executedQty", raw.get("origQty", 0)) or 0)

    @staticmethod
    def _raw_message(raw: Any) -> str:
        if not isinstance(raw, dict):
            return str(raw)[:240]
        return str(raw.get("msg", raw))[:240]

    @staticmethod
    def _raw_code(raw: Any) -> str:
        if not isinstance(raw, dict):
            return ""
        code = str(raw.get("code", ""))
        msg = str(raw.get("msg", ""))
        for marker in ('"code":', "'code':"):
            if marker in msg:
                tail = msg.split(marker, 1)[1].lstrip(" '")
                inner = ""
                for ch in tail:
                    if ch in "-0123456789":
                        inner += ch
                    elif inner:
                        break
                if inner:
                    return inner
        return code

    @classmethod
    def _is_status_unknown(cls, raw: Any) -> bool:
        code = cls._raw_code(raw)
        msg = cls._raw_message(raw).lower()
        return code == "-1007" or "status unknown" in msg or "execution status unknown" in msg
