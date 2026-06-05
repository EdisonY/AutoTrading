import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core.binance_order_rules import SymbolRules, format_decimal_down, validate_market_price


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BinanceCloseOrderParamsTest(unittest.TestCase):
    def test_format_decimal_down_never_rounds_close_quantity_up(self):
        self.assertEqual(format_decimal_down(4738.7, 1.0, 8), "4738")
        self.assertEqual(format_decimal_down(10042.123456, 0.001, 8), "10042.123")

    def test_b_v16_close_quantity_is_floored_and_no_reduce_only_retry(self):
        module = load_module("binance_client_v2_for_test", "交易客户端/binance_client_v2.py")
        client = module.BinanceClientV2.__new__(module.BinanceClientV2)
        calls = []

        def fake_request(method, path, params=None):
            calls.append((method, path, dict(params or {})))
            return {"code": "-2022", "msg": "ReduceOnly Order is rejected."}

        with patch.object(module, "_request", fake_request), patch.object(module, "_get_step_size", return_value=(1.0, 0.0)):
            result = client.close_position("ARBUSDT", "short", quantity=4738.7, order_side="BUY", position_side="SHORT")

        self.assertEqual(result["code"], "-2022")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["quantity"], "4738")
        self.assertEqual(calls[0][2]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", calls[0][2])

    def test_c_v14_close_quantity_uses_market_step_and_no_reduce_only_retry(self):
        module = load_module("binance_client_v3_for_test", "交易客户端/binance_client_v3.py")
        client = module.BinanceClientV3.__new__(module.BinanceClientV3)
        calls = []
        rules = SymbolRules(symbol="MITOUSDT", step_size=0.1, market_step_size=0.1, quantity_precision=1)

        def fake_request(method, path, params=None):
            calls.append((method, path, dict(params or {})))
            return {"code": "-2022", "msg": "ReduceOnly Order is rejected."}

        with patch.object(module, "_request", fake_request), patch.object(client, "get_symbol_rules", return_value=rules):
            result = client.close_position("MITOUSDT", "long", quantity=10042.19, order_side="SELL", position_side="LONG")

        self.assertEqual(result["code"], "-2022")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["quantity"], "10042.1")
        self.assertEqual(calls[0][2]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", calls[0][2])

    def test_market_price_check_uses_mainnet_public_base_and_queue_timeout(self):
        calls = []
        old_base = os.environ.pop("BINANCE_MARKET_BASE_URL", None)
        old_timeout = os.environ.get("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC")
        os.environ["BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC"] = "180"
        rules = SymbolRules(symbol="BTCUSDT", percent_multiplier_up=1.1, percent_multiplier_down=0.9)

        def fake_queue(**kwargs):
            calls.append(kwargs)
            if kwargs["path"].endswith("bookTicker"):
                return {"bidPrice": "99", "askPrice": "101"}
            return {"markPrice": "100"}

        try:
            with patch("core.binance_order_rules.api_queue_client_enabled", return_value=True), patch(
                "core.binance_order_rules.queued_api_request", fake_queue
            ):
                result = validate_market_price("https://testnet.binancefuture.com", rules, "BTCUSDT", "long")
        finally:
            if old_base is None:
                os.environ.pop("BINANCE_MARKET_BASE_URL", None)
            else:
                os.environ["BINANCE_MARKET_BASE_URL"] = old_base
            if old_timeout is None:
                os.environ.pop("BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC", None)
            else:
                os.environ["BINANCE_API_QUEUE_CLIENT_TIMEOUT_SEC"] = old_timeout

        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["url"].startswith("https://fapi.binance.com/") for call in calls))
        self.assertTrue(all(call["timeout_sec"] == 180 for call in calls))


if __name__ == "__main__":
    unittest.main()
