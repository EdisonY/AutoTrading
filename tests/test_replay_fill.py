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

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            simulate_replay_fill(ReplayFillRequest("ABCUSDT", "long", 10, 0), [])
        with self.assertRaises(ValueError):
            simulate_replay_fill(ReplayFillRequest("ABCUSDT", "flat", 10, 1), [{"open": 1, "high": 1, "low": 1, "close": 1}])


if __name__ == "__main__":
    unittest.main()
