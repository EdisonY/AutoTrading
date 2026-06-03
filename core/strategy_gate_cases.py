"""Serializable strategy-gate case runner for replay/live parity checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping

from core.strategy_gates import (
    StrategyGateDecision,
    evaluate_a_v11_margin_sizing_gate,
    evaluate_a_v11_entry_threshold,
    evaluate_a_v11_market_microstructure_gate,
    evaluate_a_v11_pool_capacity_replacement_gate,
    evaluate_a_v11_replacement_release_result_gate,
    evaluate_a_v11_releasable_position,
    evaluate_a_v11_replacement_signal,
    evaluate_a_v11_resonance_required_gate,
    evaluate_account_state_available_gate,
    evaluate_active_position_limit_gate,
    evaluate_b_v16_confirmation_gate,
    evaluate_b_v16_entry_threshold,
    evaluate_b_v16_small_live_stage_guard,
    evaluate_c_v14_confirmation_gate,
    evaluate_c_v14_entry_threshold,
    evaluate_c_v14_market_microstructure_gate,
    evaluate_c_v14_stale_entry_price_gate,
    evaluate_c_v14_tail_guard,
    evaluate_execution_result_gate,
    evaluate_no_same_symbol_position_gate,
    evaluate_positive_quantity_gate,
    evaluate_same_side_position_gate,
    evaluate_score_max_gate,
    evaluate_sector_position_gate,
    evaluate_symbol_blacklist_gate,
    evaluate_symbol_cooldown_gate,
    evaluate_symbol_scan_cooldown_gate,
    evaluate_symbol_stop_loss_gate,
    evaluate_timeframe_position_gate,
    evaluate_tradability_gate,
    evaluate_watchlist_score_adjustment,
)


GateEvaluator = Callable[..., StrategyGateDecision]


GATE_EVALUATORS: dict[str, GateEvaluator] = {
    "account_state_available": evaluate_account_state_available_gate,
    "active_position_limit": evaluate_active_position_limit_gate,
    "a_v11_margin_sizing": evaluate_a_v11_margin_sizing_gate,
    "a_v11_entry_threshold": evaluate_a_v11_entry_threshold,
    "a_v11_market_microstructure": evaluate_a_v11_market_microstructure_gate,
    "a_v11_pool_capacity_replacement": evaluate_a_v11_pool_capacity_replacement_gate,
    "a_v11_replacement_release_result": evaluate_a_v11_replacement_release_result_gate,
    "a_v11_releasable_position": evaluate_a_v11_releasable_position,
    "a_v11_replacement_signal": evaluate_a_v11_replacement_signal,
    "a_v11_resonance_required": evaluate_a_v11_resonance_required_gate,
    "b_v16_confirmation": evaluate_b_v16_confirmation_gate,
    "b_v16_entry_threshold": evaluate_b_v16_entry_threshold,
    "b_v16_small_live_stage_guard": evaluate_b_v16_small_live_stage_guard,
    "c_v14_confirmation": evaluate_c_v14_confirmation_gate,
    "c_v14_entry_threshold": evaluate_c_v14_entry_threshold,
    "c_v14_market_microstructure": evaluate_c_v14_market_microstructure_gate,
    "c_v14_stale_entry_price": evaluate_c_v14_stale_entry_price_gate,
    "c_v14_tail_guard": evaluate_c_v14_tail_guard,
    "execution_result": evaluate_execution_result_gate,
    "no_same_symbol_position": evaluate_no_same_symbol_position_gate,
    "positive_quantity": evaluate_positive_quantity_gate,
    "same_side_position": evaluate_same_side_position_gate,
    "score_max": evaluate_score_max_gate,
    "sector_position": evaluate_sector_position_gate,
    "symbol_blacklist": evaluate_symbol_blacklist_gate,
    "symbol_cooldown": evaluate_symbol_cooldown_gate,
    "symbol_scan_cooldown": evaluate_symbol_scan_cooldown_gate,
    "symbol_stop_loss": evaluate_symbol_stop_loss_gate,
    "timeframe_position": evaluate_timeframe_position_gate,
    "tradability": evaluate_tradability_gate,
    "watchlist_score_adjustment": evaluate_watchlist_score_adjustment,
}


def _json_safe(value: Any) -> Any:
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_inputs(gate: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    coerced = dict(inputs)
    if gate == "symbol_cooldown":
        coerced["cooldown_until"] = _parse_datetime(coerced.get("cooldown_until"))
        now_value = _parse_datetime(coerced.get("now"))
        if now_value is not None:
            coerced["now"] = now_value
    return coerced


def strategy_gate_case(
    *,
    name: str,
    gate: str,
    inputs: Mapping[str, Any],
    decision: StrategyGateDecision,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a serializable exact-case payload for replay/live parity audits."""
    return {
        "name": str(name),
        "gate": str(gate),
        "inputs": _json_safe(dict(inputs)),
        "expected_allowed": bool(decision.allowed),
        "expected_reason": decision.reason,
        "meta": {
            "decision_gate": decision.gate,
            "threshold": decision.threshold,
            "adjusted_score": decision.adjusted_score,
            **_json_safe(dict(meta or {})),
        },
    }


@dataclass(frozen=True)
class StrategyGateCase:
    name: str
    gate: str
    inputs: Mapping[str, Any]
    expected_allowed: bool | None = None
    expected_reason: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "StrategyGateCase":
        return cls(
            name=str(payload.get("name") or payload.get("gate") or ""),
            gate=str(payload.get("gate") or ""),
            inputs=payload.get("inputs") or {},
            expected_allowed=payload.get("expected_allowed"),
            expected_reason=payload.get("expected_reason"),
            meta=payload.get("meta") or {},
        )


def evaluate_strategy_gate_case(case: StrategyGateCase | Mapping[str, Any]) -> StrategyGateDecision:
    gate_case = case if isinstance(case, StrategyGateCase) else StrategyGateCase.from_mapping(case)
    evaluator = GATE_EVALUATORS.get(gate_case.gate)
    if evaluator is None:
        raise KeyError(f"unknown strategy gate case: {gate_case.gate}")
    return evaluator(**_coerce_inputs(gate_case.gate, gate_case.inputs))


def evaluate_strategy_gate_cases(cases: Iterable[StrategyGateCase | Mapping[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in cases:
        case = item if isinstance(item, StrategyGateCase) else StrategyGateCase.from_mapping(item)
        decision = evaluate_strategy_gate_case(case)
        expected_allowed_match = case.expected_allowed is None or bool(case.expected_allowed) == decision.allowed
        expected_reason_match = case.expected_reason is None or str(case.expected_reason) == decision.reason
        results.append(
            {
                "name": case.name,
                "gate": case.gate,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "decision_gate": decision.gate,
                "expected_allowed": case.expected_allowed,
                "expected_reason": case.expected_reason,
                "passed": bool(expected_allowed_match and expected_reason_match),
                "evidence": decision.evidence or {},
                "meta": dict(case.meta or {}),
            }
        )
    return results
