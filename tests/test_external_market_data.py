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
