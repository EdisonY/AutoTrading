import os
import tempfile
import time
import unittest
from pathlib import Path

from core.kline_cache import (
    kline_cache_max_age_sec,
    kline_request_url,
    kline_network_enabled,
    load_cached_klines,
    load_latest_cached_close,
    save_cached_klines,
)
from core.market_data_cache import cached_top_symbols, market_cache_max_age_seconds
from core.market_data_cache import market_data_network_enabled, scanner_binance_public_fallback_enabled


class KlineCacheTest(unittest.TestCase):
    def setUp(self):
        self._old_network = os.environ.get("SCANNER_KLINE_NETWORK_ENABLED")
        self._old_direct_network = os.environ.get("SCANNER_DIRECT_KLINE_NETWORK_ALLOWED")
        self._old_kline_base_url = os.environ.get("SCANNER_KLINE_BASE_URL")
        self._old_age = os.environ.get("SCANNER_KLINE_CACHE_MAX_AGE_SEC")
        self._old_market_age = os.environ.get("SCANNER_MARKET_CACHE_MAX_AGE_SEC")
        self._old_market_network = os.environ.get("SCANNER_MARKET_DATA_NETWORK_ENABLED")
        self._old_binance_public_fallback = os.environ.get("SCANNER_BINANCE_PUBLIC_FALLBACK_ENABLED")

    def tearDown(self):
        for key, value in {
            "SCANNER_KLINE_NETWORK_ENABLED": self._old_network,
            "SCANNER_DIRECT_KLINE_NETWORK_ALLOWED": self._old_direct_network,
            "SCANNER_KLINE_BASE_URL": self._old_kline_base_url,
            "SCANNER_KLINE_CACHE_MAX_AGE_SEC": self._old_age,
            "SCANNER_MARKET_CACHE_MAX_AGE_SEC": self._old_market_age,
            "SCANNER_MARKET_DATA_NETWORK_ENABLED": self._old_market_network,
            "SCANNER_BINANCE_PUBLIC_FALLBACK_ENABLED": self._old_binance_public_fallback,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_network_enabled_flag_defaults_off_and_requires_override(self):
        os.environ.pop("SCANNER_KLINE_NETWORK_ENABLED", None)
        os.environ.pop("SCANNER_DIRECT_KLINE_NETWORK_ALLOWED", None)
        self.assertFalse(kline_network_enabled())

        os.environ["SCANNER_KLINE_NETWORK_ENABLED"] = "1"
        self.assertFalse(kline_network_enabled())

        os.environ["SCANNER_DIRECT_KLINE_NETWORK_ALLOWED"] = "1"
        self.assertTrue(kline_network_enabled())

        for value in ("0", "false", "no", "off"):
            os.environ["SCANNER_KLINE_NETWORK_ENABLED"] = value
            self.assertFalse(kline_network_enabled())

    def test_cache_age_can_be_extended_for_staged_cache_only_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [["1", "2", "3", "4", "5", "6", "7", "8"]]
            save_cached_klines(root, "BTCUSDT", "1h", 200, rows)
            cache_file = root / "runtime" / "kline_cache" / "BTCUSDT_1h_200.json"
            old_mtime = time.time() - 3600
            os.utime(cache_file, (old_mtime, old_mtime))

            self.assertIsNone(load_cached_klines(root, "BTCUSDT", "1h", 200))

            os.environ["SCANNER_KLINE_CACHE_MAX_AGE_SEC"] = "7200"
            self.assertEqual(rows, load_cached_klines(root, "BTCUSDT", "1h", 200))
            self.assertEqual(7200, kline_cache_max_age_sec())

    def test_smaller_request_can_use_larger_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                ["1", "2", "3", "4", "5", "6", "7", "8"],
                ["2", "3", "4", "5", "6", "7", "8", "9"],
                ["3", "4", "5", "6", "7", "8", "9", "10"],
            ]
            save_cached_klines(root, "BTCUSDT", "1h", 200, rows)

            self.assertEqual(rows[-2:], load_cached_klines(root, "BTCUSDT", "1h", 2))

    def test_kline_request_url_defaults_to_mainnet_public(self):
        os.environ.pop("SCANNER_KLINE_BASE_URL", None)
        self.assertEqual(
            "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=100",
            kline_request_url("BTCUSDT", "15m", 100),
        )

        os.environ["SCANNER_KLINE_BASE_URL"] = "https://example.test/"
        self.assertEqual(
            "https://example.test/fapi/v1/klines?symbol=ETHUSDT&interval=1h&limit=2",
            kline_request_url("ETHUSDT", "1h", 2),
        )

    def test_market_cache_age_can_be_extended_for_staged_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "runtime" / "market_data_cache.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                '{"unix_ts": %.3f, "top_symbols": ["btcusdt", "ethusdt"]}' % (time.time() - 3600),
                encoding="utf-8",
            )

            self.assertEqual([], cached_top_symbols(cache, 2))

            os.environ["SCANNER_MARKET_CACHE_MAX_AGE_SEC"] = "7200"
            self.assertEqual(["BTCUSDT", "ETHUSDT"], cached_top_symbols(cache, 2))
            self.assertEqual(7200, market_cache_max_age_seconds())

    def test_market_data_network_flag_follows_kline_cache_only_staging(self):
        os.environ.pop("SCANNER_MARKET_DATA_NETWORK_ENABLED", None)
        os.environ.pop("SCANNER_KLINE_NETWORK_ENABLED", None)
        self.assertFalse(market_data_network_enabled())

        os.environ["SCANNER_KLINE_NETWORK_ENABLED"] = "0"
        self.assertFalse(market_data_network_enabled())

        os.environ["SCANNER_MARKET_DATA_NETWORK_ENABLED"] = "1"
        self.assertTrue(market_data_network_enabled())

        os.environ["SCANNER_MARKET_DATA_NETWORK_ENABLED"] = "false"
        self.assertFalse(market_data_network_enabled())

    def test_scanner_binance_public_fallback_defaults_off(self):
        os.environ.pop("SCANNER_BINANCE_PUBLIC_FALLBACK_ENABLED", None)
        self.assertFalse(scanner_binance_public_fallback_enabled())

        os.environ["SCANNER_BINANCE_PUBLIC_FALLBACK_ENABLED"] = "1"
        self.assertTrue(scanner_binance_public_fallback_enabled())

        os.environ["SCANNER_BINANCE_PUBLIC_FALLBACK_ENABLED"] = "false"
        self.assertFalse(scanner_binance_public_fallback_enabled())

    def test_latest_cached_close_uses_newest_fresh_kline_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_cached_klines(root, "BTCUSDT", "15m", 100, [["1", "2", "3", "4", "50000", "6", "7", "8"]])
            old_file = root / "runtime" / "kline_cache" / "BTCUSDT_15m_100.json"
            old_mtime = time.time() - 10
            os.utime(old_file, (old_mtime, old_mtime))

            save_cached_klines(root, "BTCUSDT", "1h", 100, [["1", "2", "3", "4", "51000", "6", "7", "8"]])

            self.assertEqual(51000.0, load_latest_cached_close(root, "BTCUSDT"))

    def test_latest_cached_close_respects_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_cached_klines(root, "BTCUSDT", "15m", 100, [["1", "2", "3", "4", "50000", "6", "7", "8"]])
            cache_file = root / "runtime" / "kline_cache" / "BTCUSDT_15m_100.json"
            old_mtime = time.time() - 3600
            os.utime(cache_file, (old_mtime, old_mtime))

            self.assertIsNone(load_latest_cached_close(root, "BTCUSDT", max_age_sec=60))
            self.assertEqual(50000.0, load_latest_cached_close(root, "BTCUSDT", max_age_sec=7200))


if __name__ == "__main__":
    unittest.main()
