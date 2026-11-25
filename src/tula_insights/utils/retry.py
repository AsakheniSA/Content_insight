"""Retrying calls that fail for reasons that pass.

Exponential backoff with full jitter, and `Retry-After` wins when the server sends it:
a server that names a delay knows better than our formula, and ignoring it is how a
rate limit turns into a ban.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from typing import TypeVar

from .logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


def retry_after_seconds(error: BaseException) -> float | None:
    """Pull a `Retry-After` header off an exception, if it carries a response."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        # The header may be an HTTP date. Backoff handles that case well enough.
        return None


def call_with_retry(
    func: Callable[[], T],
    *,
    what: str,
    attempts: int = 4,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `func`, retrying transient failures.

    `should_retry` lets a caller inspect the exception — a 429 is worth retrying, a 400
    never is. Returning False re-raises immediately.
    """
    retry_on = tuple(retry_on)
    last: BaseException | None = None

    for attempt in range(1, max(1, attempts) + 1):
        try:
            return func()
        except retry_on as error:
            last = error
            if should_retry is not None and not should_retry(error):
                raise
            if attempt >= attempts:
                raise
            named = retry_after_seconds(error)
            delay = (
                named
                if named is not None
                else min(base_seconds * (2 ** (attempt - 1)) + random.uniform(0, 1.0), max_seconds)
            )
            delay = min(delay, max_seconds)
            log.warning(
                "retry",
                extra={
                    "operation": what,
                    "attempt": attempt,
                    "of": attempts,
                    "sleep_seconds": round(delay, 2),
                    "honoured_retry_after": named is not None,
                    "error_type": type(error).__name__,
                },
            )
            sleep(delay)

    assert last is not None
    raise last
