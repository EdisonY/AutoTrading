import tempfile
import unittest
from pathlib import Path

from core.binance_api_queue import (
    BinanceApiQueue,
    PRIORITY_NORMAL,
    PRIORITY_TRADE,
    STATUS_DEFERRED,
    STATUS_DONE,
)


class BinanceApiQueueTest(unittest.TestCase):
    def make_queue(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return BinanceApiQueue(Path(tmp.name) / "queue.sqlite3")

    def test_priority_order(self):
        queue = self.make_queue()
        normal = queue.submit_request(scope="public", method="GET", path="/fapi/v1/klines", priority=PRIORITY_NORMAL, idempotency_key="n")
        trade = queue.submit_request(scope="signed", method="POST", path="/fapi/v1/order", priority=PRIORITY_TRADE, idempotency_key="t")
        ready_at = max(normal.earliest_ms, trade.earliest_ms) + 1

        first = queue.lease_next(worker_id="test", at_ms=ready_at)
        second = queue.lease_next(worker_id="test", at_ms=ready_at)

        self.assertEqual(first.request_id, trade.request_id)
        self.assertEqual(second.request_id, normal.request_id)

    def test_cooldown_defers_matching_scope(self):
        queue = self.make_queue()
        req = queue.submit_request(scope="signed", account="A/v11", method="GET", path="/fapi/v2/balance", idempotency_key="a")
        queue.set_cooldown(scope="signed", account="A/v11", until_ms=req.earliest_ms + 60_000, reason="rate_limit")

        leased = queue.lease_next(worker_id="test", at_ms=req.earliest_ms + 1)
        stored = queue.get_request(req.request_id)

        self.assertIsNone(leased)
        self.assertEqual(stored.status, STATUS_DEFERRED)
        self.assertEqual(stored.error, "rate_limit")
        self.assertGreaterEqual(stored.earliest_ms, req.earliest_ms + 60_000)

    def test_idempotent_submit_and_result_record(self):
        queue = self.make_queue()
        one = queue.submit_request(scope="public", method="GET", path="/fapi/v1/ticker/24hr", idempotency_key="same")
        two = queue.submit_request(scope="public", method="GET", path="/fapi/v1/ticker/24hr", idempotency_key="same")

        self.assertEqual(one.request_id, two.request_id)

        leased = queue.lease_next(worker_id="test", at_ms=one.earliest_ms + 1)
        done = queue.complete_request(leased.request_id, result_status=200, result_body={"ok": True})

        self.assertEqual(done.status, STATUS_DONE)
        self.assertEqual(done.result_status, 200)
        self.assertEqual(done.result_body, {"ok": True})


if __name__ == "__main__":
    unittest.main()
