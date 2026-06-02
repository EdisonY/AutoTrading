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


if __name__ == "__main__":
    unittest.main()
