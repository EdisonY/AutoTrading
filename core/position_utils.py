"""Utilities for normalizing exchange position rows."""

from __future__ import annotations

from typing import Any


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def raw_position_side(row: dict[str, Any]) -> str:
    side = str(row.get("positionSide") or "").upper()
    return side if side in ("LONG", "SHORT") else ""


def infer_position_side(row: dict[str, Any]) -> tuple[str, str]:
    """Return (effective_side, source) for a Binance positionRisk row.

    Some Binance testnet rows have inconsistent `positionSide` versus
    `positionAmt`/`notional`.  When raw unrealized PnL is available, matching it
    against entry/mark prices is the most reliable way to infer the live
    economic direction.
    """

    qty = to_float(row.get("positionAmt"))
    qty_abs = abs(qty)
    entry = to_float(row.get("entryPrice"))
    mark = to_float(row.get("markPrice"))
    raw_upnl = optional_float(row.get("unRealizedProfit", row.get("unrealizedProfit")))
    side = raw_position_side(row)

    if qty_abs > 0 and entry > 0 and mark > 0 and raw_upnl is not None:
        long_upnl = (mark - entry) * qty_abs
        short_upnl = (entry - mark) * qty_abs
        long_diff = abs(long_upnl - raw_upnl)
        short_diff = abs(short_upnl - raw_upnl)
        tolerance = max(0.05, abs(raw_upnl) * 0.02, qty_abs * mark * 0.0005)
        if min(long_diff, short_diff) <= tolerance:
            return ("LONG", "pnl_match") if long_diff <= short_diff else ("SHORT", "pnl_match")

    notional = optional_float(row.get("notional", row.get("notionalValue")))
    if notional is not None and notional != 0:
        return ("LONG" if notional > 0 else "SHORT", "notional_sign")
    if qty != 0:
        return ("LONG" if qty > 0 else "SHORT", "quantity_sign")
    if side:
        return side, "position_side"
    return "LONG", "fallback"


def position_unrealized_pnl(row: dict[str, Any], side: str | None = None) -> tuple[float, str]:
    raw_upnl = optional_float(row.get("unRealizedProfit", row.get("unrealizedProfit")))
    if raw_upnl is not None:
        return raw_upnl, "exchange_raw"
    qty = abs(to_float(row.get("positionAmt")))
    entry = to_float(row.get("entryPrice"))
    mark = to_float(row.get("markPrice"))
    effective_side = (side or infer_position_side(row)[0]).upper()
    if qty > 0 and entry > 0 and mark > 0:
        if effective_side == "SHORT":
            return (entry - mark) * qty, "calculated"
        return (mark - entry) * qty, "calculated"
    return 0.0, "missing"


def leveraged_loss_pct(row: dict[str, Any], side: str | None = None) -> float:
    entry = to_float(row.get("entryPrice"))
    mark = to_float(row.get("markPrice"))
    lev = to_float(row.get("leverage"), 4.0)
    effective_side = (side or infer_position_side(row)[0]).upper()
    if entry <= 0 or mark <= 0 or lev <= 0:
        return 0.0
    if effective_side == "SHORT":
        return max(0.0, (mark - entry) / entry * 100 * lev)
    return max(0.0, (entry - mark) / entry * 100 * lev)
