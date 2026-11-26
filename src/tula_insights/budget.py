"""Spend guardrails.

The caps in `config/budget.yaml` are enforced here, against what previous runs actually
recorded in the `runs` tab. There is no separate counter to drift out of step: the
ledger is the run history.

Policy, from §6.5 of the brief: when a cap would be exceeded the paid stage or source is
**skipped and flagged**, not failed. A run that quietly does less is better than a run
that fails loudly and leaves the feed empty — and better than one that keeps spending.
Set `on_cap_exceeded: fail` in budget.yaml to change that: the stage still stops
spending at the cap, saves what it already has, and then exits with a failure, which
opens a `pipeline-failure` issue.

Only the caps in budget.yaml count here. A source's own per-run cap in sources.yaml, or
YouTube refusing a request, is noted in the summary but is not a budget breach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from .config import AppConfig
from .models import RunStats
from .utils.clock import today as local_today
from .utils.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .store.sheets import SheetStore

log = get_logger(__name__)

#: Keys used in `runs.api_calls_by_service`. Keep these stable: the ledger reads them.
SERVICE_WEB_SEARCH = "claude_web_search"
SERVICE_MESSAGES = "claude_messages"
SERVICE_YOUTUBE_SEARCH = "youtube_search"
SERVICE_YOUTUBE_UNITS = "youtube_units"


@dataclass
class Spent:
    """What has already been spent, from the `runs` tab."""

    searches_today: int = 0
    searches_this_month: int = 0
    youtube_searches_today: int = 0
    youtube_searches_this_month: int = 0
    usd_this_month: float = 0.0
    runs_read: int = 0


@dataclass
class CapDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class UsageLedger:
    """Reads past spend and answers whether more is allowed."""

    config: AppConfig
    spent: Spent = field(default_factory=Spent)
    #: What this run has spent so far, added to `spent` when checking a cap.
    searches_this_run: int = 0
    youtube_searches_this_run: int = 0
    #: Everything that was cut short, for the job summary.
    skipped: list[str] = field(default_factory=list)
    #: The subset of `skipped` caused by a budget.yaml cap.
    cap_breaches: list[str] = field(default_factory=list)

    @classmethod
    def from_store(
        cls, config: AppConfig, store: SheetStore | None, *, on: date | None = None
    ) -> UsageLedger:
        """Build a ledger from the `runs` tab. An unreachable tab means an empty ledger."""
        ledger = cls(config=config)
        if store is None:
            return ledger
        day = on or local_today(config.settings.run.local_utc_offset_hours)
        try:
            rows = store.read_rows("runs")
        except Exception as error:
            log.warning("budget.runs_unreadable", extra={"error_type": type(error).__name__})
            return ledger

        spent = Spent(runs_read=len(rows))
        undated = 0
        for row in rows:
            stats = RunStats.from_sheet_row(row)
            started = stats.started_at
            if started is None:
                # Spend that cannot be attributed to a day or a month is not counted
                # against either. Guessing "today" would let one corrupt row silently
                # halt collection for the day, which is far harder to diagnose than a
                # slightly under-counted total.
                undated += 1
                continue
            started_date = started.date()
            same_month = (started_date.year, started_date.month) == (day.year, day.month)
            same_day = started_date == day
            if not same_month:
                continue

            calls = stats.api_calls_by_service or {}
            searches = int(calls.get(SERVICE_WEB_SEARCH, 0) or 0)
            youtube = int(calls.get(SERVICE_YOUTUBE_SEARCH, 0) or 0)

            spent.searches_this_month += searches
            spent.youtube_searches_this_month += youtube
            spent.usd_this_month += float(stats.est_cost_usd or 0.0)
            if same_day:
                spent.searches_today += searches
                spent.youtube_searches_today += youtube

        if undated:
            log.warning("budget.undated_runs", extra={"rows": undated})

        ledger.spent = spent
        log.info(
            "budget.loaded",
            extra={
                "runs_read": spent.runs_read,
                "searches_today": spent.searches_today,
                "searches_this_month": spent.searches_this_month,
                "usd_this_month": round(spent.usd_this_month, 4),
            },
        )
        return ledger

    # --- questions -------------------------------------------------------------------

    def allows_web_searches(self, count: int = 1) -> CapDecision:
        """Whether `count` more Claude web searches are within every cap."""
        budget = self.config.budget
        today_total = self.spent.searches_today + self.searches_this_run + count
        month_total = self.spent.searches_this_month + self.searches_this_run + count

        if today_total > budget.daily_caps.claude_web_searches:
            return CapDecision(
                False,
                f"daily Claude web search cap reached "
                f"({self.spent.searches_today + self.searches_this_run} used of "
                f"{budget.daily_caps.claude_web_searches})",
            )
        if month_total > budget.monthly_caps.claude_web_searches:
            return CapDecision(
                False,
                f"monthly Claude web search cap reached "
                f"({self.spent.searches_this_month + self.searches_this_run} used of "
                f"{budget.monthly_caps.claude_web_searches})",
            )
        if self.spent.usd_this_month >= budget.monthly_caps.claude_usd:
            return CapDecision(
                False,
                f"monthly Claude spend cap reached "
                f"(${self.spent.usd_this_month:.2f} of "
                f"${budget.monthly_caps.claude_usd:.2f} estimated)",
            )
        return CapDecision(True)

    def allows_spend(self, estimate_usd: float) -> CapDecision:
        """Whether a stage estimated to cost `estimate_usd` fits the monthly Claude cap."""
        cap = self.config.budget.monthly_caps.claude_usd
        if self.spent.usd_this_month + estimate_usd > cap:
            return CapDecision(
                False,
                f"monthly Claude spend cap would be exceeded "
                f"(${self.spent.usd_this_month:.2f} spent + ${estimate_usd:.2f} estimated, "
                f"cap ${cap:.2f})",
            )
        return CapDecision(True)

    def remaining_web_searches(self) -> int:
        """How many more searches are allowed right now, across every cap."""
        budget = self.config.budget
        used_today = self.spent.searches_today + self.searches_this_run
        used_month = self.spent.searches_this_month + self.searches_this_run
        if self.spent.usd_this_month >= budget.monthly_caps.claude_usd:
            return 0
        return max(
            0,
            min(
                budget.daily_caps.claude_web_searches - used_today,
                budget.monthly_caps.claude_web_searches - used_month,
            ),
        )

    def record_web_searches(self, count: int) -> None:
        self.searches_this_run += max(0, count)

    def remaining_youtube_searches(self) -> int:
        """YouTube search.list calls still allowed today and this month."""
        budget = self.config.budget
        used_today = self.spent.youtube_searches_today + self.youtube_searches_this_run
        used_month = self.spent.youtube_searches_this_month + self.youtube_searches_this_run
        return max(
            0,
            min(
                budget.daily_caps.youtube_searches - used_today,
                budget.monthly_caps.youtube_searches - used_month,
            ),
        )

    def record_youtube_searches(self, count: int) -> None:
        self.youtube_searches_this_run += max(0, count)

    def note_skip(self, what: str, reason: str, *, budget_cap: bool = True) -> None:
        """Record that `what` stopped early. `budget_cap=False` for non-budget limits."""
        message = f"{what}: {reason}"
        self.skipped.append(message)
        if budget_cap:
            self.cap_breaches.append(message)
        log.warning(
            "budget.skipped", extra={"what": what, "reason": reason, "budget_cap": budget_cap}
        )

    @property
    def fail_on_cap(self) -> bool:
        return self.config.budget.on_cap_exceeded == "fail"


@dataclass
class CapLine:
    """One cap, with how much of it is used."""

    name: str
    used: float
    cap: float
    money: bool = False

    @property
    def fraction(self) -> float:
        return self.used / self.cap if self.cap else 0.0

    def text(self) -> str:
        if self.money:
            figures = f"${self.used:.2f} of ${self.cap:.2f}"
        else:
            figures = f"{int(self.used)} of {int(self.cap)}"
        return f"{self.name:<28} {figures} ({self.fraction:.0%})"


#: Above this share of a cap, the budget report raises a warning.
WARN_FRACTION = 0.8


def cap_lines(ledger: UsageLedger) -> list[CapLine]:
    """Month-to-date and today's use against every cap in budget.yaml."""
    budget = ledger.config.budget
    spent = ledger.spent
    return [
        CapLine("Claude spend, this month", spent.usd_this_month,
                budget.monthly_caps.claude_usd, money=True),
        CapLine("Web searches, this month", spent.searches_this_month,
                budget.monthly_caps.claude_web_searches),
        CapLine("Web searches, today", spent.searches_today,
                budget.daily_caps.claude_web_searches),
        CapLine("YouTube searches, this month", spent.youtube_searches_this_month,
                budget.monthly_caps.youtube_searches),
        CapLine("YouTube searches, today", spent.youtube_searches_today,
                budget.daily_caps.youtube_searches),
    ]
