"""The YouTube collector: what it reads, what it never stores, and how it fails."""

from __future__ import annotations

import pytest
import requests

from tula_insights.budget import UsageLedger
from tula_insights.collectors.base import CollectorError, RunContext
from tula_insights.collectors.youtube import (
    CommentsUnavailable,
    QuotaRefused,
    YouTubeClient,
    YouTubeCollector,
    YouTubeError,
    looks_like_question,
)
from tula_insights.config import load_config

KEY = "AIza-test-key-not-real"  # pragma: allowlist secret


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def search_body(*video_ids):
    return {"items": [{"id": {"videoId": v}, "snippet": {"title": f"Video {v}",
                       "publishedAt": "2026-01-01T00:00:00Z", "channelTitle": "Some Creator"}}
                      for v in video_ids]}


def stats_body(*video_ids):
    return {"items": [{"id": v, "statistics": {"viewCount": "1000", "likeCount": "50",
                       "commentCount": "12"}} for v in video_ids]}


def thread(tid, text, likes=3, replies=1):
    return {
        "id": tid,
        "snippet": {
            "totalReplyCount": replies,
            "topLevelComment": {
                "id": f"c-{tid}",
                "snippet": {
                    "textOriginal": text,
                    "textDisplay": f"<b>{text}</b>",
                    "likeCount": likes,
                    "publishedAt": "2026-11-01T10:00:00Z",
                    "authorDisplayName": "Jane Smith",
                    "authorChannelId": {"value": "UC_jane"},
                    "authorChannelUrl": "http://www.youtube.com/@janesmith",
                    "authorProfileImageUrl": "https://yt3.example/jane.jpg",
                },
            },
        },
    }


class FakeSession:
    """Routes by API resource. `comments` maps video id -> body or a status code."""

    def __init__(self, *, search=None, comments=None, search_status=200):
        self.search = search or search_body()
        self.comments = comments or {}
        self.search_status = search_status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
        resource = url.rsplit("/", 1)[-1]
        if resource == "search":
            if self.search_status != 200:
                return Resp(self.search_status, {"error": {"errors": [{"reason": "quotaExceeded"}]}})
            return Resp(200, self.search)
        if resource == "videos":
            return Resp(200, stats_body(*params["id"].split(",")))
        if resource == "commentThreads":
            body = self.comments.get(params["videoId"], {"items": []})
            if isinstance(body, int):
                return Resp(body, {"error": {"errors": [{"reason": "commentsDisabled"}]}})
            return Resp(200, body)
        raise AssertionError(f"unexpected resource {resource}")


@pytest.fixture
def config(config_dir, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    return load_config()


@pytest.fixture
def source(config):
    return next(s for s in config.sources.resolve() if s.name == "youtube")


def run(config, source, session, **ctx_kwargs):
    collector = YouTubeCollector(
        client_factory=lambda key, cfg: YouTubeClient(key, session=session, max_retries=0)
    )
    ctx = RunContext(run_id="r1", config=config, ledger=UsageLedger(config=config), **ctx_kwargs)
    return collector.collect(source, ctx), ctx


# --- the filter ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("How long before the flight should I stop feeding my dog?", True),
        ("Any advice for a cat that cries the whole drive", True),
        ("My dog gets so nervous in the car, what helps", True),
        ("Great video!", False),
        ("love this so much", False),
        ("ok?", False),  # a question mark, but too short to carry a question
    ],
)
def test_the_question_filter(text, kept):
    from tula_insights.collectors.youtube import DEFAULT_QUESTION_CUES

    assert looks_like_question(text, min_chars=25, cues=DEFAULT_QUESTION_CUES) is kept


# --- collecting ----------------------------------------------------------------------


def test_question_comments_become_items_with_real_engagement(config, source):
    session = FakeSession(
        search=search_body("vid1"),
        comments={"vid1": {"items": [
            thread("t1", "Can I take my dog on a flight to Durban in the cabin?", likes=9, replies=4),
            thread("t2", "Great video!"),
        ]}},
    )
    items, _ = run(config, source, session)
    assert len(items) == 1
    item = items[0]
    assert item.source == "youtube"
    assert item.external_id == "t1"
    assert item.url == "https://www.youtube.com/watch?v=vid1&lc=c-t1"
    assert item.snippet.startswith("Can I take my dog")
    assert item.engagement["comment_likes"] == 9
    assert item.engagement["comment_replies"] == 4
    assert item.engagement["video_views"] == 1000
    assert item.published_at is not None


def test_no_author_or_channel_detail_is_ever_stored(config, source):
    session = FakeSession(
        search=search_body("vid1"),
        comments={"vid1": {"items": [thread("t1", "How do I calm my cat for a long car trip?")]}},
    )
    items, _ = run(config, source, session)
    flat = str(items[0].model_dump())
    for leaked in ("Jane Smith", "UC_jane", "janesmith", "jane.jpg", "Some Creator"):
        assert leaked not in flat


