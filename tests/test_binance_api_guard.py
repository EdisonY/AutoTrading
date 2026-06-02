import unittest

from core import binance_api_guard as guard


class BinanceApiGuardRecoveryTest(unittest.TestCase):
    def test_post_ban_phase_progression(self):
        now = 1_000_000_000

        phase, until = guard._recovery_phase({"banned_until_ms": now + 1_000}, now)
        self.assertEqual(phase, "cooldown")
        self.assertEqual(until, now + 1_000)

        phase, until = guard._recovery_phase({"banned_until_ms": now - 1}, now)
        self.assertEqual(phase, "quiet")
        self.assertGreater(until, now)

        after_quiet = now + guard._post_ban_quiet_ms() + 1
        phase, until = guard._recovery_phase({"banned_until_ms": now}, after_quiet)
        self.assertEqual(phase, "recovery")
        self.assertGreater(until, after_quiet)

        after_recovery = now + guard._post_ban_quiet_ms() + guard._recovery_window_ms() + 1
        phase, until = guard._recovery_phase({"banned_until_ms": now}, after_recovery)
        self.assertEqual(phase, "")
        self.assertEqual(until, 0)

    def test_recovery_recent_any_combines_signed_and_public(self):
        now = 1_000_000_000
        state = {
            "recent_requests": [
                {"ts_ms": now - 5_000, "account": "A/v11", "path": "/fapi/v2/balance"},
                {"ts_ms": now - 70_000, "account": "old", "path": "/old"},
            ],
            "recent_public_requests": [
                {"ts_ms": now - 10_000, "label": "C/v14", "path": "/fapi/v1/klines"},
            ],
        }
        recent = guard._recent_any(state, now)
        self.assertEqual(len(recent), 2)
        self.assertEqual([row["ts_ms"] for row in recent], [now - 10_000, now - 5_000])


if __name__ == "__main__":
    unittest.main()
