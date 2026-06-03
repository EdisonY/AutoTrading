import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_counterfactual():
    path = ROOT / "部署工具/counterfactual_open_skips.py"
    spec = importlib.util.spec_from_file_location("counterfactual_open_skips_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CounterfactualReplayFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_counterfactual()

    def test_evaluate_uses_shared_replay_fill_kernel(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=1,
            ts=event_ts,
            strategy="A/v11",
            symbol="ABCUSDT",
            side="long",
            timeframe="1m",
            score=88.0,
            stage="risk",
            layer="position",
            reason="test skip",
            sentinel=False,
            replay_decision="reject",
            replay_gate="position",
            payload={},
        )
        bars = {
            "ABCUSDT": {
                entry_ms: [entry_ms, "100", "101.5", "99.5", "100.5"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.5", "101", "100", "100.8"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "take_profit")
        self.assertEqual(result.end_price, 101.0)
        self.assertAlmostEqual(result.sim_pnl_usdt, 3.598, places=3)
        self.assertEqual(result.replay_fill["exit_reason"], "take_profit")

    def test_a_v11_uses_atr_trailing_exit_when_payload_has_atr(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=2,
            ts=event_ts,
            strategy="A/v11",
            symbol="ATRUSDT",
            side="long",
            timeframe="15m",
            score=90.0,
            stage="threshold",
            layer="strategy",
            reason="test atr trailing",
            sentinel=False,
            replay_decision="reject",
            replay_gate="threshold",
            payload={"raw_event": {"atr": 0.5}},
        )
        bars = {
            "ATRUSDT": {
                entry_ms: [entry_ms, "100", "100.7", "100.4", "100.6"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.6", "100.8", "100.1", "100.2"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "trailing_stop")
        self.assertEqual(result.replay_fill["exit_model"], "a_v11_atr_trailing")
        self.assertEqual(result.replay_fill["trailing_timeframe"], "15m")
        self.assertEqual(result.replay_fill["trailing_activation_atr"], 1.0)
        self.assertEqual(result.replay_fill["trailing_stop_atr"], 1.0)
        self.assertAlmostEqual(result.end_price, 100.3)

    def test_non_a_v11_keeps_fixed_pct_barrier_even_with_atr_payload(self):
        event_ts = datetime(2026, 6, 1, 0, 0, 30, tzinfo=timezone.utc)
        entry_ts = event_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_ms = int(entry_ts.timestamp() * 1000)
        event = self.tool.SkipEvent(
            event_id=3,
            ts=event_ts,
            strategy="B/v16",
            symbol="BETAUSDT",
            side="long",
            timeframe="15m",
            score=70.0,
            stage="confirmation",
            layer="strategy",
            reason="test fixed barrier",
            sentinel=False,
            replay_decision="reject",
            replay_gate="confirmation",
            payload={"atr": 0.5},
        )
        bars = {
            "BETAUSDT": {
                entry_ms: [entry_ms, "100", "100.7", "100.4", "100.6"],
                entry_ms + 60_000: [entry_ms + 60_000, "100.6", "100.8", "100.1", "100.2"],
            }
        }

        result = self.tool.evaluate(
            event,
            horizon=2,
            bars_by_symbol=bars,
            now=entry_ts + timedelta(minutes=3),
            margin_usdt=100,
            leverage=4,
            tp_pct=1,
            sl_pct=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.barrier_outcome, "end_of_window")
        self.assertEqual(result.replay_fill["exit_model"], "fixed_pct_barrier")
        self.assertNotIn("trailing_stop_atr", result.replay_fill)


if __name__ == "__main__":
    unittest.main()