def test_plain_text_is_requested_and_used(config, source):
    session = FakeSession(
        search=search_body("vid1"),
        comments={"vid1": {"items": [thread("t1", "Is it safe to sedate a dog for flying?")]}},
    )
    items, _ = run(config, source, session)
    comment_call = next(p for r, p in session.calls if r == "commentThreads")
    assert comment_call["textFormat"] == "plainText"
    assert "<b>" not in items[0].snippet


def test_comment_text_is_scrubbed(config, source):
    session = FakeSession(
        search=search_body("vid1"),
        comments={"vid1": {"items": [thread("t1", "Help! email me at owner@example.com about crates?")]}},
    )
    items, _ = run(config, source, session)
    assert "[email]" in items[0].snippet


def test_a_video_with_comments_off_is_skipped_not_fatal(config, source):
    session = FakeSession(
        search=search_body("off", "on"),
        comments={"off": 403, "on": {"items": [thread("t1", "Should I crate train before travel?")]}},
    )
    items, ctx = run(config, source, session)
    assert len(items) >= 1
    assert ctx.metrics["youtube.videos_comments_unavailable"] >= 1


def test_a_quota_refusal_stops_the_source_quietly(config, source):
    session = FakeSession(search_status=403)
    items, ctx = run(config, source, session)
    assert items == []
    assert any("refused" in s for s in ctx.ledger.skipped)


def test_quota_units_and_searches_are_counted(config, source):
    session = FakeSession(
        search=search_body("vid1", "vid2"),
        comments={"vid1": {"items": []}, "vid2": {"items": []}},
    )
    _, ctx = run(config, source, session)
    searches = ctx.metrics["youtube.searches"]
    # Per search: 1 search + 1 videos.list (2 ids) + 2 commentThreads.
    assert ctx.metrics["youtube.units"] == searches * 4
    assert ctx.ledger.youtube_searches_this_run == searches


def test_the_daily_search_cap_is_honoured(config, source):
    capped = source.model_copy(update={"settings": {**source.settings, "daily_search_cap": 2}})
    session = FakeSession(search=search_body())
    _, ctx = run(config, capped, session)
    assert sum(1 for r, _ in session.calls if r == "search") == 2
    assert any("daily_search_cap" in s for s in ctx.ledger.skipped)


def test_the_za_region_is_localised(config, source):
    za = source.model_copy(update={"regions": ["za"]})
    session = FakeSession()
    run(config, za, session)
    search = next(p for r, p in session.calls if r == "search")
    assert search["regionCode"] == "ZA"
    assert search["q"].endswith("South Africa")
    assert search["type"] == "video"


def test_the_item_cap_is_honoured(config, source):
    many = {"items": [thread(f"t{i}", f"Question number {i} about flying with my dog?") for i in range(80)]}
    session = FakeSession(search=search_body("vid1"), comments={"vid1": many})
    capped = source.model_copy(update={"max_items_per_run": 5})
    items, _ = run(config, capped, session)
    assert len(items) == 5


def test_a_missing_key_is_a_clear_error(config_dir, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    config = load_config()
    source = next(s for s in config.sources.resolve() if s.name == "youtube")
    ctx = RunContext(run_id="r", config=config, ledger=UsageLedger(config=config))
    with pytest.raises(CollectorError, match="YOUTUBE_API_KEY"):
        YouTubeCollector().collect(source, ctx)


# --- the key must never leak ---------------------------------------------------------


def test_a_network_error_message_never_contains_the_key():
    class Exploding:
        def get(self, url, params=None, timeout=None):
            raise requests.ConnectionError(f"failed for {url}?key={params['key']}")

    client = YouTubeClient(KEY, session=Exploding(), max_retries=0)
    with pytest.raises(YouTubeError) as caught:
        client.search_videos("dog", max_results=1, region_code=None)
    assert KEY not in str(caught.value)
    assert "googleapis" not in str(caught.value)


def test_an_http_error_message_never_contains_the_key():
    class Echoing:
        def get(self, url, params=None, timeout=None):
            return Resp(400, {"error": {"errors": [{"reason": f"bad key={params['key']}"}]}})

    client = YouTubeClient(KEY, session=Echoing(), max_retries=0)
    with pytest.raises(YouTubeError) as caught:
        client.search_videos("dog", max_results=1, region_code=None)
    assert KEY not in str(caught.value)


def test_comment_errors_and_quota_errors_are_told_apart():
    class Status:
        def __init__(self, code):
            self.code = code

        def get(self, url, params=None, timeout=None):
            return Resp(self.code, {"error": {"errors": [{"reason": "x"}]}})

    with pytest.raises(CommentsUnavailable):
        YouTubeClient(KEY, session=Status(403), max_retries=0).top_comments(
            "v", max_results=5, order="relevance"
        )
    with pytest.raises(QuotaRefused):
        YouTubeClient(KEY, session=Status(403), max_retries=0).search_videos(
            "dog", max_results=1, region_code=None
        )
