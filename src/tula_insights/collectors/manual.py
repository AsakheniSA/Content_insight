"""Rows people paste into the `manual_submissions` tab.

This is the compliant path for anything without a collector of its own: Facebook and
Instagram (no collectors exist, by design) and Reddit (its site blocks Anthropic's
crawler, and its API is out of scope). Someone on the team reads a thread the normal
way and copies the question into the sheet.

**What is kept, and what is not.** `question_or_quote` becomes the item's evidence and
`url` its source link. `submitted_by` and `notes` are **not** carried into `items`:
the first names a team member, and the second is free text that may name the poster
or carry internal commentary. Both stay in `manual_submissions`, where the team can
see them.

**Idempotence.** Each submission's identity is a hash of its URL and quote, so the
same thread pasted twice is one item, and a run that dies after writing items but
before setting `processed` re-marks them next time rather than duplicating them.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..config import ResolvedSource
from ..models import ManualSubmission, RawItem
from ..schema import MANUAL_SUBMISSIONS
from ..utils.logging import get_logger
from ..utils.pii import is_person_url, scrub
from .base import AfterWrite, Collector, CollectorError, RunContext

log = get_logger(__name__)

#: Long enough for a question, short enough to stay a title.
TITLE_CHARS = 160


def submission_key(url: str, quote: str) -> str:
    """Stable identity for a submission: same thread and quote, same key."""
    basis = f"{url.strip().lower()}|{' '.join(quote.lower().split())}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


class ManualCollector:
    """Ingests unprocessed rows from `manual_submissions`."""

    name = "manual"

    def __init__(self) -> None:
        #: row number -> (key, expected cells), for marking processed after the write.
        self._pending: dict[int, tuple[str, dict[str, str]]] = {}

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        if ctx.store is None:
            raise CollectorError(
                "The manual collector reads the manual_submissions tab, so it needs "
                "spreadsheet access. Check GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID."
            )

        rows = ctx.store.read_rows_numbered(MANUAL_SUBMISSIONS.name)
        items: list[RawItem] = []
        seen: set[str] = set()
        empty = already = 0

        for number, row in rows:
            submission = ManualSubmission.from_sheet_row(row)
            if submission.processed:
                already += 1
                continue
            quote = submission.question_or_quote.strip()
            if not quote:
                empty += 1
                continue
            if len(items) >= cfg.max_items_per_run:
                break

            key = submission_key(submission.url, quote)
            item = self._to_item(submission, key=key, ctx=ctx)
            # Mark it whether or not it is new: a duplicate of something already
            # collected is still a processed row.
            self._pending[number] = (key, self._fingerprint(row))
            if item.item_id in ctx.known_item_ids or item.item_id in seen:
                continue
            seen.add(item.item_id)
            items.append(item)

        if empty:
            ctx.warn(f"{self.name}: {empty} row(s) have no question_or_quote and were skipped")
        log.info(
            "collect.done",
            extra={
                "source": self.name,
                "rows": len(rows),
                "already_processed": already,
                "skipped_empty": empty,
                "items": len(items),
            },
        )
        return items

    def after_write(self, items: list[RawItem], ctx: RunContext) -> None:
        """Set `processed` on every row this run ingested, now that the items are saved."""
        if not self._pending or ctx.store is None or ctx.dry_run:
            return
        updates = {number: {"processed": True} for number in self._pending}
        expect = {number: cells for number, (_, cells) in self._pending.items()}
        marked = ctx.store.update_cells_by_row(
            MANUAL_SUBMISSIONS.name, updates, expect=expect
        )
        if marked < len(updates):
            ctx.warn(
                f"{self.name}: {len(updates) - marked} row(s) moved while the run was "
                f"going and were not marked processed. They are already collected and "
                f"will be marked on the next run."
            )
        self._pending.clear()

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    def _fingerprint(row: dict[str, Any]) -> dict[str, str]:
        """The cells that must still match before a row number is trusted again."""
        return {
            "url": str(row.get("url", "")),
            "question_or_quote": str(row.get("question_or_quote", "")),
        }

    def _to_item(self, submission: ManualSubmission, *, key: str, ctx: RunContext) -> RawItem:
        quote = scrub(submission.question_or_quote)
        url = submission.url.strip()
        # A link to someone's profile is dropped; the quote itself is still evidence.
        if url and is_person_url(url):
            url = ""

        title = quote if len(quote) <= TITLE_CHARS else quote[: TITLE_CHARS - 1].rstrip() + "…"
        platform = submission.platform.strip().lower()

        return RawItem(
            source=self.name,
            external_id=key,
            url=url,
            title=title,
            snippet=quote,
            query=f"manual:{platform}" if platform else "manual",
            region="",
            engagement={"basis": ["manual_submission"], "platform": platform or "unknown"},
            run_id=ctx.run_id,
        ).with_id()


_: Collector = ManualCollector()
__: AfterWrite = ManualCollector()
