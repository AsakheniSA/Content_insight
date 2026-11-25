"""Platform storage limits: YouTube data must not outlive 30 days."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeSpreadsheet
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.config import load_config
from tula_insights.models import ItemRow
from tula_insights.stages.retention import PLATFORM_COLUMNS, expired_item_ids, purge_expired
from tula_insights.store.sheets import SheetStore

NOW = datetime(2026, 12, 31, 12, tzinfo=UTC)


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def add(store, item_id, source, days_old, status="classified"):
    store.append_rows("items", [{
        "item_id": item_id,
        "source": source,
        "url": f"https://example.com/{item_id}",
        "title": "A real comment from a pet owner",
        "snippet": "Comment text",
        "question": "Can my dog fly?",
        "engagement_json": '{"comment_likes":4}',
        "collected_at": (NOW - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query": "dog flying",
        "insight_id": "ins1",
        "status": status,
    }])


def row(store, item_id):
    return next(r for r in store.read_rows("items") if r["item_id"] == item_id)


def test_youtube_rows_past_30_days_are_purged(config, store):
    add(store, "old", "youtube", days_old=31)
    assert purge_expired(store, config, now=NOW) == 1
    purged = row(store, "old")
    for column in PLATFORM_COLUMNS:
        assert purged[column] == "", f"{column} still holds platform data"
    assert purged["status"] == "purged"


def test_what_a_purged_row_keeps(config, store):
    """Enough to keep an insight's evidence count honest — nothing YouTube supplied."""
    add(store, "old", "youtube", days_old=31)
    purge_expired(store, config, now=NOW)
    kept = row(store, "old")
    assert kept["item_id"] == "old"
    assert kept["source"] == "youtube"
    assert kept["insight_id"] == "ins1"
    assert kept["query"] == "dog flying"


def test_recent_youtube_rows_are_left_alone(config, store):
    add(store, "fresh", "youtube", days_old=29)
    assert purge_expired(store, config, now=NOW) == 0
    assert row(store, "fresh")["title"]


def test_other_sources_are_not_touched(config, store):
    add(store, "web", "web_questions", days_old=90)
    assert purge_expired(store, config, now=NOW) == 0
    assert row(store, "web")["title"]


def test_already_purged_rows_are_not_rewritten(config, store):
    add(store, "done", "youtube", days_old=60, status="purged")
    assert purge_expired(store, config, now=NOW) == 0


def test_a_row_with_no_collection_date_is_not_guessed_at():
    rows = [ItemRow(item_id="x", source="youtube", collected_at=None)]
    assert expired_item_ids(rows, {"youtube": 30}, now=NOW) == []


def test_the_limit_is_exactly_the_configured_age():
    edge = ItemRow(item_id="e", source="youtube", collected_at=NOW - timedelta(days=30))
    assert expired_item_ids([edge], {"youtube": 30}, now=NOW) == ["e"]


def test_youtube_has_a_30_day_limit_in_the_real_config(config):
    assert config.settings.retention.api_data_max_days["youtube"] == 30
