import json
import tempfile
import time
import unittest
from pathlib import Path

from core.account_state import build_account_state_payload, load_central_account_state, write_account_state
from 部署工具.binance_user_stream_service import run_messages


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


if __name__ == "__main__":
    unittest.main()
