"""Persistent paper exchange ledger for full-system dry trading.

The ledger is the paper source of truth. It is intentionally local-file based:
no network, no Binance order path, and no hidden exchange state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_FEE_RATE = 0.0004
DEFAULT_STARTING_EQUITY = 100_000.0
STRATEGIES = ("A/v11", "B/v16", "C/v14")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def position_key(strategy: str, symbol: str, side: str) -> str:
    return f"{strategy}|{symbol.upper()}|{side.lower()}"


@dataclass(slots=True)
class PaperFill:
    action: str
    strategy: str
    symbol: str
    side: str
    qty: float
    price: float
    leverage: float
    fee: float
    order_id: str
    reason: str = ""
    funding: float = 0.0
    realized_pnl: float = 0.0


class PaperExchange:
    def __init__(
        self,
        root: str | Path,
        *,
        fee_rate: float = DEFAULT_FEE_RATE,
        starting_equity: float = DEFAULT_STARTING_EQUITY,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / "runtime" / "paper_exchange_state.json"
        self.latest_path = self.root / "runtime" / "paper_exchange_latest.json"
        self.fee_rate = float(fee_rate)
        self.starting_equity = float(starting_equity)

    def load(self) -> dict[str, Any]:
        state = load_json(self.path)
        if not state:
            state = {
                "version": 1,
                "mode": "paper_exchange",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "fee_rate": self.fee_rate,
                "starting_equity": self.starting_equity,
                "accounts": {
                    s: {
                        "cash": self.starting_equity,
                        "fees_paid": 0.0,
                        "funding_paid": 0.0,
                        "realized_pnl": 0.0,
                    }
                    for s in STRATEGIES
                },
                "positions": {},
                "fills": [],
            }
        state.setdefault("accounts", {})
        for strategy in STRATEGIES:
            state["accounts"].setdefault(
                strategy,
                {"cash": self.starting_equity, "fees_paid": 0.0, "funding_paid": 0.0, "realized_pnl": 0.0},
            )
        state.setdefault("positions", {})
        state.setdefault("fills", [])
        return state

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = utc_now()
        atomic_write(self.path, state)
        summary = self.summarize(state)
        atomic_write(self.latest_path, summary)
        return summary

    def open_market(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        leverage: float,
        order_id: str,
        reason: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        qty = safe_float(qty)
        price = safe_float(price)
        leverage = max(1.0, safe_float(leverage, 1.0))
        if qty <= 0 or price <= 0:
            raise ValueError("paper open requires positive qty and price")
        state = self.load()
        account = state["accounts"].setdefault(strategy, {"cash": self.starting_equity, "fees_paid": 0.0, "funding_paid": 0.0, "realized_pnl": 0.0})
        key = position_key(strategy, symbol, side)
        notional = qty * price
        fee = notional * self.fee_rate
        pos = state["positions"].get(key)
        if pos:
            old_qty = safe_float(pos.get("qty"))
            new_qty = old_qty + qty
            pos["entry_price"] = ((safe_float(pos.get("entry_price")) * old_qty) + notional) / new_qty
            pos["qty"] = new_qty
            pos["notional"] = new_qty * price
        else:
            pos = {
                "strategy": strategy,
                "symbol": symbol.upper(),
                "side": side.lower(),
                "qty": qty,
                "entry_price": price,
                "mark_price": price,
                "leverage": leverage,
                "opened_at": utc_now(),
                "reason": reason,
                "order_id": order_id,
                "fees_paid": 0.0,
                "funding_paid": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "notional": notional,
                "margin": notional / leverage,
                "mark_source": "entry",
                "context": context or {},
            }
            state["positions"][key] = pos
        pos["fees_paid"] = safe_float(pos.get("fees_paid")) + fee
        account["cash"] = safe_float(account.get("cash"), self.starting_equity) - fee
        account["fees_paid"] = safe_float(account.get("fees_paid")) + fee
        self._record_fill(state, PaperFill("OPEN", strategy, symbol.upper(), side.lower(), qty, price, leverage, fee, order_id, reason))
        return self.save(state)

    def close_market(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        order_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        price = safe_float(price)
        if price <= 0:
            raise ValueError("paper close requires positive price")
        state = self.load()
        key = position_key(strategy, symbol, side)
        pos = state["positions"].get(key)
        if not pos:
            return self.save(state)
        close_qty = min(max(0.0, safe_float(qty)), safe_float(pos.get("qty")))
        if close_qty <= 0:
            close_qty = safe_float(pos.get("qty"))
        entry = safe_float(pos.get("entry_price"))
        pnl = (price - entry) * close_qty if side.lower() == "long" else (entry - price) * close_qty
        fee = close_qty * price * self.fee_rate
        account = state["accounts"].setdefault(strategy, {"cash": self.starting_equity, "fees_paid": 0.0, "funding_paid": 0.0, "realized_pnl": 0.0})
        account["cash"] = safe_float(account.get("cash"), self.starting_equity) + pnl - fee
        account["fees_paid"] = safe_float(account.get("fees_paid")) + fee
        account["realized_pnl"] = safe_float(account.get("realized_pnl")) + pnl - fee
        pos["fees_paid"] = safe_float(pos.get("fees_paid")) + fee
        pos["realized_pnl"] = safe_float(pos.get("realized_pnl")) + pnl - fee
        remaining = safe_float(pos.get("qty")) - close_qty
        if remaining <= 1e-12:
            del state["positions"][key]
        else:
            pos["qty"] = remaining
            pos["notional"] = remaining * price
            pos["margin"] = pos["notional"] / max(1.0, safe_float(pos.get("leverage"), 1.0))
        self._record_fill(state, PaperFill("CLOSE", strategy, symbol.upper(), side.lower(), close_qty, price, safe_float(pos.get("leverage"), 1.0) if pos else 1.0, fee, order_id, reason, realized_pnl=pnl - fee))
        return self.save(state)

    def mark_to_market(
        self,
        price_resolver: Callable[[str], tuple[float | None, str]],
        funding_resolver: Callable[[str], tuple[float, str]] | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        now = time.time()
        for key, pos in list(state["positions"].items()):
            symbol = str(pos.get("symbol") or "")
            price, source = price_resolver(symbol)
            if price and price > 0:
                pos["mark_price"] = float(price)
                pos["mark_source"] = source
            mark = safe_float(pos.get("mark_price"), safe_float(pos.get("entry_price")))
            entry = safe_float(pos.get("entry_price"))
            qty = safe_float(pos.get("qty"))
            side = str(pos.get("side") or "").lower()
            upnl = (mark - entry) * qty if side == "long" else (entry - mark) * qty
            pos["unrealized_pnl"] = upnl
            pos["notional"] = qty * mark
            pos["margin"] = pos["notional"] / max(1.0, safe_float(pos.get("leverage"), 1.0))
            last_funding = safe_float(pos.get("last_funding_ts"), now)
            elapsed_hours = max(0.0, (now - last_funding) / 3600.0)
            if funding_resolver and elapsed_hours >= 1.0:
                rate, funding_source = funding_resolver(symbol)
                funding = pos["notional"] * float(rate) * (elapsed_hours / 8.0)
                if side == "long":
                    funding = -funding
                account = state["accounts"].setdefault(str(pos.get("strategy")), {"cash": self.starting_equity, "fees_paid": 0.0, "funding_paid": 0.0, "realized_pnl": 0.0})
                account["cash"] = safe_float(account.get("cash"), self.starting_equity) + funding
                account["funding_paid"] = safe_float(account.get("funding_paid")) - funding
                pos["funding_paid"] = safe_float(pos.get("funding_paid")) - funding
                pos["last_funding_ts"] = now
                pos["funding_rate"] = rate
                pos["funding_source"] = funding_source
            else:
                pos.setdefault("funding_rate", 0.0)
                pos.setdefault("funding_source", "not_due_or_unavailable")
                pos.setdefault("last_funding_ts", now)
        return self.save(state)

    def summarize(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = state or self.load()
        by_strategy: dict[str, dict[str, Any]] = {}
        total_upnl = 0.0
        total_equity = 0.0
        positions = list(state.get("positions", {}).values())
        for strategy in STRATEGIES:
            account = state.get("accounts", {}).get(strategy, {})
            rows = [p for p in positions if p.get("strategy") == strategy]
            upnl = sum(safe_float(p.get("unrealized_pnl")) for p in rows)
            fees = safe_float(account.get("fees_paid"))
            funding = safe_float(account.get("funding_paid"))
            realized = safe_float(account.get("realized_pnl"))
            cash = safe_float(account.get("cash"), self.starting_equity)
            equity = cash + upnl
            total_upnl += upnl
            total_equity += equity
            by_strategy[strategy] = {
                "cash": round(cash, 6),
                "equity": round(equity, 6),
                "positions": len(rows),
                "unrealized_pnl": round(upnl, 6),
                "realized_pnl": round(realized, 6),
                "fees_paid": round(fees, 6),
                "funding_paid": round(funding, 6),
                "open_notional": round(sum(safe_float(p.get("notional")) for p in rows), 6),
            }
        return {
            "ts": utc_now(),
            "mode": "paper_exchange",
            "fee_rate": safe_float(state.get("fee_rate"), self.fee_rate),
            "total_equity": round(total_equity, 6),
            "total_unrealized_pnl": round(total_upnl, 6),
            "open_positions": len(positions),
            "by_strategy": by_strategy,
            "positions": positions,
            "recent_fills": list(state.get("fills", []))[-50:],
            "source": str(self.path),
        }

    def _record_fill(self, state: dict[str, Any], fill: PaperFill) -> None:
        row = {
            "ts": utc_now(),
            "action": fill.action,
            "strategy": fill.strategy,
            "symbol": fill.symbol,
            "side": fill.side,
            "qty": fill.qty,
            "price": fill.price,
            "leverage": fill.leverage,
            "notional": fill.qty * fill.price,
            "fee": fill.fee,
            "funding": fill.funding,
            "realized_pnl": fill.realized_pnl,
            "order_id": fill.order_id,
            "reason": fill.reason,
        }
        state.setdefault("fills", []).append(row)
        state["fills"] = state["fills"][-2000:]
