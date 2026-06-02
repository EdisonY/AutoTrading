import unittest

from core.account_state import build_account_state_payload, load_central_account_state
from core.account_state_stream import apply_user_stream_event


class AccountStateStreamTest(unittest.TestCase):
    def test_account_update_updates_balance_and_positions(self):
        payload = build_account_state_payload([
            {
                "account": "A",
                "strategy": "A/v11",
                "wallet_usdt": 1000,
                "available_usdt": 900,
                "positions": [],
            }
        ])
        event = {
            "e": "ACCOUNT_UPDATE",
            "E": 1780449000000,
            "a": {
                "B": [{"a": "USDT", "wb": "1200.5", "cw": "1100.25"}],
                "P": [{"s": "BTCUSDT", "pa": "0.010", "ep": "50000", "up": "12.3", "ps": "LONG"}],
            },
        }

        updated = apply_user_stream_event(payload, strategy="A/v11", event=event)
        account = updated["accounts"][0]

        self.assertEqual(account["wallet_usdt"], 1200.5)
        self.assertEqual(account["available_usdt"], 1100.25)
        self.assertEqual(account["open_positions"], 1)
        self.assertEqual(account["longs"], 1)
        self.assertEqual(account["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(account["positions"][0]["side"], "LONG")

    def test_account_update_removes_zero_position(self):
        payload = build_account_state_payload([
            {
                "account": "B",
                "strategy": "B/v16",
                "wallet_usdt": 1000,
                "positions": [{"symbol": "ETHUSDT", "side": "SHORT", "qty": 2, "entry": 100, "upnl": -1}],
            }
        ])
        event = {
            "e": "ACCOUNT_UPDATE",
            "E": 1780449000000,
            "a": {"P": [{"s": "ETHUSDT", "pa": "0", "ep": "0", "up": "0", "ps": "SHORT"}]},
        }

        updated = apply_user_stream_event(payload, strategy="B/v16", event=event)

        self.assertEqual(updated["accounts"][0]["open_positions"], 0)
        self.assertEqual(updated["accounts"][0]["positions"], [])

    def test_non_account_event_is_ignored(self):
        payload = build_account_state_payload([{"account": "C", "strategy": "C/v14", "wallet_usdt": 100}])
        updated = apply_user_stream_event(payload, strategy="C/v14", event={"e": "ORDER_TRADE_UPDATE"})

        self.assertEqual(updated, payload)


if __name__ == "__main__":
    unittest.main()
