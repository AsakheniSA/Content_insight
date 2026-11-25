"""The search collector: planning, caps, identity and PII."""

from __future__ import annotations

from datetime import UTC

import pytest

from fakes import FakeSearchProvider
from tula_insights.budget import UsageLedger
from tula_insights.collectors.base import CollectorError, RunContext
from tula_insights.collectors.web_search import (
    WebSearchCollector,
    parse_page_age,
    submission_id,
)
from tula_insights.config import load_config
from tula_insights.search.provider import SearchError, SearchResponse, SearchResult


def result(url, title="A thread", position=0, recurrence=1, page_age=None, snippet=""):
    return SearchResult(
        url=url,
        title=title,
        position=position,
        recurrence=recurrence,
        page_age=page_age,
        snippet=snippet,
        snippet_source="citation" if snippet else "",
        query="a query",
    )


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def source(config):
    return next(s for s in config.sources.resolve() if s.name == "web_questions")


def collector():
    return WebSearchCollector("web_questions")


def make_ctx(config, provider, **kwargs):
    return RunContext(
        run_id="test-run",
        config=config,
        ledger=UsageLedger(config=config),
        search=provider,
        **kwargs,
    )


# --- helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "year"),
    [("April 30, 2025", 2025), ("Apr 5, 2026", 2026), ("2025-04-30", 2025), ("30 April 2025", 2025)],
)
def test_absolute_page_ages_parse(raw, year):
    parsed = parse_page_age(raw)
    assert parsed is not None and parsed.year == year


def test_an_unparseable_page_age_is_left_blank_rather_than_guessed():
    """A wrong date would poison the recency score; a blank one scores as unknown."""
    assert parse_page_age("sometime last winter") is None
    assert parse_page_age(None) is None


def test_relative_page_ages_parse():
    from datetime import datetime

    now = datetime(2026, 11, 25, tzinfo=UTC)
    assert parse_page_age("3 days ago", now=now).day == 22


def test_submission_id_is_the_thread_id():
    assert submission_id("https://www.reddit.com/r/dogs/comments/1abc2de/title/") == "1abc2de"
    assert submission_id("https://reddit.com/r/dogs/") is None


# --- collecting ----------------------------------------------------------------------


def test_results_become_items(config, source):
    provider = FakeSearchProvider([
        SearchResponse(
            results=[result("https://reddit.com/r/dogs/comments/1abc2de/one/", "Dog car sick")],
            search_count=1,
        )
    ])
    items = collector().collect(source, make_ctx(config, provider))
    assert len(items) == 1
    item = items[0]
    assert item.source == "web_questions"
    assert item.external_id == "1abc2de"
    assert item.title == "Dog car sick"
    assert item.run_id == "test-run"
    assert item.item_id


def test_engagement_records_the_weak_signals_and_names_them(config, source):
    provider = FakeSearchProvider([
        SearchResponse(
            results=[result("https://reddit.com/r/dogs/comments/a1/x/", position=3, recurrence=2,
                            page_age="May 1, 2026")],
            search_count=1,
        )
    ])
    item = collector().collect(source, make_ctx(config, provider))[0]
    assert item.engagement["result_position"] == 3
    assert item.engagement["query_recurrence"] == 2
    assert item.engagement["page_age"] == "May 1, 2026"
    assert "result_position" in item.engagement["basis"]
    assert "upvotes" not in item.engagement, "upvotes are not available through search"


def test_profile_urls_are_dropped_not_scrubbed(config, source):
    provider = FakeSearchProvider([
        SearchResponse(
            results=[
                result("https://reddit.com/user/someone"),
                result("https://reddit.com/r/dogs/comments/a1/keep/"),
            ],
            search_count=1,
        )
    ])
    items = collector().collect(source, make_ctx(config, provider))
    assert [i.url for i in items] == ["https://reddit.com/r/dogs/comments/a1/keep/"]


def test_titles_and_snippets_are_scrubbed(config, source):
    provider = FakeSearchProvider([
        SearchResponse(
            results=[result(
                "https://reddit.com/r/dogs/comments/a1/x/",
                title="u/someone asks about r/dogs travel",
                snippet="mail me at a@b.com",
            )],
            search_count=1,
        )
    ])
    item = collector().collect(source, make_ctx(config, provider))[0]
    assert item.title == "[user] asks about r/dogs travel"
    assert item.snippet == "mail me at [email]"


def test_items_already_in_the_sheet_are_skipped(config, source):
    url = "https://reddit.com/r/dogs/comments/a1/x/"
    provider = FakeSearchProvider([SearchResponse(results=[result(url)], search_count=1)])
    first = collector().collect(source, make_ctx(config, provider))[0]

    provider = FakeSearchProvider([SearchResponse(results=[result(url)], search_count=1)])
    ctx = make_ctx(config, provider, known_item_ids={first.item_id})
    assert collector().collect(source, ctx) == []


def test_a_failing_search_raises_a_collector_error_naming_the_source(config, source):
    provider = FakeSearchProvider(error=SearchError("web search is not enabled"))
    ctx = make_ctx(config, provider)
    with pytest.raises(CollectorError, match="web_questions"):
        collector().collect(source, ctx)
    assert ctx.warnings


