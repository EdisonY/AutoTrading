"""Shared data models for signals, decisions, trades, and positions.

These models intentionally accept loose JSONL rows from older scanners.  The
goal is to normalize review/analytics data without forcing every strategy file
to change at the same time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.position_utils import infer_position_side, leveraged_loss_pct, position_unrealized_pnl


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


@dataclass(slots=True)
class SignalRecord:
    strategy: str
    symbol: str
    side: str
    score: float
    timeframe: str = ""
    time: str = ""
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_row(cls, strategy: str, row: dict[str, Any]) -> "SignalRecord":
        side = str(row.get("trade_side") or row.get("side") or "").lower()
        score = row.get("net_score", row.get("score", row.get("vpb_score", 0)))
        reasons = row.get("reasons") or row.get(f"reasons_{side}") or []
        return cls(
            strategy=strategy,
            symbol=str(row.get("symbol") or ""),
            side=side,
            score=_float(score),
            timeframe=str(row.get("timeframe") or row.get("tf") or ""),
            time=str(row.get("time") or row.get("ts") or ""),
            reasons=_list(reasons),
            raw=row,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DecisionRecord:
    strategy: str
    symbol: str
    status: str
    category: str
    side: str = ""
    score: float = 0.0
    timeframe: str = ""
    time: str = ""
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_event(cls, strategy: str, row: dict[str, Any]) -> "DecisionRecord":
        event = str(row.get("event") or "").upper()
        reason = str(row.get("skip_reason") or row.get("reason") or row.get("msg") or "")
        return cls(
            strategy=strategy,
            symbol=str(row.get("symbol") or ""),
            status=event,
            category=categorize_decision(event, reason),
            side=str(row.get("side") or "").lower(),
            score=_float(row.get("score", row.get("raw_score", 0))),
            timeframe=str(row.get("timeframe") or row.get("tf") or ""),
            time=str(row.get("time") or row.get("ts") or ""),
            reason=reason,
            raw=row,
        )

    @classmethod
    def from_signal(cls, signal: SignalRecord) -> "DecisionRecord":
        return cls(
            strategy=signal.strategy,
            symbol=signal.symbol,
            status="SIGNAL_ONLY",
            category="signal_only",
            side=signal.side,
            score=abs(signal.score),
            timeframe=signal.timeframe,
            time=signal.time,
            reason="+".join(signal.reasons[:4]),
            raw=signal.raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeRecord:
    strategy: str
    symbol: str
    side: str
    pnl_usd: float
    pnl_pct: float
    entry_time: str = ""
    exit_time: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    entry_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_row(cls, strategy: str, row: dict[str, Any]) -> "TradeRecord":
        return cls(
            strategy=strategy,
            symbol=str(row.get("symbol") or ""),
            side=str(row.get("side") or "").lower(),
            pnl_usd=_float(row.get("pnl_usd")),
            pnl_pct=_float(row.get("pnl_pct")),
            entry_time=str(row.get("entry_time") or ""),
            exit_time=str(row.get("exit_time") or row.get("time") or ""),
            entry_price=_float(row.get("entry_price")),
            exit_price=_float(row.get("exit_price")),
            exit_reason=str(row.get("exit_reason") or row.get("reason") or ""),
            entry_reason=str(row.get("entry_reason") or row.get("reason") or ""),
            raw=row,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PositionSnapshot:
    account: str
    strategy: str
    symbol: str
    side: str
    qty: float
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float
    notional: float
    loss_pct: float
    time: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_exchange_row(cls, account: str, strategy: str, row: dict[str, Any]) -> "PositionSnapshot":
        qty = _float(row.get("positionAmt"))
        side, _side_source = infer_position_side(row)
        entry = _float(row.get("entryPrice"))
        mark = _float(row.get("markPrice"))
        lev = _float(row.get("leverage"), 4.0)
        upnl, _upnl_source = position_unrealized_pnl(row, side)
        notional = abs(qty) * mark
        loss = leveraged_loss_pct(row, side)
        return cls(
            account=account,
            strategy=strategy,
            symbol=str(row.get("symbol") or ""),
            side=side,
            qty=abs(qty),
            entry_price=entry,
            mark_price=mark,
            leverage=lev,
            unrealized_pnl=upnl,
            notional=notional,
            loss_pct=loss,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def categorize_decision(event: str, reason: str) -> str:
    text = f"{event} {reason}".lower()
    if event == "SENTINEL_SCANNED":
        return "sentinel_scanned"
    if event == "OPEN":
        return "opened"
    if "总持仓" in reason or "active" in text or "活跃持仓" in reason:
        return "position_limit"
    if "方向持仓" in reason or "单方向" in reason:
        return "side_limit"
    if "余额" in reason or "available" in text or "reserve" in text:
        return "capital_guard"
    if "15m" in reason or "确认" in reason:
        return "confirmation"
    if "阈值" in reason or "score" in text or "分" in reason:
        return "score_threshold"
    if "止损" in reason or "冷却" in reason or "cooldown" in text:
        return "cooldown"
    if "atr" in text or "数量太小" in reason or "qty" in text:
        return "market_microstructure"
    if "下单失败" in reason or "开仓失败" in reason or event == "OPEN_FAILED":
        return "order_failed"
    if event == "SIGNAL_ONLY":
        return "signal_only"
    if event == "NO_RECORD":
        return "pre_filter_no_record"
    return "other"
