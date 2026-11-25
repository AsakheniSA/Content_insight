"""Time handling.

Two rules, both learned the hard way on the Asakheni research agent:

* Everything stored is **UTC**. A bare ``datetime.now()`` gives local time on a laptop
  and UTC on a GitHub runner, so one column ends up holding two timezones two hours
  apart and a row can appear to have been created before it was asked for.
* "Which day is this feed for" is a **local** question. South Africa keeps one offset
  all year, so a fixed +02:00 is exact and needs no timezone database — Windows ships
  none, and the pipeline must not behave differently on a laptop and on GitHub.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta, timezone

#: South African Standard Time. Fixed all year; see the module docstring.
SAST = timezone(timedelta(hours=2))


def now_utc() -> datetime:
    """The current moment, timezone-aware, in UTC."""
    return datetime.now(UTC)


def to_utc(moment: datetime) -> datetime:
    """Convert any datetime to UTC. Naive datetimes are assumed to already be UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def local_now(offset_hours: int = 2) -> datetime:
    """The current moment in the project's local timezone."""
    return datetime.now(timezone(timedelta(hours=offset_hours)))


def today(offset_hours: int = 2) -> date:
    """The local calendar date a feed belongs to.

    ``TULA_TODAY`` (an ISO date) overrides this, for reproducible tests and for
    re-running a day's publish stage deliberately.
    """
    override = os.environ.get("TULA_TODAY", "").strip()
    if override:
        return date.fromisoformat(override)
    return local_now(offset_hours).date()


def days_between(earlier: datetime, later: datetime) -> float:
    """Fractional days from `earlier` to `later`, timezone-safe."""
    return (to_utc(later) - to_utc(earlier)).total_seconds() / 86400.0
