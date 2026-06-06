import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "策略文件" / "market_data_service.py"
    spec = importlib.util.spec_from_file_location("market_data_service_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MarketDataServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_external_cache_builds_scanner_watchlist_from_movers(self):
        first_okx = [
            {"symbol": "BTCUSDT", "quote_volume": 10_000_000, "change_pct": 0.0, "last": 100.0, "source": "okx"},
            {"symbol": "ETHUSDT", "quote_volume": 9_000_000, "change_pct": 0.0, "last": 90.0, "source": "okx"},
        ]
        first_bybit = [
            {"symbol": "BTCUSDT", "quote_volume": 5_000_000, "change_pct": 12.0, "last": 102.0, "source": "bybit"},
            {"symbol": "ETHUSDT", "quote_volume": 8_000_000, "change_pct": -8.0, "last": 88.0, "source": "bybit"},
        ]

        with patch.object(self.tool, "fetch_okx_tickers", return_value=first_okx), patch.object(self.tool, "fetch_bybit_tickers", return_value=first_bybit), patch.dict("os.environ", {"MARKET_MOVER_TOP_N": "3"}, clear=False):
            payload, state, watchlist = self.tool.build_payload({}, 10, [], interval_sec=60)

        self.assertIn("BTCUSDT", payload["market_mover_symbols"])
        self.assertIn("ETHUSDT", payload["market_mover_symbols"])
        self.assertEqual(payload["top_preview"][0]["change_pct"], 12.0)
        self.assertIn("bybit", payload["top_preview"][0]["sources"])
        reasons = {row["symbol"]: row["reason"] for row in watchlist["symbols"]}
        self.assertEqual(reasons["BTCUSDT"], "涨幅榜")
        self.assertEqual(reasons["ETHUSDT"], "跌幅榜")

        second_okx = [
            {"symbol": "BTCUSDT", "quote_volume": 70_000_000, "change_pct": 0.0, "last": 117.0, "source": "okx"},
        ]
        second_bybit = [
            {"symbol": "BTCUSDT", "quote_volume": 20_000_000, "change_pct": 17.0, "last": 117.0, "source": "bybit"},
        ]
        with patch.object(self.tool, "fetch_okx_tickers", return_value=second_okx), patch.object(self.tool, "fetch_bybit_tickers", return_value=second_bybit), patch.dict("os.environ", {"MARKET_MOVER_TOP_N": "3"}, clear=False):
            payload, _state, watchlist = self.tool.build_payload(state, 10, [], interval_sec=60)

        btc = next(row for row in watchlist["symbols"] if row["symbol"] == "BTCUSDT")
        self.assertEqual(btc["reason"], "突然加速")
        self.assertAlmostEqual(btc["velocity_pct"], 5.0)
        self.assertAlmostEqual(btc["volume_mult"], 7.0)
        self.assertEqual(payload["spike_symbols"], ["BTCUSDT"])


if __name__ == "__main__":
    unittest.main()
