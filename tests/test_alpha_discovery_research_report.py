import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "alpha_discovery_research_report.py"
    spec = importlib.util.spec_from_file_location("alpha_discovery_research_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AlphaDiscoveryResearchReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_history(self, root: Path, *, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), interval: str = "1h", bars: int = 180) -> None:
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
        start = end - timedelta(hours=bars - 1)
        for sidx, symbol in enumerate(symbols):
            price = 100.0 + sidx * 12.0
            rows_by_day: dict[str, list[dict[str, object]]] = {}
            for idx in range(bars):
                ts = start + timedelta(hours=idx)
                if idx % 18 in {4, 5}:
                    impulse = 0.020 - sidx * 0.002
                    vol_mult = 2.4
                elif idx % 18 in {11, 12}:
                    impulse = -0.017 + sidx * 0.001
                    vol_mult = 2.0
                elif idx % 18 in {0, 1, 2, 3}:
                    impulse = 0.001
                    vol_mult = 0.7
                else:
                    impulse = 0.003 if idx % 5 else -0.002
                    vol_mult = 1.0
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
                    "quote_volume": (1000 + idx) * close_price * vol_mult,
                    "source_file": "synthetic-alpha-test",
                }
                rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
                price = close_price
            for day, rows in rows_by_day.items():
                path = root / "research_store" / "historical_klines" / f"date={day}" / "data.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_generates_read_only_alpha_discovery_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)

            payload = self.tool.run_all(
                root,
                symbols=["BTCUSDT", "ETHUSDT"],
                intervals=["1h"],
                start=datetime.now(self.tool.CST) - timedelta(days=10),
                end=datetime.now(self.tool.CST),
                max_variants=2,
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["module"], "alpha_discovery")
            self.assertFalse(payload["safety"]["binance_requests_enabled"])
            self.assertFalse(payload["safety"]["live_config_mutation"])
            self.assertFalse(payload["safety"]["automatic_upgrade_allowed"])
            self.assertIn("diagnostics", payload)
            self.assertIn("g_early_momentum", payload["strategies"])
            self.assertIn("h_compression_breakout", payload["strategies"])
            self.assertIn("i_a_v11_filtered", payload["strategies"])
            html = (root / "reports" / "alpha_discovery_latest.html").read_text(encoding="utf-8")
            self.assertIn("Alpha 发现", html)
            self.assertIn("G 起涨/起跌早段动量", html)
            self.assertTrue((root / "runtime" / "alpha_discovery_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
