import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "backtest_module.py"
    spec = importlib.util.spec_from_file_location("backtest_module_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BacktestModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_complete_history(self, root: Path) -> None:
        path = root / "runtime" / "historical_kline_backfill_latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-10T14:00:00+08:00",
                    "status": "complete",
                    "mode": "apply",
                    "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": 1602051},
                    "quality": {
                        "status": "complete_with_provider_gaps",
                        "covered_symbol_count": 26,
                        "target_symbol_count": 30,
                        "covered_symbol_interval_count": 104,
                        "target_symbol_interval_count": 120,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_status_reports_complete_baseline_and_anti_overfit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_complete_history(root)

            payload = self.tool.refresh_status_files(root=root)

            self.assertTrue(payload["historical_baseline"]["complete"])
            self.assertTrue(payload["anti_overfit"]["enabled"])
            self.assertFalse(payload["anti_overfit"]["auto_apply_allowed"])
            self.assertFalse(payload["capabilities"]["strategy_pnl_metrics"])
            self.assertTrue((root / "runtime" / "backtest_module_latest.json").exists())
            self.assertTrue((root / "reports" / "backtest_module_latest.md").exists())

    def test_valid_job_creates_audited_pending_result_without_fake_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_complete_history(root)

            job = self.tool.create_job(
                {
                    "strategy": "B/v16",
                    "symbols": "BTCUSDT,ETHUSDT",
                    "interval": "1h",
                    "period_days": 365,
                    "params": {"score_threshold": 66, "overheat_cap": 85},
                    "parameter_variants": 4,
                },
                root=root,
                user="test",
            )

            self.assertTrue(job["ok"])
            self.assertEqual(job["status"], "replay_adapter_pending")
            self.assertEqual(job["result"]["status"], "replay_adapter_pending")
            self.assertIsNone(job["result"]["summary"]["net_profit_usdt"])
            self.assertEqual(job["result"]["summary"]["trades"], 0)
            self.assertEqual(job["result"]["charts"]["equity_curve"], [])
            self.assertEqual(job["result"]["recommendation"]["action"], "no_parameter_change")
            self.assertTrue((root / "runtime" / "backtest_jobs" / f"{job['job_id']}.json").exists())
            latest = json.loads((root / "runtime" / "backtest_module_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["latest_job"]["job_id"], job["job_id"])

    def test_rejects_invalid_params_and_too_many_tuned_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_complete_history(root)

            invalid_json = self.tool.create_job(
                {"strategy": "A/v11", "symbols": "BTCUSDT", "params": "{not-json"},
                root=root,
            )
            too_many = self.tool.create_job(
                {
                    "strategy": "A/v11",
                    "symbols": "BTCUSDT",
                    "params": {
                        "entry_threshold": 100,
                        "strong_signal_threshold": 112,
                        "evict_score_gap": 25,
                        "trailing_pullback_atr": 1.0,
                    },
                },
                root=root,
            )

            self.assertFalse(invalid_json["ok"])
            self.assertIn("params must be a JSON object", invalid_json["errors"])
            self.assertFalse(too_many["ok"])
            self.assertIn("too_many_tuned_parameters:max_3", too_many["errors"])


if __name__ == "__main__":
    unittest.main()
