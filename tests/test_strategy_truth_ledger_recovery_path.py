import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具/strategy_truth_ledger.py"
    spec = importlib.util.spec_from_file_location("strategy_truth_ledger_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StrategyTruthLedgerRecoveryPathTests(unittest.TestCase):
    def test_enrich_recovery_path_metrics_attaches_mfe_mae_and_drawdown(self):
        tool = load_tool()
        recovery = [
            {
                "account": "acct-b",
                "strategy": "B/v16",
                "symbol": "BTCUSDT",
                "side": "long",
                "margin": 100.0,
                "unrealized_pnl": 1.0,
                "snapshot_ts": "2026-06-03T10:00:00+08:00",
            }
        ]
        history = [
            {
                "ts": "2026-06-03T08:00:00+08:00",
                "account": "acct-b",
                "symbol": "BTCUSDT",
                "side": "long",
                "unrealized_pnl_pct_on_margin": -2.0,
                "directional_return_pct": -0.5,
            },
            {
                "ts": "2026-06-03T09:00:00+08:00",
                "account": "acct-b",
                "symbol": "BTCUSDT",
                "side": "long",
                "unrealized_pnl_pct_on_margin": 5.0,
                "directional_return_pct": 1.25,
            },
            {
                "ts": "2026-06-03T10:00:00+08:00",
                "account": "acct-b",
                "symbol": "BTCUSDT",
                "side": "long",
                "unrealized_pnl_pct_on_margin": 1.0,
                "directional_return_pct": 0.25,
            },
        ]

        enriched = tool.enrich_recovery_path_metrics(recovery, history)
        pos = enriched[0]

        self.assertEqual(pos["first_seen_ts"], "2026-06-03T08:00:00+08:00")
        self.assertEqual(pos["path_samples"], 3)
        self.assertEqual(pos["mfe_pct_on_margin"], 5.0)
        self.assertEqual(pos["mae_pct_on_margin"], -2.0)
        self.assertEqual(pos["drawdown_from_mfe_pct_on_margin"], -4.0)
        self.assertEqual(pos["mfe_price_pct"], 1.25)
        self.assertEqual(pos["mae_price_pct"], -0.5)

    def test_review_recovery_positions_exports_path_metrics(self):
        tool = load_tool()
        recovery = [
            {
                "strategy": "B/v16",
                "account": "acct-b",
                "symbol": "ETHUSDT",
                "side": "short",
                "entry_price": 2000.0,
                "mark_price": 1980.0,
                "qty": 0.1,
                "margin": 50.0,
                "unrealized_pnl": 2.5,
                "snapshot_ts": "2026-06-03T08:00:00+08:00",
                "first_seen_ts": "2026-06-03T07:00:00+08:00",
                "path_samples": 4,
                "mfe_pct_on_margin": 8.0,
                "mae_pct_on_margin": -1.0,
                "drawdown_from_mfe_pct_on_margin": -3.0,
                "mfe_price_pct": 1.5,
                "mae_price_pct": -0.2,
            }
        ]

        review = tool.review_recovery_positions(recovery)
        pos = review["positions"][0]

        self.assertEqual(review["path_metric_note"], "report_only_snapshot_path_mfe_mae_and_signal_evidence")
        self.assertEqual(pos["first_seen_ts"], "2026-06-03T07:00:00+08:00")
        self.assertEqual(pos["path_samples"], 4)
        self.assertEqual(pos["mfe_pct_on_margin"], 8.0)
        self.assertEqual(pos["mae_pct_on_margin"], -1.0)
        self.assertEqual(pos["drawdown_from_mfe_pct_on_margin"], -3.0)
        self.assertEqual(pos["mfe_price_pct"], 1.5)
        self.assertEqual(pos["mae_price_pct"], -0.2)

    def test_attach_recovery_signal_evidence_marks_reopen_and_opposite_review(self):
        tool = load_tool()
        recovery = [
            {
                "strategy": "B/v16",
                "account": "acct-b",
                "symbol": "ETHUSDT",
                "side": "short",
                "first_seen_ts": "2026-06-03T08:00:00+08:00",
                "snapshot_ts": "2026-06-03T10:00:00+08:00",
            }
        ]
        signal_events = [
            {
                "ts": "2026-06-03T07:30:00+08:00",
                "strategy": "B/v16",
                "symbol": "ETHUSDT",
                "event_type": "SIGNAL",
                "side": "long",
                "score": 90,
                "can_trade": True,
            },
            {
                "ts": "2026-06-03T08:30:00+08:00",
                "strategy": "B/v16",
                "symbol": "ETHUSDT",
                "event_type": "OPEN_SKIPPED",
                "side": "short",
                "score": -88,
                "can_trade": None,
                "reason": "same-symbol position",
            },
            {
                "ts": "2026-06-03T09:00:00+08:00",
                "strategy": "B/v16",
                "symbol": "ETHUSDT",
                "event_type": "SIGNAL",
                "side": "long",
                "score": 91,
                "can_trade": True,
                "reason": "opposite long signal",
            },
        ]

        enriched = tool.attach_recovery_signal_evidence(recovery, signal_events)
        pos = enriched[0]

        self.assertEqual(pos["same_strategy_signal_count"], 1)
        self.assertEqual(pos["same_strategy_open_like_count"], 1)
        self.assertEqual(pos["opposite_signal_count"], 1)
        self.assertEqual(pos["opposite_open_like_count"], 1)
        self.assertEqual(pos["signal_shadow_action"], "opposite_signal_review")
        self.assertEqual(pos["latest_same_strategy_signal"]["event_type"], "OPEN_SKIPPED")
        self.assertEqual(pos["latest_opposite_signal"]["score"], 91)

    def test_recovery_review_and_policy_use_signal_evidence_report_only(self):
        tool = load_tool()
        recovery = [
            {
                "strategy": "B/v16",
                "account": "acct-b",
                "symbol": "ETHUSDT",
                "side": "short",
                "margin": 100.0,
                "unrealized_pnl": 3.0,
                "snapshot_ts": "2026-06-03T10:00:00+08:00",
                "same_strategy_signal_count": 1,
                "same_strategy_open_like_count": 1,
                "opposite_signal_count": 1,
                "opposite_open_like_count": 1,
                "signal_shadow_action": "opposite_signal_review",
            }
        ]

        review = tool.review_recovery_positions(recovery)
        policies = tool.evaluate_recovery_exit_policies(recovery)
        pos = review["positions"][0]

        self.assertEqual(review["signal_counts"]["same_strategy_reopen_supported"], 1)
        self.assertEqual(review["signal_counts"]["opposite_signal_review"], 1)
        self.assertEqual(pos["risk"], "review")
        self.assertEqual(pos["shadow_action"], "opposite_signal_manual_review")
        self.assertEqual(pos["signal_shadow_action"], "opposite_signal_review")
        self.assertIn("不自动平仓", pos["note"])
        self.assertEqual(policies["opposite_signal"]["would_exit"], 1)

    def test_strategy_exit_evidence_marks_mfe_drawdown_review(self):
        tool = load_tool()
        evidence = tool.build_recovery_strategy_exit_evidence(
            {
                "strategy": "B/v16",
                "mfe_pct_on_margin": 9.0,
                "mae_pct_on_margin": -1.0,
                "drawdown_from_mfe_pct_on_margin": -6.5,
                "age_hours": 5.0,
                "same_strategy_open_like_count": 0,
                "opposite_open_like_count": 0,
            }
        )

        self.assertEqual(evidence["action"], "mfe_drawdown_manual_review")
        self.assertEqual(evidence["triggers"], ["mfe_drawdown_review"])
        self.assertEqual(evidence["automation"], "disabled_report_only")

    def test_strategy_exit_evidence_summarizes_trailing_and_hold_bias(self):
        tool = load_tool()
        recovery = [
            {
                "strategy": "A/v11",
                "symbol": "BTCUSDT",
                "side": "long",
                "mfe_pct_on_margin": 3.0,
                "drawdown_from_mfe_pct_on_margin": -2.5,
                "age_hours": 2.0,
            },
            {
                "strategy": "C/v14",
                "symbol": "ETHUSDT",
                "side": "short",
                "mfe_pct_on_margin": 1.0,
                "drawdown_from_mfe_pct_on_margin": -0.5,
                "same_strategy_open_like_count": 2,
                "age_hours": 2.0,
            },
        ]

        summary = tool.evaluate_recovery_strategy_exit_evidence(recovery)

        self.assertEqual(summary["policy"], "report_only_strategy_specific_recovery_exit_evidence")
        self.assertEqual(summary["action_counts"]["recovery_trailing_watch"], 1)
        self.assertEqual(summary["action_counts"]["same_side_reopen_hold_bias"], 1)
        self.assertEqual(summary["watch_positions"], 1)
        self.assertEqual(summary["hold_bias_positions"], 1)
        self.assertEqual(recovery[0]["strategy_exit_evidence"]["action"], "recovery_trailing_watch")

    def test_review_exports_strategy_exit_action_counts(self):
        tool = load_tool()
        recovery = [
            {
                "strategy": "A/v11",
                "account": "acct-a",
                "symbol": "BTCUSDT",
                "side": "long",
                "margin": 100.0,
                "unrealized_pnl": 1.0,
                "mfe_pct_on_margin": 3.0,
                "drawdown_from_mfe_pct_on_margin": -2.5,
            }
        ]

        tool.evaluate_recovery_strategy_exit_evidence(recovery)
        review = tool.review_recovery_positions(recovery)
        pos = review["positions"][0]

        self.assertEqual(pos["risk"], "watch")
        self.assertEqual(pos["shadow_action"], "recovery_trailing_watch")
        self.assertEqual(pos["strategy_exit_action"], "recovery_trailing_watch")
        self.assertEqual(review["strategy_exit_counts"]["recovery_trailing_watch"], 1)


if __name__ == "__main__":
    unittest.main()
