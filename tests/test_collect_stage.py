"""The collect stage: isolation, idempotence, dry runs and the runs row."""

from __future__ import annotations

import pytest

from fakes import FakeSearchProvider, FakeSpreadsheet
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.collectors.base import CollectorError
from tula_insights.config import load_config
from tula_insights.models import RunStats
from tula_insights.search.provider import SearchError, SearchResponse, SearchResult
from tula_insights.stages import collect as stage
from tula_insights.store.sheets import SheetStore


def result(url, title="A thread", position=0):
    return SearchResult(url=url, title=title, position=position, query="q")


@pytest.fixture
def config(config_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    return load_config()


@pytest.fixture
def store():
    book = FakeSpreadsheet()
    store = SheetStore(book, SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


@pytest.fixture
def wired(monkeypatch, store):
    """Point the stage at the fake sheet, and let a test choose the search results."""
    provider = FakeSearchProvider()

    def fake_open(config, dry_run=False):
        store.dry_run = dry_run  # as the real open_store would
        return store

    monkeypatch.setattr(stage, "open_store", fake_open)
    monkeypatch.setattr(
        stage, "_build_search", lambda config, result: (None, provider)
    )
    return provider


def rows(store, tab):
    return store.read_rows(tab)


# --- the happy path ------------------------------------------------------------------


def test_items_are_written_with_status_new(config, store, wired):
    wired.responses = [
        SearchResponse(
            results=[result("https://reddit.com/r/dogs/comments/1abc2de/one/", "Dog car sick")],
            search_count=1,
        )
    ]
    outcome = stage.run(config, source="web_questions")

    assert outcome.written == 1
    written = rows(store, "items")
    assert len(written) == 1
    assert written[0]["status"] == "new"
    assert written[0]["source"] == "web_questions"
    assert written[0]["item_id"]
    assert written[0]["run_id"] == outcome.run_id


def test_a_second_run_finding_the_same_thread_writes_nothing_new(config, store, wired):
    url = "https://reddit.com/r/dogs/comments/1abc2de/one/"
    wired.responses = [SearchResponse(results=[result(url)], search_count=1)]
    first = stage.run(config, source="web_questions")
    assert first.written == 1

    wired.responses = [SearchResponse(results=[result(url)], search_count=1)]
    second = stage.run(config, source="web_questions")
    assert second.written == 0
    assert second.duplicates >= 0
    assert len(rows(store, "items")) == 1, "the sheet must not grow a duplicate row"


def test_every_written_row_matches_the_items_schema(config, store, wired):
    wired.responses = [
        SearchResponse(results=[result("https://reddit.com/r/dogs/comments/1abc2de/x/")],
                       search_count=1)
    ]
    stage.run(config, source="web_questions")
    header = store.read_header("items")
    assert header == list(schema.ITEMS.columns)


# --- the runs row --------------------------------------------------------------------


def test_a_runs_row_records_the_searches_so_the_budget_can_see_them(config, store, wired):
    wired.responses = [SearchResponse(results=[], search_count=4)]
    outcome = stage.run(config, source="web_questions")

    run_rows = rows(store, "runs")
    assert len(run_rows) == 1
    stats = RunStats.from_sheet_row(run_rows[0])
    assert stats.run_id == outcome.run_id
    assert stats.api_calls_by_service["claude_web_search"] == 4
    assert stats.started_at is not None


def test_the_runs_row_carries_no_item_text(config, store, wired):
    """The sheet is shared and the repo is public: counts and ids only."""
    wired.responses = [
        SearchResponse(
            results=[result("https://reddit.com/r/dogs/comments/1abc2de/x/", "A very distinctive title")],
            search_count=1,
        )
    ]
    stage.run(config, source="web_questions")
    assert "A very distinctive title" not in str(rows(store, "runs")[0])


# --- dry runs ------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_at_all(config, store, wired):
    wired.responses = [
        SearchResponse(results=[result("https://reddit.com/r/dogs/comments/1abc2de/x/")],
                       search_count=1)
    ]
    outcome = stage.run(config, source="web_questions", dry_run=True)

    assert outcome.collected == 1, "it still does the work"
    assert outcome.written == 0
    assert rows(store, "items") == []
    assert rows(store, "runs") == [], "a dry run spends nothing and must not move the budget"


def test_a_dry_run_still_reports_what_it_would_have_done(config, store, wired):
    wired.responses = [
        SearchResponse(results=[result("https://reddit.com/r/dogs/comments/1abc2de/x/")],
                       search_count=2)
    ]
    outcome = stage.run(config, source="web_questions", dry_run=True)
    text = "\n".join(stage.summary_lines(outcome))
    assert "dry run" in text
    assert "searches      2" in text


# --- failure isolation ---------------------------------------------------------------

def test_a_failing_source_is_reported_with_its_real_reason(config, store, wired):
    wired.error = SearchError("web search is not enabled for this organisation")
    with pytest.raises(CollectorError, match="web search is not enabled"):
        stage.run(config, source="web_questions")


def test_a_crashing_collector_does_not_take_the_stage_down_silently(config, store, monkeypatch):
    monkeypatch.setattr(stage, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(stage, "_build_search", lambda config, result: (None, FakeSearchProvider()))

    class Exploding:
        name = "web_questions"

        def collect(self, cfg, ctx):
            raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(stage, "build_collector", lambda name, ctx: Exploding())
    with pytest.raises(CollectorError, match="ZeroDivisionError"):
        stage.run(config, source="web_questions")


def test_a_missing_api_key_is_reported_clearly(config_dir, store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    monkeypatch.setattr(stage, "open_store", lambda config, dry_run=False: store)

    with pytest.raises(CollectorError, match="ANTHROPIC_API_KEY"):
        stage.run(load_config(), source="web_questions")


# --- run ids -------------------------------------------------------------------------


def test_a_run_id_is_shared_across_jobs_of_one_actions_run(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert stage.make_run_id() == "gha-12345-2"


def test_a_local_run_id_is_unique(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert stage.make_run_id() != stage.make_run_id()


# --- job summary ---------------------------------------------------------------------


def test_the_job_summary_is_written_when_actions_asks_for_one(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    outcome = stage.CollectResult(run_id="r1", items_by_source={"reddit_search": 3}, written=3,
                                  searches=5, est_cost_usd=0.05)
    stage.write_job_summary(outcome)
    body = path.read_text()
    assert "reddit_search" in body and "| 3 |" in body and "0.0500" in body


def test_no_job_summary_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    stage.write_job_summary(stage.CollectResult(run_id="r1"))  # must not raise


# --- after-write hooks ---------------------------------------------------------------


def test_manual_rows_are_marked_processed_by_a_real_run(config, store, wired):
    store.append_rows(
        "manual_submissions",
        [{"question_or_quote": "Is a crate or a harness safer in the car?", "platform": "instagram"}],
    )
    stage.run(config, source="manual")
    assert store.read_rows("manual_submissions")[0]["processed"] == "TRUE"
    assert len(store.read_rows("items")) == 1


def test_a_dry_run_leaves_manual_rows_unprocessed(config, store, wired):
    store.append_rows("manual_submissions", [{"question_or_quote": "How do I calm my cat?"}])
    stage.run(config, source="manual", dry_run=True)
    assert store.read_rows("manual_submissions")[0]["processed"] == ""
    assert store.read_rows("items") == []


def test_a_failed_write_leaves_manual_rows_unprocessed(config, store, wired, monkeypatch):
    """Marking a row processed before its item is safely saved would lose it."""
    from tula_insights.store.sheets import SheetStoreError

    store.append_rows("manual_submissions", [{"question_or_quote": "Can my dog fly?"}])
    original = store.append_rows

    def failing_append(name, rows):
        if name == "items":
            raise SheetStoreError("quota exceeded")
        return original(name, rows)

    monkeypatch.setattr(store, "append_rows", failing_append)
    with pytest.raises(SheetStoreError):
        stage.run(config, source="manual")
    assert store.read_rows("manual_submissions")[0]["processed"] == ""
