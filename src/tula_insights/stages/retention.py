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
is exactly what the limit forbids. The archive step (stage 8) must skip these sources.

**Not yet covered:** insight evidence URLs (stage 4) may point at YouTube comments and
will need the same limit once insights exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..config import AppConfig
from ..models import ItemRow
from ..schema import ITEMS
from ..store.sheets import SheetStore
from ..utils.clock import now_utc
from ..utils.logging import get_logger

log = get_logger(__name__)

#: Every column that can hold something a platform supplied.
PLATFORM_COLUMNS = ("url", "title", "snippet", "question", "pain_point", "engagement_json")


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
