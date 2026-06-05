"""Unified live execution wrapper for scanner clients."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from core.account_state import load_central_account_state
from core.position_utils import infer_position_side


Side = Literal["long", "short"]


class ConfirmationStateUnavailable(RuntimeError):
    pass


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


def scanner_order_enabled() -> bool:
    value = os.environ.get("SCANNER_ORDER_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def scanner_order_disabled_result(action: str, symbol: str, side: str, quantity: float = 0.0) -> ExecutionResult:
    detail = {
        "ok": False,
        "code": "scanner_order_disabled",
        "reason": "scanner order execution disabled by SCANNER_ORDER_ENABLED=0",
    }
    return ExecutionResult(
        False,
        action,
        symbol,
        side,
        quantity,
        code="scanner_order_disabled",
        message=detail["reason"],
        raw={"preflight": detail},
    )


def close_cancel_open_orders_enabled() -> bool:
    value = os.environ.get("SCANNER_CLOSE_CANCEL_OPEN_ORDERS_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


class ExecutionEngine:
    def __init__(
        self,
        client: Any,
        name: str = "",
        *,
        account_state_root: str | Path | None = None,
        central_confirmation_max_age_seconds: float | None = None,
        require_central_confirmation: bool | None = None,
    ):
        self.client = client
        self.name = name
        self.account_state_root = Path(
            account_state_root
            or os.environ.get("CENTRAL_ACCOUNT_STATE_ROOT")
            or Path(__file__).resolve().parents[1]
        )
        self.central_confirmation_max_age_seconds = float(
            central_confirmation_max_age_seconds
            if central_confirmation_max_age_seconds is not None
            else os.environ.get("CENTRAL_ACCOUNT_STATE_CONFIRM_MAX_AGE_SEC", "15")
        )
        self.central_target_max_age_seconds = float(
            os.environ.get(
                "CENTRAL_ACCOUNT_STATE_TARGET_MAX_AGE_SEC",
                os.environ.get("BINANCE_ACCOUNT_STATE_CACHE_MAX_AGE_SEC", "60"),
            )
        )
        if require_central_confirmation is None:
            value = os.environ.get("CENTRAL_ACCOUNT_STATE_CONFIRM_REQUIRE", "1").strip().lower()
            self.require_central_confirmation = value not in {"0", "false", "no", "off"}
        else:
            self.require_central_confirmation = bool(require_central_confirmation)
        value = os.environ.get("CENTRAL_ACCOUNT_STATE_CONFIRM_REST_FALLBACK_ENABLED", "1").strip().lower()
        self.confirm_rest_fallback_enabled = value not in {"0", "false", "no", "off"}

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
        if not scanner_order_enabled():
            return scanner_order_disabled_result("open", req.symbol, req.side, float(qty or 0.0))
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
            submitted_at = datetime.now(timezone.utc)
            if req.side == "long":
                raw = self.client.open_long(req.symbol, qty, req.leverage, req.take_profit, req.stop_loss)
            else:
                raw = self.client.open_short(req.symbol, qty, req.leverage, req.take_profit, req.stop_loss)
        except Exception as exc:
            return ExecutionResult(False, "open", req.symbol, req.side, qty, code="exception", message=str(exc))

        ok = self._is_success(raw)
        if not ok:
            if self._is_status_unknown(raw):
                try:
                    exec_qty = self._confirm_position_qty(req.symbol, req.side, attempts=3)
                except ConfirmationStateUnavailable as exc:
                    exec_qty = 0.0
                    confirm_error = str(exc)
                else:
                    confirm_error = ""
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
                if confirm_error:
                    return ExecutionResult(False, "open", req.symbol, req.side, qty, code="open_confirm_account_state_unavailable", message=confirm_error, raw=raw)
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
            confirm_cache = {"min_observed_at": submitted_at}
            try:
                confirmed_qty = self._confirm_position_qty(req.symbol, req.side, cache=confirm_cache)
            except ConfirmationStateUnavailable as exc:
                return ExecutionResult(
                    False,
                    "open",
                    req.symbol,
                    req.side,
                    qty,
                    order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                    status="OPEN_CONFIRM_ACCOUNT_STATE_UNAVAILABLE",
                    code="open_confirm_account_state_unavailable",
                    message=str(exc),
                    raw=raw,
                )
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
        if not scanner_order_enabled():
            return scanner_order_disabled_result("close", req.symbol, req.side, float(req.quantity or 0.0))
        if req.cancel_open_orders and close_cancel_open_orders_enabled():
            self._cancel_open_orders(req.symbol)
        confirm_cache: dict[str, Any] = {}
        qty = float(req.quantity or 0.0)
        try:
            close_target = self._close_target(req.symbol, req.side, confirm_cache)
        except ConfirmationStateUnavailable as exc:
            return ExecutionResult(False, "close", req.symbol, req.side, qty, code="close_confirm_account_state_unavailable", message=str(exc))
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
            submitted_at = datetime.now(timezone.utc)
            raw = self._submit_close(req.symbol, req.side, qty, close_target)
        except Exception as exc:
            return ExecutionResult(False, "close", req.symbol, req.side, qty, code="exception", message=str(exc))
        confirm_cache["min_observed_at"] = submitted_at
        if self._is_success(raw):
            remaining_qty = 0.0
            if req.confirm_position:
                try:
                    remaining_qty = self._confirm_position_qty(
                        req.symbol,
                        req.side,
                        attempts=req.confirm_attempts,
                        delay_seconds=1.0,
                        cache=confirm_cache,
                    )
                except ConfirmationStateUnavailable as exc:
                    return ExecutionResult(
                        False,
                        "close",
                        req.symbol,
                        req.side,
                        qty,
                        order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                        status="CLOSE_CONFIRM_ACCOUNT_STATE_UNAVAILABLE",
                        code="close_confirm_account_state_unavailable",
                        message=str(exc),
                        raw={"initial": raw},
                    )
                if remaining_qty > 0:
                    retry_raw = None
                    try:
                        retry_target = self._close_target(req.symbol, req.side, confirm_cache, force_refresh=False)
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
                        try:
                            remaining_qty = self._confirm_position_qty(
                                req.symbol,
                                req.side,
                                attempts=req.confirm_attempts,
                                delay_seconds=1.0,
                                cache=confirm_cache,
                            )
                        except ConfirmationStateUnavailable as exc:
                            return ExecutionResult(
                                False,
                                "close",
                                req.symbol,
                                req.side,
                                qty,
                                order_id=str(raw.get("orderId", "")) if isinstance(raw, dict) else "",
                                status="CLOSE_CONFIRM_ACCOUNT_STATE_UNAVAILABLE",
                                code="close_confirm_account_state_unavailable",
                                message=str(exc),
                                raw={"initial": raw, "retry": retry_raw},
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
            try:
                remaining_qty = self._confirm_position_qty(
                    req.symbol,
                    req.side,
                    attempts=req.confirm_attempts,
                    delay_seconds=1.0,
                    cache=confirm_cache,
                )
            except ConfirmationStateUnavailable as exc:
                return ExecutionResult(
                    False,
                    "close",
                    req.symbol,
                    req.side,
                    qty,
                    status="CLOSE_CONFIRM_ACCOUNT_STATE_UNAVAILABLE",
                    code="close_confirm_account_state_unavailable",
                    message=str(exc),
                    raw=raw,
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

    def _close_target(
        self,
        symbol: str,
        side: str,
        cache: dict[str, Any] | None = None,
        force_refresh: bool = True,
    ) -> dict[str, Any]:
        try:
            positions = (
                self._get_positions_for_confirmation(cache, force_refresh=force_refresh)
                if cache and cache.get("min_observed_at")
                else self._get_positions_for_close_target(cache, force_refresh=force_refresh)
            )
            for p in positions:
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
        except ConfirmationStateUnavailable:
            raise
        except Exception:
            return {}
        return {}

    def _get_positions_for_close_target(self, cache: dict[str, Any] | None, force_refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            cache is not None
            and not force_refresh
            and not cache.get("min_observed_at")
            and "positions" in cache
            and now - float(cache.get("positions_ts") or 0.0) <= 0.75
        ):
            return list(cache.get("positions") or [])
        central_positions = self._central_positions_for_confirmation(
            min_observed_at=None,
            max_age_seconds=self.central_target_max_age_seconds,
        )
        if central_positions is not None:
            if cache is not None:
                cache["positions"] = central_positions
                cache["positions_ts"] = now
                cache["positions_source"] = "central_account_state_target"
            return central_positions
        if self.require_central_confirmation:
            raise ConfirmationStateUnavailable("fresh central account state unavailable for close target")
        if hasattr(self.client, "invalidate_account_snapshot"):
            self.client.invalidate_account_snapshot()
        positions = list(self.client.get_positions())
        if cache is not None:
            cache["positions"] = positions
            cache["positions_ts"] = now
        return positions

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

    def _confirm_position_qty(
        self,
        symbol: str,
        side: str,
        attempts: int = 1,
        delay_seconds: float = 2.0,
        cache: dict[str, Any] | None = None,
    ) -> float:
        try:
            for attempt in range(max(1, attempts)):
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                positions = self._get_positions_for_confirmation(cache, force_refresh=False)
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
        except ConfirmationStateUnavailable:
            raise
        except Exception:
            return 0.0
        return 0.0

    def _get_positions_for_confirmation(self, cache: dict[str, Any] | None, force_refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            cache is not None
            and not force_refresh
            and not cache.get("min_observed_at")
            and "positions" in cache
            and now - float(cache.get("positions_ts") or 0.0) <= 0.75
        ):
            return list(cache.get("positions") or [])
        min_observed_at = cache.get("min_observed_at") if cache is not None else None
        central_positions = self._central_positions_for_confirmation(min_observed_at=min_observed_at)
        if central_positions is not None:
            if cache is not None:
                cache["positions"] = central_positions
                cache["positions_ts"] = now
                cache["positions_source"] = "central_account_state"
            return central_positions
        if self.confirm_rest_fallback_enabled:
            if hasattr(self.client, "invalidate_account_snapshot"):
                self.client.invalidate_account_snapshot()
            positions = list(self.client.get_positions())
            if cache is not None:
                cache["positions"] = positions
                cache["positions_ts"] = now
                cache["positions_source"] = "exchange_rest_confirmation_fallback"
            return positions
        if self.require_central_confirmation:
            raise ConfirmationStateUnavailable("fresh central account state unavailable for confirmation")
        if hasattr(self.client, "invalidate_account_snapshot"):
            self.client.invalidate_account_snapshot()
        positions = list(self.client.get_positions())
        if cache is not None:
            cache["positions"] = positions
            cache["positions_ts"] = now
        return positions

    def _central_positions_for_confirmation(
        self,
        *,
        min_observed_at: Any = None,
        max_age_seconds: float | None = None,
    ) -> list[dict[str, Any]] | None:
        if not self.name:
            return None
        required_ts = min_observed_at if isinstance(min_observed_at, datetime) else None
        state = load_central_account_state(
            self.account_state_root,
            self.name,
            max_age_seconds=(
                self.central_confirmation_max_age_seconds
                if max_age_seconds is None
                else float(max_age_seconds)
            ),
            min_observed_at=required_ts,
            allow_legacy=True,
        )
        if not state:
            return None
        return list(state.positions)

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
