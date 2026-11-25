"""All Google Sheets I/O. Nothing else in the project imports gspread.

Three jobs beyond moving rows:

**Rate limiting.** The Sheets API allows 60 read and 60 write requests per minute per
user per project. The collect stage runs as a matrix of parallel jobs, all using the
same service account, so every call goes through a sliding-window limiter set below
that ceiling. A 429 is still retried with exponential backoff and jitter.

**Protecting human columns.** The `feed` tab is edited by reviewers while the pipeline
writes to it. :meth:`SheetStore.update_rows` refuses to write a column marked
``human_editable``, so a race can lose a pipeline write but never a person's decision.

**Dry runs.** ``dry_run=True`` makes every write a logged no-op, which is what
``--dry-run`` on the CLI turns on.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import rowcol_to_a1

from ..config import AppConfig, ConfigError, SheetSettings
from ..schema import TabSpec
from ..schema import tab as tab_spec
from ..utils.logging import get_logger
from ..utils.serialise import to_cell

log = get_logger(__name__)

#: Status codes worth retrying: rate limits, and Google's transient 5xx.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)


class SheetStoreError(RuntimeError):
    """Raised when the spreadsheet cannot be reached, or its shape is wrong."""


class _RateLimiter:
    """A sliding-window limiter. Thread-safe; blocks rather than raising."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            time.sleep(min(wait, 60.0))


def _status_of(error: APIError) -> int | None:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


