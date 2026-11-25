"""The store: tab creation, header validation, appends, updates and the guard rails."""

from __future__ import annotations

import pytest

from fakes import FakeSpreadsheet
from tula_insights import schema
from tula_insights.config import SheetSettings
from tula_insights.store.sheets import SheetStore, SheetStoreError, _contiguous_runs

SETTINGS = SheetSettings(
    spreadsheet_name="Tula Insights",
    cell_limit=10_000_000,
    archive_threshold_fraction=0.70,
    reads_per_minute=60,
    writes_per_minute=60,
    max_retries=2,
    backoff_base_seconds=0.01,
    backoff_max_seconds=0.05,
    new_tab_rows=1000,
)


@pytest.fixture
def book() -> FakeSpreadsheet:
    return FakeSpreadsheet()


@pytest.fixture
def store(book) -> SheetStore:
    return SheetStore(book, SETTINGS)


@pytest.fixture
def ready(store) -> SheetStore:
    """A store with every tab already created."""
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


# --- creating tabs -------------------------------------------------------------------


def test_ensure_tab_creates_with_header_frozen(store, book):
    created, problems = store.ensure_tab(schema.ITEMS)
    assert created is True
    assert problems == []
    sheet = book.worksheet("items")
    assert sheet.values[0] == list(schema.ITEMS.columns)
    assert sheet.frozen_rows == 1


def test_ensure_tab_is_idempotent(store):
    assert store.ensure_tab(schema.ITEMS)[0] is True
    assert store.ensure_tab(schema.ITEMS)[0] is False


def test_internal_tabs_are_hidden_on_creation(store, book):
    store.ensure_tab(schema.EMBEDDINGS)
    assert book.worksheet("_embeddings").hidden is True


def test_visible_tabs_are_not_hidden(store, book):
    store.ensure_tab(schema.FEED)
    assert book.worksheet("feed").hidden is False


def test_dry_run_creates_nothing(book):
    store = SheetStore(book, SETTINGS, dry_run=True)
    created, _ = store.ensure_tab(schema.ITEMS)
    assert created is True
    assert book._sheets == {}


# --- header validation ---------------------------------------------------------------


def test_missing_column_is_reported(store, book):
    store.ensure_tab(schema.FEED)
    sheet = book.worksheet("feed")
    sheet.values[0] = [c for c in schema.FEED.columns if c != "post_ref"]
    problems = store.header_problems(schema.FEED)
    assert any("missing column" in p and "post_ref" in p for p in problems)


def test_extra_column_is_reported_but_left_alone(store, book):
    store.ensure_tab(schema.FEED)
    sheet = book.worksheet("feed")
    sheet.values[0] = [*schema.FEED.columns, "someone_added_this"]
    problems = store.header_problems(schema.FEED)
    assert any("someone_added_this" in p for p in problems)
    assert "someone_added_this" in sheet.values[0], "an existing header must not be rewritten"


def test_reordered_columns_are_reported(store, book):
    store.ensure_tab(schema.FEED)
    sheet = book.worksheet("feed")
    reordered = list(schema.FEED.columns)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    sheet.values[0] = reordered
    assert any("out of order" in p for p in store.header_problems(schema.FEED))


def test_a_correct_header_has_no_problems(ready):
    for spec in schema.TABS:
        assert ready.header_problems(spec) == []


# --- reading and writing -------------------------------------------------------------


def test_append_orders_values_by_the_schema(ready, book):
    ready.append_rows("items", [{"item_id": "a1", "source": "reddit_search", "status": "new"}])
    row = book.worksheet("items").values[1]
    assert row[schema.ITEMS.index_of("item_id")] == "a1"
    assert row[schema.ITEMS.index_of("source")] == "reddit_search"
    assert len(row) == len(schema.ITEMS.columns)


def test_append_of_nothing_is_a_no_op(ready, book):
    assert ready.append_rows("items", []) == 0
    assert len(book.worksheet("items").values) == 1


