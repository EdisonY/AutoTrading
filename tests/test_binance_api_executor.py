import os
import tempfile
import unittest
from pathlib import Path

from core.binance_api_executor import execute_next_api_queue_request
from core.binance_api_queue import BinanceApiQueue, PRIORITY_HIGH, STATUS_DEFERRED, STATUS_DONE, STATUS_FAILED


class FakeTransport:
    def __init__(self, status=200, body='{"ok": true}'):
        self.status = status
        self.body = body
        self.calls = []

    def request(self, method, url, *, headers, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
        return self.status, self.body


class BinanceApiExecutorTest(unittest.TestCase):
    def make_queue(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return BinanceApiQueue(Path(tmp.name) / "queue.sqlite3")

    def test_execute_public_request_records_result(self):
        queue = self.make_queue()
        req = queue.submit_request(scope="public", method="GET", path="/fapi/v1/time", priority=PRIORITY_HIGH)
        transport = FakeTransport(body='{"serverTime": 123}')

        done = execute_next_api_queue_request(queue, transport=transport)

        self.assertEqual(done.status, STATUS_DONE)
        self.assertEqual(done.result_body, {"serverTime": 123})
        self.assertIn("/fapi/v1/time", transport.calls[0]["url"])
        self.assertEqual(queue.get_request(req.request_id).status, STATUS_DONE)

    def test_execute_signed_request_adds_signature_and_header(self):
        old = {name: os.environ.get(name) for name in ("BINANCE_A_API_KEY", "BINANCE_A_API_SECRET")}
        os.environ["BINANCE_A_API_KEY"] = "key-a"
        os.environ["BINANCE_A_API_SECRET"] = "secret-a"
        try:
            queue = self.make_queue()
            queue.submit_request(
                scope="signed",
                account="A/v11",
                method="PUT",
                path="/fapi/v1/listenKey",
                body={"listenKey": "abc"},
            )
            transport = FakeTransport(body='{"ok": true}')

            done = execute_next_api_queue_request(queue, transport=transport)

            self.assertEqual(done.status, STATUS_DONE)
            call = transport.calls[0]
            self.assertEqual(call["method"], "PUT")
            self.assertEqual(call["headers"]["X-MBX-APIKEY"], "key-a")
            self.assertIn("listenKey=abc", call["url"])
            self.assertIn("timestamp=", call["url"])
            self.assertIn("signature=", call["url"])
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_rate_limit_response_sets_cooldown_and_retries(self):
        queue = self.make_queue()
        req = queue.submit_request(scope="public", method="GET", path="/fapi/v1/time")
        transport = FakeTransport(status=429, body='{"code":-1003,"msg":"Too many requests"}')

        result = execute_next_api_queue_request(queue, transport=transport)

        self.assertEqual(result.status, STATUS_DEFERRED)
        cooldown_until, reason = queue.active_cooldown(scope="public", account="")
        self.assertGreater(cooldown_until, req.earliest_ms)
        self.assertEqual(reason, "HTTP 429")

    def test_non_rate_limit_http_error_fails_request(self):
        queue = self.make_queue()
        queue.submit_request(scope="public", method="GET", path="/bad")
        transport = FakeTransport(status=400, body='{"code":-1,"msg":"bad"}')

        result = execute_next_api_queue_request(queue, transport=transport)

        self.assertEqual(result.status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
