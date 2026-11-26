"""The news_rss collector: robots.txt, feed health, freshness and filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tula_insights.budget import UsageLedger
from tula_insights.collectors.base import CollectorError, RunContext
from tula_insights.collectors.news_rss import FeedSpec, NewsRssCollector, strip_html
from tula_insights.config import load_config


def rss(*entries):
    items = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        + (f"<pubDate>{date}</pubDate>" if date else "") + "</item>"
        for t, link, d, date in entries
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>{items}</channel></rss>'.encode()


def rfc822(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S GMT")


class Resp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeWeb:
    """url -> Resp, or a list of Resps served in turn. Unknown URLs are 404."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        page = self.pages.get(url, Resp(404))
        if isinstance(page, list):
            return page.pop(0) if len(page) > 1 else page[0]
        return page


@pytest.fixture
def config(config_dir):
    return load_config()


def source_with(config, feeds, **settings):
    base = next(s for s in config.sources.resolve() if s.name == "news_rss")
    return base.model_copy(update={"settings": {**base.settings, "feeds": feeds, **settings}})


def run(config, source, web, **ctx_kwargs):
    ctx = RunContext(run_id="r1", config=config, ledger=UsageLedger(config=config), **ctx_kwargs)
    items = NewsRssCollector(session=web, sleep=lambda _: None).collect(source, ctx)
    return items, ctx


FEED = {"id": "pets_za", "url": "https://pets.example/feed", "region": "za"}
FRESH = ("Can I fly with my cat?", "https://pets.example/a", "&lt;p&gt;Tips for &lt;b&gt;flying&lt;/b&gt;&lt;/p&gt;", rfc822(1))


# --- reading -------------------------------------------------------------------------


def test_fresh_entries_become_items(config):
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH))})
    items, ctx = run(config, source_with(config, [FEED]), web)
    assert len(items) == 1
    item = items[0]
    assert item.source == "news_rss" and item.region == "za"
    assert item.title == "Can I fly with my cat?"
    assert item.snippet == "Tips for flying", "HTML is stripped"
    assert item.query == "rss:pets_za" and item.published_at is not None
    assert ctx.metrics["news_rss.feeds_ok"] == 1


def test_a_generic_user_agent_is_sent(config):
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH))})
    run(config, source_with(config, [FEED]), web)
    assert all(h["User-Agent"] == "rss-collector/0.1 (automated feed reader)" for _, h in web.calls)


def test_old_and_undated_entries_are_skipped(config):
    old = ("Old news", "https://pets.example/old", "x", rfc822(40))
    undated = ("No date", "https://pets.example/nodate", "x", None)
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH, old, undated))})
    items, ctx = run(config, source_with(config, [FEED]), web)
    assert [i.url for i in items] == ["https://pets.example/a"]
    assert ctx.metrics["news_rss.entries_too_old_or_undated"] == 2


def test_a_general_feed_keeps_only_pet_entries(config):
    feed = {**FEED, "general": True}
    off = ("Interest rates rise", "https://pets.example/rates", "economy", rfc822(1))
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH, off))})
    items, ctx = run(config, source_with(config, [feed]), web)
    assert [i.url for i in items] == ["https://pets.example/a"]
    assert ctx.metrics["news_rss.entries_off_topic"] == 1


def test_personal_details_are_scrubbed(config):
    entry = ("Email vet@example.com about crates", "https://pets.example/b", "call 082 555 1234", rfc822(1))
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(entry))})
    items, _ = run(config, source_with(config, [FEED]), web)
    assert "[email]" in items[0].title and "[phone]" in items[0].snippet


def test_items_already_collected_are_skipped(config):
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH))})
    first, _ = run(config, source_with(config, [FEED]), web)
    again, _ = run(config, source_with(config, [FEED]),
                   FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH))}),
                   known_item_ids={first[0].item_id})
    assert again == []


def test_the_per_feed_cap(config):
    entries = [(f"Q{n}?", f"https://pets.example/{n}", "x", rfc822(1)) for n in range(10)]
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(*entries))})
    items, _ = run(config, source_with(config, [FEED], max_items_per_feed=3), web)
    assert len(items) == 3


# --- robots.txt ----------------------------------------------------------------------


