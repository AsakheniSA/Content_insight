"""Curated RSS and Atom feeds: pet, veterinary and pet-travel outlets.

Headline, a summary snippet, link and date only — the article itself is never fetched.

**Every feed is checked before it is read.** The site's `robots.txt` is consulted
first (§6.1 of the brief); a feed the site disallows for automated readers is skipped
and reported. Each feed's outcome — read, HTTP error, not actually a feed, blocked by
robots.txt, network failure — is reported by feed id in the run summary. Feed ids are
config, not collected content, so they are safe in the public logs, and they tell you
which feeds to drop from sources.yaml.

**What is kept.** Only dated entries newer than `max_age_days` (an undated entry can't
be judged for freshness), and — for feeds marked `general` because they cover more than
pets — only entries mentioning one of `require_terms`. The classifier makes the real
call on relevance; these filters keep obvious noise from costing a classification.

**Engagement.** Feeds carry no engagement signal, so these items score the neutral 0.5
on engagement and compete on recurrence, recency and trend.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..config import ResolvedSource
from ..models import RawItem
from ..utils.clock import now_utc
from ..utils.logging import get_logger
from ..utils.pii import is_person_url, scrub
from ..utils.ratelimit import RateLimiter
from ..utils.retry import call_with_retry
from .base import Collector, CollectorError, RunContext

log = get_logger(__name__)

DEFAULT_USER_AGENT = "rss-collector/0.1 (automated feed reader)"
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"
#: Rate limits, server errors, and Cloudflare's 520-524 "origin unreachable" family,
#: which is often temporary.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
_TAG = re.compile(r"<[^>]+>")

#: Outcomes, as reported per feed.
OK = "ok"
ROBOTS = "blocked by robots.txt"
NOT_A_FEED = "not a feed"
NETWORK = "network error"


class FeedSpec(BaseModel):
    """One feed, as configured in sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    region: str = "intl"
    enabled: bool = True
    #: Covers more than pets: keep only entries mentioning a require_term.
    general: bool = False
    require_terms: list[str] = []
    #: Where the address came from, for whoever maintains the list.
    found_via: str = ""
    note: str = ""

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("feed url must start with https:// or http://")
        return value


class FetchError(RuntimeError):
    def __init__(self, outcome: str, *, status: int | None = None, response: Any = None) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.status = status
        self.response = response  # lets call_with_retry honour Retry-After


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def entry_date(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key) if hasattr(entry, "get") else None
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


