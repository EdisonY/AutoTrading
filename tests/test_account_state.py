import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.account_state import build_account_state_payload, load_central_account_state, write_account_state
from core.account_state_cache import load_cached_account_state


class AccountStateTest(unittest.TestCase):
    def test_write_and_load_central_account_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "A",
                    "strategy": "A/v11",
                    "wallet_usdt": 1000,
                    "available_usdt": 900,
                    "margin_usdt": 1000,
                    "positions": [
                        {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.2, "entry": 100, "mark": 110, "upnl": 2, "notional": 22, "lev": 4}
                    ],
                }
            ])
            write_account_state(root, payload)

            state = load_central_account_state(root, "A/v11", max_age_seconds=60)
            self.assertIsNotNone(state)
            self.assertEqual(state.account, "A")
            self.assertEqual(state.balance["availableBalance"], "900.0")
            self.assertEqual(state.positions[0]["symbol"], "BTCUSDT")
            self.assertEqual(state.positions[0]["positionAmt"], "0.2")

            cached = load_cached_account_state(root, "A/v11", max_age_seconds=60)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.positions[0]["positionSide"], "LONG")

    def test_reject_stale_account_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_account_state_payload([
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "account": "B",
                    "strategy": "B/v16",
                    "stale": True,
                    "wallet_usdt": 1000,
                }
            ])
            write_account_state(root, payload)

            self.assertIsNone(load_central_account_state(root, "B/v16", max_age_seconds=60))
            self.assertIsNone(load_cached_account_state(root, "B/v16", max_age_seconds=60))

    def test_stale_empty_testnet_assumption_is_cache_only(self):
        old_allow = os.environ.get("BINANCE_ACCOUNT_STATE_ALLOW_STALE_EMPTY_TESTNET")
        old_balance = os.environ.get("BINANCE_ACCOUNT_STATE_TESTNET_BALANCE_USDT")
        os.environ["BINANCE_ACCOUNT_STATE_ALLOW_STALE_EMPTY_TESTNET"] = "1"
        os.environ["BINANCE_ACCOUNT_STATE_TESTNET_BALANCE_USDT"] = "1234"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = build_account_state_payload([
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "account": "B",
                        "strategy": "B/v16",
                        "stale": True,
                        "open_positions": 0,
                        "positions": [],
                    }
                ])
                write_account_state(root, payload)

                self.assertIsNone(load_central_account_state(root, "B/v16", max_age_seconds=60))
                cached = load_cached_account_state(root, "B/v16", max_age_seconds=60)
                self.assertIsNotNone(cached)
                self.assertTrue(cached.assumed)
                self.assertEqual(cached.assumption_reason, "stale_empty_testnet")
                self.assertEqual(cached.balance["availableBalance"], "1234.0")
                self.assertEqual(cached.positions, [])
        finally:
            if old_allow is None:
                os.environ.pop("BINANCE_ACCOUNT_STATE_ALLOW_STALE_EMPTY_TESTNET", None)
            else:
                os.environ["BINANCE_ACCOUNT_STATE_ALLOW_STALE_EMPTY_TESTNET"] = old_allow
            if old_balance is None:
                os.environ.pop("BINANCE_ACCOUNT_STATE_TESTNET_BALANCE_USDT", None)
            else:
                os.environ["BINANCE_ACCOUNT_STATE_TESTNET_BALANCE_USDT"] = old_balance

    def test_legacy_snapshot_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "account_snapshot_latest.json").write_text(
                json.dumps({
                    "accounts": [
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "account": "C",
                            "strategy": "C/v14",
                            "wallet_usdt": 500,
                            "positions": [{"symbol": "ETHUSDT", "side": "SHORT", "qty": 1}],
                        }
                    ]
                }),
                encoding="utf-8",
            )

            state = load_central_account_state(root, "C/v14", max_age_seconds=60)
            self.assertIsNotNone(state)
            self.assertEqual(state.positions[0]["positionAmt"], "-1.0")


if __name__ == "__main__":
    unittest.main()
