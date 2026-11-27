"""The publish stage: today's ranked feed for the content team.

**What it serves.** Up to `daily_feed_size` insights a day (6 = 3 posts x 2 spare), the
highest-scoring *eligible* ones, subject to:

* at most `max_per_category` from any one category;
* between `min_general_pet_care` and `max_general_pet_care` from general pet care, so
  the travel campaign doesn't crowd out everyday care (or the reverse);
* nothing below `min_total_score` — on a thin day it serves fewer, not weaker.

**Eligible** means active, never served — or served before, but with new evidence since
(`refreshed_at` after it was last served) *and* past the `cooldown_days`.

**No double-serving.** The day's feed is capped, not just each run: a second run on the
same day only fills slots the first left empty, so running twice serves nothing new.
When an insight was last served is read from the feed itself as well as from the
insight, so a run that crashes between writing the feed and marking the insights
cannot cause a repeat later.

**Angles.** Each served insight gets a one-line `suggested_angle` — a direction for a
post, never a claim (see llm/angle.py). If writing one fails, the insight is still
served, with an empty angle and a warning.

The consumer reads only today's rows of `feed`. Reviewers own its `status`,
`rejection_reason`, `post_ref` and `reviewed_by`; this stage writes them blank once, on
creation, and never again.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..budget import SERVICE_MESSAGES, UsageLedger
from ..config import AppConfig, ConfigError, PublishConfig
from ..llm.angle import AngleWriter
from ..llm.classifier import ClassificationInvalid
from ..llm.client import LlmClient, LlmError
from ..models import FeedRow, Insight, ItemRow, RunStats
from ..schema import FEED, INSIGHTS, ITEMS
from ..store.sheets import SheetStore, SheetStoreError, open_store
from ..utils.clock import now_utc, today
from ..utils.logging import get_logger
from .collect import make_run_id
from .runs import record_run

log = get_logger(__name__)

GENERAL = "general_pet_care"


class PublishError(RuntimeError):
    """The stage could not run at all."""


@dataclass
class PublishResult:
    run_id: str
    feed_date: str
    dry_run: bool = False
    #: Budget caps that stopped part of the stage (budget.yaml caps only).
    cap_breaches: list[str] = field(default_factory=list)
    already_today: int = 0
    eligible: int = 0
    served: int = 0
    angles_written: int = 0
    angles_failed: int = 0
    #: Angles lost to Claude API errors (as opposed to an angle failing our checks).
    api_errors: int = 0
    est_cost_usd: float = 0.0
    served_rows: list[tuple[int, str, float]] = field(default_factory=list)  # rank, cat, score
    warnings: list[str] = field(default_factory=list)


# --- the rules, as pure functions ----------------------------------------------------


def last_served(insights: dict[str, Insight], feed: list[FeedRow]) -> dict[str, datetime]:
    """When each insight was last served, from the feed and the insight, whichever is later."""
    out: dict[str, datetime] = {}
    for insight_id, insight in insights.items():
        if insight.last_served_at is not None:
            out[insight_id] = insight.last_served_at
    for row in feed:
        if row.served_at is None:
            continue
        if row.insight_id not in out or row.served_at > out[row.insight_id]:
            out[row.insight_id] = row.served_at
    return out


def is_eligible(
    insight: Insight,
    *,
    served_at: datetime | None,
    served_today: set[str],
    cfg: PublishConfig,
    now: datetime,
) -> bool:
    if insight.status != "active" or not insight.canonical_question.strip():
        return False
    if insight.insight_id in served_today or insight.total_score < cfg.min_total_score:
        return False
    if served_at is None:
        return True
    refreshed = insight.refreshed_at is not None and insight.refreshed_at > served_at
    cooled = now - served_at >= timedelta(days=cfg.cooldown_days)
    return refreshed and cooled


def select(
    candidates: Iterable[Insight],
    *,
    slots: int,
    already: Counter[str],
    cfg: PublishConfig,
) -> list[Insight]:
    """Pick up to `slots` insights, best first, within the category rules.

    `already` counts categories served earlier today, so the rules hold for the day,
    not just for this run.
    """
    ranked = sorted(candidates, key=lambda i: (-i.total_score, -i.item_count, i.insight_id))
    counts = Counter(already)
    picked: list[Insight] = []

    def fits(insight: Insight) -> bool:
        if counts[insight.category] >= cfg.max_per_category:
            return False
        return not (insight.category == GENERAL and counts[GENERAL] >= cfg.max_general_pet_care)

    def take(insight: Insight) -> None:
        picked.append(insight)
        counts[insight.category] += 1

    # Reserve the general-pet-care minimum first, so the best travel insights can't
    # use up every slot before any general care is considered.
    need = max(0, cfg.min_general_pet_care - counts[GENERAL])
    for insight in ranked:
        if need == 0 or len(picked) >= slots:
            break
        if insight.category == GENERAL and fits(insight):
            take(insight)
            need -= 1
    for insight in ranked:
        if len(picked) >= slots:
            break
        if insight not in picked and fits(insight):
            take(insight)
    return sorted(picked, key=lambda i: (-i.total_score, -i.item_count, i.insight_id))


# --- the stage -----------------------------------------------------------------------


def run(
    config: AppConfig,
    *,
    dry_run: bool = False,
    run_id: str | None = None,
    store: SheetStore | None = None,
    llm: LlmClient | None = None,
    now: datetime | None = None,
    feed_date: date | None = None,
) -> PublishResult:
    cfg = config.scoring.publish
    now = now or now_utc()
    day = feed_date or today(config.settings.run.local_utc_offset_hours)
    result = PublishResult(
        run_id=run_id or make_run_id(), feed_date=day.isoformat(), dry_run=dry_run
    )

    if store is None:
        try:
            store = open_store(config, dry_run=dry_run)
        except (ConfigError, SheetStoreError) as error:
            raise PublishError(f"Cannot open the spreadsheet: {error}") from error
    if llm is None and config.env.anthropic_api_key:
        llm = LlmClient(config.env.anthropic_api_key, config.budget)
    writer = AngleWriter(cfg.angle_model, max_chars=cfg.angle_max_chars) if llm else None
    if writer is None:
        result.warnings.append("ANTHROPIC_API_KEY is not set: insights are served without angles.")

    insights = {
        i.insight_id: i
        for i in (Insight.from_sheet_row(r) for r in store.read_rows(INSIGHTS.name))
        if i.insight_id
    }
    feed = [FeedRow.from_sheet_row(r) for r in store.read_rows(FEED.name)]
    todays = [f for f in feed if f.feed_date == result.feed_date]
    result.already_today = len(todays)
    slots = max(0, cfg.daily_feed_size - len(todays))

    served_at = last_served(insights, feed)
    served_today = {f.insight_id for f in todays}
    eligible = [
        i for i in insights.values()
        if is_eligible(i, served_at=served_at.get(i.insight_id), served_today=served_today,
                       cfg=cfg, now=now)
    ]
    result.eligible = len(eligible)
    if slots == 0:
        log.info("publish.day_full", extra={"feed_date": result.feed_date})
        return _finish(store, config, result, llm, dry_run)

    chosen = select(eligible, slots=slots, already=Counter(f.category for f in todays), cfg=cfg)
    if not chosen:
        result.warnings.append("No eligible insight scored high enough to serve today.")
        return _finish(store, config, result, llm, dry_run)

    if writer is not None and not dry_run:
        ledger = UsageLedger.from_store(config, store)
        decision = ledger.allows_spend(0.01 * len(chosen))
        if not decision:
            result.warnings.append(f"Angles skipped: {decision.reason}")
            result.cap_breaches.append(f"angles: {decision.reason}")
            writer = None

    pains = _pain_points(store, chosen)
    rows: list[FeedRow] = []
    insight_updates: dict[str, dict[str, object]] = {}
    for offset, insight in enumerate(chosen):
        angle = _angle(writer, llm, insight, pains.get(insight.insight_id, []), result)
        rank = len(todays) + offset + 1
        rows.append(FeedRow(
            feed_date=result.feed_date, rank=rank, insight_id=insight.insight_id,
            question=insight.canonical_question, category=insight.category, tags=insight.tags,
            suggested_angle=angle, evidence_urls=insight.evidence_urls,
            total_score=insight.total_score, served_at=now, status="served",
        ))
        update: dict[str, object] = {
            "last_served_at": now,
            "times_served": insight.times_served + 1,
        }
        if angle:
            update.update({"suggested_angle": angle, "angle_generated_at": now})
        insight_updates[insight.insight_id] = update
        result.served_rows.append((rank, insight.category, round(insight.total_score, 3)))
    result.served = len(rows)

    if not dry_run:
        # Feed first: if the run dies before the insights are marked, the feed itself
        # still records the serving, and last_served() reads it.
        store.append_rows(FEED.name, [r.to_sheet_row() for r in rows])
        store.update_rows(INSIGHTS.name, insight_updates)
    return _finish(store, config, result, llm, dry_run)


def _angle(writer, llm, insight: Insight, pains: list[str], result: PublishResult) -> str:
    if writer is None or llm is None:
        return ""
    problem = None
    reason = "unknown"
    for _attempt in range(2):
        try:
            reply = llm.create(**writer.request_params(insight, pain_points=pains, problem=problem))
            angle = writer.parse(reply)
            result.angles_written += 1
            return angle
        except ClassificationInvalid as error:
            # The reason text is ours ("it contained a number", "it was 190 characters"),
            # never the angle or the question, so it is safe in a public log.
            reason = str(error)
            if not error.retryable:
                break
            problem = reason
        except LlmError as error:
            reason = f"the API call failed: {str(error)[:200]}"
            result.api_errors += 1
            break
    result.angles_failed += 1
    result.warnings.append(
        f"Rank {len(result.served_rows) + 1} ({insight.category}) was served without an "
        f"angle: {reason}."
    )
    return ""


def _pain_points(store: SheetStore, chosen: list[Insight]) -> dict[str, list[str]]:
    wanted = {i.insight_id for i in chosen}
    out: dict[str, list[str]] = {}
    for row in store.read_rows(ITEMS.name):
        item = ItemRow.from_sheet_row(row)
        if item.insight_id in wanted and item.pain_point:
            bucket = out.setdefault(item.insight_id, [])
            if item.pain_point not in bucket:
                bucket.append(item.pain_point)
    return out


def _finish(
    store: SheetStore, config: AppConfig, result: PublishResult, llm, dry_run: bool
) -> PublishResult:
    if llm is not None:
        result.est_cost_usd = llm.usage.estimate_usd(config.budget)
    _write_summary_file(result)
    if dry_run:
        return result
    usage = llm.usage if llm is not None else None
    stats = RunStats(
        run_id=result.run_id,
        trigger="schedule" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "dispatch",
        inputs={"publish": {"feed_date": result.feed_date}},
        status="partial" if result.warnings else "success",
        served_today=result.served,
        tokens_in=usage.tokens_in if usage else 0,
        tokens_out=usage.tokens_out if usage else 0,
        tokens_cache_read=usage.tokens_cache_read if usage else 0,
        est_cost_usd=result.est_cost_usd,
        api_calls_by_service=(
            {SERVICE_MESSAGES: sum(usage.calls_by_model.values())} if usage else {}
        ),
        git_sha=os.environ.get("GITHUB_SHA", "")[:12],
        errors=[w[:200] for w in result.warnings[:10]],
        finished_at=now_utc(),
    )
    try:
        record_run(store, stats)
    except SheetStoreError as error:
        result.warnings.append(f"Could not write the runs row: {error}")
    return result


def _write_summary_file(result: PublishResult) -> None:
    """Counts-only JSON for the workflow's artifact, when the workflow asks for it."""
    path = os.environ.get("TULA_RUN_SUMMARY", "").strip()
    if not path:
        return
    summary = {
        "run_id": result.run_id,
        "feed_date": result.feed_date,
        "dry_run": result.dry_run,
        "already_today": result.already_today,
        "eligible": result.eligible,
        "served": result.served,
        "angles_written": result.angles_written,
        "angles_failed": result.angles_failed,
        "by_category": dict(Counter(cat for _, cat, _ in result.served_rows)),
        "est_cost_usd": result.est_cost_usd,
        "warnings": len(result.warnings),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)


# --- reporting -----------------------------------------------------------------------


def summary_lines(result: PublishResult) -> list[str]:
    """Counts, categories and scores only — the feed's text stays in the sheet."""
    suffix = " (dry run — nothing written)" if result.dry_run else ""
    lines = [
        f"Publish run {result.run_id} for {result.feed_date}{suffix}", "",
        f"  already served today  {result.already_today}",
        f"  eligible insights     {result.eligible}",
        f"  served now            {result.served}",
        f"  angles written        {result.angles_written} (failed: {result.angles_failed})",
        f"  est. cost             ${result.est_cost_usd:.4f}",
    ]
    if result.served_rows:
        lines += ["", "  Served (rank, category, score):"]
        lines += [f"    {r:>2}  {cat:<32} {score:.3f}" for r, cat, score in result.served_rows]
    if result.warnings:
        lines += ["", "  Warnings:"] + [f"    - {w}" for w in result.warnings[:10]]
    return lines


def write_job_summary(result: PublishResult) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    body = "\n".join(["```", *summary_lines(result), "```", ""])
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"### Publish — {result.feed_date}\n\n{body}\n")
    except OSError:  # pragma: no cover
        pass
