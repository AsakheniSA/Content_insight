"""Backoff, Retry-After, and the difference between pacing and a ceiling."""

from __future__ import annotations

import pytest

from tula_insights.utils.ratelimit import BudgetExceeded, RateLimiter, RequestBudget
from tula_insights.utils.retry import call_with_retry, retry_after_seconds


class Boom(Exception):
    def __init__(self, retry_after=None):
        super().__init__("boom")
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"retry-after": retry_after}})()


def test_a_successful_call_is_not_retried():
    calls = []
    assert call_with_retry(lambda: calls.append(1) or "ok", what="x") == "ok"
    assert len(calls) == 1


def test_it_retries_then_succeeds():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise Boom()
        return "ok"

    assert call_with_retry(flaky, what="x", attempts=4, sleep=lambda _: None) == "ok"
    assert len(attempts) == 3


def test_it_gives_up_and_re_raises_the_last_error():
    with pytest.raises(Boom):
        call_with_retry(lambda: (_ for _ in ()).throw(Boom()), what="x", attempts=2,
                        sleep=lambda _: None)


def test_should_retry_false_re_raises_immediately():
    attempts = []

    def always_bad():
        attempts.append(1)
        raise Boom()

    with pytest.raises(Boom):
        call_with_retry(always_bad, what="x", attempts=5, should_retry=lambda _: False,
                        sleep=lambda _: None)
    assert len(attempts) == 1, "a 400 must not be retried"


def test_retry_after_is_honoured_over_the_backoff_formula():
    slept = []
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 2:
            raise Boom(retry_after="7")
        return "ok"

    call_with_retry(flaky, what="x", attempts=3, base_seconds=100.0, max_seconds=600.0,
                    sleep=slept.append)
    assert slept == [7.0], "the server's own delay wins over our formula"


def test_retry_after_is_still_capped():
    slept = []
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 2:
            raise Boom(retry_after="99999")
        return "ok"

    call_with_retry(flaky, what="x", attempts=3, max_seconds=30.0, sleep=slept.append)
    assert slept == [30.0]


def test_backoff_grows():
    slept = []
    with pytest.raises(Boom):
        call_with_retry(lambda: (_ for _ in ()).throw(Boom()), what="x", attempts=4,
                        base_seconds=1.0, max_seconds=100.0, sleep=slept.append)
    assert len(slept) == 3
    assert slept[0] < slept[-1]


@pytest.mark.parametrize("header", [None, "", "not-a-number", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_an_unusable_retry_after_falls_back_to_backoff(header):
    error = Boom(retry_after=header) if header is not None else Boom()
    assert retry_after_seconds(error) is None


def test_an_error_with_no_response_has_no_retry_after():
    assert retry_after_seconds(Exception("plain")) is None


# --- budgets -------------------------------------------------------------------------


def test_a_budget_allows_up_to_its_limit():
    budget = RequestBudget(3, label="searches")
    budget.take(2)
    assert budget.remaining == 1
    assert budget.allows(1)
    assert not budget.allows(2)


def test_going_over_a_budget_raises_with_the_label():
    budget = RequestBudget(1, label="searches")
    budget.take(1)
    with pytest.raises(BudgetExceeded, match="searches"):
        budget.take(1)


def test_spend_records_overshoot_without_raising():
    budget = RequestBudget(1)
    budget.spend(5)
    assert budget.used == 5
    assert budget.remaining == 0


def test_a_zero_budget_allows_nothing():
    assert not RequestBudget(0).allows(1)


def test_a_rate_limiter_lets_the_first_calls_straight_through():
    import time

    limiter = RateLimiter(5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - start < 0.5