class NewsRssCollector:
    name = "news_rss"

    def __init__(self, session: Any | None = None, sleep=time.sleep) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self._robots: dict[str, RobotFileParser | None] = {}

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        settings = cfg.settings or {}
        feeds = self._feeds(settings)
        if not feeds:
            raise CollectorError(
                f"{self.name}: no enabled feeds in sources.yaml. Add feeds under "
                f"settings.feeds, or set the source to enabled: false."
            )
        self.user_agent = str(settings.get("user_agent") or DEFAULT_USER_AGENT)
        max_age = timedelta(days=int(settings.get("max_age_days", 14)))
        per_feed = int(settings.get("max_items_per_feed", 25))
        default_terms = [t.lower() for t in settings.get("default_require_terms") or []]
        limiter = RateLimiter(cfg.requests_per_minute)
        self._requests_left = cfg.daily_request_cap
        cutoff = now_utc() - max_age

        items: list[RawItem] = []
        seen: set[str] = set()
        outcomes: dict[str, str] = {}

        for feed in feeds:
            if len(items) >= cfg.max_items_per_run:
                break
            outcome, entries = self._read(feed, cfg, limiter)
            outcomes[feed.id] = outcome
            ctx.count(f"news_rss.feed.{feed.id}.{_slug(outcome)}")
            if outcome != OK:
                ctx.warn(f"{self.name}: feed {feed.id} — {outcome}")
                continue
            terms = [t.lower() for t in feed.require_terms]
            if not terms and feed.general:
                terms = default_terms
            kept = 0
            for entry in entries:
                if kept >= per_feed or len(items) >= cfg.max_items_per_run:
                    break
                ctx.count("news_rss.entries_read")
                item = self._to_item(entry, feed, ctx, cutoff=cutoff, terms=terms)
                if item is None:
                    continue
                if item.item_id in seen or item.item_id in ctx.known_item_ids:
                    continue
                seen.add(item.item_id)
                items.append(item)
                kept += 1

        working = sum(1 for o in outcomes.values() if o == OK)
        ctx.count("news_rss.feeds_ok", working)
        ctx.count("news_rss.feeds_failed", len(outcomes) - working)
        if outcomes and working == 0:
            raise CollectorError(
                f"{self.name}: none of the {len(outcomes)} feeds could be read: "
                + "; ".join(f"{fid} ({o})" for fid, o in outcomes.items())
            )
        log.info("collect.done", extra={"source": self.name, "feeds": len(outcomes),
                                        "feeds_ok": working, "items": len(items)})
        return items

    # --- feeds -----------------------------------------------------------------------

    @staticmethod
    def _feeds(settings: dict[str, Any]) -> list[FeedSpec]:
        specs = []
        for raw in settings.get("feeds") or []:
            try:
                spec = FeedSpec.model_validate(raw)
            except ValidationError as error:
                message = f"news_rss: a feed in sources.yaml is invalid: {error}"
                raise CollectorError(message) from None
            if spec.enabled:
                specs.append(spec)
        return specs

    def _read(self, feed: FeedSpec, cfg: ResolvedSource, limiter: RateLimiter) -> tuple[str, list]:
        try:
            if not self._allowed(feed.url, cfg, limiter):
                return ROBOTS, []
            body = self._get(feed.url, cfg, limiter, what=f"feed {feed.id}")
        except FetchError as error:
            return error.outcome, []
        parsed = feedparser.parse(body)
        if not parsed.version and not parsed.entries:
            return NOT_A_FEED, []
        return OK, list(parsed.entries)

    def _allowed(self, url: str, cfg: ResolvedSource, limiter: RateLimiter) -> bool:
        """Honour robots.txt. No robots.txt (404) means allowed; 401/403 means not."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            try:
                text = self._get(f"{origin}/robots.txt", cfg, limiter, what="robots.txt",
                                 accept="text/plain")
                parser.parse(text.decode("utf-8", errors="replace").splitlines())
                self._robots[origin] = parser
            except FetchError as error:
                if error.status in {401, 403}:
                    parser.disallow_all = True
                    self._robots[origin] = parser
                elif error.status is not None and 400 <= error.status < 500:
                    self._robots[origin] = None  # no robots.txt: nothing is disallowed
                else:
                    raise  # couldn't tell: treat the feed as unreadable this run
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _get(self, url: str, cfg: ResolvedSource, limiter: RateLimiter, *, what: str,
             accept: str = FEED_ACCEPT) -> bytes:
        if self._requests_left <= 0:
            raise FetchError("daily request cap reached")
        self._requests_left -= 1

        def send() -> bytes:
            limiter.acquire()
            try:
                response = self.session.get(
                    url, headers={"User-Agent": self.user_agent, "Accept": accept},
                    timeout=cfg.timeout_seconds,
                )
            except requests.RequestException:
                raise FetchError(NETWORK) from None
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}", status=response.status_code,
                                 response=response)
            return response.content

        return call_with_retry(
            send, what=what, attempts=cfg.max_retries + 1,
            base_seconds=cfg.backoff_base_seconds, max_seconds=cfg.backoff_max_seconds,
            retry_on=(FetchError,),
            should_retry=lambda e: isinstance(e, FetchError)
            and (e.outcome == NETWORK or e.status in RETRYABLE_STATUS),
            sleep=self.sleep,
        )

    # --- conversion ------------------------------------------------------------------

    def _to_item(self, entry: Any, feed: FeedSpec, ctx: RunContext, *, cutoff: datetime,
                 terms: list[str]) -> RawItem | None:
        link = (entry.get("link") or "").strip()
        if not link or is_person_url(link):
            return None
        published = entry_date(entry)
        if published is None or published < cutoff:
            ctx.count("news_rss.entries_too_old_or_undated")
            return None
        title = strip_html(entry.get("title"))
        summary = strip_html(entry.get("summary") or entry.get("description"))
        if terms:
            haystack = f"{title} {summary}".lower()
            if not any(term in haystack for term in terms):
                ctx.count("news_rss.entries_off_topic")
                return None
        return RawItem(
            source=self.name,
            external_id=None,
            url=link,
            title=scrub(title),
            snippet=scrub(summary),
            published_at=published,
            query=f"rss:{feed.id}",
            region=feed.region,
            engagement={"basis": [], "feed": feed.id},
            run_id=ctx.run_id,
        ).with_id()


def _slug(outcome: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", outcome.lower()).strip("_")


_: Collector = NewsRssCollector()
