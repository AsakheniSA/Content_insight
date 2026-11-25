"""Manual submissions: what is kept, what is not, and the processed flag."""

from __future__ import annotations

import pytest

from fakes import FakeSpreadsheet
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.budget import UsageLedger
from tula_insights.collectors.base import CollectorError, RunContext
from tula_insights.collectors.manual import ManualCollector, submission_key
from tula_insights.config import load_config
from tula_insights.store.sheets import SheetStore, SheetStoreError


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def source(config):
    return next(s for s in config.sources.resolve() if s.name == "manual")


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def submit(store, **fields):
    row = {
        "submitted_at": "2026-11-25",
        "submitted_by": "Thandi",
        "platform": "reddit",
        "url": "https://www.reddit.com/r/dogs/comments/1abc2de/flying/",
        "question_or_quote": "Can my staffie fly in the cabin to Cape Town?",
        "notes": "posted by u/someone, 40 replies",
        "processed": "",
    }
    row.update(fields)
    store.append_rows("manual_submissions", [row])


def ctx(config, store, **kwargs):
    return RunContext(
        run_id="r1", config=config, ledger=UsageLedger(config=config), store=store, **kwargs
    )


def processed_flags(store):
    return [r["processed"] for r in store.read_rows("manual_submissions")]


# --- collecting ----------------------------------------------------------------------


def test_a_submission_becomes_an_item(config, source, store):
    submit(store)
    items = ManualCollector().collect(source, ctx(config, store))
    assert len(items) == 1
    item = items[0]
    assert item.source == "manual"
    assert item.snippet == "Can my staffie fly in the cabin to Cape Town?"
    assert item.title == item.snippet
    assert item.url.startswith("https://www.reddit.com/")
    assert item.engagement["platform"] == "reddit"
    assert item.query == "manual:reddit"


def test_the_submitter_and_notes_never_reach_the_item(config, source, store):
    """submitted_by names a team member; notes may name the poster."""
    submit(store)
    item = ManualCollector().collect(source, ctx(config, store))[0]
    flat = str(item.model_dump())
    assert "Thandi" not in flat
    assert "40 replies" not in flat
    assert "u/someone" not in flat


def test_the_quote_is_scrubbed(config, source, store):
    submit(store, question_or_quote="u/jane asks: email me a@b.com about crates")
    item = ManualCollector().collect(source, ctx(config, store))[0]
    assert item.snippet == "[user] asks: email me [email] about crates"


def test_a_profile_link_is_dropped_but_the_quote_kept(config, source, store):
    submit(store, url="https://www.facebook.com/profile/12345")
    item = ManualCollector().collect(source, ctx(config, store))[0]
    assert item.url == ""
    assert item.snippet


def test_a_submission_without_a_url_still_counts(config, source, store):
    submit(store, url="", platform="instagram")
    items = ManualCollector().collect(source, ctx(config, store))
    assert len(items) == 1
    assert items[0].item_id


def test_long_quotes_get_a_short_title_and_a_full_snippet(config, source, store):
    long_quote = "My dog " + "really " * 60 + "hates the car."
    submit(store, question_or_quote=long_quote)
    item = ManualCollector().collect(source, ctx(config, store))[0]
    assert len(item.title) <= 160
    assert item.title.endswith("…")
    assert len(item.snippet) > len(item.title)


def test_empty_rows_are_skipped_with_a_warning(config, source, store):
    submit(store, question_or_quote="")
    context = ctx(config, store)
    assert ManualCollector().collect(source, context) == []
    assert any("no question_or_quote" in w for w in context.warnings)


def test_processed_rows_are_skipped(config, source, store):
    submit(store, processed="TRUE")
    assert ManualCollector().collect(source, ctx(config, store)) == []


def test_the_same_thread_pasted_twice_is_one_item(config, source, store):
    submit(store, submitted_by="A")
    submit(store, submitted_by="B", question_or_quote="  can my STAFFIE fly in the cabin to cape town? ")
    assert len(ManualCollector().collect(source, ctx(config, store))) == 1


def test_identity_ignores_case_and_spacing():
    assert submission_key("https://x.com/a", "Hello  World") == submission_key(
        "HTTPS://X.COM/A", "hello world"
    )


def test_an_item_already_collected_is_not_returned_again(config, source, store):
    submit(store)
    first = ManualCollector().collect(source, ctx(config, store))[0]
    again = ManualCollector().collect(source, ctx(config, store, known_item_ids={first.item_id}))
    assert again == []


def test_no_sheet_access_is_a_clear_error(config, source):
    with pytest.raises(CollectorError, match="spreadsheet access"):
        ManualCollector().collect(source, ctx(config, None))


# --- the processed flag --------------------------------------------------------------


def test_rows_are_marked_processed_after_the_write(config, source, store):
    submit(store)
    submit(store, question_or_quote="How do I calm a cat on a long drive?")
    collector = ManualCollector()
    context = ctx(config, store)
    items = collector.collect(source, context)
    assert processed_flags(store) == ["", ""], "collecting alone must not mark anything"

    collector.after_write(items, context)
    assert processed_flags(store) == ["TRUE", "TRUE"]


def test_a_duplicate_row_is_still_marked_processed(config, source, store):
    submit(store)
    first = ManualCollector().collect(source, ctx(config, store))[0]

    collector = ManualCollector()
    context = ctx(config, store, known_item_ids={first.item_id})
    items = collector.collect(source, context)
    collector.after_write(items, context)
    assert processed_flags(store) == ["TRUE"]


def test_a_dry_run_marks_nothing(config, source, store):
    submit(store)
    collector = ManualCollector()
    context = ctx(config, store, dry_run=True)
    collector.after_write(collector.collect(source, context), context)
    assert processed_flags(store) == [""]


def test_a_row_that_moved_is_not_mislabelled(config, source, store):
    """If someone sorts or inserts rows mid-run, the flag must not land on the wrong row."""
    submit(store, question_or_quote="first question")
    collector = ManualCollector()
    context = ctx(config, store)
    items = collector.collect(source, context)

    # A teammate inserts a new row above it between the read and the write.
    sheet = store.book.worksheet("manual_submissions")
    new_row = ["2026-11-25", "C", "facebook", "", "an unrelated new question", "", ""]
    sheet.values.insert(1, new_row)

    collector.after_write(items, context)
    flags = processed_flags(store)
    assert flags[0] == "", "the newly inserted row must not be marked"
    assert any("moved" in w for w in context.warnings)


def test_the_pipeline_still_cannot_write_columns_people_edit(store):
    submit(store)
    with pytest.raises(SheetStoreError, match="edited by people"):
        store.update_cells_by_row("manual_submissions", {2: {"question_or_quote": "x"}})
