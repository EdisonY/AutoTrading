import importlib.util
import json
import sqlite3
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

    def test_summarize_window_adds_exit_models_and_cost_sensitivity(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            """
            create table events (
                id integer primary key,
                ts text,
                strategy text,
                symbol text,
                event_type text,
                category text,
                side text,
                reason text,
                payload_json text
            )
            """
        )
        con.executemany(
            """
            insert into events (ts, strategy, symbol, event_type, category, side, reason, payload_json)
            values (?, 'A/v11', ?, ?, '', ?, ?, ?)
            """,
            [
                (
                    "2026-06-03T10:00:00+08:00",
                    "AAAUSDT",
                    "CLOSE",
                    "long",
                    "浮动止损",
                    json.dumps({"pnl_usd": -12.5, "reason": "浮动止损", "timeframe": "15m"}),
                ),
                (
                    "2026-06-03T11:00:00+08:00",
                    "BBBUSDT",
                    "FORCED_CLOSE",
                    "short",
                    "交易所硬顶30%",
                    json.dumps({"pnl_usd": -90, "reason": "交易所硬顶30%", "timeframe": "30m"}),
                ),
                (
                    "2026-06-03T12:00:00+08:00",
                    "CCCUSDT",
                    "CLOSE",
                    "long",
                    "交易所止盈止损自动平仓",
                    json.dumps({"pnl_usd": 20, "reason": "交易所止盈止损自动平仓", "timeframe": "15m"}),
                ),
            ],
        )
        rows = list(con.execute("select * from events order by id"))
        con.close()
        metrics = self.tool.summarize_window(
            rows,
            self.tool.parse_dt("2026-06-03T09:00:00+08:00"),
            self.tool.parse_dt("2026-06-03T13:00:00+08:00"),
        )

        exit_models = {item["model"]: item for item in metrics["exit_models"]}
        self.assertEqual(exit_models["atr_trailing_stop"]["count"], 1)
        self.assertEqual(exit_models["max_loss_guard"]["count"], 1)
        self.assertEqual(exit_models["exchange_auto_close"]["count"], 1)
        self.assertAlmostEqual(exit_models["max_loss_guard"]["pnl_usdt"], -90.0)
        self.assertEqual(metrics["closed_samples"], 3)
        self.assertAlmostEqual(metrics["cost_sensitivity"][0]["estimated_cost_usdt"], 1.2)
        self.assertAlmostEqual(metrics["cost_sensitivity"][2]["pnl_after_cost_usdt"], -85.5)
        self.assertTrue(metrics["cost_sensitivity"][2]["rollback_review_loss_hit"])

    def test_decision_packet_includes_exit_model_and_cost_fields(self):
        windows = {
            "24h": {"closed_samples": 10, "pnl_after_cost_usdt": -10, "forced_close_rate": 0},
            "72h": {
                "closed_samples": 60,
                "pnl_after_cost_usdt": -90,
                "forced_close_rate": 0.05,
                "exit_models": [{"model": "max_loss_guard", "count": 4, "pnl_usdt": -130}],
                "cost_sensitivity": [
                    {"cost_pct": 0.10, "pnl_after_cost_usdt": -78},
                    {"cost_pct": 0.25, "pnl_after_cost_usdt": -114},
                ],
            },
            "168h": {"closed_samples": 80, "pnl_after_cost_usdt": -110, "forced_close_rate": 0.04},
        }
        packet = self.tool.decision_packet({}, windows, self.tool.verdict(windows))

        self.assertEqual(packet["exit_model_summary_72h"][0]["model"], "max_loss_guard")
        self.assertEqual(packet["cost_sensitivity_72h"][1]["cost_pct"], 0.25)
        self.assertIn("72h exit models: max_loss_guard(4, -130.00)", packet["risk"])
        self.assertIn("72h after-cost pnl at 0.25% cost -114.00 USDT", packet["risk"])


if __name__ == "__main__":
    unittest.main()
