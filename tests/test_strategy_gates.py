import unittest
from datetime import datetime, timedelta, timezone

from core.strategy_gates import (
    effective_a_v11_signal_score,
    evaluate_account_state_available_gate,
    evaluate_a_v11_entry_threshold,
    evaluate_a_v11_margin_sizing_gate,
    evaluate_a_v11_market_microstructure_gate,
    evaluate_a_v11_pool_capacity_replacement_gate,
    evaluate_a_v11_releasable_position,
    evaluate_a_v11_replacement_signal,
    evaluate_a_v11_resonance_required_gate,
    evaluate_active_position_limit_gate,
    evaluate_b_v16_confirmation_gate,
    evaluate_b_v16_entry_threshold,
    evaluate_b_v16_small_live_stage_guard,
    evaluate_c_v14_confirmation_gate,
    evaluate_c_v14_entry_threshold,
    evaluate_c_v14_market_microstructure_gate,
    evaluate_c_v14_stale_entry_price_gate,
    evaluate_c_v14_tail_guard,
    evaluate_consecutive_loss_cooldown_gate,
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


class StrategyGateParityTest(unittest.TestCase):
    def test_account_state_available_gate(self):
        ok = evaluate_account_state_available_gate(account_state_available=True)
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.reason, "account_state_available")

        missing = evaluate_account_state_available_gate(account_state_available=False)
        self.assertFalse(missing.allowed)
        self.assertEqual(missing.reason, "account_state_unavailable")
        self.assertEqual(missing.gate, "risk_gate")

        failed = evaluate_account_state_available_gate(account_state_available=False, read_error=True)
        self.assertFalse(failed.allowed)
        self.assertEqual(failed.reason, "account_state_read_failed")

    def test_tradability_gate(self):
        ok = evaluate_tradability_gate(tradable=True, reason="")
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.reason, "symbol_tradable")

        rejected = evaluate_tradability_gate(tradable=False, reason="MARKET_LOT_SIZE缺失")
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.gate, "tradability")
        self.assertEqual(rejected.reason, "MARKET_LOT_SIZE缺失")

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

        atr_zero = evaluate_a_v11_market_microstructure_gate(
            atr=0,
            side="long",
            stop_loss=9,
            entry_price=10,
        )
        self.assertFalse(atr_zero.allowed)
        self.assertEqual(atr_zero.reason, "ATR=0，止损止盈计算无效")

        bad_long_sl = evaluate_a_v11_market_microstructure_gate(
            atr=1,
            side="long",
            stop_loss=10,
            entry_price=10,
        )
        self.assertFalse(bad_long_sl.allowed)
        self.assertEqual(bad_long_sl.reason, "多单止损价10>=开仓价10")

        bad_short_sl = evaluate_a_v11_market_microstructure_gate(
            atr=1,
            side="short",
            stop_loss=9,
            entry_price=10,
        )
        self.assertFalse(bad_short_sl.allowed)
        self.assertEqual(bad_short_sl.reason, "空单止损价9<=开仓价10")

        self.assertTrue(
            evaluate_a_v11_market_microstructure_gate(
                atr=1,
                side="short",
                stop_loss=11,
                entry_price=10,
            ).allowed
        )

        pool_has_room = evaluate_a_v11_pool_capacity_replacement_gate(
            timeframe_full=False,
            replacement_signal_allowed=False,
        )
        self.assertTrue(pool_has_room.allowed)
        self.assertEqual(pool_has_room.reason, "timeframe_pool_has_capacity")

        pool_full_allowed = evaluate_a_v11_pool_capacity_replacement_gate(
            timeframe_full=True,
            replacement_signal_allowed=True,
        )
        self.assertTrue(pool_full_allowed.allowed)
        self.assertEqual(pool_full_allowed.reason, "timeframe_pool_full_replacement_allowed")

        pool_full_rejected = evaluate_a_v11_pool_capacity_replacement_gate(
            timeframe_full=True,
            replacement_signal_allowed=False,
            reject_reason="VPB周期池满且未达到强信号替换条件",
        )
        self.assertFalse(pool_full_rejected.allowed)
        self.assertEqual(pool_full_rejected.reason, "VPB周期池满且未达到强信号替换条件")

        resonance_missing = evaluate_a_v11_resonance_required_gate(
            require_resonance=True,
            has_resonance=False,
        )
        self.assertFalse(resonance_missing.allowed)
        self.assertEqual(resonance_missing.reason, "无共振，REQUIRE_RESONANCE=True")
        self.assertTrue(
            evaluate_a_v11_resonance_required_gate(
                require_resonance=False,
                has_resonance=False,
            ).allowed
        )
        self.assertTrue(
            evaluate_a_v11_resonance_required_gate(
                require_resonance=True,
                has_resonance=True,
            ).allowed
        )

        sizing_ok = evaluate_a_v11_margin_sizing_gate(
            quantity=40,
            price=10,
            risk_usdt=100,
            leverage=4,
            order_margin_tolerance_pct=0.05,
        )
        self.assertTrue(sizing_ok.allowed)
        self.assertEqual(sizing_ok.evidence["expected_margin_usdt"], 100)

        sizing_bad = evaluate_a_v11_margin_sizing_gate(
            quantity=20,
            price=10,
            risk_usdt=100,
            leverage=4,
            order_margin_tolerance_pct=0.05,
        )
        self.assertFalse(sizing_bad.allowed)
        self.assertEqual(sizing_bad.reason, "margin_sizing_out_of_tolerance")

        min_notional_adjusted = evaluate_a_v11_margin_sizing_gate(
            quantity=55,
            price=10,
            risk_usdt=100,
            leverage=4,
            order_margin_tolerance_pct=0.05,
            min_notional_floor=560,
        )
        self.assertTrue(min_notional_adjusted.allowed)
        self.assertTrue(min_notional_adjusted.evidence["min_notional_adjustment"])

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

        disabled = evaluate_b_v16_small_live_stage_guard(
            enabled=False,
            signal={},
            side="long",
            score=0,
            min_score=55,
            reverse_pass_score=65,
        )
        self.assertTrue(disabled.allowed)
        self.assertEqual(disabled.reason, "")

        low_score = evaluate_b_v16_small_live_stage_guard(
            enabled=True,
            signal={"reasons_long": ["CVD+OFI强势"]},
            side="long",
            score=54.9,
            min_score=55,
            reverse_pass_score=65,
        )
        self.assertFalse(low_score.allowed)
        self.assertEqual(low_score.reason, "小仓阶段保护: 分数54.9<55")

        reverse = evaluate_b_v16_small_live_stage_guard(
            enabled=True,
            signal={"reasons_long": ["CVD+OFI强势"], "reasons_short": ["EMA空头"]},
            side="long",
            score=64.9,
            min_score=55,
            reverse_pass_score=65,
        )
        self.assertFalse(reverse.allowed)
        self.assertEqual(reverse.reason, "小仓阶段保护: 逆势EMA且分数64.9<65")

        no_structure = evaluate_b_v16_small_live_stage_guard(
            enabled=True,
            signal={"reasons_long": ["量能放大"]},
            side="long",
            score=70,
            min_score=55,
            reverse_pass_score=65,
        )
        self.assertFalse(no_structure.allowed)
        self.assertEqual(no_structure.reason, "小仓阶段保护: 缺少订单流强共振或RSI结构")

        ok = evaluate_b_v16_small_live_stage_guard(
            enabled=True,
            signal={"reasons_short": ["RSI背离"], "reasons_long": ["EMA空头"]},
            side="short",
            score=70,
            min_score=55,
            reverse_pass_score=65,
        )
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.reason, "small_live_stage_guard_pass")

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

        c_market = evaluate_c_v14_market_microstructure_gate(atr=0)
        self.assertFalse(c_market.allowed)
        self.assertEqual(c_market.reason, "ATR=0")
        self.assertTrue(evaluate_c_v14_market_microstructure_gate(atr=0.1).allowed)

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

        self.assertFalse(evaluate_timeframe_position_gate(has_timeframe_position=True).allowed)
        self.assertEqual(
            evaluate_timeframe_position_gate(has_timeframe_position=True).reason,
            "timeframe_position_exists",
        )
        self.assertTrue(evaluate_timeframe_position_gate(has_timeframe_position=False).allowed)

    def test_symbol_blacklist_gate(self):
        blocked = evaluate_symbol_blacklist_gate(
            symbol="abcusdt",
            blacklisted_symbols={"ABCUSDT"},
            reason="ATR=0黑名单",
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "ATR=0黑名单")
        self.assertEqual(blocked.gate, "pre_filter")

        allowed = evaluate_symbol_blacklist_gate(symbol="XYZUSDT", blacklisted_symbols={"ABCUSDT"})
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reason, "symbol_allowed")

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

        overheat = evaluate_score_max_gate(score=86, score_max=85)
        self.assertFalse(overheat.allowed)
        self.assertEqual(overheat.reason, "评分86超过85")
        self.assertEqual(evaluate_score_max_gate(score=86.0, score_max=85).reason, "评分86.0超过85")
        self.assertTrue(evaluate_score_max_gate(score=85, score_max=85).allowed)

        active_limit = evaluate_active_position_limit_gate(open_positions=4, max_active_positions=4)
        self.assertFalse(active_limit.allowed)
        self.assertEqual(active_limit.reason, "活跃持仓4>=4只管理不新开")
        self.assertTrue(evaluate_active_position_limit_gate(open_positions=3, max_active_positions=4).allowed)

        qty_zero = evaluate_positive_quantity_gate(quantity=0)
        self.assertFalse(qty_zero.allowed)
        self.assertEqual(qty_zero.reason, "qty<=0")
        self.assertEqual(qty_zero.gate, "execution")
        self.assertTrue(evaluate_positive_quantity_gate(quantity=0.001).allowed)

        exec_ok = evaluate_execution_result_gate(success=True, preflight_rejected=False)
        self.assertTrue(exec_ok.allowed)
        self.assertEqual(exec_ok.gate, "execution")
        self.assertEqual(exec_ok.reason, "execution_success")

        exec_preflight = evaluate_execution_result_gate(
            success=False,
            preflight_rejected=True,
            code="exchange_min_notional",
            reason="min notional",
        )
        self.assertFalse(exec_preflight.allowed)
        self.assertEqual(exec_preflight.gate, "execution_preflight")
        self.assertEqual(exec_preflight.reason, "min notional")

        exec_failed = evaluate_execution_result_gate(
            success=False,
            preflight_rejected=False,
            code="-1007",
            message="status unknown",
        )
        self.assertFalse(exec_failed.allowed)
        self.assertEqual(exec_failed.gate, "execution")
        self.assertEqual(exec_failed.reason, "status unknown")

    def test_watchlist_score_adjustment(self):
        penalized = evaluate_watchlist_score_adjustment(
            symbol="abcusdt",
            score=8,
            watchlist_symbols={"ABCUSDT"},
            penalty=10,
        )
        self.assertEqual(penalized.adjusted_score, 0.0)
        self.assertEqual(penalized.reason, "watchlist_penalty_applied")

        unchanged = evaluate_watchlist_score_adjustment(
            symbol="XYZUSDT",
            score=80,
            watchlist_symbols={"ABCUSDT"},
            penalty=10,
        )
        self.assertEqual(unchanged.adjusted_score, 80)
        self.assertEqual(unchanged.reason, "score_unchanged")

    def test_consecutive_loss_cooldown_gate(self):
        now = datetime(2026, 6, 3, 4, 0, tzinfo=timezone.utc)
        clear = evaluate_consecutive_loss_cooldown_gate(
            consecutive_losses=4,
            last_loss_time=now - timedelta(minutes=10),
            now=now,
            min_consecutive_losses=5,
            cooldown_minutes=120,
        )
        self.assertTrue(clear.allowed)
        self.assertEqual(clear.reason, "consecutive_loss_cooldown_clear")

        active = evaluate_consecutive_loss_cooldown_gate(
            consecutive_losses=5,
            last_loss_time=now - timedelta(minutes=30),
            now=now,
            min_consecutive_losses=5,
            cooldown_minutes=120,
        )
        self.assertFalse(active.allowed)
        self.assertEqual(active.reason, "consecutive_loss_cooldown_active")
        self.assertEqual(active.evidence["remaining_minutes"], 90)

        expired = evaluate_consecutive_loss_cooldown_gate(
            consecutive_losses=5,
            last_loss_time=now - timedelta(minutes=121),
            now=now,
            min_consecutive_losses=5,
            cooldown_minutes=120,
        )
        self.assertTrue(expired.allowed)
        self.assertEqual(expired.reason, "consecutive_loss_cooldown_expired")

    def test_symbol_cooldown_gate(self):
        now = datetime(2026, 6, 3, 4, 0, tzinfo=timezone.utc)
        self.assertTrue(evaluate_symbol_cooldown_gate(cooldown_until=None, now=now).allowed)
        self.assertTrue(
            evaluate_symbol_cooldown_gate(
                cooldown_until=now - timedelta(minutes=1),
                now=now,
            ).allowed
        )
        active = evaluate_symbol_cooldown_gate(
            cooldown_until=now + timedelta(minutes=17),
            now=now,
        )
        self.assertFalse(active.allowed)
        self.assertEqual(active.reason, "symbol_cooldown_active")
        self.assertEqual(active.evidence["remaining_minutes"], 17)

    def test_symbol_scan_cooldown_gate(self):
        self.assertTrue(evaluate_symbol_scan_cooldown_gate(cooldown_ticks=0).allowed)
        self.assertTrue(evaluate_symbol_scan_cooldown_gate(cooldown_ticks=None).allowed)
        active = evaluate_symbol_scan_cooldown_gate(cooldown_ticks=3)
        self.assertFalse(active.allowed)
        self.assertEqual(active.reason, "symbol_scan_cooldown_active")
        self.assertEqual(active.evidence["cooldown_ticks"], 3)


if __name__ == "__main__":
    unittest.main()
