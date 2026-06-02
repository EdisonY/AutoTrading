import tempfile
import unittest
from pathlib import Path

from core.binance_api_queue import PRIORITY_HIGH, PRIORITY_TRADE
from core.binance_user_stream import (
    listen_key_due_records,
    listen_key_queue_request,
    mark_listen_key_error,
    upsert_listen_key,
)


class BinanceUserStreamTest(unittest.TestCase):
    def test_upsert_and_due_keepalive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = upsert_listen_key(
                root,
                account="A",
                strategy="A/v11",
                listen_key="abc",
                ttl_ms=60_000,
                at_ms=1_000_000,
            )

            self.assertEqual(record.listen_key, "abc")
            due = listen_key_due_records(root, at_ms=1_050_001, keepalive_margin_ms=15_000)
            self.assertEqual(due[0]["due_action"], "keepalive")

    def test_expired_key_restarts_and_error_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upsert_listen_key(root, account="B", strategy="B/v16", listen_key="old", ttl_ms=10_000, at_ms=1_000_000)
            due = listen_key_due_records(root, at_ms=1_020_000)

            self.assertEqual(due[0]["due_action"], "restart")

            mark_listen_key_error(root, account="B", strategy="B/v16", error="socket closed", at_ms=1_021_000)
            self.assertEqual(listen_key_due_records(root, at_ms=1_021_001), [])

    def test_queue_specs(self):
        start = listen_key_queue_request(action="start", account="A", strategy="A/v11")
        keepalive = listen_key_queue_request(action="keepalive", account="A", strategy="A/v11", listen_key="abc")
        close = listen_key_queue_request(action="close", account="A", strategy="A/v11", listen_key="abc")

        self.assertEqual(start["method"], "POST")
        self.assertEqual(start["priority"], PRIORITY_HIGH)
        self.assertEqual(keepalive["body"], {"listenKey": "abc"})
        self.assertEqual(close["method"], "DELETE")
        self.assertEqual(close["priority"], PRIORITY_TRADE)


if __name__ == "__main__":
    unittest.main()
