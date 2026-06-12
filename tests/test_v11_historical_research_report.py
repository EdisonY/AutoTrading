import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "v11_historical_research_report.py"
    spec = importlib.util.spec_from_file_location("v11_historical_research_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class V11HistoricalResearchReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_history(self, root: Path, *, symbol: str = "BTCUSDT", interval: str = "1h", bars: int = 180) -> None:
        progress = root / "runtime" / "historical_kline_backfill_latest.json"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": bars},
                    "quality": {
                        "status": "complete_with_provider_gaps",
                        "covered_symbol_count": 1,
                        "covered_symbol_interval_count": 1,
                        "target_symbol_count": 1,
                        "target_symbol_interval_count": 1,
                    },
                    "universe": {"symbols": [symbol]},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        end = datetime.now(self.tool.CST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(hours=bars - 1)
        price = 100.0
        rows_by_day: dict[str, list[dict[str, object]]] = {}
        for idx in range(bars):
            ts = start + timedelta(hours=idx)
            impulse = 0.018 if idx % 11 in {0, 1, 2} else -0.006 if idx % 11 in {6, 7} else 0.004
            open_price = price
            close_price = max(1.0, open_price * (1.0 + impulse))
            high = max(open_price, close_price) * 1.006
            low = min(open_price, close_price) * 0.994
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
                "volume": 1000 + idx,
                "quote_volume": (1000 + idx) * close_price * (2.2 if idx % 11 in {0, 1, 2} else 1.0),
                "source_file": "synthetic-v11-test",
            }
            rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
            price = close_price
        for day, rows in rows_by_day.items():
            path = root / "research_store" / "historical_klines" / f"date={day}" / "data.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def test_generates_read_only_json_and_html_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)

            payload = self.tool.run_research(
                root=root,
                intervals=["1h"],
                symbols=["BTCUSDT"],
                period_days=10,
                max_variants=3,
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["strategy"], "A/v11")
            self.assertEqual(payload["engine_parity"], "research_adapter")
            self.assertFalse(payload["safety"]["binance_requests_enabled"])
            self.assertFalse(payload["safety"]["paper_or_real_orders"])
            self.assertFalse(payload["safety"]["live_config_mutation"])
            self.assertIn("1h", payload["interval_results"])
            self.assertTrue((root / "runtime" / "v11_historical_research_latest.json").exists())
            html = (root / "reports" / "v11_historical_research_latest.html").read_text(encoding="utf-8")
            self.assertIn("A/v11 一年历史回测与参数研究", html)
            self.assertIn("自动应用", html)
            self.assertIn("详细开平仓记录", html)

    def test_universe_symbols_prefers_clean_research_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root, symbol="PAXGUSDT")
            universe_path = root / "runtime" / "historical_kline_research_universe_latest.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "policy": "test_clean_universe",
                        "eligible_symbols": ["btcusdt", " ETHUSDT ", ""],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(self.tool.universe_symbols(root), ["BTCUSDT", "ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
