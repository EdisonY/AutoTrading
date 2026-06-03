import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具/a_v11_rollout_review.py"
    spec = importlib.util.spec_from_file_location("a_v11_rollout_review_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AV11RolloutReviewPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_decision_packet_contains_rollback_path_and_maturity(self):
        windows = {
            "24h": {"closed_samples": 25, "pnl_after_cost_usdt": -20, "forced_close_rate": 0.02},
            "72h": {
                "closed_samples": 80,
                "pnl_after_cost_usdt": -120,
                "forced_close_rate": 0.08,
                "close_reasons": [{"reason": "hard stop", "count": 3}],
                "top_losers": [{"symbol": "ABCUSDT", "side": "long", "pnl_usdt": -30}],
            },
            "168h": {"closed_samples": 90, "pnl_after_cost_usdt": -150, "forced_close_rate": 0.05},
        }
        decision = self.tool.verdict(windows)
        packet = self.tool.decision_packet(
            {"selected_live_parameter": {"trail_pullback_15m": 1.0}, "decision_reason": "approved evidence"},
            windows,
            decision,
        )

        self.assertEqual(packet["evidence_maturity"]["label"], "reviewable_72h")
        self.assertIn("72h after-cost pnl -120.00 USDT", packet["risk"])
        self.assertIn("keep automatic rollback disabled", packet["rollback_path"])
        self.assertEqual(packet["automation"], "disabled_report_only")


if __name__ == "__main__":
    unittest.main()
