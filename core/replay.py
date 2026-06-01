"""Shared replay primitives for live/backtest/counterfactual parity.

The replay layer starts as a normalization boundary. Existing scanners can keep
their live loops, while research tools convert SQLite rows into these models and
gradually move gate evaluation onto one shared path.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ReplayEventType(str, Enum):
    SIGNAL = "SIGNAL"
    OPEN = "OPEN"
    OPEN_SKIPPED = "OPEN_SKIPPED"
    OPEN_FAILED = "OPEN_FAILED"
    CLOSE = "CLOSE"
    FORCED_CLOSE = "FORCED_CLOSE"
    CLOSE_FAILED = "CLOSE_FAILED"
    FORCED_CLOSE_FAILED = "FORCED_CLOSE_FAILED"
    SYSTEM = "SYSTEM"
    SENTINEL_SCANNED = "SENTINEL_SCANNED"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ReplayEvent:
    ts: str
    strategy: str
    symbol: str
    event_type: ReplayEventType
    side: str = ""
    score: float | None = None
    stage: str = ""
    layer: str = ""
    reason: str = ""
    price: float | None = None
    timeframe: str = ""
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_event_store_row(cls, row: dict[str, Any]) -> "ReplayEvent":
        payload = parse_payload(row.get("payload_json"))
        event_type = normalize_event_type(row.get("event_type") or payload.get("event"))
        return cls(
            ts=str(row.get("ts") or payload.get("time") or payload.get("ts") or ""),
            strategy=str(row.get("strategy") or payload.get("strategy") or ""),
            symbol=str(row.get("symbol") or payload.get("symbol") or ""),
            event_type=event_type,
            side=str(row.get("side") or payload.get("side") or payload.get("trade_side") or "").lower(),
            score=to_float(row.get("score", payload.get("score", payload.get("net_score"))), default=None),
            stage=str(row.get("stage") or payload.get("decision_stage") or ""),
            layer=str(row.get("layer") or payload.get("filter_layer") or ""),
            reason=str(row.get("reason") or payload.get("skip_reason") or payload.get("reason") or ""),
            price=to_float(payload.get("price"), default=None),
            timeframe=str(payload.get("timeframe") or payload.get("tf") or ""),
            payload=payload,
        )

    @property
    def is_open_intent(self) -> bool:
        return self.event_type in {
            ReplayEventType.SIGNAL,
            ReplayEventType.OPEN,
            ReplayEventType.OPEN_SKIPPED,
            ReplayEventType.OPEN_FAILED,
        }

    @property
    def is_terminal_close(self) -> bool:
        return self.event_type in {
            ReplayEventType.CLOSE,
            ReplayEventType.FORCED_CLOSE,
            ReplayEventType.CLOSE_FAILED,
            ReplayEventType.FORCED_CLOSE_FAILED,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return data


@dataclass(slots=True)
class ReplayDecision:
    event: ReplayEvent
    decision: str
    gate: str
    accepted: bool
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "decision": self.decision,
            "gate": self.gate,
            "accepted": self.accepted,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def to_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def normalize_event_type(value: Any) -> ReplayEventType:
    text = str(value or "").upper()
    try:
        return ReplayEventType(text)
    except ValueError:
        return ReplayEventType.UNKNOWN


def infer_gate_from_reason(reason: str) -> tuple[str, str]:
    """Best-effort gate inference for legacy rows that predate stage/layer fields."""
    text = str(reason or "").strip()
    lower = text.lower()
    if not text:
        return "", ""

    patterns: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("position", "same_symbol_position", ("同币种已有持仓", "已有持仓", "禁止叠仓", "duplicate position")),
        ("capacity", "pool_capacity", ("周期池满", "池满", "满仓", "无可释放", "释放弱仓", "方向持仓", "capacity")),
        ("cooldown", "cooldown", ("冷却", "cooldown", "止损冷却")),
        ("risk", "margin_or_balance", ("保证金", "余额", "margin", "balance", "not enough")),
        ("pre_filter", "market_filter", ("黑名单", "tradfi", "atr=0", "atr 0", "过滤")),
        ("score", "score_threshold", ("分数", "阈值", "score", "threshold")),
        ("confirmation", "entry_confirmation", ("确认", "confirmation", "confirm")),
        (
            "execution",
            "exchange_reject",
            ("-4164", "-1109", "min notional", "percent_price", "preflight", "exchange", "order rejected"),
        ),
    )
    for gate, evidence, needles in patterns:
        if any(needle.lower() in lower for needle in needles):
            return gate, evidence
    return "", ""


def classify_replay_decision(event: ReplayEvent) -> ReplayDecision:
    """Classify an observed live event into the initial replay gate taxonomy."""
    if event.event_type == ReplayEventType.OPEN:
        return ReplayDecision(event, "accepted_open", "open", True, event.reason)
    if event.event_type == ReplayEventType.SIGNAL:
        return ReplayDecision(event, "candidate", "entry_candidate", True, event.reason)
    if event.event_type == ReplayEventType.OPEN_SKIPPED:
        inferred_gate, evidence = infer_gate_from_reason(event.reason)
        gate = event.stage or event.layer or inferred_gate or "unknown_gate"
        inferred = {"inferred_from_reason": evidence} if inferred_gate and not (event.stage or event.layer) else {}
        return ReplayDecision(event, "rejected", gate, False, event.reason, inferred)
    if event.event_type == ReplayEventType.OPEN_FAILED:
        inferred_gate, evidence = infer_gate_from_reason(event.reason)
        gate = event.stage or event.layer or inferred_gate or "execution"
        inferred = {"inferred_from_reason": evidence} if inferred_gate and not (event.stage or event.layer) else {}
        return ReplayDecision(event, "execution_failed", gate, False, event.reason, inferred)
    if event.is_terminal_close:
        return ReplayDecision(event, "close_observed", event.stage or "exit", True, event.reason)
    return ReplayDecision(event, "observed", event.stage or event.layer or "unknown", False, event.reason)
