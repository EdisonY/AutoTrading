import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "部署工具" / "d_e_f_historical_research_report.py"
    spec = importlib.util.spec_from_file_location("d_e_f_historical_research_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DEFHistoricalResearchReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_universe_symbols_prefers_clean_research_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "historical_kline_backfill_latest.json").write_text(
                json.dumps(
                    {
                        "progress": {"written_rows": 1000, "percent": 100},
                        "universe": {"symbols": ["PAXGUSDT", "WLFIUSDT"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runtime / "historical_kline_research_universe_latest.json").write_text(
                json.dumps(
                    {
                        "policy": "test_clean_universe",
                        "eligible_symbols": ["BTCUSDT", " ETHUSDT ", ""],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(self.tool.universe_symbols(root), ["BTCUSDT", "ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
