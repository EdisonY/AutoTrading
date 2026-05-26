"""A small paper broker for replay and strategy dry-runs.

It is deliberately simple: market orders fill at the supplied price, positions
are netted per (symbol, side), and realized PnL is recorded on close.  This gives
review tooling a live/backtest-compatible broker surface without touching real
exchange clients.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal


Side = Literal["long", "short"]


@dataclass(slots=True)
class PaperOrder:
    order_id: int
    symbol: str
    side: Side
    qty: float
    price: float
    leverage: float = 1.0
    reason: str = ""
    time: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass(slots=True)
class PaperPosition:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    leverage: float
    reason: str = ""
    opened_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass(slots=True)
class PaperTrade:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    pnl_usd: float
    pnl_pct: float
    reason: str
    opened_at: str
    closed_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class PaperBroker:
    def __init__(self, starting_cash: float = 10_000.0, fee_rate: float = 0.0004):
        self.cash = float(starting_cash)
        self.fee_rate = float(fee_rate)
        self._next_order_id = 1
        self.positions: dict[tuple[str, Side], PaperPosition] = {}
        self.orders: list[PaperOrder] = []
        self.trades: list[PaperTrade] = []

    def open_market(self, symbol: str, side: Side, qty: float, price: float, leverage: float = 1.0, reason: str = "") -> PaperOrder:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        order = PaperOrder(self._next_order_id, symbol, side, float(qty), float(price), float(leverage), reason)
        self._next_order_id += 1
        key = (symbol, side)
        existing = self.positions.get(key)
        if existing:
            total_qty = existing.qty + qty
            existing.entry_price = (existing.entry_price * existing.qty + price * qty) / total_qty
            existing.qty = total_qty
            existing.reason = f"{existing.reason}; {reason}".strip("; ")
        else:
            self.positions[key] = PaperPosition(symbol, side, float(qty), float(price), float(leverage), reason)
        self.cash -= qty * price * self.fee_rate
        self.orders.append(order)
        return order

    def close_market(self, symbol: str, side: Side, price: float, reason: str = "", qty: float | None = None) -> PaperTrade:
        key = (symbol, side)
        pos = self.positions.get(key)
        if not pos:
            raise KeyError(f"no paper position: {symbol} {side}")
        close_qty = pos.qty if qty is None else min(float(qty), pos.qty)
        if side == "long":
            pnl = (price - pos.entry_price) * close_qty
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
        else:
            pnl = (pos.entry_price - price) * close_qty
            pnl_pct = (pos.entry_price - price) / pos.entry_price * 100 * pos.leverage
        fee = close_qty * price * self.fee_rate
        self.cash += pnl - fee
        trade = PaperTrade(symbol, side, close_qty, pos.entry_price, float(price), pnl - fee, pnl_pct, reason, pos.opened_at)
        self.trades.append(trade)
        pos.qty -= close_qty
        if pos.qty <= 1e-12:
            del self.positions[key]
        return trade

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        unrealized = 0.0
        for pos in self.positions.values():
            mark = marks.get(pos.symbol, pos.entry_price)
            if pos.side == "long":
                unrealized += (mark - pos.entry_price) * pos.qty
            else:
                unrealized += (pos.entry_price - mark) * pos.qty
        return self.cash + unrealized

    def snapshot(self) -> dict:
        return {
            "cash": self.cash,
            "equity": self.equity(),
            "positions": [asdict(p) for p in self.positions.values()],
            "orders": [asdict(o) for o in self.orders],
            "trades": [asdict(t) for t in self.trades],
        }

