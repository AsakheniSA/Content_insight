"""Enforcing platform storage limits on collected data.

Some platforms limit how long the data their API returns may be kept. YouTube's
Developer Policies allow non-authorised API data — comment text, titles, view and like
counts — to be stored for no more than 30 calendar days before it is deleted or
refreshed. `settings.yaml → retention.api_data_max_days` lists each such source.

A purged row keeps its `item_id`, `source`, `collected_at`, `query` and `insight_id`,
so an insight's evidence count still adds up, but every column the platform supplied
is blanked and its status becomes `purged`. Rows are blanked rather than deleted
because deleting sheet rows shifts every row number beneath them, which is how a
concurrent edit lands on the wrong row.

Purged rows must never be archived: archiving would keep the data indefinitely, which
is exactly what the limit forbids. Any future archive step must skip these sources.

Insight evidence links need no separate step: insights are recomputed from their
items on every score run, so a purged item's link simply drops out. **Feed rows** are
different — they copy the links at the moment they are served and are kept for good —
so :func:`purge_expired_feed_links` strips platform links from feed rows past the limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from ..config import AppConfig
from ..models import FeedRow, ItemRow
from ..schema import FEED, ITEMS
from ..store.sheets import SheetStore
from ..utils.clock import now_utc
from ..utils.logging import get_logger
from ..utils.serialise import join_list

log = get_logger(__name__)

#: Every column that can hold something a platform supplied.
PLATFORM_COLUMNS = ("url", "title", "snippet", "question", "pain_point", "engagement_json")

#: The link hosts that count as each limited source's data.
LINK_HOSTS = {"youtube": ("youtube.com", "youtu.be")}


def expired_item_ids(rows: list[ItemRow], limits: dict[str, int], *, now: datetime) -> list[str]:
    """Item ids whose source has a storage limit they have passed."""
    expired = []
    for row in rows:
        days = limits.get(row.source)
        if not days or row.status == "purged" or row.collected_at is None:
            continue
        if row.collected_at <= now - timedelta(days=days):
            expired.append(row.item_id)
    return expired


def purge_expired(
    store: SheetStore, config: AppConfig, *, now: datetime | None = None
) -> int:
    """Blank platform-supplied columns on rows past their limit. Returns rows purged."""
    limits = config.settings.retention.api_data_max_days
    if not limits:
        return 0
    rows = [ItemRow.from_sheet_row(r) for r in store.read_rows(ITEMS.name)]
    expired = expired_item_ids(rows, limits, now=now or now_utc())
    if not expired:
        return 0
    blank = dict.fromkeys(PLATFORM_COLUMNS, "")
    written = store.update_rows(
        ITEMS.name, {item_id: {**blank, "status": "purged"} for item_id in expired}
    )
    log.info("retention.purged", extra={"rows": written, "sources": ",".join(sorted(limits))})
    return written


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def purge_expired_feed_links(
    store: SheetStore, config: AppConfig, *, now: datetime | None = None
) -> int:
    """Strip platform links from feed rows served longer ago than the limit.

    Feed rows have no unique key (an insight can be served again on a later day), so
    rows are addressed by number, with each row's insight and date re-checked just
    before writing — a row someone moved is skipped, not mislabelled.
    """
    limits = config.settings.retention.api_data_max_days
    if not limits:
        return 0
    now = now or now_utc()
    updates: dict[int, dict[str, str]] = {}
    expect: dict[int, dict[str, str]] = {}
    for number, row in store.read_rows_numbered(FEED.name):
        feed = FeedRow.from_sheet_row(row)
        if feed.served_at is None or not feed.evidence_urls:
            continue
        kept = []
        for url in feed.evidence_urls:
            host = _host(url)
            days = [
                limits[source]
                for source, hosts in LINK_HOSTS.items()
                if source in limits and any(host == h or host.endswith("." + h) for h in hosts)
            ]
            if days and feed.served_at <= now - timedelta(days=min(days)):
                continue
            kept.append(url)
        if len(kept) != len(feed.evidence_urls):
            updates[number] = {"evidence_urls": join_list(kept)}
            expect[number] = {"insight_id": row.get("insight_id", ""),
                              "feed_date": row.get("feed_date", "")}
    if not updates:
        return 0
    written = store.update_cells_by_row(FEED.name, updates, expect=expect)
    log.info("retention.feed_links_purged", extra={"rows": written})
    return written
