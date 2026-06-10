import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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

    def write_synthetic_kline_store(self, root: Path, *, symbol: str = "BTCUSDT", interval: str = "1h", bars: int = 620) -> None:
        table = root / "research_store" / "historical_klines"
        end = datetime.now(self.tool.CST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(hours=bars - 1)
        rows_by_day: dict[str, list[dict[str, object]]] = {}
        price = 10000.0
        for idx in range(bars):
            ts = start + timedelta(hours=idx)
            pulse = 0.012 if idx % 9 in {0, 1, 2} else -0.004 if idx % 9 in {5, 6} else 0.003
            open_price = price
            close_price = max(100.0, open_price * (1.0 + pulse))
            high = max(open_price, close_price) * 1.004
            low = min(open_price, close_price) * 0.996
            volume = 1000.0 + (idx % 9) * 120.0
            quote_volume = volume * close_price * (1.9 if idx % 9 in {0, 1, 2} else 1.0)
            open_ms = int(ts.timestamp() * 1000)
            row = {
                "symbol": symbol,
                "interval": interval,
                "date": ts.date().isoformat(),
                "open_time": ts.isoformat(timespec="seconds"),
                "open_time_ms": open_ms,
                "close_time_ms": open_ms + 60 * 60_000 - 1,
                "open": round(open_price, 8),
                "high": round(high, 8),
                "low": round(low, 8),
                "close": round(close_price, 8),
                "volume": round(volume, 8),
                "quote_volume": round(quote_volume, 8),
                "source_file": "synthetic-test",
            }
            rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
            price = close_price
        for day, rows in rows_by_day.items():
            path = table / f"date={day}" / "data.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def test_status_reports_complete_baseline_and_anti_overfit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_complete_history(root)

            payload = self.tool.refresh_status_files(root=root)

            self.assertTrue(payload["historical_baseline"]["complete"])
            self.assertTrue(payload["anti_overfit"]["enabled"])
            self.assertFalse(payload["anti_overfit"]["auto_apply_allowed"])
            self.assertTrue(payload["capabilities"]["strategy_pnl_metrics"])
            self.assertTrue(payload["capabilities"]["historical_store_query"])
            self.assertEqual(payload["status"], "backtest_engine_ready")
            self.assertTrue((root / "runtime" / "backtest_module_latest.json").exists())
            self.assertTrue((root / "reports" / "backtest_module_latest.md").exists())

    def test_valid_job_without_bars_returns_data_unavailable_without_fake_pnl(self):
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
            self.assertEqual(job["status"], "data_unavailable")
            self.assertEqual(job["result"]["status"], "data_unavailable")
            self.assertEqual(job["result"]["summary"]["net_profit_usdt"], 0.0)
            self.assertEqual(job["result"]["summary"]["trades"], 0)
            self.assertEqual(job["result"]["charts"]["equity_curve"], [])
            self.assertEqual(job["result"]["recommendation"]["action"], "no_parameter_change")
            self.assertEqual(job["result"]["engine_parity"], "research_adapter")
            self.assertFalse(job["result"]["safety"]["paper_or_real_orders"])
            self.assertTrue((root / "runtime" / "backtest_jobs" / f"{job['job_id']}.json").exists())
            latest = json.loads((root / "runtime" / "backtest_module_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["latest_job"]["job_id"], job["job_id"])

    def test_valid_job_runs_research_adapter_with_numeric_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_complete_history(root)
            self.write_synthetic_kline_store(root)

            job = self.tool.create_job(
                {
                    "strategy": "B/v16",
                    "symbols": "BTCUSDT",
                    "interval": "1h",
                    "period_days": 30,
                    "params": {"score_threshold": 40, "overheat_cap": 100},
                    "parameter_variants": 3,
                },
                root=root,
                user="test",
            )

            self.assertTrue(job["ok"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["execution_state"], "completed_research_only")
            self.assertEqual(job["result"]["status"], "completed")
            self.assertEqual(job["result"]["engine_parity"], "research_adapter")
            self.assertIsInstance(job["result"]["summary"]["net_profit_usdt"], float)
            self.assertGreater(job["result"]["summary"]["trades"], 0)
            self.assertGreater(len(job["result"]["charts"]["equity_curve"]), 1)
            self.assertIn("monthly_returns", job["result"]["charts"])
            self.assertTrue(job["result"]["parameter_sweep"]["variants"])
            self.assertIn("anti_overfit_review", job["result"]["parameter_sweep"])
            self.assertEqual(job["result"]["recommendation"]["action"], "research_review_only")
            self.assertFalse(job["result"]["recommendation"]["auto_apply_allowed"])
            self.assertFalse(job["result"]["safety"]["binance_requests_enabled"])
            self.assertFalse(job["result"]["safety"]["paper_or_real_orders"])
            self.assertFalse(job["safety"]["strategy_frequency_change"])

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

    def test_shadow_root_delegates_to_tencent_when_local_store_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shadow"
            old_remote = self.tool.os.environ.get("BACKTEST_REMOTE_DELEGATE")
            old_disable = self.tool.os.environ.get("BACKTEST_DISABLE_REMOTE_DELEGATE")
            try:
                self.tool.os.environ["BACKTEST_REMOTE_DELEGATE"] = "1"
                self.tool.os.environ.pop("BACKTEST_DISABLE_REMOTE_DELEGATE", None)
                self.assertTrue(self.tool.should_delegate_to_tencent(root))
                self.write_synthetic_kline_store(root)
                self.assertFalse(self.tool.should_delegate_to_tencent(root))
            finally:
                if old_remote is None:
                    self.tool.os.environ.pop("BACKTEST_REMOTE_DELEGATE", None)
                else:
                    self.tool.os.environ["BACKTEST_REMOTE_DELEGATE"] = old_remote
                if old_disable is None:
                    self.tool.os.environ.pop("BACKTEST_DISABLE_REMOTE_DELEGATE", None)
                else:
                    self.tool.os.environ["BACKTEST_DISABLE_REMOTE_DELEGATE"] = old_disable


if __name__ == "__main__":
    unittest.main()
