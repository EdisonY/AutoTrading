import importlib.util
import sys
import tempfile
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

    def test_early_movers_use_existing_ticker_snapshots_without_normal_volume_gate(self):
        previous_state = {
            "EARLYUPUSDT": {"quote_volume": 1_200_000, "change_pct": 1.2, "last_price": 100.0},
            "EARLYDOWNUSDT": {"quote_volume": 1_100_000, "change_pct": 1.5, "last_price": 100.0},
            "LATEUSDT": {"quote_volume": 50_000_000, "change_pct": 18.0, "last_price": 50.0},
        }
        rows = [
            ("EARLYUPUSDT", 1_250_000, 1.8, {"last": 100.8, "source": "okx"}),
            ("EARLYDOWNUSDT", 1_300_000, 1.1, {"last": 99.2, "source": "bybit"}),
            ("LATEUSDT", 51_000_000, 19.0, {"last": 51.0, "source": "okx"}),
        ]

        watchlist = self.tool.build_market_mover_watchlist(
            rows,
            previous_state,
            top_n=5,
            interval_sec=60,
            velocity_threshold=0.8,
            min_quote_volume=3_000_000,
            volume_spike_mult=5.0,
            early_enabled=True,
            early_price_tick_threshold=0.45,
            early_max_change_pct=5.0,
            early_min_quote_volume=1_000_000,
        )

        by_symbol = {row["symbol"]: row for row in watchlist["symbols"]}
        self.assertEqual(by_symbol["EARLYUPUSDT"]["reason"], "起涨捕捉")
        self.assertEqual(by_symbol["EARLYUPUSDT"]["phase"], "起涨初段")
        self.assertAlmostEqual(by_symbol["EARLYUPUSDT"]["price_tick_pct"], 0.8)
        self.assertEqual(by_symbol["EARLYDOWNUSDT"]["reason"], "起跌捕捉")
        self.assertEqual(by_symbol["EARLYDOWNUSDT"]["phase"], "起跌初段")
        self.assertAlmostEqual(by_symbol["EARLYDOWNUSDT"]["price_tick_pct"], -0.8)
        self.assertEqual(watchlist["early_mover_count"], 2)
        self.assertEqual(watchlist["thresholds"]["early_min_quote_volume"], 1_000_000)

    def test_kline_prefetch_saves_rotating_batch(self):
        payload = {
            "top_symbols": ["BTCUSDT", "ETHUSDT"],
            "market_mover_symbols": ["SOLUSDT"],
        }
        rows = [["1", "2", "3", "4", "5", "6", "7", "8"]]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "MARKET_KLINE_PREFETCH_ENABLED": "1",
                "MARKET_KLINE_PREFETCH_SYMBOL_LIMIT": "3",
                "MARKET_KLINE_PREFETCH_HOT_SYMBOL_LIMIT": "2",
                "MARKET_KLINE_PREFETCH_HOT_SPECS": "15m:100,1h:200",
                "MARKET_KLINE_PREFETCH_WARM_SPECS": "15m:100",
                "MARKET_KLINE_PREFETCH_MAX_REQUESTS_PER_RUN": "2",
                "MARKET_KLINE_PREFETCH_CACHE_MAX_AGE_SEC": "600",
            },
            clear=False,
        ), patch.object(self.tool, "fetch_okx_klines", return_value=rows) as mock_okx, patch.object(
            self.tool, "fetch_bybit_klines", return_value=[]
        ):
            status, cursor = self.tool.prefetch_klines(Path(tmp), payload, 0)

            self.assertTrue(status["enabled"])
            self.assertEqual(status["attempted"], 2)
            self.assertEqual(status["saved"], 2)
            self.assertEqual(cursor, 2)
            self.assertEqual(mock_okx.call_count, 2)
            self.assertTrue((Path(tmp) / "runtime" / "kline_cache" / "BTCUSDT_15m_100.json").exists())
            self.assertTrue((Path(tmp) / "runtime" / "kline_cache" / "BTCUSDT_1h_200.json").exists())

    def test_kline_prefetch_uses_fresh_cache_without_fetch(self):
        payload = {"top_symbols": ["BTCUSDT"], "market_mover_symbols": []}
        rows = [["1", "2", "3", "4", "5", "6", "7", "8"]]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "MARKET_KLINE_PREFETCH_ENABLED": "1",
                "MARKET_KLINE_PREFETCH_SYMBOL_LIMIT": "1",
                "MARKET_KLINE_PREFETCH_HOT_SYMBOL_LIMIT": "1",
                "MARKET_KLINE_PREFETCH_HOT_SPECS": "15m:100",
                "MARKET_KLINE_PREFETCH_MAX_REQUESTS_PER_RUN": "1",
                "MARKET_KLINE_PREFETCH_CACHE_MAX_AGE_SEC": "600",
            },
            clear=False,
        ), patch.object(self.tool, "fetch_okx_klines", return_value=[]) as mock_okx:
            self.tool.save_cached_klines(Path(tmp), "BTCUSDT", "15m", 100, rows)

            status, cursor = self.tool.prefetch_klines(Path(tmp), payload, 0)

            self.assertEqual(status["fresh"], 1)
            self.assertEqual(status["attempted"], 0)
            self.assertEqual(cursor, 0)
            self.assertEqual(mock_okx.call_count, 0)

    def test_kline_prefetch_disabled(self):
        with patch.dict("os.environ", {"MARKET_KLINE_PREFETCH_ENABLED": "0"}, clear=False):
            status, cursor = self.tool.prefetch_klines(Path("."), {"top_symbols": ["BTCUSDT"]}, 7)

        self.assertFalse(status["enabled"])
        self.assertEqual(cursor, 7)


if __name__ == "__main__":
    unittest.main()
