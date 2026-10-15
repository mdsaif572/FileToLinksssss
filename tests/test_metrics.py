# tests/test_metrics.py
"""Tests for the runtime counters and the stream-failure alerter.

Everything is driven by an injected clock, so the window/cooldown logic is
verified without sleeping.
"""

import asyncio
import os
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from Thunder.utils.metrics import (FailureAlerter, StreamMetrics,  # noqa: E402
                                   human_bytes)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingBot:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram is unhappy")
        self.messages.append((chat_id, text))


class StreamMetricsTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.metrics = StreamMetrics(clock=self.clock)

    def test_counts_streams_bytes_and_disconnects(self):
        self.metrics.record_stream_started()
        self.metrics.record_bytes(1024)
        self.metrics.record_stream_completed()
        self.metrics.record_client_disconnect()

        snapshot = self.metrics.snapshot()["streams"]
        self.assertEqual(snapshot["started"], 1)
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["client_disconnects"], 1)
        self.assertEqual(snapshot["bytes_served"], 1024)
        self.assertEqual(snapshot["active"], 0)

    def test_active_never_goes_negative(self):
        self.metrics.record_stream_completed()
        self.assertEqual(self.metrics.active_streams(), 0)

    def test_cache_hit_rate(self):
        self.metrics.record_message_cache(hit=True)
        self.metrics.record_message_cache(hit=True)
        self.metrics.record_message_cache(hit=False)
        self.assertAlmostEqual(self.metrics.cache_hit_rate(), 66.67, places=1)

    def test_only_severe_errors_open_the_alert_window(self):
        self.metrics.record_error("not_found", "missing")
        self.assertEqual(self.metrics.failures_in_window(), 0)

        self.metrics.record_error("stream", "upstream died")
        self.assertEqual(self.metrics.failures_in_window(), 1)
        self.assertEqual(self.metrics.failed, 1)

    def test_errors_are_attributed_to_clients(self):
        self.metrics.record_error("reference", "expired", client="Client:2")
        self.assertEqual(self.metrics.client_errors["Client:2"], 1)
        self.assertEqual(self.metrics.snapshot()["errors_by_client"], {"Client:2": 1})

    def test_failure_window_forgets_old_failures(self):
        self.metrics.record_error("stream", "first")
        self.assertEqual(self.metrics.failures_in_window(300), 1)

        self.clock.advance(301)
        self.assertEqual(self.metrics.failures_in_window(300), 0)

    def test_report_mentions_the_last_error(self):
        self.metrics.record_stream_started()
        self.metrics.record_error("stream", "FILE_REFERENCE_EXPIRED")
        report = self.metrics.format_report()
        self.assertIn("Streaming Metrics", report)
        self.assertIn("stream", report)

    def test_human_bytes(self):
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(2048), "2.0 KB")


class FailureAlerterTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.metrics = StreamMetrics(clock=self.clock)
        self.alerter = FailureAlerter(
            self.metrics, threshold=3, window_seconds=300,
            cooldown_seconds=900, clock=self.clock
        )

    def _record_failures(self, count):
        for index in range(count):
            self.metrics.record_error("stream", f"failure {index}")

    def test_not_due_below_threshold(self):
        self._record_failures(2)
        self.assertFalse(self.alerter.due())

    def test_due_at_threshold(self):
        self._record_failures(3)
        self.assertTrue(self.alerter.due())

    def test_cooldown_suppresses_a_second_alert(self):
        self._record_failures(3)
        self.alerter._last_alert_at = self.clock.now
        self.assertFalse(self.alerter.due())

        # Past the cooldown, but only a fresh burst triggers the next alert:
        # the old failures have left the shorter alert window by then.
        self.clock.advance(901)
        self.assertFalse(self.alerter.due())
        self._record_failures(3)
        self.assertTrue(self.alerter.due())

    def test_message_includes_the_count_and_last_error(self):
        self._record_failures(4)
        message = self.alerter.build_message()
        self.assertIn("4 stream errors", message)
        self.assertIn("failure 3", message)


class FailureAlerterWatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_watch_alerts_once_and_keeps_running(self):
        clock = FakeClock()
        metrics = StreamMetrics(clock=clock)
        alerter = FailureAlerter(
            metrics, threshold=3, window_seconds=300, cooldown_seconds=900,
            clock=clock
        )
        bot = RecordingBot()
        ticks = 0

        async def fake_sleep(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                for index in range(3):
                    metrics.record_error("stream", f"boom {index}")
            if ticks >= 5:
                raise asyncio.CancelledError
            clock.advance(1)

        with self.assertRaises(asyncio.CancelledError):
            await alerter.watch(bot, chat_id=42, sleep=fake_sleep)

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0][0], 42)
        self.assertIn("3 stream errors", bot.messages[0][1])

    async def test_watch_survives_a_failing_send(self):
        clock = FakeClock()
        metrics = StreamMetrics(clock=clock)
        alerter = FailureAlerter(metrics, threshold=1, clock=clock)
        bot = RecordingBot(fail=True)
        errors = []
        ticks = 0

        async def fake_sleep(_seconds):
            nonlocal ticks
            ticks += 1
            metrics.record_error("stream", "boom")
            if ticks >= 2:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await alerter.watch(bot, chat_id=42, sleep=fake_sleep, on_error=errors.append)

        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