def test_a_feed_disallowed_by_robots_is_skipped_and_reported(config):
    other = {"id": "ok_feed", "url": "https://ok.example/feed"}
    web = FakeWeb({
        "https://pets.example/robots.txt": Resp(200, b"User-agent: *\nDisallow: /feed\n"),
        "https://pets.example/feed": Resp(200, rss(FRESH)),
        "https://ok.example/feed": Resp(200, rss(("Q?", "https://ok.example/q", "x", rfc822(1)))),
    })
    items, ctx = run(config, source_with(config, [FEED, other]), web)
    assert [i.url for i in items] == ["https://ok.example/q"]
    assert not any(url == "https://pets.example/feed" for url, _ in web.calls), "never fetched"
    assert any("pets_za" in w and "robots" in w for w in ctx.warnings)


def test_no_robots_file_means_allowed(config):
    web = FakeWeb({"https://pets.example/robots.txt": Resp(404),
                   "https://pets.example/feed": Resp(200, rss(FRESH))})
    items, _ = run(config, source_with(config, [FEED]), web)
    assert len(items) == 1


def test_a_forbidden_robots_file_means_not_allowed(config):
    web = FakeWeb({"https://pets.example/robots.txt": Resp(403),
                   "https://pets.example/feed": Resp(200, rss(FRESH))})
    with pytest.raises(CollectorError, match="robots"):
        run(config, source_with(config, [FEED]), web)


def test_robots_is_read_once_per_site(config):
    second = {"id": "same_site", "url": "https://pets.example/other-feed"}
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH)),
                   "https://pets.example/other-feed": Resp(200, rss(FRESH))})
    run(config, source_with(config, [FEED, second]), web)
    assert sum(1 for url, _ in web.calls if url.endswith("robots.txt")) == 1


# --- feed health ---------------------------------------------------------------------


def test_one_broken_feed_does_not_stop_the_others(config):
    broken = {"id": "gone", "url": "https://gone.example/feed"}
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH)),
                   "https://gone.example/feed": Resp(404)})
    items, ctx = run(config, source_with(config, [broken, FEED]), web)
    assert len(items) == 1
    assert any("gone" in w and "HTTP 404" in w for w in ctx.warnings)
    assert ctx.metrics["news_rss.feed.gone.http_404"] == 1
    assert ctx.metrics["news_rss.feeds_failed"] == 1


def test_an_html_page_is_reported_as_not_a_feed(config):
    web = FakeWeb({"https://pets.example/feed": Resp(200, b"<html><body>Shop</body></html>")})
    with pytest.raises(CollectorError, match="not a feed"):
        run(config, source_with(config, [FEED]), web)


def test_every_feed_failing_fails_the_source_naming_each(config):
    two = [FEED, {"id": "also_gone", "url": "https://x.example/feed"}]
    with pytest.raises(CollectorError) as caught:
        run(config, source_with(config, two), FakeWeb({}))
    assert "pets_za" in str(caught.value) and "also_gone" in str(caught.value)


def test_a_transient_error_is_retried(config):
    web = FakeWeb({"https://pets.example/feed": [Resp(503), Resp(200, rss(FRESH))]})
    items, _ = run(config, source_with(config, [FEED]), web)
    assert len(items) == 1


def test_the_daily_request_cap_stops_fetching(config):
    many = [{"id": f"f{n}", "url": f"https://s{n}.example/feed"} for n in range(5)]
    pages = {f"https://s{n}.example/feed": Resp(200, rss((f"Q{n}?", f"https://s{n}.example/q", "x", rfc822(1))))
             for n in range(5)}
    source = source_with(config, many).model_copy(update={"daily_request_cap": 4})
    items, ctx = run(config, source, FakeWeb(pages))
    # Each feed costs two requests (robots.txt, then the feed): the cap allows two feeds.
    assert len(items) == 2
    assert any("daily request cap" in w for w in ctx.warnings)


def test_disabled_feeds_are_not_read(config):
    web = FakeWeb({"https://pets.example/feed": Resp(200, rss(FRESH))})
    with pytest.raises(CollectorError, match="no enabled feeds"):
        run(config, source_with(config, [{**FEED, "enabled": False}]), web)


# --- config --------------------------------------------------------------------------


def test_every_configured_feed_is_valid(config):
    news = next(s for s in config.sources.resolve() if s.name == "news_rss")
    specs = [FeedSpec.model_validate(f) for f in news.settings["feeds"]]
    assert len(specs) >= 5
    assert len({s.id for s in specs}) == len(specs), "feed ids must be unique"
    assert all(s.region in config.keywords.regions for s in specs)


def test_strip_html():
    assert strip_html("<p>A &amp; <b>B</b></p>") == "A & B"
    assert strip_html(None) == ""