def test_tool_errors_become_warnings_without_failing(config, source):
    provider = FakeSearchProvider([
        SearchResponse(results=[], error_codes=["too_many_requests"], search_count=0)
    ])
    ctx = make_ctx(config, provider)
    assert collector().collect(source, ctx) == []
    assert any("too_many_requests" in w for w in ctx.warnings)


# --- caps ----------------------------------------------------------------------------


def test_the_category_filter_narrows_the_plan(config, source):
    provider = FakeSearchProvider([SearchResponse() for _ in range(20)])
    ctx = make_ctx(config, provider, category="gear_packing")
    collector().collect(source, ctx)
    assert provider.requests, "a matching category should still be searched"
    searched = {p for request in provider.requests for p in request.phrases}
    assert "dog car harness safety" in searched
    assert "rabies titre test pet" not in searched


def test_an_unmatched_category_collects_nothing_and_says_so(config, source):
    provider = FakeSearchProvider()
    ctx = make_ctx(config, provider, category="no_such_category")
    assert collector().collect(source, ctx) == []
    assert provider.requests == []
    assert any("no keyword sets" in w for w in ctx.warnings)


def test_the_budget_cap_stops_searching_and_is_recorded(config, source):
    provider = FakeSearchProvider([SearchResponse(search_count=40) for _ in range(10)])
    ctx = make_ctx(config, provider)
    # The daily cap is 40 searches; one response burns all of it.
    collector().collect(source, ctx)
    assert len(provider.requests) == 1
    assert ctx.ledger.skipped
    assert "cap" in ctx.ledger.skipped[0]


def test_max_uses_is_clamped_to_the_remaining_allowance(config, source):
    provider = FakeSearchProvider([SearchResponse(search_count=39), SearchResponse()])
    # Raise the source's own cap out of the way so the budget cap is what bites.
    roomy = source.model_copy(update={"daily_request_cap": 100})
    ctx = make_ctx(config, provider)
    collector().collect(roomy, ctx)
    assert provider.requests[0].max_uses == 3
    assert provider.requests[1].max_uses == 1, "only one search of the daily cap was left"


def test_the_item_cap_is_honoured(config, source):
    many = [result(f"https://reddit.com/r/dogs/comments/x{i}/t/") for i in range(500)]
    provider = FakeSearchProvider([SearchResponse(results=many, search_count=1)])
    capped = source.model_copy(update={"max_items_per_run": 5})
    items = collector().collect(capped, make_ctx(config, provider))
    assert len(items) == 5


def test_the_source_request_cap_stops_the_run(config, source):
    provider = FakeSearchProvider([SearchResponse(search_count=3) for _ in range(10)])
    tight = source.model_copy(update={"daily_request_cap": 3})
    ctx = make_ctx(config, provider)
    collector().collect(tight, ctx)
    assert len(provider.requests) == 1
    assert any("daily_request_cap" in s for s in ctx.ledger.skipped)


def test_only_the_sources_regions_are_searched(config, source):
    provider = FakeSearchProvider([SearchResponse() for _ in range(20)])
    za_only = source.model_copy(update={"regions": ["za"]})
    collector().collect(za_only, make_ctx(config, provider))
    assert {r.region for r in provider.requests} == {"za"}


# --- measuring what comes back -------------------------------------------------------


def test_reddit_results_and_queries_are_counted(config, source):
    provider = FakeSearchProvider([
        SearchResponse(
            results=[
                result("https://www.reddit.com/r/dogs/comments/1abc2de/x/"),
                result("https://old.reddit.com/r/cats/comments/2bcd3ef/y/"),
                result("https://example.com/pet-travel"),
            ],
            queries=["dog car sick reddit", "dog car sick forum", "dog car sick"],
            search_count=3,
        )
    ])
    context = make_ctx(config, provider)
    collector().collect(source, context)

    assert context.metrics["web_questions.results_from_reddit"] == 2
    assert context.metrics["web_questions.queries_mentioning_reddit"] == 1
    assert context.metrics["web_questions.queries"] == 3
    assert context.metrics["web_questions.results"] == 3
    assert context.metrics["web_questions.distinct_sites"] == 3


def test_a_lookalike_domain_is_not_counted_as_reddit():
    from tula_insights.collectors.web_search import is_reddit

    assert is_reddit("https://www.reddit.com/r/x/")
    assert is_reddit("https://old.reddit.com/r/x/")
    assert not is_reddit("https://notreddit.com/r/x/")
    assert not is_reddit("https://reddit.com.evil.example/r/x/")


def test_the_query_hint_reaches_the_model(config, source):
    provider = FakeSearchProvider([SearchResponse() for _ in range(10)])
    collector().collect(source, make_ctx(config, provider))
    assert 'word "forum"' in provider.requests[0].intent


def test_no_hint_means_the_intent_is_unchanged(config, source):
    provider = FakeSearchProvider([SearchResponse() for _ in range(10)])
    plain = source.model_copy(update={"settings": {**source.settings, "query_hint": ""}})
    collector().collect(plain, make_ctx(config, provider))
    assert "reddit" not in provider.requests[0].intent.lower()


def test_metrics_carry_no_text():
    """The run summary is public: every metric must be a plain number."""
    from tula_insights.stages.collect import CollectResult, summary_lines

    outcome = CollectResult(run_id="r", metrics={"web_questions.results_from_reddit": 4})
    text = "\n".join(summary_lines(outcome))
    assert "web_questions.results_from_reddit" in text and "4" in text
