"""Search-based discovery, through the Claude API web search tool.

One collector class serves every search-driven source. What differs between them —
which domains to allow or block, what to look for — lives in each source's
`settings` block in `config/sources.yaml`, so adding a search source is a YAML change.

**Sources using it**

* `web_questions` — the general web: forums, Q&A sites, veterinary and pet-travel
  pages, airline and government pet rules. The main discovery source.
* `reddit_search` — **disabled.** Reddit blocks Anthropic's crawler, so the Claude web
  search tool rejects any request restricted to `reddit.com` ("The following domains
  are not accessible to our user agent"). It stays registered so it can be switched
  back on if that ever changes. Reddit threads can still come in by hand, through the
  `manual_submissions` tab.

**Engagement.** The search tool returns no view, vote or comment counts, so the signals
here are weak and the scoring stage must treat them as such:

* `result_position` — where the search engine ranked it. A proxy for relevance.
* `query_recurrence` — how many of this run's queries surfaced the same page. The
  stronger of the two: a page that several different phrasings all find is about
  something people keep asking.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ..config import KeywordSet, ResolvedSource
from ..models import RawItem
from ..search.provider import SearchError, SearchRequest, SearchResponse, SearchResult
from ..utils.clock import today
from ..utils.logging import get_logger
from ..utils.pii import is_person_url, scrub
from .base import Collector, CollectorError, RunContext

log = get_logger(__name__)

#: Used when a source's settings give no `intent`.
DEFAULT_INTENT = (
    "Find pages where pet owners ask questions or describe problems — forum threads, "
    "question-and-answer pages, discussions — about: {category}."
)

#: `/r/<sub>/comments/<id>/<slug>` — the id is the stable identity of a Reddit thread.
_SUBMISSION_RE = re.compile(r"/comments/([a-z0-9]{4,12})(?:/|$)", re.IGNORECASE)

#: Page-age formats seen from the search tool. Anything else is kept as raw text and
#: `published_at` is left blank rather than guessed at.
_PAGE_AGE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")

_RELATIVE_RE = re.compile(r"(\d+)\s+(day|week|month|year|hour|minute)s?\s+ago", re.IGNORECASE)
_RELATIVE_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}

#: Two-letter country hints for regions, for locality in search results.
_REGION_COUNTRY = {"za": "ZA"}


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
        amount = int(match.group(1))
        days = _RELATIVE_DAYS.get(match.group(2).lower(), 0) * amount
        base = now or datetime.now(UTC)
        return base - timedelta(days=days)
    return None


def host_of(url: str) -> str:
    """The host of a URL, lowercased, without a leading `www.`."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_reddit(url: str) -> bool:
    host = host_of(url)
    return host == "reddit.com" or host.endswith(".reddit.com")


def submission_id(url: str) -> str | None:
    """The Reddit submission id in a thread URL, if it is one."""
    match = _SUBMISSION_RE.search(url)
    return match.group(1).lower() if match else None


class WebSearchCollector:
    """Finds pages where pet owners ask questions, through the search provider."""

    def __init__(self, name: str) -> None:
        self.name = name

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        provider = ctx.require_search()
        settings = cfg.settings or {}

        allowed_domains = list(settings.get("allowed_domains") or [])
        blocked_domains = list(settings.get("blocked_domains") or [])
        max_uses = int(settings.get("max_uses_per_request", 3))
        sets_per_run = int(settings.get("keyword_sets_per_run", 6))
        basis = list(settings.get("engagement_basis") or ["result_position", "query_recurrence"])
        intent_template = str(settings.get("intent") or DEFAULT_INTENT)
        query_hint = " ".join(str(settings.get("query_hint") or "").split())

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

            request = SearchRequest(
                phrases=list(keyword_set.phrases),
                intent=self._intent(ctx, keyword_set, intent_template, query_hint),
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                max_uses=min(max_uses, allowance),
                region=region,
                country=_REGION_COUNTRY.get(region),
            )

            try:
                response = provider.search(request)
            except SearchError as error:
                message = f"{self.name}: search failed for {keyword_set.id} ({region})"
                log.warning(
                    "collect.search_failed",
                    extra={"source": self.name, "keyword_set": keyword_set.id, "region": region},
                )
                ctx.warn(message)
                raise CollectorError(f"{message}: {error}") from error

            searches_done += response.search_count
            ctx.ledger.record_web_searches(response.search_count)
            self._measure(response, ctx)
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
        day = today(ctx.config.settings.run.local_utc_offset_hours)
        offset = day.toordinal() % len(pairs)
        rotated = pairs[offset:] + pairs[:offset]
        return rotated[: max(1, limit)]

    @staticmethod
    def _intent(ctx: RunContext, keyword_set: KeywordSet, template: str, hint: str = "") -> str:
        try:
            category = ctx.config.categories.by_id(keyword_set.category)
            subject = f"{category.name} — {category.description}"
        except KeyError:
            subject = "pet care"
        intent = template.replace("{category}", subject)
        return f"{intent}\n\n{hint}" if hint else intent

    def _measure(self, response: SearchResponse, ctx: RunContext) -> None:
        """Counts for the run summary: what the searches asked for and got back.

        Numbers only. The reddit counts tell apart the two ways Reddit could be missing:
        the model never asked for it, or it asked and the search engine returned none.
        """
        prefix = self.name
        ctx.count(f"{prefix}.queries", len(response.queries))
        ctx.count(
            f"{prefix}.queries_mentioning_reddit",
            sum("reddit" in query.lower() for query in response.queries),
        )
        ctx.count(f"{prefix}.results", len(response.results))
        ctx.count(f"{prefix}.results_from_reddit", sum(is_reddit(r.url) for r in response.results))
        hosts = {host_of(r.url) for r in response.results if r.url}
        ctx.count(f"{prefix}.distinct_sites", len(hosts))

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
            external_id=submission_id(url),
            url=url,
            title=scrub(result.title),
            snippet=scrub(result.snippet),
            published_at=parse_page_age(result.page_age),
            query=result.query or "; ".join(keyword_set.phrases[:2]),
            region=region,
            engagement=engagement,
            run_id=ctx.run_id,
        ).with_id()


_: Collector = WebSearchCollector("web_questions")
