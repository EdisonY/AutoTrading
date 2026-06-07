import unittest
from unittest.mock import patch

from core import external_market_data as emd


class ExternalMarketDataTests(unittest.TestCase):
    def test_okx_inst_id_maps_usdt_swap(self):
        self.assertEqual(emd.okx_inst_id("BTCUSDT"), "BTC-USDT-SWAP")
        self.assertEqual(emd.okx_inst_id("ETH-USDT-SWAP"), "ETH-USDT-SWAP")

    def test_okx_bar_maps_hour_case(self):
        self.assertEqual(emd.okx_bar("1h"), "1H")
        self.assertEqual(emd.okx_bar("15m"), "15m")

    def test_bybit_interval_maps_hour_to_minutes(self):
        self.assertEqual(emd.bybit_interval("1h"), "60")
        self.assertEqual(emd.bybit_interval("15m"), "15")

    def test_okx_symbol_supported_rejects_non_ascii(self):
        self.assertTrue(emd.okx_symbol_supported("BTCUSDT"))
        self.assertFalse(emd.okx_symbol_supported("币安人生USDT"))

    @patch.dict("os.environ", {"OKX_MARKET_DATA_MAX_PER_MIN": "0"}, clear=False)
    def test_okx_rate_budget_exhausted_does_not_sleep(self):
        original = list(emd._OKX_REQUEST_TIMES)
        try:
            emd._OKX_REQUEST_TIMES[:] = [0.0]
            with patch("core.external_market_data.time.time", return_value=10.0):
                self.assertTrue(emd._okx_rate_limit())
        finally:
            emd._OKX_REQUEST_TIMES[:] = original

    @patch.dict("os.environ", {"OKX_MARKET_DATA_MAX_PER_MIN": "1"}, clear=False)
    def test_okx_rate_budget_returns_false_when_full(self):
        original = list(emd._OKX_REQUEST_TIMES)
        try:
            emd._OKX_REQUEST_TIMES[:] = [100.0]
            with patch("core.external_market_data.time.time", return_value=120.0):
                self.assertFalse(emd._okx_rate_limit())
        finally:
            emd._OKX_REQUEST_TIMES[:] = original

    @patch("core.external_market_data.okx_market_data_enabled", return_value=True)
    @patch("core.external_market_data.okx_public_get")
    def test_fetch_okx_klines_returns_binance_shape(self, mock_get, _enabled):
        mock_get.return_value = {
            "code": "0",
            "data": [
                ["2000", "11", "13", "10", "12", "5", "50", "60"],
                ["1000", "9", "12", "8", "11", "4", "40", "44"],
            ],
        }

        rows = emd.fetch_okx_klines("BTCUSDT", "1h", 2)

        self.assertEqual(rows[0][0], "1000")
        self.assertEqual(rows[0][1:6], ["9", "12", "8", "11", "4"])
        self.assertEqual(rows[0][7], "44")
        self.assertEqual(rows[1][0], "2000")

    @patch("core.external_market_data.bybit_market_data_enabled", return_value=True)
    @patch("core.external_market_data.bybit_public_get")
    def test_fetch_bybit_klines_returns_binance_shape(self, mock_get, _enabled):
        mock_get.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    ["2000", "11", "13", "10", "12", "5", "60"],
                    ["1000", "9", "12", "8", "11", "4", "44"],
                ]
            },
        }

        rows = emd.fetch_bybit_klines("BTCUSDT", "1m", 2)

        self.assertEqual(rows[0][0], "1000")
        self.assertEqual(rows[0][1:6], ["9", "12", "8", "11", "4"])
        self.assertEqual(rows[0][6], "60999")
        self.assertEqual(rows[0][7], "44")
        self.assertEqual(rows[1][0], "2000")

    @patch.dict("os.environ", {"OKX_MARKET_DATA_NEGATIVE_TTL_SEC": "60"}, clear=False)
    @patch("core.external_market_data.okx_market_data_enabled", return_value=True)
    @patch("core.external_market_data.okx_public_get")
    def test_empty_okx_klines_are_negative_cached(self, mock_get, _enabled):
        original = dict(emd._OKX_NEGATIVE_UNTIL)
        try:
            emd._OKX_NEGATIVE_UNTIL.clear()
            mock_get.return_value = {"code": "0", "data": []}

            self.assertEqual(emd.fetch_okx_klines("MISSINGUSDT", "15m", 2), [])
            self.assertEqual(emd.fetch_okx_klines("MISSINGUSDT", "15m", 2), [])

            self.assertEqual(mock_get.call_count, 1)
        finally:
            emd._OKX_NEGATIVE_UNTIL.clear()
            emd._OKX_NEGATIVE_UNTIL.update(original)

    @patch("core.external_market_data.okx_market_data_enabled", return_value=True)
    @patch("core.external_market_data.okx_public_get")
    def test_fetch_okx_ofi(self, mock_get, _enabled):
        mock_get.return_value = {
            "code": "0",
            "data": [
                {
                    "bids": [["10", "3"], ["9", "1"]],
                    "asks": [["11", "1"], ["12", "1"]],
                }
            ],
        }

        self.assertAlmostEqual(emd.fetch_okx_ofi("BTCUSDT"), (4 - 2) / 6)

    @patch("core.external_market_data.okx_market_data_enabled", return_value=True)
    @patch("core.external_market_data.okx_public_get")
    def test_fetch_okx_cvd(self, mock_get, _enabled):
        mock_get.return_value = {
            "code": "0",
            "data": [
                {"side": "buy", "sz": "3"},
                {"side": "sell", "sz": "1"},
            ],
        }

        self.assertAlmostEqual(emd.fetch_okx_cvd("BTCUSDT"), 0.5)


if __name__ == "__main__":
    unittest.main()
