import importlib.util
import json
import sqlite3
import sys
import tempfile
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
        self.assertIn("paper_cost_sensitivity", packet)

    def test_window_quality_includes_paper_cost_sensitivity(self):
        quality = self.evolution.classify_window_quality(
            {
                "window_hours": 72,
                "opens": 60,
                "closes": 55,
                "forced_closes": 0,
                "open_failed": 0,
                "close_failed": 0,
                "realized_pnl_usdt": -25.0,
            }
        )

        rows = quality["paper_cost_sensitivity"]
        self.assertEqual([row["cost_pct"] for row in rows], [0.10, 0.15, 0.25, 0.35])
        self.assertEqual(quality["conservative_cost"]["cost_pct"], 0.25)
        self.assertTrue(quality["conservative_cost"]["rollback_loss_hit"])
        self.assertEqual(quality["label"], "bad")
        self.assertTrue(any("conservative_cost_0.25%" in reason for reason in quality["reasons"]))

    def test_decision_packet_surfaces_conservative_cost_risk(self):
        post_approval = {
            "windows": {
                "24h": {
                    "quality": {
                        "closed_samples": 55,
                        "required_closed_samples": 50,
                        "paper_cost_sensitivity": self.evolution.build_paper_cost_sensitivity(-25.0, 55),
                        "conservative_cost": self.evolution.build_paper_cost_sensitivity(-25.0, 55)[2],
                    }
                },
                "72h": {
                    "quality": {
                        "closed_samples": 55,
                        "required_closed_samples": 50,
                        "paper_cost_sensitivity": self.evolution.build_paper_cost_sensitivity(-10.0, 55),
                        "conservative_cost": self.evolution.build_paper_cost_sensitivity(-10.0, 55)[2],
                    }
                },
            }
        }

        packet = self.evolution.build_decision_packet(
            {"proposal": "cost stress"},
            {"shadow_pnl": 20, "original_pnl": 10, "sample_trades": 55},
            "support",
            {"pnl": 5},
            {},
            post_approval,
            [],
            "rollback_watch",
            "investigate_live_degradation",
            70,
            40,
        )

        self.assertIn("24h paper_cost_0.25% pnl -80.00", packet["risk"]["items"])
        self.assertEqual(packet["paper_cost_sensitivity"]["conservative_cost_pct"], 0.25)
        self.assertEqual(len(packet["paper_cost_sensitivity"]["window_24h"]), 4)

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

    def test_close_failed_attribution_flows_to_rollback_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "events.sqlite3"
            con = sqlite3.connect(db_path)
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
                    payload_json text
                )
                """
            )
            con.execute(
                """
                insert into events (ts, strategy, symbol, event_type, category, side, payload_json)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-03T16:00:00+08:00",
                    "B/v16",
                    "BTCUSDT",
                    "CLOSE_FAILED",
                    "order",
                    "long",
                    json.dumps({"reason": "close_confirm_failed remaining position still open"}),
                ),
            )
            con.commit()
            con.close()

            approvals = {
                "EXP-B": {
                    "base_strategy": "B/v16",
                    "approved_at": "2026-06-03T15:00:00+08:00",
                }
            }
            account_snapshot = {
                "accounts": [
                    {
                        "strategy": "B/v16",
                        "positions": [{"symbol": "BTCUSDT", "side": "long", "qty": 1}],
                    }
                ]
            }

            windows = self.evolution.summarize_post_approval_windows(db_path, approvals, account_snapshot)
            day = windows["EXP-B"]["windows"]["24h"]

            self.assertEqual(day["raw_close_failed"], 1)
            self.assertEqual(day["close_failed"], 1)
            self.assertEqual(day["resolved_close_failed"], 0)
            self.assertEqual(day["close_failed_reasons"][0]["reason"], "position_still_open_after_close")

            decision = {
                "candidate_id": "EXP-B",
                "strategy": "B/v16",
                "priority": "P0",
                "status": "rollback_required",
                "post_approval_live": windows["EXP-B"],
            }
            item = self.rollback.extract_item(decision)
            self.assertEqual(item["window_24h"]["close_failed"], 1)
            self.assertEqual(item["window_24h"]["close_failed_reasons"][0]["reason"], "position_still_open_after_close")
            payload = self.rollback.build_payload(Path(tmp) / "missing.json")
            self.assertEqual(payload["summary"]["items"], 0)
            rendered = self.rollback.render_md({"generated_at": "now", "summary": {"items": 1}, "items": [item]})
            self.assertIn("position_still_open_after_close", rendered)


if __name__ == "__main__":
    unittest.main()
