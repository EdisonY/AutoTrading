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
    confirm_position: bool = True
    confirm_attempts: int = 3
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
            "preflight" in self.raw
            or "preflight_market_price" in self.raw
            or "preflight_exchange_rule" in self.raw
        )

    @property
    def preflight_detail(self) -> dict[str, Any]:
        if not isinstance(self.raw, dict):
            return {}
        detail = (
            self.raw.get("preflight")
            or self.raw.get("preflight_market_price")
            or self.raw.get("preflight_exchange_rule")
            or {}
        )
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
            if self._is_exchange_min_notional_reject(raw):
                return ExecutionResult(
                    False,
                    "open",
                    req.symbol,
                    req.side,
                    qty,
                    code="exchange_min_notional",
                    message=self._raw_message(raw) or "exchange rejected notional below minimum",
                    raw={
                        "preflight_exchange_rule": {
                            "ok": False,
                            "code": "exchange_min_notional",
                            "reason": self._raw_message(raw) or "exchange rejected notional below minimum",
                            "raw_code": self._raw_code(raw),
                            "raw": raw,
                        }
                    },
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
        if req.confirm_position:
            confirmed_qty = self._confirm_position_qty(req.symbol, req.side)
            if confirmed_qty > 0:
                exec_qty = confirmed_qty
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
        qty = float(req.quantity or 0.0)
        close_target = self._close_target(req.symbol, req.side)
        if qty <= 0:
            qty = float(close_target.get("quantity") or 0.0)
        if qty <= 0:
            return ExecutionResult(
                True,
                "close",
                req.symbol,
                req.side,
                0.0,
                status="ALREADY_CLOSED",
                message="no matching exchange position",
                raw={"remaining_qty": 0.0},
            )
        try:
            raw = self._submit_close(req.symbol, req.side, qty, close_target)
        except Exception as exc:
            return ExecutionResult(False, "close", req.symbol, req.side, qty, code="exception", message=str(exc))
        if self._is_success(raw):
            remaining_qty = 0.0
            if req.confirm_position:
                remaining_qty = self._confirm_position_qty(
                    req.symbol,
                    req.side,
                    attempts=req.confirm_attempts,
                    delay_seconds=1.0,
                )
                if remaining_qty > 0:
                    retry_raw = None
                    try:
                        retry_target = self._close_target(req.symbol, req.side)
                        retry_raw = self._submit_close(req.symbol, req.side, remaining_qty, retry_target)
                    except Exception as exc:
                        return ExecutionResult(
                            False,
                            "close",
                            req.symbol,
                            req.side,
                            qty,
                            order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                            status="CLOSE_CONFIRM_RETRY_EXCEPTION",
                            code="close_confirm_retry_exception",
                            message=str(exc),
                            raw={"initial": raw, "retry": retry_raw, "remaining_qty": remaining_qty},
                        )
                    if self._is_success(retry_raw):
                        remaining_qty = self._confirm_position_qty(
                            req.symbol,
                            req.side,
                            attempts=req.confirm_attempts,
                            delay_seconds=1.0,
                        )
                    if remaining_qty > 0:
                        return ExecutionResult(
                            False,
                            "close",
                            req.symbol,
                            req.side,
                            qty,
                            order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                            status="CLOSE_CONFIRM_FAILED",
                            code="close_confirm_failed",
                            message=f"position still present after close/retry: remaining_qty={remaining_qty:g}",
                            raw={"initial": raw, "retry": retry_raw, "remaining_qty": remaining_qty},
                        )
            return ExecutionResult(
                True,
                "close",
                req.symbol,
                req.side,
                qty,
                order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                status="CONFIRMED_CLOSED" if req.confirm_position else str(raw.get("status", "")) if isinstance(raw, dict) else "",
                raw={"initial": raw, "remaining_qty": remaining_qty} if req.confirm_position else raw,
            )
        if req.confirm_position:
            remaining_qty = self._confirm_position_qty(
                req.symbol,
                req.side,
                attempts=req.confirm_attempts,
                delay_seconds=1.0,
            )
            if remaining_qty <= 0:
                return ExecutionResult(
                    True,
                    "close",
                    req.symbol,
                    req.side,
                    qty,
                    status="CLOSE_ERROR_BUT_POSITION_GONE",
                    code=self._raw_code(raw),
                    message=self._raw_message(raw),
                    raw={"initial": raw, "remaining_qty": remaining_qty},
                )
        return ExecutionResult(
            False,
            "close",
            req.symbol,
            req.side,
            qty,
            status=str(raw.get("status", "")) if isinstance(raw, dict) else "",
            code=self._raw_code(raw),
            message=self._raw_message(raw),
            raw=raw,
        )

    def _close_target(self, symbol: str, side: str) -> dict[str, Any]:
        try:
            if hasattr(self.client, "invalidate_account_snapshot"):
                self.client.invalidate_account_snapshot()
            for p in self.client.get_positions():
                if p.get("symbol") != symbol:
                    continue
                amt = float(p.get("positionAmt", 0) or 0)
                if abs(amt) <= 0:
                    continue
                effective_side = infer_position_side(p)[0]
                if effective_side != side.upper():
                    continue
                raw_position_side = str(p.get("positionSide") or "").upper()
                return {
                    "quantity": abs(amt),
                    "position_side": raw_position_side,
                    "order_side": "SELL" if amt > 0 else "BUY",
                }
        except Exception:
            return {}
        return {}

    def _submit_close(self, symbol: str, side: str, quantity: float, close_target: dict[str, Any]) -> Any:
        kwargs = {"quantity": quantity}
        if close_target.get("position_side"):
            kwargs["position_side"] = close_target["position_side"]
        if close_target.get("order_side"):
            kwargs["order_side"] = close_target["order_side"]
        try:
            return self.client.close_position(symbol, side, **kwargs)
        except TypeError:
            return self.client.close_position(symbol, side, quantity=quantity)

    def _cancel_open_orders(self, symbol: str) -> None:
        try:
            if hasattr(self.client, "_delete"):
                self.client._delete(symbol)
        except Exception:
            pass

    def _confirm_position_qty(self, symbol: str, side: str, attempts: int = 1, delay_seconds: float = 2.0) -> float:
        try:
            for _ in range(max(1, attempts)):
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
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

    @classmethod
    def _is_exchange_min_notional_reject(cls, raw: Any) -> bool:
        code = cls._raw_code(raw)
        msg = cls._raw_message(raw).lower()
        return code == "-4164" or "notional must be no smaller than 5" in msg
