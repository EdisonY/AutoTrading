import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core.binance_order_rules import SymbolRules, format_decimal_down


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


if __name__ == "__main__":
    unittest.main()
