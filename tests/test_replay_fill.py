import unittest

from core.replay_fill import ReplayFillRequest, simulate_replay_fill


class ReplayFillTest(unittest.TestCase):
    def test_long_take_profit_after_fee(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=10,
                quantity=2,
                stop_loss=9,
                take_profit=12,
                fee_bps=10,
            ),
            [
                {"ts": "t1", "open": 10, "high": 11, "low": 9.5, "close": 10.5},
                {"ts": "t2", "open": 10.5, "high": 12.2, "low": 10.2, "close": 12},
            ],
        )

        self.assertEqual(result.exit_reason, "take_profit")
        self.assertEqual(result.exit_ts, "t2")
        self.assertEqual(result.gross_pnl_usdt, 4.0)
        self.assertEqual(result.fee_usdt, 0.044)
        self.assertEqual(result.net_pnl_usdt, 3.956)

    def test_short_stop_loss(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="short",
                entry_price=10,
                quantity=3,
                stop_loss=11,
                take_profit=8,
                fee_bps=0,
            ),
            [{"ts": "t1", "open": 10, "high": 11.2, "low": 9.5, "close": 10.5}],
        )

        self.assertEqual(result.exit_reason, "stop_loss")
        self.assertEqual(result.gross_pnl_usdt, -3.0)

    def test_conservative_intrabar_prefers_stop_when_both_hit(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=10,
                quantity=1,
                stop_loss=9,
                take_profit=11,
                fee_bps=0,
                conservative_intrabar=True,
            ),
            [{"ts": "t1", "open": 10, "high": 11.5, "low": 8.5, "close": 10.2}],
        )

        self.assertEqual(result.exit_reason, "stop_loss")
        self.assertEqual(result.exit_price, 9)

    def test_end_of_window_with_slippage(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=10,
                quantity=1,
                fee_bps=0,
                slippage_bps=10,
            ),
            [{"ts": "t1", "open": 10, "high": 10.2, "low": 9.8, "close": 10.1}],
        )

        self.assertEqual(result.exit_reason, "end_of_window")
        self.assertAlmostEqual(result.entry_price, 10.01)
        self.assertAlmostEqual(result.exit_price, 10.0899)
        self.assertAlmostEqual(result.net_pnl_usdt, 0.0799)

    def test_long_trailing_stop_after_activation(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=1,
                trailing_stop_pct=2,
                trailing_activation_pct=3,
                fee_bps=0,
            ),
            [
                {"ts": "t1", "open": 100, "high": 102, "low": 99, "close": 101},
                {"ts": "t2", "open": 101, "high": 105, "low": 103, "close": 104},
                {"ts": "t3", "open": 104, "high": 104.2, "low": 102.8, "close": 103},
            ],
        )

        self.assertEqual(result.exit_reason, "trailing_stop")
        self.assertEqual(result.exit_ts, "t3")
        self.assertAlmostEqual(result.exit_price, 102.9)
        self.assertAlmostEqual(result.net_pnl_usdt, 2.9)

    def test_short_trailing_stop_after_activation(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="short",
                entry_price=100,
                quantity=2,
                trailing_stop_pct=1,
                trailing_activation_pct=2,
                fee_bps=0,
            ),
            [
                {"ts": "t1", "open": 100, "high": 100.5, "low": 98.5, "close": 99},
                {"ts": "t2", "open": 99, "high": 99.2, "low": 96, "close": 97},
                {"ts": "t3", "open": 97, "high": 97.2, "low": 96.5, "close": 97},
            ],
        )

        self.assertEqual(result.exit_reason, "trailing_stop")
        self.assertEqual(result.exit_ts, "t2")
        self.assertAlmostEqual(result.exit_price, 96.96)
        self.assertAlmostEqual(result.net_pnl_usdt, 6.08)

    def test_long_atr_trailing_stop_after_activation(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=1,
                atr=2,
                trailing_stop_atr=1.0,
                trailing_activation_atr=1.0,
                fee_bps=0,
            ),
            [
                {"ts": "t1", "open": 100, "high": 101, "low": 99.5, "close": 100.5},
                {"ts": "t2", "open": 100.5, "high": 103, "low": 101.5, "close": 102.5},
                {"ts": "t3", "open": 102.5, "high": 103.2, "low": 101.0, "close": 101.2},
            ],
        )

        self.assertEqual(result.exit_reason, "trailing_stop")
        self.assertEqual(result.exit_ts, "t3")
        self.assertAlmostEqual(result.exit_price, 101.2)
        self.assertAlmostEqual(result.net_pnl_usdt, 1.2)

    def test_short_atr_trailing_stop_after_activation(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="short",
                entry_price=100,
                quantity=2,
                atr=2,
                trailing_stop_atr=0.8,
                trailing_activation_atr=1.0,
                fee_bps=0,
            ),
            [
                {"ts": "t1", "open": 100, "high": 100.5, "low": 99, "close": 99.5},
                {"ts": "t2", "open": 99.5, "high": 98.0, "low": 96, "close": 96.8},
                {"ts": "t3", "open": 96.8, "high": 97.7, "low": 96.5, "close": 97.2},
            ],
        )

        self.assertEqual(result.exit_reason, "trailing_stop")
        self.assertEqual(result.exit_ts, "t2")
        self.assertAlmostEqual(result.exit_price, 97.6)
        self.assertAlmostEqual(result.net_pnl_usdt, 4.8)

    def test_atr_trailing_requires_positive_atr(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    symbol="ABCUSDT",
                    side="long",
                    entry_price=100,
                    quantity=1,
                    atr=0,
                    trailing_stop_atr=1,
                ),
                [{"ts": "t1", "open": 100, "high": 102, "low": 99, "close": 101}],
            )

    def test_partial_fill_quantity_cap_scales_pnl(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=10,
                quantity=10,
                take_profit=11,
                fee_bps=0,
                max_fill_quantity=4,
            ),
            [{"ts": "t1", "open": 10, "high": 11.2, "low": 9.9, "close": 11}],
        )

        self.assertEqual(result.exit_reason, "take_profit")
        self.assertEqual(result.quantity, 4)
        self.assertEqual(result.requested_quantity, 10)
        self.assertEqual(result.unfilled_quantity, 6)
        self.assertEqual(result.fill_ratio, 0.4)
        self.assertTrue(result.partial_fill)
        self.assertEqual(result.fill_status, "partial")
        self.assertEqual(result.gross_pnl_usdt, 4.0)

    def test_partial_fill_notional_cap_scales_pnl(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="short",
                entry_price=20,
                quantity=5,
                take_profit=18,
                fee_bps=0,
                max_fill_notional_usdt=40,
            ),
            [{"ts": "t1", "open": 20, "high": 20.2, "low": 17.8, "close": 18}],
        )

        self.assertEqual(result.exit_reason, "take_profit")
        self.assertEqual(result.quantity, 2)
        self.assertEqual(result.requested_quantity, 5)
        self.assertEqual(result.unfilled_quantity, 3)
        self.assertEqual(result.fill_ratio, 0.4)
        self.assertTrue(result.partial_fill)
        self.assertEqual(result.gross_pnl_usdt, 4.0)

    def test_partial_fill_can_be_rejected_for_strict_replay(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    symbol="ABCUSDT",
                    side="long",
                    entry_price=10,
                    quantity=10,
                    max_fill_quantity=4,
                    allow_partial_fill=False,
                ),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )

    def test_depth_order_book_entry_uses_asks_for_long(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=3,
                fee_bps=0,
                entry_order_book={"asks": [["100", "1"], ["101", "2"]]},
            ),
            [{"ts": "t1", "open": 100, "high": 102.2, "low": 99.5, "close": 102}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.order_book_levels_used, 2)
        self.assertAlmostEqual(result.entry_price, 100.6666666667)
        self.assertEqual(result.quantity, 3)
        self.assertAlmostEqual(result.gross_pnl_usdt, 4.0)
        self.assertAlmostEqual(result.depth_slippage_usdt, 2.0)
        self.assertAlmostEqual(result.slippage_usdt, 2.0)
        self.assertEqual(result.order_book_available_quantity, 3)
        self.assertEqual(result.order_book_fill_ratio, 1.0)

    def test_depth_order_book_entry_uses_bids_for_short(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="short",
                entry_price=100,
                quantity=3,
                fee_bps=0,
                entry_order_book={"bids": [["100", "1"], ["99", "2"]]},
            ),
            [{"ts": "t1", "open": 100, "high": 100.5, "low": 97.5, "close": 98}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.order_book_levels_used, 2)
        self.assertAlmostEqual(result.entry_price, 99.3333333333)
        self.assertEqual(result.quantity, 3)
        self.assertAlmostEqual(result.gross_pnl_usdt, 4.0)
        self.assertAlmostEqual(result.depth_slippage_usdt, 2.0)
        self.assertAlmostEqual(result.slippage_usdt, 2.0)
        self.assertEqual(result.order_book_available_quantity, 3)
        self.assertEqual(result.order_book_fill_ratio, 1.0)

    def test_depth_partial_fill_when_book_liquidity_is_thin(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=5,
                fee_bps=0,
                entry_order_book={"asks": [["100", "2"]]},
            ),
            [{"ts": "t1", "open": 100, "high": 101.5, "low": 99.5, "close": 101}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.quantity, 2)
        self.assertEqual(result.requested_quantity, 5)
        self.assertEqual(result.unfilled_quantity, 3)
        self.assertEqual(result.fill_ratio, 0.4)
        self.assertTrue(result.partial_fill)
        self.assertEqual(result.order_book_available_quantity, 2)
        self.assertEqual(result.order_book_fill_ratio, 0.4)

    def test_depth_order_book_can_limit_levels(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=4,
                fee_bps=0,
                entry_order_book={"asks": [["100", "2"], ["101", "2"]]},
                entry_order_book_max_levels=1,
            ),
            [{"ts": "t1", "open": 100, "high": 101.5, "low": 99.5, "close": 101}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.order_book_levels_used, 1)
        self.assertEqual(result.order_book_available_quantity, 2)
        self.assertEqual(result.quantity, 2)
        self.assertEqual(result.unfilled_quantity, 2)
        self.assertEqual(result.fill_ratio, 0.5)
        self.assertEqual(result.order_book_fill_ratio, 0.5)

    def test_depth_order_book_can_discount_visible_liquidity(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=4,
                fee_bps=0,
                entry_order_book={"asks": [["100", "2"], ["101", "2"]]},
                entry_order_book_liquidity_factor=0.5,
            ),
            [{"ts": "t1", "open": 100, "high": 101.5, "low": 99.5, "close": 101}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.order_book_levels_used, 2)
        self.assertEqual(result.order_book_available_quantity, 2)
        self.assertAlmostEqual(result.entry_price, 100.5)
        self.assertEqual(result.quantity, 2)
        self.assertEqual(result.unfilled_quantity, 2)
        self.assertEqual(result.fill_ratio, 0.5)
        self.assertEqual(result.order_book_fill_ratio, 0.5)
        self.assertAlmostEqual(result.depth_slippage_usdt, 1.0)

    def test_depth_order_book_queue_ahead_consumes_front_liquidity(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=3,
                fee_bps=0,
                entry_order_book={"asks": [["100", "2"], ["101", "3"]]},
                entry_order_book_queue_ahead_quantity=2,
            ),
            [{"ts": "t1", "open": 100, "high": 102.5, "low": 99.5, "close": 102}],
        )

        self.assertEqual(result.entry_fill_source, "order_book")
        self.assertEqual(result.order_book_levels_used, 1)
        self.assertEqual(result.order_book_available_quantity, 3)
        self.assertEqual(result.quantity, 3)
        self.assertAlmostEqual(result.entry_price, 101)
        self.assertAlmostEqual(result.depth_slippage_usdt, 3.0)
        self.assertEqual(result.order_book_queue_ahead_quantity, 2)

    def test_entry_market_impact_bps_worsens_entry_price(self):
        result = simulate_replay_fill(
            ReplayFillRequest(
                symbol="ABCUSDT",
                side="long",
                entry_price=100,
                quantity=2,
                fee_bps=0,
                entry_market_impact_bps=10,
            ),
            [{"ts": "t1", "open": 100, "high": 101.5, "low": 99.5, "close": 101}],
        )

        self.assertEqual(result.entry_fill_source, "synthetic")
        self.assertAlmostEqual(result.entry_price, 100.1)
        self.assertAlmostEqual(result.market_impact_usdt, 0.2)
        self.assertAlmostEqual(result.gross_pnl_usdt, 1.8)

    def test_depth_partial_fill_can_be_rejected(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    symbol="ABCUSDT",
                    side="long",
                    entry_price=100,
                    quantity=5,
                    fee_bps=0,
                    allow_partial_fill=False,
                    entry_order_book={"asks": [["100", "2"]]},
                ),
                [{"ts": "t1", "open": 100, "high": 101, "low": 99, "close": 100}],
            )

    def test_explicit_empty_depth_book_is_not_synthetic(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    symbol="ABCUSDT",
                    side="long",
                    entry_price=100,
                    quantity=1,
                    entry_order_book={"asks": []},
                ),
                [{"ts": "t1", "open": 100, "high": 101, "low": 99, "close": 100}],
            )

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(ReplayFillRequest("ABCUSDT", "long", 10, 0), [])
        with self.assertRaises(ValueError):
            simulate_replay_fill(ReplayFillRequest("ABCUSDT", "flat", 10, 1), [{"open": 1, "high": 1, "low": 1, "close": 1}])
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest("ABCUSDT", "long", 10, 1, max_fill_quantity=-1),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    "ABCUSDT",
                    "long",
                    10,
                    1,
                    entry_order_book={"asks": [["10", "1"]]},
                    entry_order_book_max_levels=0,
                ),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    "ABCUSDT",
                    "long",
                    10,
                    1,
                    entry_order_book={"asks": [["10", "1"]]},
                    entry_order_book_liquidity_factor=1.5,
                ),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    "ABCUSDT",
                    "long",
                    10,
                    1,
                    entry_order_book_queue_ahead_quantity=-1,
                ),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )
        with self.assertRaises(ValueError):
            simulate_replay_fill(
                ReplayFillRequest(
                    "ABCUSDT",
                    "long",
                    10,
                    1,
                    entry_market_impact_bps=-1,
                ),
                [{"ts": "t1", "open": 10, "high": 11, "low": 9, "close": 10}],
            )


if __name__ == "__main__":
    unittest.main()
