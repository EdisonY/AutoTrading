"""Serializable strategy-gate case runner for replay/live parity checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from core.strategy_gates import (
    StrategyGateDecision,
    evaluate_a_v11_entry_threshold,
    evaluate_b_v16_entry_threshold,
    evaluate_c_v14_entry_threshold,
    evaluate_execution_result_gate,
    evaluate_no_same_symbol_position_gate,
    evaluate_positive_quantity_gate,
    evaluate_score_max_gate,
    evaluate_tradability_gate,
)


GateEvaluator = Callable[..., StrategyGateDecision]


GATE_EVALUATORS: dict[str, GateEvaluator] = {
    "a_v11_entry_threshold": evaluate_a_v11_entry_threshold,
    "b_v16_entry_threshold": evaluate_b_v16_entry_threshold,
    "c_v14_entry_threshold": evaluate_c_v14_entry_threshold,
    "execution_result": evaluate_execution_result_gate,
    "no_same_symbol_position": evaluate_no_same_symbol_position_gate,
    "positive_quantity": evaluate_positive_quantity_gate,
    "score_max": evaluate_score_max_gate,
    "tradability": evaluate_tradability_gate,
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
    return evaluator(**dict(gate_case.inputs))


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
