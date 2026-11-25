"""The score components, as pure functions of an insight's items.

Every number here comes from `config/scoring.yaml`. Each component is normalised to
0 to 1; the total is their weighted sum (weights sum to 1) plus the feedback penalty,
clamped to 0 to 1.

* **recurrence** — how many items say this: log1p(count) / log1p(saturation), capped.
* **cross_source** — how many of the enabled sources say it.
* **engagement** — the mean of its items' engagement percentiles *within their own
  source*, so a YouTube comment is ranked against YouTube comments, not web pages.
* **recency** — exponential decay on the newest item's date (half-life in config).
* **trend** — items per day in the last window vs the baseline before it, capped.

An item's date is when it was *published* if known (a YouTube comment's own date),
else when it was collected. Using the published date means an old thread found today
does not look like a new trend.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .config import ScoringConfig
from .models import ItemRow


def item_date(item: ItemRow) -> datetime | None:
    return item.published_at or item.collected_at


def raw_engagement(source: str, engagement: dict[str, Any]) -> float | None:
    """A source-specific engagement figure. Comparable only within one source.

    None means the source carries no engagement signal (manual submissions, purged
    rows); such items get the neutral percentile 0.5.
    """
    if not engagement:
        return None
    if source == "youtube":
        likes = float(engagement.get("comment_likes") or 0)
        replies = float(engagement.get("comment_replies") or 0)
        # Replies weigh double: they show a question other people engaged with.
        return math.log1p(likes) + 2 * math.log1p(replies)
    if source in {"web_questions", "reddit_search"}:
        position = engagement.get("result_position")
        recurrence = float(engagement.get("query_recurrence") or 1)
        # Recurrence across queries is the stronger of the two weak signals.
        return recurrence - 0.1 * float(position if position is not None else 0)
    return None


def percentiles(values: dict[str, float | None]) -> dict[str, float]:
    """Percentile rank (0 to 1) of each value among the others; ties share a rank."""
    known = {k: v for k, v in values.items() if v is not None}
    out = dict.fromkeys(values, 0.5)
    if len(known) < 2:
        return out
    ordered = sorted(known.values())
    last = len(ordered) - 1
    for key, value in known.items():
        low = ordered.index(value)
        high = last - ordered[::-1].index(value)
        out[key] = ((low + high) / 2) / last
    return out


def recurrence(count: int, cfg: ScoringConfig) -> float:
    saturation = cfg.recurrence.saturation_count
    return min(1.0, math.log1p(count) / math.log1p(saturation)) if count > 0 else 0.0


def cross_source(sources: Iterable[str], enabled_sources: int) -> float:
    return min(1.0, len(set(sources)) / max(1, enabled_sources))


def recency(last_seen: datetime | None, now: datetime, cfg: ScoringConfig) -> float:
    if last_seen is None:
        return 0.0
    age_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    return 0.5 ** (age_days / cfg.recency.half_life_days)


def trend(dates: Iterable[datetime], now: datetime, cfg: ScoringConfig) -> float:
    window = cfg.trend.window_days
    baseline = cfg.trend.baseline_days
    cap = cfg.trend.ratio_cap
    recent_start = now - timedelta(days=window)
    base_start = recent_start - timedelta(days=baseline)
    recent = sum(1 for d in dates if recent_start < d <= now)
    base = sum(1 for d in dates if base_start < d <= recent_start)
    if recent == 0:
        return 0.0
    if base == 0:
        return 1.0  # new and rising: the most a trend can say
    ratio = (recent / window) / (base / baseline)
    return min(ratio, cap) / cap


def total(components: dict[str, float], feedback: float, cfg: ScoringConfig) -> float:
    weights = cfg.weights.model_dump()
    weighted = sum(weights[name] * components[name] for name in weights)
    return max(0.0, min(1.0, weighted + feedback))
