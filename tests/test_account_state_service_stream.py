import json
import tempfile
import time
import unittest
from pathlib import Path

from core.account_state import build_account_state_payload, load_central_account_state, write_account_state
from 部署工具.account_state_service import apply_stream_events_once


class AccountStateServiceStreamTest(unittest.TestCase):
    def test_apply_stream_events_updates_written_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {"account": "A", "strategy": "A/v11", "wallet_usdt": 1000, "positions": []}
            ])
            write_account_state(root, payload)
            events = root / "events.jsonl"
            events.write_text(
                json.dumps({
                    "e": "ACCOUNT_UPDATE",
                    "E": int(time.time() * 1000),
                    "a": {
                        "B": [{"a": "USDT", "wb": "1300", "cw": "1250"}],
                        "P": [{"s": "SOLUSDT", "pa": "3", "ep": "150", "up": "2.5", "ps": "LONG"}],
                    },
                })
                + "\n",
                encoding="utf-8",
            )

            apply_stream_events_once(events_path=events, strategy="A/v11", root=root)
            state = load_central_account_state(root, "A/v11", max_age_seconds=60 * 60)

            self.assertIsNotNone(state)
            self.assertEqual(float(state.balance["totalWalletBalance"]), 1300.0)
            self.assertEqual(state.positions[0]["symbol"], "SOLUSDT")

    def test_apply_stream_events_skips_duplicate_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {"account": "A", "strategy": "A/v11", "wallet_usdt": 1000, "positions": []}
            ])
            write_account_state(root, payload)
            event = {
                "e": "ACCOUNT_UPDATE",
                "E": int(time.time() * 1000),
                "T": int(time.time() * 1000),
                "a": {"B": [{"a": "USDT", "wb": "1400", "cw": "1350"}]},
            }
            events = root / "events.jsonl"
            events.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")

            apply_stream_events_once(events_path=events, strategy="A/v11", root=root)
            state = load_central_account_state(root, "A/v11", max_age_seconds=60 * 60)

            self.assertEqual(float(state.balance["totalWalletBalance"]), 1400.0)
            offset_path = root / "runtime" / "account_state_stream_offsets.json"
            self.assertTrue(offset_path.exists())


if __name__ == "__main__":
    unittest.main()
