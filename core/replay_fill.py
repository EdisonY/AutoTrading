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
    fee_bps: float = 5.0
    slippage_bps: float = 0.0
    conservative_intrabar: bool = True


@dataclass(frozen=True)
class ReplayFillResult:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    exit_reason: str
    exit_ts: str
    gross_pnl_usdt: float
    fee_usdt: float
    slippage_usdt: float
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


def _trailing_exit_price(req: ReplayFillRequest, *, entry_price: float, best_price: float) -> float | None:
    if req.trailing_stop_pct is None:
        return None
    trailing_pct = float(req.trailing_stop_pct or 0.0)
    if trailing_pct <= 0:
        return None
    activation = float(req.trailing_activation_pct or 0.0)
    side = req.side.lower()
    if side == "short":
        favorable_pct = (float(entry_price) - float(best_price)) / float(entry_price) * 100
        if favorable_pct < activation:
            return None
        return float(best_price) * (1 + trailing_pct / 100)
    favorable_pct = (float(best_price) - float(entry_price)) / float(entry_price) * 100
    if favorable_pct < activation:
        return None
    return float(best_price) * (1 - trailing_pct / 100)


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
    qty = float(req.quantity or 0.0)
    if qty <= 0:
        raise ValueError("quantity must be positive")
    side = str(req.side or "").lower()
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")

    entry_px = _exit_price_with_slippage(price=req.entry_price, side=side, is_entry=True, slippage_bps=req.slippage_bps)
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
        exit_reason=exit_reason,
        exit_ts=exit_ts,
        gross_pnl_usdt=round(gross, 8),
        fee_usdt=round(fee, 8),
        slippage_usdt=round(slippage, 8),
        net_pnl_usdt=round(gross - fee, 8),
        bars_held=bars_held,
    )