class SheetStore:
    """A handle on the Tula Insights spreadsheet."""

    def __init__(
        self,
        spreadsheet: gspread.Spreadsheet,
        settings: SheetSettings,
        *,
        dry_run: bool = False,
    ) -> None:
        self.book = spreadsheet
        self.settings = settings
        self.dry_run = dry_run
        self._reads = _RateLimiter(settings.reads_per_minute)
        self._writes = _RateLimiter(settings.writes_per_minute)
        self._worksheets: dict[str, gspread.Worksheet] = {}

    # --- plumbing --------------------------------------------------------------------

    def _call(self, kind: str, what: str, func, *args: Any, **kwargs: Any) -> Any:
        """Run one API call under the rate limiter, retrying transient failures."""
        limiter = self._reads if kind == "read" else self._writes
        attempts = self.settings.max_retries + 1
        delay = self.settings.backoff_base_seconds

        for attempt in range(1, attempts + 1):
            limiter.acquire()
            try:
                return func(*args, **kwargs)
            except APIError as error:
                status = _status_of(error)
                if status not in RETRYABLE_STATUS or attempt == attempts:
                    raise SheetStoreError(
                        f"Sheets {kind} failed during {what} (HTTP {status}) after "
                        f"{attempt} attempt(s)."
                    ) from error
                sleep_for = min(
                    delay * (2 ** (attempt - 1)) + random.uniform(0, 1.0),
                    self.settings.backoff_max_seconds,
                )
                log.warning(
                    "sheets.retry",
                    extra={
                        "operation": what,
                        "status": status,
                        "attempt": attempt,
                        "sleep_seconds": round(sleep_for, 2),
                    },
                )
                time.sleep(sleep_for)
        raise SheetStoreError(f"Sheets {kind} failed during {what}: retries exhausted.")

    def worksheet(self, name: str) -> gspread.Worksheet:
        """The worksheet for a tab, cached. Raises if it has not been created yet."""
        if name in self._worksheets:
            return self._worksheets[name]
        try:
            worksheet = self._call("read", f"open tab {name}", self.book.worksheet, name)
        except SheetStoreError:
            raise
        except WorksheetNotFound as error:
            raise SheetStoreError(
                f"The spreadsheet has no {name!r} tab. Run `python scripts/setup_sheet.py` "
                f"to create the missing tabs."
            ) from error
        self._worksheets[name] = worksheet
        return worksheet

    def existing_tab_names(self) -> set[str]:
        worksheets = self._call("read", "list tabs", self.book.worksheets)
        return {w.title for w in worksheets}

    # --- schema ----------------------------------------------------------------------

    def ensure_tab(self, spec: TabSpec) -> tuple[bool, list[str]]:
        """Create `spec`'s tab if missing; validate its header if present.

        Returns ``(created, problems)``. Problems are header mismatches, described in
        words — this never rewrites or reorders an existing header, because doing so on
        a tab the team is using would silently move their data under new labels.
        """
        if spec.name in self.existing_tab_names():
            return False, self.header_problems(spec)

        if self.dry_run:
            log.info("sheets.dry_run.create_tab", extra={"tab": spec.name})
            return True, []

        worksheet = self._call(
            "write",
            f"create tab {spec.name}",
            self.book.add_worksheet,
            title=spec.name,
            rows=self.settings.new_tab_rows,
            cols=max(len(spec.columns), 10),
        )
        self._worksheets[spec.name] = worksheet
        self._call(
            "write",
            f"write header {spec.name}",
            worksheet.update,
            [list(spec.columns)],
            "A1",
            value_input_option=gspread.utils.ValueInputOption.raw,
        )
        self._call("write", f"freeze header {spec.name}", worksheet.freeze, rows=1)
        if spec.hidden:
            self._call("write", f"hide {spec.name}", worksheet.hide)
        log.info("sheets.tab_created", extra={"tab": spec.name, "columns": len(spec.columns)})
        return True, []

    def header_problems(self, spec: TabSpec) -> list[str]:
        """Describe every way an existing tab's header differs from the schema."""
        header = self.read_header(spec.name)
        problems: list[str] = []

        if not header:
            problems.append(f"{spec.name}: header row is empty")
            return problems

        expected = list(spec.columns)
        missing = [c for c in expected if c not in header]
        unexpected = [c for c in header if c and c not in expected]

        if missing:
            problems.append(f"{spec.name}: missing column(s): {', '.join(missing)}")
        if unexpected:
            problems.append(
                f"{spec.name}: unexpected column(s): {', '.join(unexpected)} "
                f"(left alone — remove them by hand if they are stale)"
            )
        if not missing and not unexpected and header[: len(expected)] != expected:
            problems.append(
                f"{spec.name}: columns are out of order. Expected: {', '.join(expected)}"
            )
        return problems

    def read_header(self, name: str) -> list[str]:
        worksheet = self.worksheet(name)
        return self._call("read", f"read header {name}", worksheet.row_values, 1)

    # --- reading ---------------------------------------------------------------------

    def read_rows(self, name: str) -> list[dict[str, Any]]:
        """Every data row of a tab, keyed by the sheet's own header."""
        worksheet = self.worksheet(name)
        values = self._call("read", f"read {name}", worksheet.get_all_values)
        if not values:
            return []
        header = [h.strip() for h in values[0]]
        rows: list[dict[str, Any]] = []
        for raw in values[1:]:
            if not any(str(cell).strip() for cell in raw):
                continue
            padded = list(raw) + [""] * (len(header) - len(raw))
            rows.append(dict(zip(header, padded, strict=False)))
        return rows

    def read_column(self, name: str, column: str) -> list[str]:
        """One column's values, in sheet order. Cheaper than reading the whole tab."""
        spec = tab_spec(name)
        worksheet = self.worksheet(name)
        letter = rowcol_to_a1(1, spec.index_of(column) + 1).rstrip("1")
        values = self._call(
            "read", f"read {name}.{column}", worksheet.get_values, f"{letter}2:{letter}"
        )
        return [row[0].strip() if row else "" for row in values]

    def existing_keys(self, name: str) -> set[str]:
        """The set of primary keys already in a tab, for skip-if-present collectors."""
        spec = tab_spec(name)
        if not spec.key_column:
            raise SheetStoreError(f"{name!r} has no key column")
        return {value for value in self.read_column(name, spec.key_column) if value}

    # --- writing ---------------------------------------------------------------------

    def append_rows(self, name: str, rows: Sequence[dict[str, Any]]) -> int:
        """Append rows, ordering each one by the schema. Returns how many were written."""
        if not rows:
            return 0
        spec = tab_spec(name)
        self._reject_unknown_columns(spec, rows)

        values = [[to_cell(row.get(column)) for column in spec.columns] for row in rows]

        if self.dry_run:
            log.info("sheets.dry_run.append", extra={"tab": name, "rows": len(values)})
            return len(values)

        worksheet = self.worksheet(name)
        self._call(
            "write",
            f"append to {name}",
            worksheet.append_rows,
            values,
            value_input_option=gspread.utils.ValueInputOption.raw,
        )
        log.info("sheets.appended", extra={"tab": name, "rows": len(values)})
        return len(values)

    def update_rows(self, name: str, updates: dict[str, dict[str, Any]]) -> int:
        """Update existing rows in place, keyed by the tab's primary key.

        `updates` maps a key to the columns to change. Columns marked
        ``human_editable`` on the tab are rejected: the pipeline does not overwrite a
        reviewer's decision, so a mistake here fails loudly instead of quietly.
        """
        if not updates:
            return 0
        spec = tab_spec(name)
        if not spec.key_column:
            raise SheetStoreError(f"{name!r} has no key column, so rows cannot be updated")
        self._reject_unknown_columns(spec, updates.values())
        self._reject_human_columns(spec, updates.values())

        row_numbers = self._row_numbers(spec)
        payload: list[dict[str, Any]] = []
        missing: list[str] = []

        for key, changes in updates.items():
            row_number = row_numbers.get(key)
            if row_number is None:
                missing.append(key)
                continue
            indices = sorted(spec.index_of(column) for column in changes)
            by_index = {spec.index_of(column): to_cell(value) for column, value in changes.items()}
            for run in _contiguous_runs(indices):
                start, end = run[0], run[-1]
                payload.append(
                    {
                        "range": (
                            f"{rowcol_to_a1(row_number, start + 1)}:"
                            f"{rowcol_to_a1(row_number, end + 1)}"
                        ),
                        "values": [[by_index[i] for i in run]],
                    }
                )

        if missing:
            log.warning(
                "sheets.update_missing_keys", extra={"tab": name, "count": len(missing)}
            )
        if not payload:
            return 0
        if self.dry_run:
            log.info("sheets.dry_run.update", extra={"tab": name, "ranges": len(payload)})
            return len(updates) - len(missing)

        worksheet = self.worksheet(name)
        self._call(
            "write",
            f"update {name}",
            worksheet.batch_update,
            payload,
            value_input_option=gspread.utils.ValueInputOption.raw,
        )
        written = len(updates) - len(missing)
        log.info("sheets.updated", extra={"tab": name, "rows": written})
        return written

    def _row_numbers(self, spec: TabSpec) -> dict[str, int]:
        """Map each primary key to its 1-based sheet row number."""
        assert spec.key_column is not None
        keys = self.read_column(spec.name, spec.key_column)
        # Row 1 is the header, so the first data row is row 2.
        return {key: index + 2 for index, key in enumerate(keys) if key}

    @staticmethod
    def _reject_unknown_columns(spec: TabSpec, rows: Iterable[dict[str, Any]]) -> None:
        known = set(spec.columns)
        for row in rows:
            unknown = set(row) - known
            if unknown:
                raise SheetStoreError(
                    f"{spec.name!r} has no column(s) {', '.join(sorted(unknown))}. "
                    f"Add them to tula_insights.schema first."
                )

    @staticmethod
    def _reject_human_columns(spec: TabSpec, rows: Iterable[dict[str, Any]]) -> None:
        protected = set(spec.human_editable)
        for row in rows:
            clash = set(row) & protected
            if clash:
                raise SheetStoreError(
                    f"{', '.join(sorted(clash))} on {spec.name!r} are edited by people. "
                    f"The pipeline must not overwrite them."
                )

    # --- housekeeping ----------------------------------------------------------------

    def cell_count(self) -> int:
        """Total cells across every tab, for the retention check."""
        worksheets = self._call("read", "measure sheet", self.book.worksheets)
        return sum(w.row_count * w.col_count for w in worksheets)

    def needs_archiving(self) -> bool:
        """True once the spreadsheet passes the configured fraction of its cell limit."""
        threshold = self.settings.cell_limit * self.settings.archive_threshold_fraction
        count = self.cell_count()
        log.info(
            "sheets.size",
            extra={"cells": count, "threshold": int(threshold), "limit": self.settings.cell_limit},
        )
        return count >= threshold


def _contiguous_runs(indices: Sequence[int]) -> list[list[int]]:
    """Group sorted column indices into contiguous runs, to batch cell ranges."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def open_store(config: AppConfig, *, dry_run: bool = False) -> SheetStore:
    """Authenticate and open the spreadsheet named by ``SHEET_ID``."""
    config.env.require("google_service_account_json", "sheet_id")
    try:
        info = config.env.service_account_info()
    except ConfigError:
        raise
    client = gspread.service_account_from_dict(info, scopes=SCOPES)
    try:
        book = client.open_by_key(config.env.sheet_id)
    except SpreadsheetNotFound as error:
        raise SheetStoreError(
            f"No spreadsheet with ID {config.env.sheet_id!r} is visible to "
            f"{info.get('client_email')}. Share the sheet with that address as an Editor."
        ) from error
    except APIError as error:
        raise SheetStoreError(
            f"Could not open the spreadsheet (HTTP {_status_of(error)}). Check that the "
            f"Sheets API and Drive API are enabled on the service account's project."
        ) from error
    return SheetStore(book, config.settings.sheet, dry_run=dry_run)
