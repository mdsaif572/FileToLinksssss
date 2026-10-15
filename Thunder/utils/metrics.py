# Thunder/utils/metrics.py
"""Runtime counters for the streaming server.

Deliberately dependency-free: the HTTP routes, the streamer and the bot plugins
all import this module, so it must not import pyrogram/aiohttp or anything else
from Thunder (that is how import cycles start). The clock is injectable so the
alerting logic can be tested without sleeping or waiting.
"""

import time
from collections import Counter, deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_ALERT_THRESHOLD = 5
DEFAULT_ALERT_COOLDOWN_SECONDS = 900.0
DEFAULT_ALERT_POLL_SECONDS = 30.0
_MAX_FAILURE_SAMPLES = 1000

# Error kinds worth waking the owner up for.
_SEVERE_KINDS = frozenset({"stream", "reference", "internal"})


def human_bytes(value: float) -> str:
    """Small local formatter so this module stays import-free."""
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class StreamMetrics:
    """Counts what the streaming layer does and remembers recent failures."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.started = 0
        self.completed = 0
        self.client_disconnects = 0
        self.bytes_served = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.failed = 0
        self.errors: Counter = Counter()
        self.client_errors: Counter = Counter()
        self.last_error: Optional[Tuple[float, str, str]] = None
        self._failure_times: Deque[float] = deque(maxlen=_MAX_FAILURE_SAMPLES)

    # -- recording ---------------------------------------------------------

    def record_stream_started(self) -> None:
        self.started += 1

    def record_stream_completed(self) -> None:
        self.completed += 1

    def record_client_disconnect(self) -> None:
        self.client_disconnects += 1

    def record_bytes(self, count: int) -> None:
        if count > 0:
            self.bytes_served += count

    def record_message_cache(self, hit: bool) -> None:
        if hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

    def record_error(self, kind: str, detail: str = "", client: Optional[str] = None) -> None:
        """``kind`` groups errors: stream, reference, not_found, internal, ..."""
        now = self._clock()
        self.errors[kind] += 1
        if client:
            self.client_errors[client] += 1
        if kind in _SEVERE_KINDS:
            self.failed += 1
            self._failure_times.append(now)
        self.last_error = (now, kind, (detail or "")[:300])

    # -- queries -----------------------------------------------------------

    def active_streams(self) -> int:
        active = self.started - self.completed - self.failed - self.client_disconnects
        return max(active, 0)

    def failures_in_window(self, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> int:
        cutoff = self._clock() - window_seconds
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()
        return len(self._failure_times)

    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total * 100.0) if total else 0.0

    def snapshot(self) -> Dict[str, Any]:
        last = None
        if self.last_error:
            last = {
                "seconds_ago": round(self._clock() - self.last_error[0], 1),
                "kind": self.last_error[1],
                "detail": self.last_error[2],
            }
        return {
            "streams": {
                "started": self.started,
                "completed": self.completed,
                "failed": self.failed,
                "active": self.active_streams(),
                "client_disconnects": self.client_disconnects,
                "bytes_served": self.bytes_served,
            },
            "message_cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate_percent": round(self.cache_hit_rate(), 1),
            },
            "errors": dict(self.errors),
            "errors_by_client": dict(self.client_errors),
            "last_error": last,
        }

    def format_report(self) -> str:
        """Markdown block for the bot's /status command."""
        last_error = self.last_error
        if last_error:
            last_line = f"{last_error[1]} ({int(self._clock() - last_error[0])}s ago)"
        else:
            last_line = "none"

        client_lines = "\n".join(
            f"> • `{client}`: `{count}`" for client, count in self.client_errors.most_common(5)
        ) or "> • none"

        return (
            "📈 **Streaming Metrics**\n\n"
            f"> ▶ Started: `{self.started}`\n"
            f"> ✅ Completed: `{self.completed}`\n"
            f"> ⚡ Active: `{self.active_streams()}`\n"
            f"> ❌ Failed: `{self.failed}`\n"
            f"> 🔌 Client disconnects: `{self.client_disconnects}`\n"
            f"> 📤 Served: `{human_bytes(self.bytes_served)}`\n"
            f"> 🧠 Message cache hit rate: `{self.cache_hit_rate():.1f}%`\n"
            f"> 🕒 Last error: `{last_line}`\n\n"
            "**Errors by client**\n"
            f"{client_lines}"
        )


class FailureAlerter:
    """DMs the owner when streaming failures pile up inside a time window."""

    def __init__(
        self,
        metrics: StreamMetrics,
        *,
        threshold: int = DEFAULT_ALERT_THRESHOLD,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_ALERT_COOLDOWN_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.metrics = metrics
        self.threshold = max(int(threshold), 1)
        self.window_seconds = float(window_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock or metrics._clock
        self._last_alert_at: Optional[float] = None

    def due(self) -> bool:
        if self.metrics.failures_in_window(self.window_seconds) < self.threshold:
            return False
        if self._last_alert_at is not None:
            if self._clock() - self._last_alert_at < self.cooldown_seconds:
                return False
        return True

    def build_message(self) -> str:
        count = self.metrics.failures_in_window(self.window_seconds)
        last = self.metrics.last_error
        detail = last[2] if last else "unknown"
        minutes = max(int(self.window_seconds // 60), 1)
        return (
            "⚠️ **Streaming is failing**\n\n"
            f"> {count} stream errors in the last {minutes} minute(s).\n"
            f"> Last error: `{detail}`\n\n"
            "Open the log (`/log` or the host dashboard) to see the details."
        )

    async def watch(self, bot, chat_id, *, poll_seconds: float = DEFAULT_ALERT_POLL_SECONDS,
                    sleep=None, on_error=None) -> None:
        """Poll for a while; send at most one alert per cooldown window."""
        if sleep is None:
            import asyncio

            sleep = asyncio.sleep

        while True:
            await sleep(poll_seconds)
            if not self.due():
                continue
            self._last_alert_at = self._clock()
            try:
                await bot.send_message(chat_id, self.build_message())
            except Exception as e:  # never let alerting take the bot down
                if on_error is not None:
                    on_error(e)


# Imported by the routes, the message cache and the bot plugins.
metrics = StreamMetrics()