def test_read_rows_skips_blank_lines(ready, book):
    ready.append_rows("items", [{"item_id": "a1"}, {"item_id": "a2"}])
    book.worksheet("items").values.insert(2, [])
    rows = ready.read_rows("items")
    assert [r["item_id"] for r in rows] == ["a1", "a2"]


def test_existing_keys_reads_only_the_key_column(ready):
    ready.append_rows("items", [{"item_id": "a1"}, {"item_id": "a2"}])
    assert ready.existing_keys("items") == {"a1", "a2"}


def test_update_rows_changes_only_the_named_cells(ready, book):
    ready.append_rows("items", [{"item_id": "a1", "source": "s", "status": "new"}])
    ready.update_rows("items", {"a1": {"status": "classified", "confidence": 0.9}})
    row = book.worksheet("items").values[1]
    assert row[schema.ITEMS.index_of("status")] == "classified"
    assert row[schema.ITEMS.index_of("confidence")] == 0.9
    assert row[schema.ITEMS.index_of("source")] == "s", "an untouched column must survive"


def test_update_of_an_unknown_key_is_counted_not_fatal(ready):
    ready.append_rows("items", [{"item_id": "a1"}])
    assert ready.update_rows("items", {"a1": {"status": "x"}, "ghost": {"status": "y"}}) == 1


def test_dry_run_writes_nothing(book, ready):
    store = SheetStore(book, SETTINGS, dry_run=True)
    before = len(book.worksheet("items").values)
    store.append_rows("items", [{"item_id": "a1"}])
    assert len(book.worksheet("items").values) == before


# --- guard rails ---------------------------------------------------------------------


def test_writing_an_unknown_column_fails_loudly(ready):
    with pytest.raises(SheetStoreError, match="no column"):
        ready.append_rows("items", [{"item_id": "a1", "not_a_column": 1}])


def test_the_pipeline_cannot_overwrite_a_reviewer_decision(ready):
    ready.append_rows(
        "feed",
        [{"feed_date": "2026-11-25", "rank": 1, "insight_id": "i1", "question": "q"}],
    )
    with pytest.raises(SheetStoreError, match="edited by people"):
        ready.update_rows("feed", {"i1": {"status": "used"}})


def test_the_pipeline_may_still_update_its_own_feed_columns(ready, book):
    ready.append_rows(
        "feed",
        [{"feed_date": "2026-11-25", "rank": 1, "insight_id": "i1", "question": "q"}],
    )
    ready.update_rows("feed", {"i1": {"suggested_angle": "a new angle"}})
    row = book.worksheet("feed").values[1]
    assert row[schema.FEED.index_of("suggested_angle")] == "a new angle"


def test_updating_a_tab_without_a_key_fails(ready):
    with pytest.raises(SheetStoreError, match="no key column"):
        ready.update_rows("manual_submissions", {"x": {"processed": True}})


def test_a_missing_tab_tells_you_how_to_create_it(store):
    with pytest.raises(SheetStoreError, match="setup_sheet"):
        store.read_rows("items")


# --- housekeeping --------------------------------------------------------------------


def test_needs_archiving_is_false_on_a_small_sheet(ready):
    assert ready.needs_archiving() is False


def test_needs_archiving_trips_at_the_threshold(book):
    settings = SETTINGS.model_copy(update={"cell_limit": 1000, "archive_threshold_fraction": 0.5})
    store = SheetStore(book, settings)
    store.ensure_tab(schema.ITEMS)
    book.worksheet("items").row_count = 1000
    book.worksheet("items").col_count = 1
    assert store.needs_archiving() is True


def test_contiguous_runs_groups_adjacent_columns():
    assert _contiguous_runs([0, 1, 2, 5, 6, 9]) == [[0, 1, 2], [5, 6], [9]]
    assert _contiguous_runs([]) == []
