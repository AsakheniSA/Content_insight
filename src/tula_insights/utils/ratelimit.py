"""Rate limiting and per-run request budgets.

Two different jobs that are easy to confuse:

* :class:`RateLimiter` paces calls so a remote API is not hit too fast. It blocks.
* :class:`RequestBudget` enforces a hard ceiling on how many calls a run may make. It
  refuses, and the caller stops.

Both are thread-safe, because the collect stage runs as a matrix of parallel jobs and,
within a job, a collector may fan out.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """A sliding-window limiter. Blocks until a call is allowed; never raises."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            time.sleep(min(wait, 60.0))


class BudgetExceeded(RuntimeError):
    """Raised when a request would take a run past a configured cap."""


class RequestBudget:
    """A countdown of how many requests remain. Not a limiter — a ceiling."""

    def __init__(self, limit: int, *, label: str = "requests") -> None:
        self.limit = max(0, limit)
        self.label = label
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._used)

    def allows(self, count: int = 1) -> bool:
        with self._lock:
            return self._used + count <= self.limit

    def take(self, count: int = 1) -> None:
        """Consume budget, or raise :class:`BudgetExceeded`."""
        with self._lock:
            if self._used + count > self.limit:
                raise BudgetExceeded(
                    f"{self.label} cap of {self.limit} reached "
                    f"({self._used} used, {count} more requested)"
                )
            self._used += count

    def spend(self, count: int) -> None:
        """Record usage that already happened, even if it overshoots the cap."""
        with self._lock:
            self._used += count
