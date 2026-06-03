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


if __name__ == "__main__":
    unittest.main()
