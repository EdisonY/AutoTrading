import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "indicator_factory.py"
    spec = importlib.util.spec_from_file_location("indicator_factory_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class IndicatorFactoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def write_history(self, root: Path, *, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), interval: str = "15m", bars: int = 320) -> None:
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
            price = 100.0 + sidx * 5.0
            rows_by_day: dict[str, list[dict[str, object]]] = {}
            for idx in range(bars):
                ts = start + timedelta(minutes=15 * idx)
                trend = 0.002 if idx % 90 < 45 else -0.0018
                pulse = 0.025 if idx % 64 == 20 else -0.022 if idx % 73 == 37 else 0.0
                wave = 0.002 * (1 if idx % 10 < 5 else -1)
                move = trend + pulse + wave + sidx * 0.0001
                open_price = price
                close_price = max(1.0, open_price * (1.0 + move))
                high = max(open_price, close_price) * 1.005
                low = min(open_price, close_price) * 0.995
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
                    "source_file": "synthetic-indicator-factory-test",
                }
                rows_by_day.setdefault(ts.date().isoformat(), []).append(row)
                price = close_price
            for day, rows in rows_by_day.items():
                path = root / "research_store" / "historical_klines" / f"date={day}" / "data.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_registry_and_data_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)
            registry = self.tool.indicator_registry()
            combos = self.tool.generate_combos(registry, 2, 4)
            plan = self.tool.data_coverage_plan(root, 730)

            self.assertGreaterEqual(len(registry), 40)
            self.assertGreaterEqual(sum(1 for item in registry if item["implemented"]), 18)
            self.assertGreater(len(combos), 100)
            self.assertTrue(plan["local_extension_possible"])
            self.assertIn("historical_kline_backfill.py", plan["recommended_command"])

    def test_run_factory_smoke_writes_db_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_history(root)
            payload = self.tool.run_factory(
                root,
                days=5,
                intervals=["15m"],
                max_combos=3,
                all_combos=False,
                stage="test",
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["module"], "indicator_factory")
            self.assertEqual(payload["summary"]["tested_combos"], 3)
            self.assertFalse(payload["safety"]["cloud_compute"])
            self.assertFalse(payload["safety"]["automatic_upgrade_allowed"])
            db = root / "research_lab" / "indicator_factory" / "results.sqlite"
            html = root / "research_lab" / "indicator_factory" / "indicator_factory_latest.html"
            self.assertTrue(db.exists())
            self.assertTrue(html.exists())
            html_text = html.read_text(encoding="utf-8")
            self.assertIn("本地指标工厂", html_text)
            self.assertIn("指标注册库", html_text)


if __name__ == "__main__":
    unittest.main()
