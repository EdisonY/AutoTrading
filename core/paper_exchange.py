"""Persistent paper exchange ledger for full-system dry trading.

The ledger is the paper source of truth. It is intentionally local-file based:
no network, no Binance order path, and no hidden exchange state.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.replay_depth_cache import default_depth_cache_dirs, load_depth_snapshot
from core.replay_fill import ReplayFillRequest, simulate_replay_fill


DEFAULT_FEE_RATE = 0.0004
DEFAULT_STARTING_EQUITY = 100_000.0
DEFAULT_FILL_MODEL_VERSION = "v2"
DEFAULT_FILL_MAX_DEPTH_AGE_SEC = 300.0
DEFAULT_FALLBACK_SPREAD_BPS = 5.0
DEFAULT_FALLBACK_SLIPPAGE_BPS = 2.0
DEFAULT_ORDER_BOOK_MAX_LEVELS = 20
DEFAULT_ORDER_BOOK_LIQUIDITY_FACTOR = 1.0
DEFAULT_ORDER_BOOK_QUEUE_AHEAD_QUANTITY = 0.0
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
    details: dict[str, Any] = field(default_factory=dict)


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
        fill = self._apply_fill_model(
            action="OPEN",
            symbol=symbol,
            position_side=side,
            qty=qty,
            price=price,
        )
        qty = safe_float(fill.get("executed_qty"), qty)
        price = safe_float(fill.get("executed_price"), price)
        if qty <= 0 or price <= 0:
            raise ValueError("paper open fill produced non-positive qty or price")
        state = self.load()
        account = state["accounts"].setdefault(strategy, {"cash": self.starting_equity, "fees_paid": 0.0, "funding_paid": 0.0, "realized_pnl": 0.0})
        key = position_key(strategy, symbol, side)
        notional = qty * price
        fee = notional * self.fee_rate
        position_context = dict(context or {})
        position_context["paper_fill"] = fill
        pos = state["positions"].get(key)
        if pos:
            old_qty = safe_float(pos.get("qty"))
            new_qty = old_qty + qty
            pos["entry_price"] = ((safe_float(pos.get("entry_price")) * old_qty) + notional) / new_qty
            pos["qty"] = new_qty
            pos["notional"] = new_qty * safe_float(pos.get("entry_price"), price)
            pos["latest_paper_fill"] = fill
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
                "context": position_context,
            }
            state["positions"][key] = pos
        pos["fees_paid"] = safe_float(pos.get("fees_paid")) + fee
        account["cash"] = safe_float(account.get("cash"), self.starting_equity) - fee
        account["fees_paid"] = safe_float(account.get("fees_paid")) + fee
        self._record_fill(state, PaperFill("OPEN", strategy, symbol.upper(), side.lower(), qty, price, leverage, fee, order_id, reason, details=fill))
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
        fill = self._apply_fill_model(
            action="CLOSE",
            symbol=symbol,
            position_side=side,
            qty=close_qty,
            price=price,
        )
        close_qty = safe_float(fill.get("executed_qty"), close_qty)
        price = safe_float(fill.get("executed_price"), price)
        if close_qty <= 0 or price <= 0:
            raise ValueError("paper close fill produced non-positive qty or price")
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
            pos["latest_paper_fill"] = fill
        self._record_fill(state, PaperFill("CLOSE", strategy, symbol.upper(), side.lower(), close_qty, price, safe_float(pos.get("leverage"), 1.0) if pos else 1.0, fee, order_id, reason, realized_pnl=pnl - fee, details=fill))
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
                pos["mark_updated_at"] = utc_now()
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
            "paper_fill_model_version": self.fill_model_version(),
            "fidelity": {
                "level": "closer_to_live_but_not_identical",
                "price": "OKX 15m/latest cached close; Binance mark/index may differ",
                "time": "updated when paper_exchange_runner runs, not exchange tick-by-tick",
                "slippage": "paper_fill_model_v2 consumes local depth bids/asks when fresh; otherwise side-aware synthetic spread/slippage fallback; not live queue parity",
                "fees": f"ledger fee_rate={safe_float(state.get('fee_rate'), self.fee_rate):.6f}",
                "funding": "OKX public funding when available; missing/unavailable records 0 with source",
            },
            "fee_rate": safe_float(state.get("fee_rate"), self.fee_rate),
            "total_equity": round(total_equity, 6),
            "total_unrealized_pnl": round(total_upnl, 6),
            "open_positions": len(positions),
            "by_strategy": by_strategy,
            "positions": positions,
            "recent_fills": list(state.get("fills", []))[-50:],
            "source": str(self.path),
        }

    @staticmethod
    def fill_model_version() -> str:
        value = os.environ.get("PAPER_FILL_MODEL_VERSION", DEFAULT_FILL_MODEL_VERSION).strip().lower()
        return value or DEFAULT_FILL_MODEL_VERSION

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        return safe_float(os.environ.get(name), default)

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(float(os.environ.get(name, default)))
        except Exception:
            return default

    @staticmethod
    def _execution_side(*, action: str, position_side: str) -> str:
        side = str(position_side or "").lower()
        act = str(action or "").upper()
        if act == "CLOSE":
            return "short" if side == "long" else "long"
        return "long" if side == "long" else "short"

    def _apply_fill_model(self, *, action: str, symbol: str, position_side: str, qty: float, price: float) -> dict[str, Any]:
        requested_qty = safe_float(qty)
        requested_price = safe_float(price)
        version = self.fill_model_version()
        if version in {"v1", "legacy", "requested"}:
            return self._fill_detail(
                version="v1",
                source="requested_price",
                action=action,
                symbol=symbol,
                position_side=position_side,
                execution_side=self._execution_side(action=action, position_side=position_side),
                requested_qty=requested_qty,
                requested_price=requested_price,
                executed_qty=requested_qty,
                executed_price=requested_price,
                extra={"fallback_reason": "legacy_requested_price"},
            )

        execution_side = self._execution_side(action=action, position_side=position_side)
        now = datetime.now(timezone.utc)
        max_age = max(1.0, self._env_float("PAPER_FILL_MAX_DEPTH_AGE_SEC", DEFAULT_FILL_MAX_DEPTH_AGE_SEC))
        snapshot = load_depth_snapshot(
            symbol,
            now,
            side=execution_side,
            cache_dirs=default_depth_cache_dirs(self.root),
            max_age_seconds=max_age,
        )
        if snapshot is not None:
            try:
                req = ReplayFillRequest(
                    symbol=str(symbol).upper(),
                    side=execution_side,
                    entry_price=requested_price,
                    quantity=requested_qty,
                    fee_bps=0.0,
                    slippage_bps=0.0,
                    allow_partial_fill=str(action).upper() == "OPEN",
                    entry_order_book=snapshot.order_book,
                    entry_order_book_max_levels=max(1, self._env_int("PAPER_FILL_ORDER_BOOK_MAX_LEVELS", DEFAULT_ORDER_BOOK_MAX_LEVELS)),
                    entry_order_book_liquidity_factor=max(0.0, min(1.0, self._env_float("PAPER_FILL_ORDER_BOOK_LIQUIDITY_FACTOR", DEFAULT_ORDER_BOOK_LIQUIDITY_FACTOR))),
                    entry_order_book_queue_ahead_quantity=max(0.0, self._env_float("PAPER_FILL_ORDER_BOOK_QUEUE_AHEAD_QTY", DEFAULT_ORDER_BOOK_QUEUE_AHEAD_QUANTITY)),
                )
                result = simulate_replay_fill(req, [{"ts": now.isoformat(), "open": requested_price, "high": requested_price, "low": requested_price, "close": requested_price}])
                return self._fill_detail(
                    version="v2",
                    source="order_book",
                    action=action,
                    symbol=symbol,
                    position_side=position_side,
                    execution_side=execution_side,
                    requested_qty=requested_qty,
                    requested_price=requested_price,
                    executed_qty=float(result.quantity),
                    executed_price=float(result.entry_price),
                    extra={
                        "fill_ratio": result.fill_ratio,
                        "partial_fill": result.partial_fill,
                        "fill_status": "PARTIALLY_FILLED" if result.partial_fill else "FILLED",
                        "slippage_usdt": result.slippage_usdt,
                        "slippage_bps": self._price_slippage_bps(requested_price, float(result.entry_price)),
                        "depth_slippage_usdt": result.depth_slippage_usdt,
                        "order_book_levels_used": result.order_book_levels_used,
                        "order_book_available_quantity": result.order_book_available_quantity,
                        "order_book_fill_ratio": result.order_book_fill_ratio,
                        "order_book_queue_ahead_quantity": result.order_book_queue_ahead_quantity,
                        "depth_snapshot_source": snapshot.source,
                        "depth_snapshot_age_seconds": round(snapshot.age_seconds, 6),
                        "depth_max_age_seconds": max_age,
                    },
                )
            except Exception as exc:
                return self._synthetic_fill_detail(
                    action=action,
                    symbol=symbol,
                    position_side=position_side,
                    execution_side=execution_side,
                    requested_qty=requested_qty,
                    requested_price=requested_price,
                    fallback_reason=f"depth_fill_error:{str(exc)[:120]}",
                )
        return self._synthetic_fill_detail(
            action=action,
            symbol=symbol,
            position_side=position_side,
            execution_side=execution_side,
            requested_qty=requested_qty,
            requested_price=requested_price,
            fallback_reason="fresh_depth_snapshot_unavailable",
        )

    def _synthetic_fill_detail(
        self,
        *,
        action: str,
        symbol: str,
        position_side: str,
        execution_side: str,
        requested_qty: float,
        requested_price: float,
        fallback_reason: str,
    ) -> dict[str, Any]:
        spread_bps = max(0.0, self._env_float("PAPER_FILL_FALLBACK_SPREAD_BPS", DEFAULT_FALLBACK_SPREAD_BPS))
        slip_bps = max(0.0, self._env_float("PAPER_FILL_FALLBACK_SLIPPAGE_BPS", DEFAULT_FALLBACK_SLIPPAGE_BPS))
        adverse_bps = spread_bps / 2.0 + slip_bps
        rate = adverse_bps / 10_000.0
        executed_price = requested_price * (1.0 + rate if execution_side == "long" else 1.0 - rate)
        return self._fill_detail(
            version="v2",
            source="synthetic_fallback",
            action=action,
            symbol=symbol,
            position_side=position_side,
            execution_side=execution_side,
            requested_qty=requested_qty,
            requested_price=requested_price,
            executed_qty=requested_qty,
            executed_price=executed_price,
            extra={
                "fill_ratio": 1.0,
                "partial_fill": False,
                "fill_status": "FILLED",
                "slippage_usdt": abs(executed_price - requested_price) * requested_qty,
                "slippage_bps": adverse_bps,
                "depth_slippage_usdt": 0.0,
                "order_book_levels_used": 0,
                "order_book_available_quantity": 0.0,
                "order_book_fill_ratio": 0.0,
                "order_book_queue_ahead_quantity": 0.0,
                "depth_snapshot_source": "",
                "depth_snapshot_age_seconds": None,
                "fallback_reason": fallback_reason,
                "fallback_spread_bps": spread_bps,
                "fallback_slippage_bps": slip_bps,
            },
        )

    @staticmethod
    def _price_slippage_bps(requested_price: float, executed_price: float) -> float:
        if requested_price <= 0:
            return 0.0
        return round(abs(float(executed_price) - float(requested_price)) / float(requested_price) * 10_000.0, 8)

    def _fill_detail(
        self,
        *,
        version: str,
        source: str,
        action: str,
        symbol: str,
        position_side: str,
        execution_side: str,
        requested_qty: float,
        requested_price: float,
        executed_qty: float,
        executed_price: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        detail = {
            "paper_fill_model_version": version,
            "paper_fill_source": source,
            "action": str(action).upper(),
            "symbol": str(symbol).upper(),
            "position_side": str(position_side).lower(),
            "execution_side": execution_side,
            "liquidity_side": "asks" if execution_side == "long" else "bids",
            "requested_qty": round(float(requested_qty), 10),
            "requested_price": round(float(requested_price), 10),
            "executed_qty": round(float(executed_qty), 10),
            "executed_price": round(float(executed_price), 10),
            "unfilled_qty": round(max(0.0, float(requested_qty) - float(executed_qty)), 10),
            "fill_ratio": round(float(executed_qty) / float(requested_qty), 8) if requested_qty > 0 else 0.0,
            "partial_fill": float(executed_qty) + 1e-12 < float(requested_qty),
            "fill_status": "PARTIALLY_FILLED" if float(executed_qty) + 1e-12 < float(requested_qty) else "FILLED",
        }
        if extra:
            detail.update(extra)
        return detail

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
        row.update(fill.details or {})
        state.setdefault("fills", []).append(row)
        state["fills"] = state["fills"][-2000:]
