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

        self.assertEqual(review["path_metric_note"], "report_only_snapshot_path_mfe_mae")
        self.assertEqual(pos["first_seen_ts"], "2026-06-03T07:00:00+08:00")
        self.assertEqual(pos["path_samples"], 4)
        self.assertEqual(pos["mfe_pct_on_margin"], 8.0)
        self.assertEqual(pos["mae_pct_on_margin"], -1.0)
        self.assertEqual(pos["drawdown_from_mfe_pct_on_margin"], -3.0)
        self.assertEqual(pos["mfe_price_pct"], 1.5)
        self.assertEqual(pos["mae_price_pct"], -0.2)


if __name__ == "__main__":
    unittest.main()
