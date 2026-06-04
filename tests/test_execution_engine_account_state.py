import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.account_state import build_account_state_payload, write_account_state
from core.execution_engine import CloseRequest, ConfirmationStateUnavailable, ExecutionEngine, OpenRequest


class FakeClient:
    def __init__(self):
        self.get_positions_calls = 0
        self.open_calls = []
        self.close_calls = []
        self.delete_calls = []

    def get_positions(self):
        self.get_positions_calls += 1
        return [{"symbol": "ETHUSDT", "positionAmt": "2", "positionSide": "LONG"}]

    def invalidate_account_snapshot(self):
        pass

    def open_long(self, *args):
        self.open_calls.append(("open_long", args))
        return {"orderId": "should-not-call"}

    def open_short(self, *args):
        self.open_calls.append(("open_short", args))
        return {"orderId": "should-not-call"}

    def close_position(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))
        return {"orderId": "should-not-call"}

    def _delete(self, symbol):
        self.delete_calls.append(symbol)


class ExecutionEngineAccountStateTest(unittest.TestCase):
    def test_confirmation_uses_fresh_central_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5}],
                }
            ])
            write_account_state(root, payload)
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11", account_state_root=root, central_confirmation_max_age_seconds=60)

            positions = engine._get_positions_for_confirmation(
                {"min_observed_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
                force_refresh=True,
            )

            self.assertEqual(client.get_positions_calls, 0)
            self.assertEqual(positions[0]["symbol"], "BTCUSDT")

    def test_order_disabled_blocks_open_before_client_order_call(self):
        old_value = os.environ.get("SCANNER_ORDER_ENABLED")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        try:
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11")

            result = engine.open_position(OpenRequest(
                symbol="BTCUSDT",
                side="long",
                price=100.0,
                risk_usdt=100.0,
                leverage=4,
                take_profit=110.0,
                stop_loss=90.0,
                quantity=1.0,
            ))
        finally:
            if old_value is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_value

        self.assertFalse(result.success)
        self.assertTrue(result.preflight_rejected)
        self.assertEqual(result.code, "scanner_order_disabled")
        self.assertEqual(client.open_calls, [])

    def test_order_disabled_blocks_close_before_cancel_or_client_order_call(self):
        old_value = os.environ.get("SCANNER_ORDER_ENABLED")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        try:
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11")

            result = engine.close_position(CloseRequest(
                symbol="BTCUSDT",
                side="long",
                quantity=1.0,
                cancel_open_orders=True,
            ))
        finally:
            if old_value is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_value

        self.assertFalse(result.success)
        self.assertTrue(result.preflight_rejected)
        self.assertEqual(result.code, "scanner_order_disabled")
        self.assertEqual(client.delete_calls, [])
        self.assertEqual(client.close_calls, [])
        self.assertEqual(client.get_positions_calls, 0)

    def test_confirmation_requires_fresh_central_state_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5}],
                }
            ])
            write_account_state(root, payload)
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11", account_state_root=root, central_confirmation_max_age_seconds=60)

            with self.assertRaises(ConfirmationStateUnavailable):
                engine._get_positions_for_confirmation(
                    {"min_observed_at": datetime.now(timezone.utc)},
                    force_refresh=True,
                )

            self.assertEqual(client.get_positions_calls, 0)

    def test_confirmation_fallback_can_be_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5}],
                }
            ])
            write_account_state(root, payload)
            client = FakeClient()
            engine = ExecutionEngine(
                client,
                "A/v11",
                account_state_root=root,
                central_confirmation_max_age_seconds=60,
                require_central_confirmation=False,
            )

            positions = engine._get_positions_for_confirmation({"min_observed_at": datetime.now(timezone.utc)}, force_refresh=True)

            self.assertEqual(client.get_positions_calls, 1)
            self.assertEqual(positions[0]["symbol"], "ETHUSDT")

    def test_close_target_uses_recent_state_without_post_submit_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5}],
                }
            ])
            write_account_state(root, payload)
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11", account_state_root=root, central_confirmation_max_age_seconds=15)

            target = engine._close_target("BTCUSDT", "long", {}, force_refresh=True)

            self.assertEqual(client.get_positions_calls, 0)
            self.assertEqual(target["quantity"], 0.5)
            self.assertEqual(target["position_side"], "LONG")

    def test_post_submit_confirmation_does_not_reuse_target_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            payload = build_account_state_payload([
                {
                    "ts": observed_at.isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5}],
                }
            ])
            write_account_state(root, payload)
            client = FakeClient()
            engine = ExecutionEngine(client, "A/v11", account_state_root=root, central_confirmation_max_age_seconds=60)
            cache = {}
            engine._close_target("BTCUSDT", "long", cache, force_refresh=True)

            with self.assertRaises(ConfirmationStateUnavailable):
                engine._get_positions_for_confirmation(
                    {"min_observed_at": observed_at + timedelta(milliseconds=500), **cache},
                    force_refresh=False,
                )

            self.assertEqual(client.get_positions_calls, 0)


if __name__ == "__main__":
    unittest.main()
