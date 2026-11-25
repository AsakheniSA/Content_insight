"""Spend caps, read from the run history rather than a separate counter."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from fakes import FakeSpreadsheet
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.budget import SERVICE_WEB_SEARCH, UsageLedger
from tula_insights.config import load_config
from tula_insights.models import RunStats
from tula_insights.store.sheets import SheetStore

TODAY = date(2026, 11, 25)


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def store():
    book = FakeSpreadsheet()
    store = SheetStore(book, SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def add_run(store, *, when: datetime, searches: int, usd: float = 0.0):
    stats = RunStats(
        run_id=f"r-{when:%Y%m%d%H%M%S}-{searches}",
        started_at=when,
        est_cost_usd=usd,
        api_calls_by_service={SERVICE_WEB_SEARCH: searches},
    )
    store.append_rows("runs", [stats.to_sheet_row()])


def test_an_empty_history_allows_spending(config, store):
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_today == 0
    assert ledger.allows_web_searches(1)


def test_no_store_means_an_empty_ledger(config):
    ledger = UsageLedger.from_store(config, None)
    assert ledger.spent.runs_read == 0
    assert ledger.allows_web_searches(1)


def test_todays_runs_count_against_the_daily_cap(config, store):
    noon = datetime(2026, 11, 25, 12, tzinfo=UTC)
    add_run(store, when=noon, searches=39)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_today == 39
    assert ledger.remaining_web_searches() == 1
    assert ledger.allows_web_searches(1)
    assert not ledger.allows_web_searches(2)


def test_the_daily_cap_message_names_the_numbers(config, store):
    add_run(store, when=datetime(2026, 11, 25, 9, tzinfo=UTC), searches=40)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    decision = ledger.allows_web_searches(1)
    assert not decision
    assert "40" in decision.reason and "daily" in decision.reason


def test_yesterdays_runs_do_not_count_against_today(config, store):
    yesterday = datetime(2026, 11, 24, 12, tzinfo=UTC)
    add_run(store, when=yesterday, searches=40)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_today == 0
    assert ledger.spent.searches_this_month == 40, "but they still count for the month"


def test_last_months_runs_count_for_neither(config, store):
    add_run(store, when=datetime(2026, 10, 25, 12, tzinfo=UTC), searches=500)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_this_month == 0


def test_the_monthly_search_cap_applies(config, store):
    for day in range(1, 25):
        add_run(store, when=datetime(2026, 11, day, 12, tzinfo=UTC), searches=70)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_this_month == 24 * 70
    decision = ledger.allows_web_searches(1)
    assert not decision
    assert "monthly" in decision.reason


def test_the_monthly_spend_cap_stops_everything(config, store):
    add_run(store, when=datetime(2026, 11, 2, tzinfo=UTC), searches=1, usd=25.0)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    decision = ledger.allows_web_searches(1)
    assert not decision
    assert "spend cap" in decision.reason
    assert ledger.remaining_web_searches() == 0


def test_searches_made_during_this_run_count_immediately(config, store):
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    ledger.record_web_searches(40)
    assert not ledger.allows_web_searches(1)
    assert ledger.remaining_web_searches() == 0


def test_spend_within_this_run_adds_to_history(config, store):
    add_run(store, when=datetime(2026, 11, 25, 8, tzinfo=UTC), searches=20)
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    ledger.record_web_searches(15)
    assert ledger.remaining_web_searches() == 5


def test_skips_are_recorded_for_the_summary(config):
    ledger = UsageLedger(config=config)
    ledger.note_skip("reddit_search", "daily cap reached")
    assert ledger.skipped == ["reddit_search: daily cap reached"]


def test_a_run_row_with_no_timestamp_is_ignored(config, store, caplog):
    """Spend that cannot be dated is counted against no day and no month.

    Guessing "today" would let one corrupt row silently halt collection, which is much
    harder to diagnose than a slightly under-counted total.
    """
    store.append_rows("runs", [{"run_id": "broken", "api_calls_by_service": '{"claude_web_search":99}'}])
    ledger = UsageLedger.from_store(config, store, on=TODAY)
    assert ledger.spent.searches_today == 0
    assert ledger.spent.searches_this_month == 0
    assert "budget.undated_runs" in caplog.text


def test_an_unreadable_runs_tab_does_not_stop_a_run(config):
    class Broken:
        def read_rows(self, _name):
            raise RuntimeError("sheet is on fire")

    ledger = UsageLedger.from_store(config, Broken(), on=TODAY)
    assert ledger.spent.runs_read == 0
    assert ledger.allows_web_searches(1)


def test_the_default_policy_is_to_skip_not_fail(config):
    assert UsageLedger(config=config).fail_on_cap is False


def test_a_month_boundary_resets_the_monthly_total(config, store):
    add_run(store, when=datetime(2026, 11, 30, 23, tzinfo=UTC), searches=100)
    ledger = UsageLedger.from_store(config, store, on=date(2026, 12, 1))
    assert ledger.spent.searches_this_month == 0


def test_times_are_read_back_as_utc(config, store):
    sast_evening = datetime(2026, 11, 25, 1, tzinfo=timezone(timedelta(hours=2)))
    add_run(store, when=sast_evening, searches=5)
    ledger = UsageLedger.from_store(config, store, on=date(2026, 11, 24))
    assert ledger.spent.searches_today == 5, "01:00 SAST is 23:00 UTC the previous day"
