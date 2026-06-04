import os
import tempfile
import time
import unittest
from pathlib import Path

from core.kline_cache import (
    kline_cache_max_age_sec,
    kline_network_enabled,
    load_cached_klines,
    save_cached_klines,
)


class KlineCacheTest(unittest.TestCase):
    def setUp(self):
        self._old_network = os.environ.get("SCANNER_KLINE_NETWORK_ENABLED")
        self._old_age = os.environ.get("SCANNER_KLINE_CACHE_MAX_AGE_SEC")

    def tearDown(self):
        for key, value in {
            "SCANNER_KLINE_NETWORK_ENABLED": self._old_network,
            "SCANNER_KLINE_CACHE_MAX_AGE_SEC": self._old_age,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_network_enabled_flag_defaults_on_and_accepts_false_values(self):
        os.environ.pop("SCANNER_KLINE_NETWORK_ENABLED", None)
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


if __name__ == "__main__":
    unittest.main()
