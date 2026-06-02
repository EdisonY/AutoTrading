import threading
import time
import tempfile
import unittest
from pathlib import Path

from core.binance_api_queue import BinanceApiQueue
from core.binance_api_queue_client import priority_for_request, queued_api_request


class BinanceApiQueueClientTest(unittest.TestCase):
    def make_queue(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return BinanceApiQueue(Path(tmp.name) / "queue.sqlite3")

    def test_waits_for_executor_result(self):
        queue = self.make_queue()

        def worker():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                leased = queue.lease_next(worker_id="test-worker")
                if leased:
                    queue.complete_request(leased.request_id, result_status=200, result_body={"ok": True})
                    return
                time.sleep(0.02)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            result = queued_api_request(
                scope="public",
                label="test",
                method="GET",
                path="/fapi/v1/time",
                queue=queue,
                timeout_sec=2,
                poll_interval_sec=0.02,
            )
        finally:
            thread.join(timeout=2)

        self.assertEqual(result, {"ok": True})

    def test_times_out_without_executor(self):
        queue = self.make_queue()

        result = queued_api_request(
            scope="signed",
            account="A",
            label="A/v11",
            method="GET",
            path="/fapi/v2/balance",
            queue=queue,
            timeout_sec=0.1,
            poll_interval_sec=0.02,
        )

        self.assertEqual(result["code"], "-1")
        self.assertEqual(result["queue_status"], "queued")
        self.assertTrue(result["request_cancelled"])
        self.assertEqual(queue.summary()["counts"], {"failed": 1})

    def test_active_cooldown_blocks_submit(self):
        queue = self.make_queue()
        now = int(time.time() * 1000)
        queue.set_cooldown(scope="public", until_ms=now + 60_000, reason="HTTP 418")

        result = queued_api_request(
            scope="public",
            label="test",
            method="GET",
            path="/fapi/v1/klines",
            queue=queue,
            timeout_sec=0.1,
            poll_interval_sec=0.02,
        )

        self.assertEqual(result["code"], "-1")
        self.assertEqual(result["queue_status"], "cooldown")
        self.assertEqual(queue.summary()["counts"], {})

    def test_trade_paths_have_trade_priority(self):
        self.assertGreater(priority_for_request("POST", "/fapi/v1/order"), priority_for_request("GET", "/fapi/v2/balance"))


if __name__ == "__main__":
    unittest.main()
