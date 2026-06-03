import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.account_state import build_account_state_payload, write_account_state
from core.execution_engine import ConfirmationStateUnavailable, ExecutionEngine


class FakeClient:
    def __init__(self):
        self.get_positions_calls = 0

    def get_positions(self):
        self.get_positions_calls += 1
        return [{"symbol": "ETHUSDT", "positionAmt": "2", "positionSide": "LONG"}]

    def invalidate_account_snapshot(self):
        pass


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
