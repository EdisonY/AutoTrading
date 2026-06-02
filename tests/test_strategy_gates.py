import unittest

from core.strategy_gates import (
    effective_a_v11_signal_score,
    evaluate_a_v11_entry_threshold,
    evaluate_a_v11_releasable_position,
    evaluate_a_v11_replacement_signal,
    evaluate_b_v16_confirmation_gate,
    evaluate_b_v16_entry_threshold,
    evaluate_c_v14_confirmation_gate,
    evaluate_c_v14_entry_threshold,
    evaluate_c_v14_stale_entry_price_gate,
    evaluate_c_v14_tail_guard,
    evaluate_no_same_symbol_position_gate,
    evaluate_same_side_position_gate,
    evaluate_sector_position_gate,
    evaluate_symbol_stop_loss_gate,
)


class StrategyGateParityTest(unittest.TestCase):
    def test_a_v11_threshold_and_replacement(self):
        decision = evaluate_a_v11_entry_threshold(
            timeframe="15m",
            side="short",
            score=-122,
            score_thresholds={"15m": 115},
            score_threshold=120,
            short_entry_penalty=5,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.threshold, 120)

        self.assertEqual(
            effective_a_v11_signal_score(score=100, side="long", resonance=True, resonance_bonus=8),
            108,
        )
        self.assertEqual(
            effective_a_v11_signal_score(score=-100, side="short", resonance=True, resonance_bonus=8),
            -108,
        )
        self.assertTrue(
            evaluate_a_v11_replacement_signal(
                effective_score=-112,
                strong_signal_threshold=112,
            ).allowed
        )
        self.assertFalse(
            evaluate_a_v11_replacement_signal(
                effective_score=111.9,
                strong_signal_threshold=112,
            ).allowed
        )

        releasable = evaluate_a_v11_releasable_position(
            new_score=125,
            new_symbol="NEWUSDT",
            new_side="long",
            old_tf="15m",
            old_symbol="OLDUSDT",
            old_side="long",
            old_score=90,
            pnl_pct=-1.0,
            age_min=30,
            same_side_required=False,
            preferred_tf="15m",
            require_preferred_tf=True,
            strong_signal_threshold=112,
            elite_score=120,
            min_age_minutes=20,
            elite_min_age_minutes=10,
            score_gap=25,
            soft_protect_pnl_pct=2.0,
            soft_protect_score_gap=25,
            hard_protect_pnl_pct=2.0,
        )
        self.assertTrue(releasable.allowed)
        self.assertEqual(releasable.evidence["gap_required"], 0)
        self.assertEqual(tuple(releasable.evidence["release_rank"]), (0, 0, -1.0, 90.0, -30))

        protected = evaluate_a_v11_releasable_position(
            new_score=130,
            new_symbol="NEWUSDT",
            new_side="long",
            old_tf="15m",
            old_symbol="OLDUSDT",
            old_side="long",
            old_score=90,
            pnl_pct=2.0,
            age_min=30,
            same_side_required=False,
            preferred_tf="15m",
            require_preferred_tf=True,
            strong_signal_threshold=112,
            elite_score=120,
            min_age_minutes=20,
            elite_min_age_minutes=10,
            score_gap=25,
            soft_protect_pnl_pct=2.0,
            soft_protect_score_gap=25,
            hard_protect_pnl_pct=2.0,
        )
        self.assertFalse(protected.allowed)
        self.assertEqual(protected.reason, "hard_profit_protected")

    def test_b_v16_threshold_and_confirmation(self):
        confirm = evaluate_b_v16_confirmation_gate(
            side="long",
            raw_score=90,
            confirm_signal={"trade_side": "short", "net_score": 30},
            open_positions=1,
            max_active_new_positions=4,
            no_confirm_high_score_pass=85,
            confirm_opposite_reject_score=25,
            opposite_high_score_pass=80,
            weak_confirm_pass_score=75,
            confirm_min_score=15,
            confirm_bonus=5,
            confirm_strong_bonus=8,
        )
        self.assertFalse(confirm.allowed)
        self.assertEqual(confirm.gate, "confirmation")

        decision = evaluate_b_v16_entry_threshold(
            timeframe="1h",
            side="short",
            score=80,
            symbol="ALTUSDT",
            open_positions=2,
            confirm_reason="15m无信号但高分放行",
            score_thresholds={"1h": 80},
            score_min=80,
            short_entry_penalty=10,
            major_symbols={"BTCUSDT", "ETHUSDT"},
            low_position_threshold_discount=5,
            no_confirm_threshold_penalty=8,
            weak_opposite_confirm_penalty=4,
            confirm_bonus=5,
            confirm_strong_bonus=8,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.threshold, 93)

    def test_c_v14_threshold_confirmation_and_tail(self):
        threshold = evaluate_c_v14_entry_threshold(
            timeframe="1h",
            side="short",
            score_thresholds={"1h": 50},
            score_min=50,
            long_penalty=0,
            short_entry_penalty=10,
        )
        self.assertTrue(threshold.allowed)
        self.assertEqual(threshold.threshold, 60)

        confirm = evaluate_c_v14_confirmation_gate(
            side="long",
            entry_score=90,
            confirm_signal={"trade_side": "short", "net_score": 10, "can_trade": True},
            confirm_timeframe="15m",
            no_confirm_high_score_pass=88,
            weak_confirm_min_score=18,
            confirm_min_score=25,
        )
        self.assertTrue(confirm.allowed)
        self.assertIn("弱反向", confirm.reason)

        tail = evaluate_c_v14_tail_guard(
            signal={"net_score": 60, "bb_pos": 90, "rsi": 60},
            side="long",
            tail_guard_min_score=75,
            tail_guard_long_bb_pos=83,
            tail_guard_short_bb_pos=17,
            tail_guard_min_vol_ratio=1.4,
            tail_guard_max_atr_pct=0.055,
        )
        self.assertFalse(tail.allowed)
        self.assertEqual(tail.gate, "tail_guard")

        stale = evaluate_c_v14_stale_entry_price_gate(recent_prices=[10.0, 10.0, 10.0])
        self.assertFalse(stale.allowed)
        self.assertEqual(stale.gate, "market_data_guard")
        fresh = evaluate_c_v14_stale_entry_price_gate(recent_prices=[10.0, 10.0, 10.1])
        self.assertTrue(fresh.allowed)

    def test_same_symbol_position_gate(self):
        self.assertTrue(
            evaluate_no_same_symbol_position_gate(
                has_exchange_position=False,
                has_local_position=False,
            ).allowed
        )
        self.assertFalse(
            evaluate_no_same_symbol_position_gate(
                has_exchange_position=True,
                has_local_position=False,
            ).allowed
        )
        self.assertFalse(
            evaluate_no_same_symbol_position_gate(
                has_exchange_position=False,
                has_local_position=True,
            ).allowed
        )

        self.assertFalse(evaluate_same_side_position_gate(has_same_side_position=True).allowed)
        self.assertTrue(evaluate_same_side_position_gate(has_same_side_position=False).allowed)

    def test_risk_position_gates(self):
        sl_gate = evaluate_symbol_stop_loss_gate(stop_loss_count=2, max_stop_loss_per_symbol=2)
        self.assertFalse(sl_gate.allowed)
        self.assertEqual(sl_gate.reason, "当日止损2次已达上限")
        self.assertTrue(
            evaluate_symbol_stop_loss_gate(
                stop_loss_count=1,
                max_stop_loss_per_symbol=2,
            ).allowed
        )

        sector_gate = evaluate_sector_position_gate(
            sector="AI",
            sector_position_count=3,
            max_positions_per_sector=3,
        )
        self.assertFalse(sector_gate.allowed)
        self.assertEqual(sector_gate.reason, "赛道[AI]已满3仓")
        self.assertTrue(
            evaluate_sector_position_gate(
                sector="Other",
                sector_position_count=99,
                max_positions_per_sector=3,
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()
