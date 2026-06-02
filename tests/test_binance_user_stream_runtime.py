import json
import tempfile
import unittest
from pathlib import Path

from core.binance_user_stream_runtime import parse_stream_message, process_stream_messages, user_stream_url


class BinanceUserStreamRuntimeTest(unittest.TestCase):
    def test_parse_and_url(self):
        self.assertEqual(user_stream_url("abc"), "wss://stream.binancefuture.com/ws/abc")
        self.assertEqual(parse_stream_message('{"e":"ACCOUNT_UPDATE"}')["e"], "ACCOUNT_UPDATE")
        self.assertIsNone(parse_stream_message("not-json"))

    def test_process_stream_messages_writes_log_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = process_stream_messages(
                root,
                strategy="A/v11",
                messages=[
                    json.dumps({"e": "ACCOUNT_UPDATE", "E": 1, "a": {"B": [], "P": []}}),
                    "",
                ],
            )

            self.assertEqual(result["parsed"], 1)
            self.assertEqual(result["ignored"], 1)
            self.assertTrue(Path(result["log_path"]).exists())
            self.assertTrue(Path(result["last_event_file"]).exists())


if __name__ == "__main__":
    unittest.main()
