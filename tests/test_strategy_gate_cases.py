import json
import unittest
from datetime import datetime, timedelta

from core.strategy_gate_cases import evaluate_strategy_gate_case, evaluate_strategy_gate_cases, strategy_gate_case
from core.strategy_gates import evaluate_positive_quantity_gate, evaluate_symbol_blacklist_gate, evaluate_symbol_cooldown_gate


class _ScalarLike:
    def __init__(self, value):
        self._value = value

    def item(self):
        return self._value

    def __float__(self):
        return float(self._value)


class StrategyGateCasesTest(unittest.TestCase):
    def test_evaluates_serialized_gate_case(self):
        decision = evaluate_strategy_gate_case(
            {
                "name": "a-pass",
                "gate": "a_v11_entry_threshold",
                "inputs": {
                    "timeframe": "15m",
                    "side": "short",
                    "score": -125,
                    "score_thresholds": {"15m": 115},
                    "score_threshold": 120,
                    "short_entry_penalty": 5,
                },
            }
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "threshold_pass")

    def test_batch_reports_expected_matches(self):
        results = evaluate_strategy_gate_cases(
            [
                {
                    "name": "b-threshold-fail",
                    "gate": "b_v16_entry_threshold",
                    "inputs": {
                        "timeframe": "1h",
                        "side": "short",
                        "score": 80,
                        "symbol": "ALTUSDT",
                        "open_positions": 2,
                        "confirm_reason": "15m无信号但高分放行",
                        "score_thresholds": {"1h": 80},
                        "score_min": 80,
                        "short_entry_penalty": 10,
                        "major_symbols": {"BTCUSDT", "ETHUSDT"},
                        "low_position_threshold_discount": 5,
                        "no_confirm_threshold_penalty": 8,
                        "weak_opposite_confirm_penalty": 4,
                        "confirm_bonus": 5,
                        "confirm_strong_bonus": 8,
                    },
                    "expected_allowed": False,
                    "expected_reason": "threshold_fail",
                },
                {
                    "name": "c-threshold-pass",
                    "gate": "c_v14_entry_threshold",
                    "inputs": {
                        "timeframe": "1h",
                        "side": "long",
                        "score_thresholds": {"1h": 50},
                        "score_min": 50,
                        "long_penalty": 0,
                        "short_entry_penalty": 10,
                    },
                    "expected_allowed": True,
                },
                {
                    "name": "execution-preflight",
                    "gate": "execution_result",
                    "inputs": {
                        "success": False,
                        "preflight_rejected": True,
                        "code": "exchange_min_notional",
                        "reason": "min notional",
                    },
                    "expected_allowed": False,
                    "expected_reason": "min notional",
                },
                {
                    "name": "account-state-missing",
                    "gate": "account_state_available",
                    "inputs": {"account_state_available": False},
                    "expected_allowed": False,
                    "expected_reason": "account_state_unavailable",
                },
                {
                    "name": "a-sizing-out-of-tolerance",
                    "gate": "a_v11_margin_sizing",
                    "inputs": {
                        "quantity": 1,
                        "price": 50,
                        "risk_usdt": 100,
                        "leverage": 4,
                        "order_margin_tolerance_pct": 0.1,
                    },
                    "expected_allowed": False,
                    "expected_reason": "margin_sizing_out_of_tolerance",
                },
                {
                    "name": "b-confirm-pass",
                    "gate": "b_v16_confirmation",
                    "inputs": {
                        "side": "long",
                        "raw_score": 92,
                        "confirm_signal": {"trade_side": "long", "net_score": 36},
                        "open_positions": 1,
                        "max_active_new_positions": 4,
                        "no_confirm_high_score_pass": 95,
                        "confirm_opposite_reject_score": 35,
                        "opposite_high_score_pass": 90,
                        "weak_confirm_pass_score": 88,
                        "confirm_min_score": 25,
                        "confirm_bonus": 5,
                        "confirm_strong_bonus": 8,
                    },
                    "expected_allowed": True,
                    "expected_reason": "15m确认36+8",
                },
                {
                    "name": "c-stale-entry-price",
                    "gate": "c_v14_stale_entry_price",
                    "inputs": {"recent_prices": [1.23, 1.23, 1.23], "repeated_count": 3},
                    "expected_allowed": False,
                    "expected_reason": "入场价连续3次相同，疑似数据冻结",
                },
                {
                    "name": "intentional-mismatch",
                    "gate": "positive_quantity",
                    "inputs": {"quantity": 0},
                    "expected_allowed": True,
                },
            ]
        )

        self.assertEqual([row["passed"] for row in results], [True, True, True, True, True, True, True, False])
        self.assertEqual(results[-1]["reason"], "qty<=0")

    def test_unknown_gate_raises(self):
        with self.assertRaises(KeyError):
            evaluate_strategy_gate_case({"gate": "missing", "inputs": {}})

    def test_a_v11_replacement_orchestration_cases_replay(self):
        results = evaluate_strategy_gate_cases(
            [
                {
                    "name": "a-replacement-signal",
                    "gate": "a_v11_replacement_signal",
                    "inputs": {"effective_score": 108, "strong_signal_threshold": 112},
                    "expected_allowed": False,
                    "expected_reason": "replacement_signal_fail",
                },
                {
                    "name": "a-pool-full",
                    "gate": "a_v11_pool_capacity_replacement",
                    "inputs": {
                        "timeframe_full": True,
                        "replacement_signal_allowed": False,
                        "reject_reason": "周期池满且未达到强信号替换条件",
                    },
                    "expected_allowed": False,
                    "expected_reason": "周期池满且未达到强信号替换条件",
                },
                {
                    "name": "a-release-missing",
                    "gate": "a_v11_replacement_release_result",
                    "inputs": {"release_success": False, "reason": "周期池满且无可释放弱仓"},
                    "expected_allowed": False,
                    "expected_reason": "周期池满且无可释放弱仓",
                },
            ]
        )

        self.assertEqual([row["passed"] for row in results], [True, True, True])
        self.assertEqual(
            [row["reason"] for row in results],
            ["replacement_signal_fail", "周期池满且未达到强信号替换条件", "周期池满且无可释放弱仓"],
        )

    def test_strategy_gate_case_is_json_safe_and_replayable(self):
        decision = evaluate_symbol_blacklist_gate(
            symbol="BTCUSDT",
            blacklisted_symbols={"BTCUSDT", "ETHUSDT"},
            reason="blocked",
        )

        case = strategy_gate_case(
            name="blacklist-case",
            gate="symbol_blacklist",
            inputs={
                "symbol": "BTCUSDT",
                "blacklisted_symbols": {"BTCUSDT", "ETHUSDT"},
                "reason": "blocked",
            },
            decision=decision,
            meta={"seen_at": datetime(2026, 6, 3, 10, 30), "tags": {"live", "parity"}},
        )

        json.dumps(case, ensure_ascii=False)
        replayed = evaluate_strategy_gate_case(case)

        self.assertFalse(replayed.allowed)
        self.assertEqual(replayed.reason, "blocked")
        self.assertFalse(case["expected_allowed"])
        self.assertEqual(case["expected_reason"], "blocked")
        self.assertEqual(case["meta"]["seen_at"], "2026-06-03T10:30:00")
        self.assertIsInstance(case["inputs"]["blacklisted_symbols"], list)
        self.assertIsInstance(case["meta"]["tags"], list)

    def test_strategy_gate_case_normalizes_scalar_like_values(self):
        decision = evaluate_positive_quantity_gate(quantity=_ScalarLike(2.5))

        case = strategy_gate_case(
            name="scalar-like-quantity",
            gate="positive_quantity",
            inputs={"quantity": _ScalarLike(2.5)},
            decision=decision,
            meta={"flag": _ScalarLike(True)},
        )

        json.dumps(case, ensure_ascii=False)
        replayed = evaluate_strategy_gate_case(case)

        self.assertTrue(replayed.allowed)
        self.assertEqual(case["inputs"]["quantity"], 2.5)
        self.assertIs(case["meta"]["flag"], True)

    def test_strategy_gate_case_replays_serialized_datetime_inputs(self):
        now = datetime(2026, 6, 3, 19, 30)
        cooldown_until = now + timedelta(minutes=12)
        decision = evaluate_symbol_cooldown_gate(cooldown_until=cooldown_until, now=now)
        case = strategy_gate_case(
            name="symbol-cooldown-active",
            gate="symbol_cooldown",
            inputs={"cooldown_until": cooldown_until, "now": now},
            decision=decision,
        )

        restored = json.loads(json.dumps(case, ensure_ascii=False))
        replayed = evaluate_strategy_gate_case(restored)

        self.assertFalse(replayed.allowed)
        self.assertEqual(replayed.reason, "symbol_cooldown_active")


if __name__ == "__main__":
    unittest.main()
