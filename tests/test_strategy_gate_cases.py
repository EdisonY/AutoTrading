import unittest

from core.strategy_gate_cases import evaluate_strategy_gate_case, evaluate_strategy_gate_cases


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


if __name__ == "__main__":
    unittest.main()
