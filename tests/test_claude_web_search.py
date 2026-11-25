"""The search provider must take its results from the tool, never from the model."""

from __future__ import annotations

import pytest

from fakes import (
    FakeLlm,
    citation,
    message,
    search_result,
    text,
    tool_error,
    tool_result,
    tool_use,
)
from tula_insights.search.claude_web_search import TOOL_TYPE, ClaudeWebSearchProvider
from tula_insights.search.provider import SearchError, SearchRequest


def provider(responses):
    return ClaudeWebSearchProvider(FakeLlm(responses))


def basic_request(**kwargs):
    defaults = {"phrases": ["dog car sick"], "allowed_domains": ["reddit.com"], "max_uses": 3}
    return SearchRequest(**{**defaults, **kwargs})


# --- harvesting ----------------------------------------------------------------------


def test_results_come_from_the_tool_blocks():
    response = message([
        tool_use("t1", "dog car sick reddit"),
        tool_result("t1", [
            search_result("https://reddit.com/r/dogs/comments/a1/one/", "Dog car sick", "May 1, 2026"),
            search_result("https://reddit.com/r/dogs/comments/b2/two/", "Travel nausea"),
        ]),
        text("I ran 1 search."),
    ])
    out = provider([response]).search(basic_request())

    assert [r.url for r in out.results] == [
        "https://reddit.com/r/dogs/comments/a1/one/",
        "https://reddit.com/r/dogs/comments/b2/two/",
    ]
    assert out.results[0].title == "Dog car sick"
    assert out.results[0].page_age == "May 1, 2026"
    assert out.results[0].query == "dog car sick reddit"
    assert out.queries == ["dog car sick reddit"]


def test_a_url_the_model_merely_writes_is_never_collected():
    """The model's prose is not a source of URLs. This is the anti-hallucination rule."""
    response = message([
        tool_use("t1", "q"),
        tool_result("t1", [search_result("https://reddit.com/r/dogs/comments/real/x/")]),
        text("I also recommend https://reddit.com/r/dogs/comments/invented/y/ which is great."),
    ])
    urls = [r.url for r in provider([response]).search(basic_request()).results]
    assert urls == ["https://reddit.com/r/dogs/comments/real/x/"]


def test_snippets_come_from_citations_and_are_labelled():
    url = "https://reddit.com/r/dogs/comments/a1/one/"
    response = message([
        tool_use("t1", "q"),
        tool_result("t1", [search_result(url, "A thread")]),
        text("Owners report", citations=[citation(url, "my dog drools the whole way")]),
    ])
    result = provider([response]).search(basic_request()).results[0]
    assert result.snippet == "my dog drools the whole way"
    assert result.snippet_source == "citation"


def test_an_uncited_result_simply_has_no_snippet():
    """A search result carries no snippet field, so no citation means no excerpt."""
    response = message([
        tool_use("t1", "q"),
        tool_result("t1", [search_result("https://reddit.com/r/dogs/comments/a1/x/")]),
        text("done"),
    ])
    result = provider([response]).search(basic_request()).results[0]
    assert result.snippet == ""
    assert result.snippet_source == ""


def test_the_same_thread_from_two_queries_is_one_result_with_recurrence():
    url = "https://reddit.com/r/dogs/comments/a1/one/"
    response = message([
        tool_use("t1", "first query"),
        tool_result("t1", [search_result("https://reddit.com/other/", "Other"), search_result(url)]),
        tool_use("t2", "second query"),
        tool_result("t2", [search_result(url)]),
        text("done"),
    ], searches=2)
    out = provider([response]).search(basic_request())

    repeated = next(r for r in out.results if r.url == url)
    assert repeated.recurrence == 2
    assert repeated.position == 0, "the best position across queries is kept"
    assert out.results[0].url == url, "recurrence ranks above position"


def test_results_are_ranked_by_recurrence_then_position():
    once = "https://reddit.com/r/dogs/comments/c3/once/"
    twice = "https://reddit.com/r/dogs/comments/d4/twice/"
    response = message([
        tool_use("t1", "a"),
        tool_result("t1", [search_result(once), search_result(twice)]),
        tool_use("t2", "b"),
        tool_result("t2", [search_result(twice)]),
        text("done"),
    ], searches=2)
    assert [r.url for r in provider([response]).search(basic_request()).results] == [twice, once]


# --- counting ------------------------------------------------------------------------


def test_search_count_comes_from_usage_not_from_query_count():
    """Billing counts searches; a failed search is not billed. Only usage knows."""
    response = message([
        tool_use("t1", "a"),
        tool_result("t1", [search_result("https://reddit.com/x/")]),
        text("done"),
    ], searches=3)
    out = provider([response]).search(basic_request())
    assert out.search_count == 3
    assert len(out.queries) == 1


def test_tokens_are_reported():
    response = message([text("done")], tokens_in=1234, tokens_out=56, searches=0)
    out = provider([response]).search(basic_request())
    assert out.tokens_in == 1234
    assert out.tokens_out == 56


