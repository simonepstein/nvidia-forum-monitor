from __future__ import annotations

import threading
import time


def _sleep(seconds: float) -> None:
    """The single sleep hook; tests replace it so pacing does not slow the suite."""
    time.sleep(seconds)


class RateLimiter:
    """Spaces calls so they run no more often than `requests_per_second`.

    A zero or negative rate never means "unlimited": it falls back to one request per second.
    """

    def __init__(self, requests_per_second: float) -> None:
        rate = requests_per_second if requests_per_second > 0 else 1.0
        self.min_interval = 1.0 / rate
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                _sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self.min_interval
