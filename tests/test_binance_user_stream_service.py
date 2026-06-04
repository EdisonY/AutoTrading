import json
import tempfile
import time
import unittest
from pathlib import Path

from core.account_state import build_account_state_payload, load_central_account_state, write_account_state
from 部署工具.binance_user_stream_service import run_messages, touch_account_state_row


class BinanceUserStreamServiceTest(unittest.TestCase):
    def test_run_messages_applies_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_account_state(root, build_account_state_payload([
                {"account": "A", "strategy": "A/v11", "wallet_usdt": 1000, "positions": []}
            ]))
            event = {
                "e": "ACCOUNT_UPDATE",
                "E": int(time.time() * 1000),
                "a": {"B": [{"a": "USDT", "wb": "1500", "cw": "1400"}], "P": []},
            }

            run_messages(root=root, strategy="A/v11", messages=[json.dumps(event)], apply_state=True)
            state = load_central_account_state(root, "A/v11", max_age_seconds=60 * 60 * 24 * 365)

            self.assertEqual(float(state.balance["totalWalletBalance"]), 1500.0)

    def test_touch_account_state_row_refreshes_verified_strategy_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {"account": "A", "strategy": "A/v11", "wallet_usdt": 1000, "positions": []}
            ])
            payload["accounts"][0]["ts"] = "2000-01-01T00:00:00+00:00"
            write_account_state(root, payload)

            self.assertFalse(load_central_account_state(root, "A/v11", max_age_seconds=60, allow_legacy=False))
            touched = touch_account_state_row(root=root, strategy="A/v11")
            self.assertTrue(touched)
            state = load_central_account_state(root, "A/v11", max_age_seconds=60, allow_legacy=False)
            self.assertIsNotNone(state)
            refreshed = json.loads((root / "runtime" / "account_state_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(refreshed["summary"]["fresh_accounts"], 1)
            self.assertEqual(refreshed["summary"]["stale_accounts"], [])
            self.assertEqual(refreshed["summary"]["partial_error_count"], 0)

    def test_touch_account_state_row_does_not_refresh_stale_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {"account": "A", "strategy": "A/v11", "wallet_usdt": 0, "positions": []}
            ])
            payload["accounts"][0]["ts"] = "2000-01-01T00:00:00+00:00"
            payload["accounts"][0]["stale"] = True
            payload["accounts"][0]["snapshot_error"] = "bootstrap_empty_no_signed_rest_waiting_for_user_stream"
            payload["summary"]["fresh_accounts"] = 0
            payload["summary"]["stale_accounts"] = ["A"]
            payload["summary"]["partial_error_count"] = 1
            payload["errors"] = ["bootstrap_empty_no_signed_rest_waiting_for_user_stream"]
            write_account_state(root, payload)

            touched = touch_account_state_row(root=root, strategy="A/v11")
            self.assertFalse(touched)
            self.assertIsNone(load_central_account_state(root, "A/v11", max_age_seconds=60, allow_legacy=False))


if __name__ == "__main__":
    unittest.main()
