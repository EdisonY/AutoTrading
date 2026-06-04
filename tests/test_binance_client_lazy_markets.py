import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_binance_client_module():
    module_name = "binance_client_lazy_markets_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / "交易客户端" / "binance_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class BinanceClientLazyMarketsTest(unittest.TestCase):
    def test_init_does_not_load_exchange_info_until_needed(self):
        module = load_binance_client_module()
        calls = []

        def fake_get_markets():
            calls.append("loaded")
            return {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "base": "BTC",
                    "active": True,
                    "contractSize": 1.0,
                }
            }

        module.API_KEY = "test-key"
        module.API_SECRET = "test-secret"
        module.get_markets = fake_get_markets
        module._markets_cache = None

        client = module.BinanceClient()

        self.assertEqual(calls, [])
        self.assertEqual(client._markets, {})

        tradable = client.is_tradable("BTCUSDT")

        self.assertEqual(calls, ["loaded"])
        self.assertTrue(tradable["tradable"])
        self.assertEqual(tradable["ctVal"], 1.0)


if __name__ == "__main__":
    unittest.main()
