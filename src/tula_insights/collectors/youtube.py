"""YouTube comments, through the official YouTube Data API v3.

Comments under pet-travel and pet-care videos are owners asking real questions, and
this is the only automatic source with real engagement numbers: likes and replies on
the comment, and views, likes and comment counts on the video.

**Per run:** one `search.list` call per keyword set (the scarce quota: since June 2026
it has its own bucket of 100 calls a day), one `videos.list` call per 50 videos for
their statistics, and one `commentThreads.list` call per video for its top comments.
Comments that look like a question or a problem become items; the classifier makes
the real judgement in the next stage.

**What is never stored.** Every comment arrives with its author's name, channel ID,
channel URL and profile image. None of them is read. Channel names of the videos are
not stored either. Comment text is PII-scrubbed like everything else.

**Storage limit.** YouTube's Developer Policies allow this data — comment text and
statistics alike — to be kept for no more than 30 days. `settings.yaml →
retention.api_data_max_days.youtube` enforces that: older YouTube rows are purged by
`stages/retention.py`, never archived.

**The API key never reaches a log.** It travels in the query string, and request
libraries echo the URL in their exception messages. Every error raised here is built
from the HTTP status and Google's error reason alone, and passed through `_redact`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from ..config import KeywordSet, ResolvedSource
from ..models import RawItem
from ..utils.clock import today
from ..utils.logging import get_logger
from ..utils.pii import scrub
from ..utils.ratelimit import RateLimiter
from ..utils.retry import call_with_retry
from .base import Collector, CollectorError, RunContext

log = get_logger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"
MAX_IDS_PER_VIDEOS_CALL = 50   # videos.list maxResults ceiling, per the API spec
MAX_COMMENTS_PER_CALL = 100    # commentThreads.list maxResults ceiling, per the API spec
TITLE_CHARS = 160

#: Retried: rate limits and Google's transient failures.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Help-seeking phrases that mark a comment worth classifying even without a "?".
DEFAULT_QUESTION_CUES = (
    "how do i", "how can i", "how do you", "any advice", "any tips", "anyone know",
    "does anyone", "has anyone", "should i", "is it safe", "can i", "what do i",
    "help", "worried", "nervous", "scared", "struggling",
)

_REGION_QUERY_SUFFIX = {"za": "South Africa"}
_REGION_CODE = {"za": "ZA"}


class YouTubeError(RuntimeError):
    """A YouTube request failed. The message never contains the API key or the URL."""

    def __init__(self, message: str, *, status: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class CommentsUnavailable(YouTubeError):
    """Comments are disabled or hidden on this video. Skip it; not a failure."""


class QuotaRefused(YouTubeError):
    """YouTube refused a quota-bearing call. Stop this source for the day."""


@dataclass
class Video:
    video_id: str
    title: str
    published_at: str


class YouTubeClient:
    """The three read-only calls this project makes, counted and retried."""

    def __init__(
        self,
        api_key: str,
        *,
        session: Any | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        requests_per_minute: int = 60,
    ) -> None:
        if not api_key:
            raise YouTubeError("YOUTUBE_API_KEY is empty")
        self._key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.limiter = RateLimiter(requests_per_minute)
        #: Quota units spent this run, by Google's published costs.
        self.units = 0
        self.searches = 0

    # --- the three calls -------------------------------------------------------------

    def search_videos(
        self, query: str, *, max_results: int, region_code: str | None, language: str = "en"
    ) -> list[Video]:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max(1, min(max_results, 50)),
            "order": "relevance",
            "relevanceLanguage": language,
            "safeSearch": "moderate",
        }
        if region_code:
            params["regionCode"] = region_code
        body = self._get("search", params, what="search.list")
        self.searches += 1
        self.units += 1  # search.list: 1 unit in its own daily bucket (100 calls/day)

        videos = []
        for entry in body.get("items", []) or []:
            video_id = ((entry.get("id") or {}).get("videoId") or "").strip()
            if not video_id:
                continue
            snippet = entry.get("snippet") or {}
            videos.append(
                Video(
                    video_id=video_id,
                    title=str(snippet.get("title") or ""),
                    published_at=str(snippet.get("publishedAt") or ""),
                )
            )
        return videos

    def video_statistics(self, video_ids: Iterable[str]) -> dict[str, dict[str, int]]:
        ids = list(dict.fromkeys(i for i in video_ids if i))
        stats: dict[str, dict[str, int]] = {}
        for start in range(0, len(ids), MAX_IDS_PER_VIDEOS_CALL):
            chunk = ids[start : start + MAX_IDS_PER_VIDEOS_CALL]
            body = self._get(
                "videos",
                {"part": "statistics", "id": ",".join(chunk), "maxResults": len(chunk)},
                what="videos.list",
            )
            self.units += 1
            for entry in body.get("items", []) or []:
                raw = entry.get("statistics") or {}
                stats[entry.get("id", "")] = {
                    "views": _to_int(raw.get("viewCount")),
                    "likes": _to_int(raw.get("likeCount")),
                    "comments": _to_int(raw.get("commentCount")),
                }
        return stats

    def top_comments(self, video_id: str, *, max_results: int, order: str) -> list[dict[str, Any]]:
        body = self._get(
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max(1, min(max_results, MAX_COMMENTS_PER_CALL)),
                "order": order if order in {"relevance", "time"} else "relevance",
                "textFormat": "plainText",
            },
            what="commentThreads.list",
            comments=True,
        )
        self.units += 1
        threads = []
        for entry in body.get("items", []) or []:
            snippet = entry.get("snippet") or {}
            top = (snippet.get("topLevelComment") or {}).get("snippet") or {}
            # Deliberately read field by field: the author fields that arrive alongside
            # these are never touched, so they cannot leak into an item by accident.
            threads.append(
                {
                    "thread_id": str(entry.get("id") or ""),
                    "comment_id": str((snippet.get("topLevelComment") or {}).get("id") or ""),
                    "text": str(top.get("textOriginal") or top.get("textDisplay") or ""),
                    "likes": _to_int(top.get("likeCount")),
                    "replies": _to_int(snippet.get("totalReplyCount")),
                    "published_at": str(top.get("publishedAt") or ""),
                }
            )
        return threads

    # --- plumbing --------------------------------------------------------------------

    def _get(
        self, resource: str, params: dict[str, Any], *, what: str, comments: bool = False
    ) -> dict[str, Any]:
        def send() -> dict[str, Any]:
            self.limiter.acquire()
            try:
                response = self.session.get(
                    f"{API_ROOT}/{resource}",
                    params={**params, "key": self._key},
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                # Never str(error): it can carry the full URL, key included.
                raise YouTubeError(
                    f"{what}: network error ({type(error).__name__})", status=None
                ) from None
            if response.status_code == 200:
                return response.json()
            raise self._error(response, what=what, comments=comments)

        return call_with_retry(
            send,
            what=what,
            attempts=self.max_retries + 1,
            retry_on=(YouTubeError,),
            should_retry=lambda e: isinstance(e, YouTubeError)
            and (e.status is None or e.status in RETRYABLE_STATUS),
        )

    def _error(self, response: Any, *, what: str, comments: bool) -> YouTubeError:
        status = response.status_code
        reason = ""
        try:
            errors = (response.json().get("error") or {}).get("errors") or []
            reason = str(errors[0].get("reason") or "") if errors else ""
        except (ValueError, AttributeError):
            pass
        reason = _redact(reason, self._key)
        message = f"{what}: HTTP {status}" + (f" ({reason})" if reason else "")
        if comments and status in {403, 404}:
            return CommentsUnavailable(message, status=status, reason=reason)
        if status == 403:
            return QuotaRefused(message, status=status, reason=reason)
        return YouTubeError(message, status=status, reason=reason)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _redact(text: str, key: str) -> str:
    """Belt and braces: remove the API key from any text, wherever it came from."""
    if not text:
        return ""
    cleaned = text.replace(key, "[key]") if key else text
    return re.sub(r"key=[^&\s]+", "key=[key]", cleaned)


def parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def looks_like_question(text: str, *, min_chars: int, cues: Iterable[str]) -> bool:
    """A cheap pre-filter. The classifier makes the real judgement."""
    stripped = " ".join(text.split())
    if len(stripped) < min_chars:
        return False
    if "?" in stripped:
        return True
    lowered = stripped.lower()
    return any(cue in lowered for cue in cues)


class YouTubeCollector:
    """Finds comments where pet owners ask questions."""

    name = "youtube"

    def __init__(self, client_factory=None) -> None:
        #: Injectable for tests: (api_key, cfg) -> YouTubeClient.
        self._client_factory = client_factory

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        api_key = ctx.config.env.youtube_api_key
        if not api_key:
            raise CollectorError(
                "YOUTUBE_API_KEY is not set. Create a key restricted to the YouTube Data "
                "API v3 in Google Cloud and add it as a repository secret."
            )
        settings = cfg.settings or {}
        daily_search_cap = int(settings.get("daily_search_cap", 50))
        videos_per_search = int(settings.get("max_videos_per_search", 10))
        comments_per_video = int(settings.get("max_comments_per_video", 40))
        order = str(settings.get("comment_order", "relevance"))
        min_chars = int(settings.get("min_comment_chars", 25))
        cues = [c.lower() for c in settings.get("question_cues") or DEFAULT_QUESTION_CUES]
        sets_per_run = int(settings.get("keyword_sets_per_run", 6))
        language = str(settings.get("relevance_language", "en"))

        client = (
            self._client_factory(api_key, cfg)
            if self._client_factory
            else YouTubeClient(
                api_key,
                timeout_seconds=cfg.timeout_seconds,
                max_retries=cfg.max_retries,
                requests_per_minute=cfg.requests_per_minute,
            )
        )

        plan = self._plan(cfg, ctx, limit=sets_per_run)
        if not plan:
            ctx.warn(f"{self.name}: no keyword sets matched the requested filters")
            return []

        items: list[RawItem] = []
        seen: set[str] = set()

        try:
            for _keyword_set, region, phrase in plan:
                if len(items) >= cfg.max_items_per_run:
                    break
                if client.searches >= daily_search_cap:
                    ctx.ledger.note_skip(
                        self.name, f"source daily_search_cap of {daily_search_cap} reached"
                    )
                    break
                if ctx.ledger.remaining_youtube_searches() <= 0:
                    ctx.ledger.note_skip(self.name, "YouTube search budget exhausted")
                    break

                query = " ".join(filter(None, [phrase, _REGION_QUERY_SUFFIX.get(region)]))
                videos = client.search_videos(
                    query,
                    max_results=videos_per_search,
                    region_code=_REGION_CODE.get(region),
                    language=language,
                )
                ctx.ledger.record_youtube_searches(1)
                ctx.count("youtube.searches")
                ctx.count("youtube.videos", len(videos))
                if not videos:
                    continue

                stats = client.video_statistics(v.video_id for v in videos)

                for video in videos:
                    if len(items) >= cfg.max_items_per_run:
                        break
                    try:
                        threads = client.top_comments(
                            video.video_id, max_results=comments_per_video, order=order
                        )
                    except CommentsUnavailable:
                        ctx.count("youtube.videos_comments_unavailable")
                        continue
                    ctx.count("youtube.comments_read", len(threads))

                    for thread in threads:
                        if len(items) >= cfg.max_items_per_run:
                            break
                        if not looks_like_question(thread["text"], min_chars=min_chars, cues=cues):
                            continue
                        item = self._to_item(
                            thread,
                            video=video,
                            stats=stats.get(video.video_id, {}),
                            query=query,
                            region=region,
                            ctx=ctx,
                        )
                        if item.item_id in seen or item.item_id in ctx.known_item_ids:
                            continue
                        seen.add(item.item_id)
                        items.append(item)
                        ctx.count("youtube.comments_kept")
        except QuotaRefused as error:
            ctx.ledger.note_skip(self.name, f"YouTube refused further requests: {error}")
        except YouTubeError as error:
            raise CollectorError(f"{self.name}: {error}") from None
        finally:
            ctx.count("youtube.units", client.units)

        log.info(
            "collect.done",
            extra={"source": self.name, "searches": client.searches, "units": client.units,
                   "items": len(items)},
        )
        return items

    # --- planning --------------------------------------------------------------------

    def _plan(
        self, cfg: ResolvedSource, ctx: RunContext, *, limit: int
    ) -> list[tuple[KeywordSet, str, str]]:
        """(keyword set, region, phrase) triples. One phrase per set, rotating daily."""
        regions = set(cfg.regions) or set(ctx.config.keywords.regions)
        day = today(ctx.config.settings.run.local_utc_offset_hours).toordinal()
        triples = []
        for keyword_set in ctx.config.keywords.keyword_sets:
            if ctx.category and ctx.category != "all" and keyword_set.category != ctx.category:
                continue
            phrase = keyword_set.phrases[day % len(keyword_set.phrases)]
            for region in keyword_set.regions:
                if region in regions:
                    triples.append((keyword_set, region, phrase))
        if not triples:
            return []
        offset = day % len(triples)
        return (triples[offset:] + triples[:offset])[: max(1, limit)]

    # --- conversion ------------------------------------------------------------------

    def _to_item(
        self,
        thread: dict[str, Any],
        *,
        video: Video,
        stats: dict[str, int],
        query: str,
        region: str,
        ctx: RunContext,
    ) -> RawItem:
        text = scrub(thread["text"])
        title = text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"
        url = f"https://www.youtube.com/watch?v={video.video_id}"
        if thread.get("comment_id"):
            url += f"&lc={thread['comment_id']}"
        return RawItem(
            source=self.name,
            external_id=thread.get("thread_id") or None,
            url=url,
            title=title,
            snippet=text,
            published_at=parse_iso(thread.get("published_at", "")),
            query=query,
            region=region,
            engagement={
                "basis": ["comment_likes", "comment_replies", "video_views"],
                "comment_likes": thread.get("likes", 0),
                "comment_replies": thread.get("replies", 0),
                "video_id": video.video_id,
                "video_views": stats.get("views", 0),
                "video_likes": stats.get("likes", 0),
                "video_comments": stats.get("comments", 0),
            },
            run_id=ctx.run_id,
        ).with_id()


_: Collector = YouTubeCollector()
