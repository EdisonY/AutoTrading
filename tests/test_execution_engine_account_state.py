import json
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


class UnfilledOpenClient(FakeClient):
    def open_long(self, *args):
        self.open_calls.append(("open_long", args))
        return {
            "orderId": "new-unfilled",
            "status": "NEW",
            "origQty": "5",
            "executedQty": "0",
            "cumQty": "0",
            "symbol": "BTCUSDT",
        }

    def get_positions(self):
        self.get_positions_calls += 1
        return []


class ReduceOnlyRejectCloseClient(FakeClient):
    def close_position(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))
        if len(self.close_calls) == 1:
            return {"code": "-2022", "msg": "ReduceOnly Order is rejected."}
        return {"orderId": "retry-close", "status": "FILLED", "executedQty": "4738.7"}

    def _delete(self, symbol):
        self.delete_calls.append(symbol)
        return {"code": 200, "msg": "The operation of cancel all open order is done."}


class ReduceOnlyCancelFailClient(ReduceOnlyRejectCloseClient):
    def _delete(self, symbol):
        self.delete_calls.append(symbol)
        return {"code": "-1003", "msg": "queued request blocked by active cooldown"}


class ReduceOnlyNoCancelClient:
    def __init__(self):
        self.close_calls = []

    def close_position(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))
        return {"code": "-2022", "msg": "ReduceOnly Order is rejected."}


class RuleCheckingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.calc_size_calls = 0
        self.validate_order_quantity_calls = 0

    def calc_size(self, *args):
        self.calc_size_calls += 1
        return 0.0

    def validate_order_quantity(self, *args):
        self.validate_order_quantity_calls += 1
        return {"ok": False, "quantity": 0.0, "reason": "should-not-call"}


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

    def test_paper_calc_quantity_does_not_call_client_rules(self):
        old_mode = os.environ.get("SCANNER_EXECUTION_MODE")
        os.environ["SCANNER_EXECUTION_MODE"] = "paper"
        try:
            client = RuleCheckingClient()
            engine = ExecutionEngine(client, "A/v11")

            qty = engine.calc_quantity("BTCUSDT", 20000.0, 100.0, 4, max_quantity=0.015)
        finally:
            if old_mode is None:
                os.environ.pop("SCANNER_EXECUTION_MODE", None)
            else:
                os.environ["SCANNER_EXECUTION_MODE"] = old_mode

        self.assertEqual(qty, 0.015)
        self.assertEqual(client.calc_size_calls, 0)
        self.assertEqual(client.validate_order_quantity_calls, 0)

    def test_paper_open_works_when_real_orders_disabled(self):
        old_order = os.environ.get("SCANNER_ORDER_ENABLED")
        old_mode = os.environ.get("SCANNER_EXECUTION_MODE")
        old_ledger = os.environ.get("PAPER_EXCHANGE_LEDGER_ENABLED")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        os.environ["SCANNER_EXECUTION_MODE"] = "paper"
        os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = "0"
        try:
            client = RuleCheckingClient()
            engine = ExecutionEngine(client, "B/v16")

            result = engine.open_position(OpenRequest(
                symbol="ETHUSDT",
                side="short",
                price=2500.0,
                risk_usdt=100.0,
                leverage=4,
                take_profit=2400.0,
                stop_loss=2550.0,
            ))
        finally:
            if old_order is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_order
            if old_mode is None:
                os.environ.pop("SCANNER_EXECUTION_MODE", None)
            else:
                os.environ["SCANNER_EXECUTION_MODE"] = old_mode
            if old_ledger is None:
                os.environ.pop("PAPER_EXCHANGE_LEDGER_ENABLED", None)
            else:
                os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = old_ledger

        self.assertTrue(result.success)
        self.assertEqual(result.status, "PAPER_FILLED")
        self.assertTrue(result.order_id.startswith("PAPER-B/v16-"))
        self.assertEqual(result.quantity, 0.16)
        self.assertEqual(client.open_calls, [])
        self.assertEqual(client.calc_size_calls, 0)
        self.assertEqual(client.validate_order_quantity_calls, 0)

    def test_paper_open_writes_paper_exchange_ledger(self):
        old_order = os.environ.get("SCANNER_ORDER_ENABLED")
        old_mode = os.environ.get("SCANNER_EXECUTION_MODE")
        old_ledger = os.environ.get("PAPER_EXCHANGE_LEDGER_ENABLED")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        os.environ["SCANNER_EXECUTION_MODE"] = "paper"
        os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = RuleCheckingClient()
                engine = ExecutionEngine(client, "A/v11", account_state_root=root)

                result = engine.open_position(OpenRequest(
                    symbol="BTCUSDT",
                    side="long",
                    price=100.0,
                    risk_usdt=100.0,
                    leverage=4,
                    take_profit=110.0,
                    stop_loss=90.0,
                ))

                latest = json.loads((root / "runtime" / "paper_exchange_latest.json").read_text(encoding="utf-8"))
        finally:
            if old_order is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_order
            if old_mode is None:
                os.environ.pop("SCANNER_EXECUTION_MODE", None)
            else:
                os.environ["SCANNER_EXECUTION_MODE"] = old_mode
            if old_ledger is None:
                os.environ.pop("PAPER_EXCHANGE_LEDGER_ENABLED", None)
            else:
                os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = old_ledger

        self.assertTrue(result.success)
        self.assertEqual(latest["mode"], "paper_exchange")
        self.assertEqual(latest["open_positions"], 1)
        self.assertGreater(latest["by_strategy"]["A/v11"]["fees_paid"], 0.16)
        fill = latest["recent_fills"][-1]
        self.assertEqual(fill["paper_fill_model_version"], "v2")
        self.assertEqual(fill["paper_fill_source"], "synthetic_fallback")

    def test_paper_close_skips_cancel_position_and_client_close(self):
        old_order = os.environ.get("SCANNER_ORDER_ENABLED")
        old_mode = os.environ.get("SCANNER_EXECUTION_MODE")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        os.environ["SCANNER_EXECUTION_MODE"] = "paper"
        try:
            client = FakeClient()
            engine = ExecutionEngine(client, "C/v14")

            result = engine.close_position(CloseRequest(
                symbol="SOLUSDT",
                side="long",
                quantity=3.0,
                cancel_open_orders=True,
            ))
        finally:
            if old_order is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_order
            if old_mode is None:
                os.environ.pop("SCANNER_EXECUTION_MODE", None)
            else:
                os.environ["SCANNER_EXECUTION_MODE"] = old_mode

        self.assertTrue(result.success)
        self.assertEqual(result.status, "PAPER_CLOSED")
        self.assertTrue(result.order_id.startswith("PAPER-CLOSE-C/v14-"))
        self.assertEqual(client.delete_calls, [])
        self.assertEqual(client.close_calls, [])
        self.assertEqual(client.get_positions_calls, 0)

    def test_paper_close_without_matching_ledger_fill_keeps_requested_fill(self):
        old_order = os.environ.get("SCANNER_ORDER_ENABLED")
        old_mode = os.environ.get("SCANNER_EXECUTION_MODE")
        old_ledger = os.environ.get("PAPER_EXCHANGE_LEDGER_ENABLED")
        old_fill_model = os.environ.get("PAPER_FILL_MODEL_VERSION")
        os.environ["SCANNER_ORDER_ENABLED"] = "0"
        os.environ["SCANNER_EXECUTION_MODE"] = "paper"
        os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = "1"
        os.environ["PAPER_FILL_MODEL_VERSION"] = "v1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client = FakeClient()
                engine = ExecutionEngine(client, "A/v11", account_state_root=root)
                open_result = engine.open_position(OpenRequest(
                    symbol="BTCUSDT",
                    side="long",
                    price=100.0,
                    risk_usdt=100.0,
                    leverage=4,
                    take_profit=110.0,
                    stop_loss=90.0,
                ))
                self.assertTrue(open_result.success)
                close_result = engine.close_position(CloseRequest(
                    symbol="BTCUSDT",
                    side="long",
                    quantity=open_result.quantity,
                    context={"strategy": "A/v11", "exit_price": 101.0},
                ))
                self.assertTrue(close_result.success)

                result = engine.close_position(CloseRequest(
                    symbol="ETHUSDT",
                    side="long",
                    quantity=3.0,
                    context={"strategy": "A/v11", "exit_price": 200.0},
                ))
        finally:
            if old_order is None:
                os.environ.pop("SCANNER_ORDER_ENABLED", None)
            else:
                os.environ["SCANNER_ORDER_ENABLED"] = old_order
            if old_mode is None:
                os.environ.pop("SCANNER_EXECUTION_MODE", None)
            else:
                os.environ["SCANNER_EXECUTION_MODE"] = old_mode
            if old_ledger is None:
                os.environ.pop("PAPER_EXCHANGE_LEDGER_ENABLED", None)
            else:
                os.environ["PAPER_EXCHANGE_LEDGER_ENABLED"] = old_ledger
            if old_fill_model is None:
                os.environ.pop("PAPER_FILL_MODEL_VERSION", None)
            else:
                os.environ["PAPER_FILL_MODEL_VERSION"] = old_fill_model

        self.assertTrue(result.success)
        self.assertEqual(result.quantity, 3.0)
        self.assertEqual(result.raw["avgPrice"], 200.0)
        self.assertNotIn("paper_fill", result.raw)

    def test_close_does_not_cancel_open_orders_by_default(self):
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

            result = engine.close_position(CloseRequest(
                symbol="BTCUSDT",
                side="long",
                quantity=0.5,
                cancel_open_orders=True,
                confirm_position=False,
            ))

            self.assertTrue(result.success)
            self.assertEqual(client.delete_calls, [])
            self.assertEqual(client.close_calls[0][1]["position_side"], "LONG")

    def test_close_cancel_open_orders_can_be_enabled(self):
        old_value = os.environ.get("SCANNER_CLOSE_CANCEL_OPEN_ORDERS_ENABLED")
        os.environ["SCANNER_CLOSE_CANCEL_OPEN_ORDERS_ENABLED"] = "1"
        try:
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

                result = engine.close_position(CloseRequest(
                    symbol="BTCUSDT",
                    side="long",
                    quantity=0.5,
                    cancel_open_orders=True,
                    confirm_position=False,
                ))
        finally:
            if old_value is None:
                os.environ.pop("SCANNER_CLOSE_CANCEL_OPEN_ORDERS_ENABLED", None)
            else:
                os.environ["SCANNER_CLOSE_CANCEL_OPEN_ORDERS_ENABLED"] = old_value

        self.assertTrue(result.success)
        self.assertEqual(client.delete_calls, ["BTCUSDT"])

    def test_close_reduce_only_reject_cancels_symbol_orders_then_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "B",
                    "strategy": "B/v16",
                    "positions": [{"symbol": "ARBUSDT", "side": "SHORT", "qty": 4738.7}],
                }
            ])
            write_account_state(root, payload)
            client = ReduceOnlyRejectCloseClient()
            engine = ExecutionEngine(client, "B/v16", account_state_root=root, central_confirmation_max_age_seconds=60)

            result = engine.close_position(CloseRequest(
                symbol="ARBUSDT",
                side="short",
                quantity=4738.7,
                confirm_position=False,
            ))

            self.assertTrue(result.success)
            self.assertEqual(client.delete_calls, ["ARBUSDT"])
            self.assertEqual(len(client.close_calls), 2)
            self.assertEqual(client.close_calls[0][1]["position_side"], "SHORT")
            self.assertEqual(client.close_calls[0][1]["order_side"], "BUY")
            self.assertIn("_reduce_only_recovery", result.raw)

    def test_close_reduce_only_reject_does_not_retry_when_cancel_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "B",
                    "strategy": "B/v16",
                    "positions": [{"symbol": "ARBUSDT", "side": "SHORT", "qty": 4738.7}],
                }
            ])
            write_account_state(root, payload)
            client = ReduceOnlyCancelFailClient()
            engine = ExecutionEngine(client, "B/v16", account_state_root=root, central_confirmation_max_age_seconds=60)

            result = engine.close_position(CloseRequest(
                symbol="ARBUSDT",
                side="short",
                quantity=4738.7,
                confirm_position=False,
            ))

            self.assertFalse(result.success)
            self.assertEqual(result.code, "close_reduce_only_cancel_failed")
            self.assertEqual(client.delete_calls, ["ARBUSDT"])
            self.assertEqual(len(client.close_calls), 1)

    def test_close_reduce_only_reject_does_not_retry_without_cancel_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "B",
                    "strategy": "B/v16",
                    "positions": [{"symbol": "ARBUSDT", "side": "SHORT", "qty": 4738.7}],
                }
            ])
            write_account_state(root, payload)
            client = ReduceOnlyNoCancelClient()
            engine = ExecutionEngine(client, "B/v16", account_state_root=root, central_confirmation_max_age_seconds=60)

            result = engine.close_position(CloseRequest(
                symbol="ARBUSDT",
                side="short",
                quantity=4738.7,
                confirm_position=False,
            ))

            self.assertFalse(result.success)
            self.assertEqual(result.code, "close_reduce_only_cancel_failed")
            self.assertEqual(len(client.close_calls), 1)

    def test_confirmation_rest_fallback_confirms_when_central_state_is_too_old(self):
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

            positions = engine._get_positions_for_confirmation(
                {"min_observed_at": datetime.now(timezone.utc)},
                force_refresh=True,
            )

            self.assertEqual(client.get_positions_calls, 1)
            self.assertEqual(positions[0]["symbol"], "ETHUSDT")

    def test_confirmation_rest_fallback_can_be_disabled(self):
        old_value = os.environ.get("CENTRAL_ACCOUNT_STATE_CONFIRM_REST_FALLBACK_ENABLED")
        os.environ["CENTRAL_ACCOUNT_STATE_CONFIRM_REST_FALLBACK_ENABLED"] = "0"
        try:
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
        finally:
            if old_value is None:
                os.environ.pop("CENTRAL_ACCOUNT_STATE_CONFIRM_REST_FALLBACK_ENABLED", None)
            else:
                os.environ["CENTRAL_ACCOUNT_STATE_CONFIRM_REST_FALLBACK_ENABLED"] = old_value

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

    def test_post_submit_confirmation_does_not_reuse_target_cache_and_falls_back_to_rest(self):
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

            positions = engine._get_positions_for_confirmation(
                {"min_observed_at": observed_at + timedelta(milliseconds=500), **cache},
                force_refresh=False,
            )

            self.assertEqual(client.get_positions_calls, 1)
            self.assertEqual(positions[0]["symbol"], "ETHUSDT")

    def test_open_order_id_without_fill_or_confirmed_position_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "positions": [],
                }
            ])
            write_account_state(root, payload)
            client = UnfilledOpenClient()
            engine = ExecutionEngine(client, "A/v11", account_state_root=root, central_confirmation_max_age_seconds=60)

            result = engine.open_position(OpenRequest(
                symbol="BTCUSDT",
                side="long",
                price=100.0,
                risk_usdt=100.0,
                leverage=4,
                take_profit=110.0,
                stop_loss=90.0,
                quantity=5.0,
            ))

            self.assertFalse(result.success)
            self.assertEqual(result.code, "open_submitted_unconfirmed")
            self.assertEqual(result.status, "OPEN_SUBMITTED_UNCONFIRMED")
            self.assertEqual(result.order_id, "new-unfilled")
            self.assertEqual(result.quantity, 5.0)
            self.assertEqual(client.get_positions_calls, 1)


if __name__ == "__main__":
    unittest.main()
