import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StrategyDecisionPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evolution = load_tool("strategy_evolution_gate_tool", "部署工具/strategy_evolution_gate.py")
        cls.rollback = load_tool("rollback_watch_review_tool", "部署工具/rollback_watch_review.py")

    def test_decision_packet_contains_operator_fields(self):
        win = {
            "14d": {"status": "pass"},
            "30d": {"status": "pass"},
        }
        post_approval = {
            "windows": {
                "72h": {"quality": {"closed_samples": 55, "required_closed_samples": 50}},
            }
        }
        packet = self.evolution.build_decision_packet(
            {"proposal": "tighten trailing pullback", "problem": "quality decay"},
            {"shadow_pnl": 140, "original_pnl": 20, "sample_trades": 80},
            "support",
            {"pnl": 33.5},
            {"open_positions": 3, "unrealized_pnl_usdt": -12.0},
            post_approval,
            ["72h after-cost PnL weak"],
            "rollback_watch",
            "investigate_live_degradation",
            77,
            35,
        )

        self.assertEqual(packet["change"], "tighten trailing pullback")
        self.assertIn("shadow pnl delta +120.00", packet["expected_advantage"])
        self.assertIn("72h after-cost PnL weak", packet["risk"]["items"])
        self.assertIn("prepare_rollback_review_packet", packet["rollback_path"])
        self.assertEqual(packet["automation"], "disabled_report_only")
        self.assertEqual(packet["evidence_maturity"]["label"], "reviewable")

    def test_rollback_review_keeps_decision_packet(self):
        decision = {
            "candidate_id": "EXP-1",
            "strategy": "A/v11",
            "priority": "P1",
            "status": "rollback_watch",
            "blockers": ["24h loss"],
            "decision_packet": {
                "change": "tighten exit",
                "expected_advantage": "less giveback",
                "risk": ["sample thin"],
                "rollback_path": ["revert config"],
                "automation": "disabled_report_only",
                "evidence_maturity": {"label": "thin"},
            },
            "post_approval_live": {
                "approved_at": "2026-06-01T00:00:00+08:00",
                "windows": {
                    "24h": {
                        "opens": 10,
                        "quality": {
                            "closed_samples": 8,
                            "realized_pnl_after_cost": -90,
                            "forced_close_rate": 0.0,
                            "open_failed_rate": 0.0,
                        },
                        "regime": {"label": "range"},
                    },
                    "72h": {
                        "quality": {
                            "closed_samples": 20,
                            "realized_pnl_after_cost": -100,
                        }
                    },
                },
            },
        }

        item = self.rollback.extract_item(decision)
        self.assertIsNotNone(item)
        self.assertEqual(item["decision_packet"]["change"], "tighten exit")
        rendered = self.rollback.render_md({"generated_at": "now", "summary": {"items": 1, "decision_packets": 1}, "items": [item]})
        self.assertIn("## Decision Packets", rendered)
        self.assertIn("tighten exit", rendered)


if __name__ == "__main__":
    unittest.main()