# --- errors --------------------------------------------------------------------------


def test_a_tool_error_is_recorded_not_raised():
    """Web search errors arrive inside a 200 response, with content as an object."""
    response = message([
        tool_use("t1", "a"),
        tool_error("t1", "max_uses_exceeded"),
        text("done"),
    ])
    out = provider([response]).search(basic_request())
    assert out.error_codes == ["max_uses_exceeded"]
    assert out.results == []
    assert out.ok is False


def test_a_partial_failure_keeps_the_results_that_worked():
    response = message([
        tool_use("t1", "a"),
        tool_result("t1", [search_result("https://reddit.com/r/dogs/comments/a1/x/")]),
        tool_use("t2", "b"),
        tool_error("t2", "too_many_requests"),
        text("done"),
    ], searches=2)
    out = provider([response]).search(basic_request())
    assert len(out.results) == 1
    assert out.error_codes == ["too_many_requests"]


def test_an_empty_result_list_is_not_an_error():
    response = message([tool_use("t1", "a"), tool_result("t1", []), text("done")])
    out = provider([response]).search(basic_request())
    assert out.results == []
    assert out.error_codes == []
    assert out.ok is True


# --- pause_turn ----------------------------------------------------------------------


def test_a_paused_turn_is_resumed_and_results_are_merged():
    first = message([
        tool_use("t1", "a"),
        tool_result("t1", [search_result("https://reddit.com/r/dogs/comments/a1/one/")]),
    ], stop_reason="pause_turn", searches=1)
    second = message([
        tool_use("t2", "b"),
        tool_result("t2", [search_result("https://reddit.com/r/dogs/comments/b2/two/")]),
        text("done"),
    ], searches=1)

    llm = FakeLlm([first, second])
    out = ClaudeWebSearchProvider(llm).search(basic_request())

    assert len(out.results) == 2
    assert out.search_count == 2
    assert out.stop_reason == "end_turn"
    assert len(llm.requests) == 2

    # The paused assistant message must go back untouched: each result carries an
    # encrypted_content blob the API refuses to accept if it has been altered.
    resumed = llm.requests[1]["messages"]
    assert resumed[-1]["role"] == "assistant"
    assert resumed[-1]["content"] is first.content


def test_endless_pausing_gives_up_without_losing_results():
    from tula_insights.search.claude_web_search import MAX_RESUMES

    paused = [
        message(
            [tool_use(f"t{i}", "q"), tool_result(f"t{i}", [search_result(f"https://reddit.com/{i}/")])],
            stop_reason="pause_turn",
        )
        for i in range(MAX_RESUMES + 1)
    ]
    out = ClaudeWebSearchProvider(FakeLlm(paused)).search(basic_request())
    assert len(out.results) == MAX_RESUMES + 1
    assert out.stop_reason == "pause_turn"


# --- request shaping -----------------------------------------------------------------


def test_the_tool_is_the_basic_version_with_the_configured_limits():
    llm = FakeLlm([message([text("done")], searches=0)])
    ClaudeWebSearchProvider(llm).search(basic_request(max_uses=2))
    tool = llm.requests[0]["tools"][0]
    assert tool["type"] == TOOL_TYPE == "web_search_20250305"
    assert tool["max_uses"] == 2
    assert tool["allowed_domains"] == ["reddit.com"]
    assert "blocked_domains" not in tool


def test_blocked_domains_are_used_when_there_is_no_allow_list():
    llm = FakeLlm([message([text("done")], searches=0)])
    ClaudeWebSearchProvider(llm).search(
        SearchRequest(phrases=["x"], blocked_domains=["pinterest.com"])
    )
    tool = llm.requests[0]["tools"][0]
    assert tool["blocked_domains"] == ["pinterest.com"]
    assert "allowed_domains" not in tool


def test_sending_both_domain_lists_is_refused_before_the_api_sees_it():
    """The API returns 400 for both; fail with a message that names the config file."""
    with pytest.raises(SearchError, match=r"config/sources\.yaml"):
        provider([]).search(
            SearchRequest(phrases=["x"], allowed_domains=["a.com"], blocked_domains=["b.com"])
        )


def test_the_country_hint_is_passed_through():
    llm = FakeLlm([message([text("done")], searches=0)])
    ClaudeWebSearchProvider(llm).search(basic_request(country="ZA"))
    assert llm.requests[0]["tools"][0]["user_location"] == {
        "type": "approximate",
        "country": "ZA",
    }


def test_the_phrases_reach_the_model():
    llm = FakeLlm([message([text("done")], searches=0)])
    ClaudeWebSearchProvider(llm).search(basic_request(phrases=["alpha", "beta"], region="za"))
    content = llm.requests[0]["messages"][0]["content"]
    assert "alpha" in content and "beta" in content
    assert "za" in content


def test_no_phrases_means_no_api_call():
    llm = FakeLlm([])
    out = ClaudeWebSearchProvider(llm).search(SearchRequest(phrases=[]))
    assert out.results == []
    assert llm.requests == []
