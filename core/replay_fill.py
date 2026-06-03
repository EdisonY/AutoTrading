"""Deterministic replay fill helpers for counterfactual open outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ReplayBar:
    ts: str
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "ReplayBar":
        return cls(
            ts=str(payload.get("ts") or payload.get("time") or ""),
            open=float(payload.get("open")),
            high=float(payload.get("high")),
            low=float(payload.get("low")),
            close=float(payload.get("close")),
        )


@dataclass(frozen=True)
class ReplayFillRequest:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop_pct: float | None = None
    trailing_activation_pct: float = 0.0
    atr: float | None = None
    trailing_stop_atr: float | None = None
    trailing_activation_atr: float = 0.0
    fee_bps: float = 5.0
    slippage_bps: float = 0.0
    max_fill_quantity: float | None = None
    max_fill_notional_usdt: float | None = None
    allow_partial_fill: bool = True
    entry_order_book: dict[str, Any] | None = None
    entry_order_book_max_levels: int | None = None
    entry_order_book_liquidity_factor: float = 1.0
    entry_order_book_queue_ahead_quantity: float = 0.0
    entry_market_impact_bps: float = 0.0
    conservative_intrabar: bool = True


@dataclass(frozen=True)
class ReplayFillResult:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    requested_quantity: float
    unfilled_quantity: float
    fill_ratio: float
    partial_fill: bool
    fill_status: str
    entry_fill_source: str
    order_book_levels_used: int
    order_book_available_quantity: float
    order_book_fill_ratio: float
    order_book_queue_ahead_quantity: float
    exit_reason: str
    exit_ts: str
    gross_pnl_usdt: float
    fee_usdt: float
    slippage_usdt: float
    depth_slippage_usdt: float
    market_impact_usdt: float
    net_pnl_usdt: float
    bars_held: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _exit_price_with_slippage(*, price: float, side: str, is_entry: bool, slippage_bps: float) -> float:
    rate = float(slippage_bps or 0.0) / 10_000.0
    side_key = str(side or "").lower()
    if rate <= 0:
        return float(price)
    if side_key == "long":
        return float(price) * (1 + rate if is_entry else 1 - rate)
    return float(price) * (1 - rate if is_entry else 1 + rate)


def _gross_pnl(*, side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    if str(side or "").lower() == "short":
        return (float(entry_price) - float(exit_price)) * float(quantity)
    return (float(exit_price) - float(entry_price)) * float(quantity)


def _effective_quantity(req: ReplayFillRequest, *, entry_price: float) -> tuple[float, float, bool, str]:
    requested = float(req.quantity or 0.0)
    caps = [requested]
    if req.max_fill_quantity is not None:
        cap_qty = float(req.max_fill_quantity or 0.0)
        if cap_qty < 0:
            raise ValueError("max_fill_quantity cannot be negative")
        caps.append(cap_qty)
    if req.max_fill_notional_usdt is not None:
        cap_notional = float(req.max_fill_notional_usdt or 0.0)
        if cap_notional < 0:
            raise ValueError("max_fill_notional_usdt cannot be negative")
        caps.append(cap_notional / float(entry_price))
    effective = min(caps)
    if effective <= 0:
        raise ValueError("fillable quantity must be positive")
    partial = effective < requested
    if partial and not req.allow_partial_fill:
        raise ValueError("partial fill required but allow_partial_fill is false")
    fill_ratio = effective / requested if requested > 0 else 0.0
    status = "partial" if partial else "filled"
    return effective, fill_ratio, partial, status


def _book_level_price_qty(level: Any) -> tuple[float, float] | None:
    try:
        if isinstance(level, dict):
            price = float(level.get("price", level.get("p")))
            qty = float(level.get("quantity", level.get("qty", level.get("q"))))
        else:
            price = float(level[0])
            qty = float(level[1])
    except Exception:
        return None
    if price <= 0 or qty <= 0:
        return None
    return price, qty


def _entry_book_levels(req: ReplayFillRequest, *, side: str) -> list[tuple[float, float]]:
    book = req.entry_order_book
    if not isinstance(book, dict):
        return []
    key = "asks" if side == "long" else "bids"
    levels = [_book_level_price_qty(level) for level in (book.get(key) or [])]
    parsed = [level for level in levels if level is not None]
    parsed = sorted(parsed, key=lambda item: item[0], reverse=(side == "short"))
    max_levels = req.entry_order_book_max_levels
    if max_levels is not None:
        if int(max_levels) <= 0:
            raise ValueError("entry_order_book_max_levels must be positive")
        parsed = parsed[: int(max_levels)]
    liquidity_factor = float(req.entry_order_book_liquidity_factor)
    if liquidity_factor < 0 or liquidity_factor > 1:
        raise ValueError("entry_order_book_liquidity_factor must be between 0 and 1")
    if liquidity_factor != 1.0:
        parsed = [(price, qty * liquidity_factor) for price, qty in parsed]
    return [(price, qty) for price, qty in parsed if qty > 0]


def _depth_entry_fill(
    req: ReplayFillRequest,
    *,
    side: str,
    target_quantity: float,
    reference_entry_price: float,
) -> tuple[float, float, float, int, str, float, float]:
    queue_ahead = float(req.entry_order_book_queue_ahead_quantity or 0.0)
    if queue_ahead < 0:
        raise ValueError("entry_order_book_queue_ahead_quantity cannot be negative")
    levels = _entry_book_levels(req, side=side)
    if not levels:
        if isinstance(req.entry_order_book, dict):
            raise ValueError("order book has no fillable liquidity")
        return reference_entry_price, target_quantity, 0.0, 0, "synthetic", 0.0, 0.0
    if queue_ahead:
        queue_remaining = queue_ahead
        effective_levels: list[tuple[float, float]] = []
        for price, available_qty in levels:
            if queue_remaining >= available_qty:
                queue_remaining -= available_qty
                continue
            effective_qty = available_qty - queue_remaining
            queue_remaining = 0.0
            if effective_qty > 0:
                effective_levels.append((price, effective_qty))
        levels = effective_levels
        if not levels:
            raise ValueError("order book queue ahead consumes all fillable liquidity")
    remaining = float(target_quantity)
    filled = 0.0
    notional = 0.0
    levels_used = 0
    available_quantity = sum(qty for _price, qty in levels)
    for price, available_qty in levels:
        if remaining <= 0:
            break
        take_qty = min(remaining, available_qty)
        filled += take_qty
        notional += take_qty * price
        remaining -= take_qty
        levels_used += 1
    if filled <= 0:
        raise ValueError("order book has no fillable liquidity")
    if filled < target_quantity and not req.allow_partial_fill:
        raise ValueError("partial fill required but allow_partial_fill is false")
    avg_price = notional / filled
    if side == "short":
        depth_cost = max(0.0, reference_entry_price - avg_price) * filled
    else:
        depth_cost = max(0.0, avg_price - reference_entry_price) * filled
    book_fill_ratio = filled / target_quantity if target_quantity > 0 else 0.0
    return avg_price, filled, depth_cost, levels_used, "order_book", available_quantity, book_fill_ratio


def _entry_price_with_market_impact(*, price: float, side: str, impact_bps: float) -> float:
    impact = float(impact_bps or 0.0)
    if impact < 0:
        raise ValueError("entry_market_impact_bps cannot be negative")
    if impact <= 0:
        return float(price)
    rate = impact / 10_000.0
    if side == "short":
        return float(price) * (1 - rate)
    return float(price) * (1 + rate)


def _trailing_exit_price(req: ReplayFillRequest, *, entry_price: float, best_price: float) -> float | None:
    side = req.side.lower()
    candidates: list[float] = []

    trailing_pct = float(req.trailing_stop_pct or 0.0) if req.trailing_stop_pct is not None else 0.0
    if trailing_pct > 0:
        activation = float(req.trailing_activation_pct or 0.0)
        if side == "short":
            favorable_pct = (float(entry_price) - float(best_price)) / float(entry_price) * 100
            if favorable_pct >= activation:
                candidates.append(float(best_price) * (1 + trailing_pct / 100))
        else:
            favorable_pct = (float(best_price) - float(entry_price)) / float(entry_price) * 100
            if favorable_pct >= activation:
                candidates.append(float(best_price) * (1 - trailing_pct / 100))

    trailing_atr = float(req.trailing_stop_atr or 0.0) if req.trailing_stop_atr is not None else 0.0
    if trailing_atr > 0:
        atr = float(req.atr or 0.0)
        activation_atr = float(req.trailing_activation_atr or 0.0)
        if side == "short":
            favorable_move = float(entry_price) - float(best_price)
            if favorable_move >= atr * activation_atr:
                candidates.append(float(best_price) + atr * trailing_atr)
        else:
            favorable_move = float(best_price) - float(entry_price)
            if favorable_move >= atr * activation_atr:
                candidates.append(float(best_price) - atr * trailing_atr)

    if not candidates:
        return None
    return min(candidates) if side == "short" else max(candidates)


def _bar_exit(req: ReplayFillRequest, bar: ReplayBar, trailing_price: float | None = None) -> tuple[str, float] | None:
    side = req.side.lower()
    if side == "short":
        hit_stop = req.stop_loss is not None and bar.high >= float(req.stop_loss)
        hit_take = req.take_profit is not None and bar.low <= float(req.take_profit)
        hit_trailing = trailing_price is not None and bar.high >= float(trailing_price)
    else:
        hit_stop = req.stop_loss is not None and bar.low <= float(req.stop_loss)
        hit_take = req.take_profit is not None and bar.high >= float(req.take_profit)
        hit_trailing = trailing_price is not None and bar.low <= float(trailing_price)
    if req.conservative_intrabar:
        if hit_stop:
            return "stop_loss", float(req.stop_loss)
        if hit_trailing:
            return "trailing_stop", float(trailing_price)
        if hit_take:
            return "take_profit", float(req.take_profit)
        return None
    if hit_take:
        return "take_profit", float(req.take_profit)
    if hit_stop:
        return "stop_loss", float(req.stop_loss)
    if hit_trailing:
        return "trailing_stop", float(trailing_price)
    return None


def simulate_replay_fill(req: ReplayFillRequest, bars: Iterable[ReplayBar | dict[str, Any]]) -> ReplayFillResult:
    parsed_bars = [bar if isinstance(bar, ReplayBar) else ReplayBar.from_mapping(bar) for bar in bars]
    if not parsed_bars:
        raise ValueError("bars required")
    requested_qty = float(req.quantity or 0.0)
    if requested_qty <= 0:
        raise ValueError("quantity must be positive")
    side = str(req.side or "").lower()
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if req.trailing_stop_atr is not None and float(req.trailing_stop_atr or 0.0) > 0 and float(req.atr or 0.0) <= 0:
        raise ValueError("atr must be positive when trailing_stop_atr is enabled")

    reference_entry_px = _exit_price_with_slippage(price=req.entry_price, side=side, is_entry=True, slippage_bps=req.slippage_bps)
    target_qty, _, _, _ = _effective_quantity(req, entry_price=reference_entry_px)
    raw_entry_px, qty, depth_slippage, levels_used, fill_source, book_available_qty, book_fill_ratio = _depth_entry_fill(
        req,
        side=side,
        target_quantity=target_qty,
        reference_entry_price=float(req.entry_price),
    )
    impacted_entry_px = _entry_price_with_market_impact(
        price=raw_entry_px,
        side=side,
        impact_bps=req.entry_market_impact_bps,
    )
    market_impact = abs(impacted_entry_px - raw_entry_px) * qty
    entry_px = _exit_price_with_slippage(price=impacted_entry_px, side=side, is_entry=True, slippage_bps=req.slippage_bps)
    partial = qty < requested_qty
    if partial and not req.allow_partial_fill:
        raise ValueError("partial fill required but allow_partial_fill is false")
    fill_ratio = qty / requested_qty if requested_qty > 0 else 0.0
    fill_status = "partial" if partial else "filled"
    exit_reason = "end_of_window"
    exit_price = parsed_bars[-1].close
    exit_ts = parsed_bars[-1].ts
    bars_held = len(parsed_bars)
    best_price = entry_px
    for idx, bar in enumerate(parsed_bars, start=1):
        if side == "short":
            best_price = min(best_price, float(bar.low))
        else:
            best_price = max(best_price, float(bar.high))
        trailing_price = _trailing_exit_price(req, entry_price=entry_px, best_price=best_price)
        hit = _bar_exit(req, bar, trailing_price)
        if hit:
            exit_reason, exit_price = hit
            exit_ts = bar.ts
            bars_held = idx
            break

    exit_px = _exit_price_with_slippage(price=exit_price, side=side, is_entry=False, slippage_bps=req.slippage_bps)
    gross = _gross_pnl(side=side, entry_price=entry_px, exit_price=exit_px, quantity=qty)
    notional = (abs(entry_px * qty) + abs(exit_px * qty))
    fee = notional * float(req.fee_bps or 0.0) / 10_000.0
    slippage = abs((entry_px - float(req.entry_price)) * qty) + abs((exit_px - float(exit_price)) * qty)
    return ReplayFillResult(
        symbol=req.symbol,
        side=side,
        entry_price=round(entry_px, 10),
        exit_price=round(exit_px, 10),
        quantity=qty,
        requested_quantity=round(requested_qty, 10),
        unfilled_quantity=round(max(0.0, requested_qty - qty), 10),
        fill_ratio=round(fill_ratio, 8),
        partial_fill=partial,
        fill_status=fill_status,
        entry_fill_source=fill_source,
        order_book_levels_used=levels_used,
        order_book_available_quantity=round(book_available_qty, 10),
        order_book_fill_ratio=round(book_fill_ratio, 8),
        order_book_queue_ahead_quantity=round(float(req.entry_order_book_queue_ahead_quantity or 0.0), 10),
        exit_reason=exit_reason,
        exit_ts=exit_ts,
        gross_pnl_usdt=round(gross, 8),
        fee_usdt=round(fee, 8),
        slippage_usdt=round(slippage, 8),
        depth_slippage_usdt=round(depth_slippage, 8),
        market_impact_usdt=round(market_impact, 8),
        net_pnl_usdt=round(gross - fee, 8),
        bars_held=bars_held,
    )
