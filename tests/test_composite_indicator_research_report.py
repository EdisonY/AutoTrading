import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "composite_indicator_research_report.py"
    spec = importlib.util.spec_from_file_location("composite_indicator_research_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CompositeIndicatorResearchReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_history(self, root: Path, *, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), interval: str = "15m", bars: int = 260) -> None:
        progress = root / "runtime" / "historical_kline_backfill_latest.json"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "progress": {"pending_tasks": 0, "percent": 100.0, "written_rows": bars * len(symbols)},
                    "quality": {
                        "status": "complete_with_provider_gaps",
                        "covered_symbol_count": len(symbols),
                        "covered_symbol_interval_count": len(symbols),
                        "target_symbol_count": len(symbols),
                        "target_symbol_interval_count": len(symbols),
                    },
                    "universe": {"symbols": list(symbols)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        end = datetime.now(self.tool.CST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(minutes=15 * (bars - 1))
        for sidx, symbol in enumerate(symbols):
            price = 100.0 + sidx * 6.0
            rows_by_day: dict[str, list[dict[str, object]]] = {}
            for idx in range(bars):
                ts = start + timedelta(minutes=15 * idx)
                compress = 0.001 if idx % 48 < 24 else 0.004
                pulse = 0.035 if idx % 67 == 30 else -0.030 if idx % 71 == 40 else 0.0
                trend = 0.0015 if (idx // 80) % 2 == 0 else -0.0012
                wave = compress * (1 if idx % 8 < 4 else -1)
                move = trend + wave + pulse + sidx * 0.0001
                open_price = price
                close_price = max(1.0, open_price * (1.0 + move))
                high = max(open_price, close_price) * 1.006
                low = min(open_price, close_price) * 0.994
                open_ms = int(ts.timestamp() * 1000)
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "date": ts.date().isoformat(),
                    "open_time": ts.isoformat(timespec="seconds"),
                    "open_time_ms": open_ms,
                    "close_time_ms": open_ms + 15 * 60_000 - 1,
                    "open": round(open_price, 8),
                    "high": round(high, 8),
                    "low": round(low, 8),
                    "close": round(close_price, 8),
                    "volume": 1000 + idx,
                    "quote_volume": (1000 + idx) * close_price * (2.0 if pulse else 1.0),
                    "source_file": "synthetic-composite-test",
                }
                rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
                price = close_price
            for day, rows in rows_by_day.items():
                path = root / "research_store" / "historical_klines" / f"date={day}" / "data.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_generates_read_only_composite_research_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)

            payload = self.tool.run_all(
                root,
                symbols=["BTCUSDT", "ETHUSDT"],
                intervals=["15m"],
                start=datetime.now(self.tool.CST) - timedelta(days=6),
                end=datetime.now(self.tool.CST),
                max_variants=1,
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["module"], "composite_indicator_research")
            self.assertEqual(payload["engine_parity"], "historical_research_adapter")
            self.assertFalse(payload["safety"]["binance_requests_enabled"])
            self.assertFalse(payload["safety"]["live_config_mutation"])
            self.assertFalse(payload["safety"]["automatic_upgrade_allowed"])
            self.assertIn("m_qqe_squeeze", payload["strategies"])
            self.assertIn("r_ichimoku_vwap", payload["strategies"])
            self.assertIn("candidate_scan", payload)
            html = (root / "reports" / "composite_indicator_research_latest.html").read_text(encoding="utf-8")
            self.assertIn("组合指标研究报告", html)
            self.assertIn("QQE", html)
            self.assertTrue((root / "runtime" / "composite_indicator_research_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
