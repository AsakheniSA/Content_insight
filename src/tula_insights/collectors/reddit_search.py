"""Reddit threads, found through the Claude web search tool.

**Why this is a search collector and not a Reddit client.** Reddit's self-service Data
API closed in November 2025 and unauthenticated endpoints now refuse requests, so this
collector never contacts Reddit at all. It reads the search index, through the one
search provider the project pays for, restricted to `reddit.com`. No Reddit page is
fetched or scraped.

**What that costs us.** There are no upvote or comment counts, because the search tool
does not return them and the only way to get them would be to fetch the thread. The
engagement signals here are therefore weak and the scoring stage must treat them as
such:

* `result_position` — where the search engine ranked it. A proxy for relevance.
* `query_recurrence` — how many of this run's queries surfaced the same thread. The
  stronger of the two: a thread that several different phrasings all find is a thread
  about something people keep asking.

Coverage is whatever the search index holds, which is not all of Reddit. This is a
documented limitation, not a bug.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..config import KeywordSet, ResolvedSource
from ..models import RawItem
from ..search.provider import SearchError, SearchRequest, SearchResponse, SearchResult
from ..utils.logging import get_logger
from ..utils.pii import is_person_url, scrub
from .base import Collector, CollectorError, RunContext

log = get_logger(__name__)

#: `/r/<sub>/comments/<id>/<slug>` — the id is the stable identity of a thread.
_SUBMISSION_RE = re.compile(r"/comments/([a-z0-9]{4,12})(?:/|$)", re.IGNORECASE)

#: Page-age formats seen from the search tool. Anything else is kept as raw text and
#: `published_at` is left blank rather than guessed at.
_PAGE_AGE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")

_RELATIVE_RE = re.compile(r"(\d+)\s+(day|week|month|year|hour|minute)s?\s+ago", re.IGNORECASE)
_RELATIVE_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def parse_page_age(raw: str | None, *, now: datetime | None = None) -> datetime | None:
    """Best-effort parse of the provider's page-age string.

    Returns None rather than a guess when the format is unrecognised: a wrong date
    would poison the recency score, and a blank one simply scores as unknown.
    """
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in _PAGE_AGE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    match = _RELATIVE_RE.search(text)
    if match:
        from datetime import timedelta

        amount = int(match.group(1))
        days = _RELATIVE_DAYS.get(match.group(2).lower(), 0) * amount
        base = now or datetime.now(UTC)
        return base - timedelta(days=days)
    return None


def submission_id(url: str) -> str | None:
    """The Reddit submission id in a thread URL, if it is a thread URL."""
    match = _SUBMISSION_RE.search(url)
    return match.group(1).lower() if match else None


class RedditSearchCollector:
    """Finds Reddit threads where pet owners ask questions."""

    name = "reddit_search"

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        provider = ctx.require_search()
        settings = cfg.settings or {}

        allowed_domains = list(settings.get("allowed_domains") or ["reddit.com"])
        max_uses = int(settings.get("max_uses_per_request", 3))
        sets_per_run = int(settings.get("keyword_sets_per_run", 6))
        basis = list(settings.get("engagement_basis") or ["result_position", "query_recurrence"])

        plan = self._plan(cfg, ctx, limit=sets_per_run)
        if not plan:
            ctx.warn(f"{self.name}: no keyword sets matched the requested filters")
            return []

        items: list[RawItem] = []
        seen_ids: set[str] = set()
        searches_done = 0

        for keyword_set, region in plan:
            if len(items) >= cfg.max_items_per_run:
                log.info(
                    "collect.item_cap",
                    extra={"source": self.name, "cap": cfg.max_items_per_run},
                )
                break

            # Three caps, any of which can stop the run: the source's own daily request
            # cap, the budget's daily/monthly search caps, and the per-request max_uses.
            if searches_done + max_uses > cfg.daily_request_cap:
                ctx.ledger.note_skip(
                    self.name,
                    f"source daily_request_cap of {cfg.daily_request_cap} reached",
                )
                break
            allowance = ctx.ledger.remaining_web_searches()
            if allowance <= 0:
                decision = ctx.ledger.allows_web_searches(1)
                ctx.ledger.note_skip(self.name, decision.reason or "web search budget exhausted")
                break
            uses = min(max_uses, allowance)

            request = SearchRequest(
                phrases=list(keyword_set.phrases),
                intent=self._intent(ctx, keyword_set),
                allowed_domains=allowed_domains,
                max_uses=uses,
                region=region,
                country=self._country(ctx, region),
            )

            try:
                response = provider.search(request)
            except SearchError as error:
                # One failed keyword set must not lose the others.
                message = f"{self.name}: search failed for {keyword_set.id} ({region})"
                log.warning(
                    "collect.search_failed",
                    extra={"source": self.name, "keyword_set": keyword_set.id, "region": region},
                )
                ctx.warn(message)
                raise CollectorError(f"{message}: {error}") from error

            searches_done += response.search_count
            ctx.ledger.record_web_searches(response.search_count)
            for code in response.error_codes:
                ctx.warn(f"{self.name}: search tool error {code}")

            items.extend(
                self._to_items(
                    response,
                    ctx=ctx,
                    region=region,
                    keyword_set=keyword_set,
                    basis=basis,
                    seen_ids=seen_ids,
                    remaining=cfg.max_items_per_run - len(items),
                )
            )

        log.info(
            "collect.done",
            extra={
                "source": self.name,
                "keyword_sets": len(plan),
                "searches": searches_done,
                "items": len(items),
            },
        )
        return items

    # --- planning --------------------------------------------------------------------

    def _plan(
        self, cfg: ResolvedSource, ctx: RunContext, *, limit: int
    ) -> list[tuple[KeywordSet, str]]:
        """Which (keyword set, region) pairs to search, in a stable rotating order."""
        source_regions = set(cfg.regions) or set(ctx.config.keywords.regions)
        pairs: list[tuple[KeywordSet, str]] = []

        for keyword_set in ctx.config.keywords.keyword_sets:
            if ctx.category and ctx.category != "all" and keyword_set.category != ctx.category:
                continue
            for region in keyword_set.regions:
                if region in source_regions:
                    pairs.append((keyword_set, region))

        if not pairs:
            return []

        # Rotate by day so that a daily run does not always search the same first few
        # sets when the cap bites. Deterministic, so a re-run of the same day repeats.
        offset = self._rotation_offset(ctx, len(pairs))
        rotated = pairs[offset:] + pairs[:offset]
        return rotated[: max(1, limit)]

    @staticmethod
    def _rotation_offset(ctx: RunContext, length: int) -> int:
        from ..utils.clock import today

        day = today(ctx.config.settings.run.local_utc_offset_hours)
        return day.toordinal() % length if length else 0

    @staticmethod
    def _intent(ctx: RunContext, keyword_set: KeywordSet) -> str:
        try:
            category = ctx.config.categories.by_id(keyword_set.category)
        except KeyError:
            return "Find Reddit threads where pet owners ask questions."
        return (
            "Find Reddit threads where pet owners ask questions or describe problems "
            f"about: {category.name} — {category.description}"
        )

    @staticmethod
    def _country(ctx: RunContext, region: str) -> str | None:
        """A two-letter country hint for the search, for region-specific results."""
        return {"za": "ZA"}.get(region)

    # --- conversion ------------------------------------------------------------------

    def _to_items(
        self,
        response: SearchResponse,
        *,
        ctx: RunContext,
        region: str,
        keyword_set: KeywordSet,
        basis: list[str],
        seen_ids: set[str],
        remaining: int,
    ) -> list[RawItem]:
        items: list[RawItem] = []

        for result in response.results:
            if len(items) >= remaining:
                break
            item = self._to_item(
                result, ctx=ctx, region=region, keyword_set=keyword_set, basis=basis
            )
            if item is None:
                continue
            if item.item_id in seen_ids or item.item_id in ctx.known_item_ids:
                continue
            seen_ids.add(item.item_id)
            items.append(item)

        return items

    def _to_item(
        self,
        result: SearchResult,
        *,
        ctx: RunContext,
        region: str,
        keyword_set: KeywordSet,
        basis: list[str],
    ) -> RawItem | None:
        """Turn one search result into an item, or drop it."""
        url = result.url.strip()

        # A profile or user feed identifies a person. There is no scrubbed version of
        # such a URL, so it is dropped rather than cleaned.
        if is_person_url(url):
            return None

        external_id = submission_id(url)

        engagement: dict[str, object] = {
            "basis": basis,
            "result_position": result.position,
            "query_recurrence": result.recurrence,
            # Kept raw: see parse_page_age for why it is not always a date.
            "page_age": result.page_age,
            "keyword_set": keyword_set.id,
        }
        if result.snippet_source:
            engagement["snippet_source"] = result.snippet_source

        return RawItem(
            source=self.name,
            external_id=external_id,
            url=url,
            title=scrub(result.title),
            snippet=scrub(result.snippet),
            published_at=parse_page_age(result.page_age),
            query=result.query or "; ".join(keyword_set.phrases[:2]),
            region=region,
            engagement=engagement,
            run_id=ctx.run_id,
        ).with_id()


_: Collector = RedditSearchCollector()
