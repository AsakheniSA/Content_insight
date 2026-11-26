"""The collect stage: run the enabled collectors and append what they find.

State lives in the sheet, never on the runner, so this stage is safe to re-run and
safe to run as one job of a parallel matrix. Each collector appends only its own
source's rows, and every row carries a content-hashed `item_id`, so two jobs that find
the same page produce the same id and the duplicate is dropped on the next read rather
than becoming a second row.

A failing source is isolated: it is logged, recorded as a warning on the run, and the
other sources carry on. The stage only fails outright if it cannot reach the sheet or
if every source failed.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from ..budget import (
    SERVICE_MESSAGES,
    SERVICE_WEB_SEARCH,
    SERVICE_YOUTUBE_SEARCH,
    SERVICE_YOUTUBE_UNITS,
    UsageLedger,
)
from ..collectors.base import AfterWrite, CollectorError, RunContext
from ..collectors.registry import build_collector, plan_sources
from ..config import AppConfig, ConfigError
from ..llm.client import LlmClient
from ..models import RawItem, RunStats
from ..schema import ITEMS
from ..search.claude_web_search import ClaudeWebSearchProvider
from ..store.sheets import SheetStore, SheetStoreError, open_store
from ..utils.clock import now_utc
from ..utils.logging import get_logger
from .runs import record_run

log = get_logger(__name__)


@dataclass
class CollectResult:
    run_id: str
    items_by_source: dict[str, int] = field(default_factory=dict)
    written: int = 0
    duplicates: int = 0
    searches: int = 0
    warnings: list[str] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    #: Why each failed source failed, in order. Separate from `warnings`, which also
    #: holds non-fatal notes — so the aggregate error names a real cause.
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    est_cost_usd: float = 0.0
    dry_run: bool = False
    #: Named counts from collectors. Numbers only — this is printed in public logs.
    metrics: dict[str, int] = field(default_factory=dict)

    @property
    def collected(self) -> int:
        return sum(self.items_by_source.values())


def make_run_id() -> str:
    """A run id shared by every job of one pipeline run."""
    actions_run = os.environ.get("GITHUB_RUN_ID", "").strip()
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip()
    if actions_run:
        return f"gha-{actions_run}-{attempt}"
    return f"local-{now_utc():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def run(
    config: AppConfig,
    *,
    source: str = "all",
    category: str = "all",
    dry_run: bool = False,
    run_id: str | None = None,
    purge: bool = True,
    run_stats_out: str | None = None,
) -> CollectResult:
    """Collect from every planned source. Returns a summary; writes rows as a side effect.

    In the scheduled pipeline each source runs as its own parallel job, so two things
    change there: the platform purge runs once in a prepare step instead (`purge=False`),
    and each job writes its run figures to a file (`run_stats_out`) for one final job
    to merge into the run's row, instead of every job racing to create that row.
    """
    run_id = run_id or make_run_id()
    result = CollectResult(run_id=run_id, dry_run=dry_run)

    store = _open_store(config, dry_run=dry_run, result=result)
    ledger = UsageLedger.from_store(config, store)
    if purge:
        _enforce_storage_limits(store, config, result, dry_run=dry_run)

    llm, search = _build_search(config, result)

    sources = plan_sources(config, source=source)
    if not sources:
        result.warnings.append("No enabled sources matched — nothing to collect.")
        return result

    all_items: list[RawItem] = []
    #: Collectors with something to record once their items are saved, and their context.
    hooks: list[tuple[AfterWrite, list[RawItem], RunContext]] = []

    # Item ids are unique across every source, so one read serves them all.
    known = _known_item_ids(store, result)

    for cfg in sources:
        ctx = RunContext(
            run_id=run_id,
            config=config,
            ledger=ledger,
            store=store,
            llm=llm,
            search=search,
            dry_run=dry_run,
            category=category,
            known_item_ids=known,
        )
        try:
            collector = build_collector(cfg.name, ctx)
            items = collector.collect(cfg, ctx)
        except CollectorError as error:
            log.warning("collect.source_failed", extra={"source": cfg.name})
            result.failed_sources.append(cfg.name)
            result.failures.append(f"{cfg.name}: {error}")
            result.warnings.extend(ctx.warnings)
            continue
        except Exception as error:
            log.warning(
                "collect.source_crashed",
                extra={"source": cfg.name, "error_type": type(error).__name__},
            )
            result.failed_sources.append(cfg.name)
            result.failures.append(f"{cfg.name} crashed: {type(error).__name__}: {error}")
            result.warnings.extend(ctx.warnings)
            continue

        result.warnings.extend(ctx.warnings)
        for name, value in ctx.metrics.items():
            result.metrics[name] = result.metrics.get(name, 0) + value
        before = len(items)
        items = _drop_duplicates(items, known)
        result.duplicates += before - len(items)
        result.items_by_source[cfg.name] = len(items)
        all_items.extend(items)
        if isinstance(collector, AfterWrite):
            hooks.append((collector, items, ctx))

    result.skipped = list(ledger.skipped)
    result.searches = ledger.searches_this_run
    if llm is not None:
        result.est_cost_usd = llm.usage.estimate_usd(config.budget)

    result.written = _write_items(store, all_items, dry_run=dry_run)
    # Only reached if the write succeeded — a failure raises before this line. That
    # ordering is what stops a submission being marked processed and then lost.
    if not dry_run:
        for collector, items, ctx in hooks:
            try:
                collector.after_write(items, ctx)
            except Exception as error:
                log.warning(
                    "collect.after_write_failed",
                    extra={"error_type": type(error).__name__},
                )
                result.warnings.append(f"Could not finish recording a source: {error}")
            result.warnings.extend(w for w in ctx.warnings if w not in result.warnings)
    _record_run(store, config, result, llm=llm, dry_run=dry_run, source=source,
                category=category, run_stats_out=run_stats_out)

    if sources and len(result.failed_sources) == len(sources):
        raise CollectorError(
            "Every source failed.\n  "
            + "\n  ".join(result.failures or ["(no reason recorded)"])
        )
    return result


# --- wiring --------------------------------------------------------------------------


def _open_store(config: AppConfig, *, dry_run: bool, result: CollectResult) -> SheetStore | None:
    try:
        return open_store(config, dry_run=dry_run)
    except (ConfigError, SheetStoreError) as error:
        if not dry_run:
            raise
        # A dry run should work on a laptop with no Google credentials. Without the
        # sheet there is no duplicate history, so everything looks new — say so.
        result.warnings.append(
            f"No spreadsheet access, so this dry run cannot tell new items from ones "
            f"already collected: {error}"
        )
        log.warning("collect.no_store_dry_run")
        return None


def _enforce_storage_limits(
    store: SheetStore | None, config: AppConfig, result: CollectResult, *, dry_run: bool
) -> None:
    """Purge rows past a platform's storage limit, before collecting more.

    Runs first so that the limit is enforced on every run, even one whose collection
    later fails. A purge failure is reported loudly but does not stop collection.
    """
    if store is None or dry_run:
        return
    from .retention import purge_expired, purge_expired_feed_links

    try:
        purged = purge_expired(store, config)
        feed_rows = purge_expired_feed_links(store, config)
    except Exception as error:
        log.warning("collect.purge_failed", extra={"error_type": type(error).__name__})
        result.warnings.append(
            f"Could not enforce platform storage limits this run: {error}. "
            f"Expired platform data may still be in the items tab."
        )
        return
    if purged:
        result.metrics["retention.rows_purged"] = purged
    if feed_rows:
        result.metrics["retention.feed_rows_links_purged"] = feed_rows


def _build_search(
    config: AppConfig, result: CollectResult
) -> tuple[LlmClient | None, ClaudeWebSearchProvider | None]:
    if not config.env.anthropic_api_key:
        result.warnings.append(
            "ANTHROPIC_API_KEY is not set, so no search-based collector can run."
        )
        return None, None
    llm = LlmClient(config.env.anthropic_api_key, config.budget)
    return llm, ClaudeWebSearchProvider(llm)


def _known_item_ids(store: SheetStore | None, result: CollectResult) -> set[str]:
    if store is None:
        return set()
    try:
        return store.existing_keys("items")
    except SheetStoreError as error:
        result.warnings.append(f"Could not read existing item ids: {error}")
        return set()


def _drop_duplicates(items: list[RawItem], known: set[str]) -> list[RawItem]:
    seen: set[str] = set()
    kept: list[RawItem] = []
    for item in items:
        identified = item.with_id()
        if identified.item_id in known or identified.item_id in seen:
            continue
        seen.add(identified.item_id)
        kept.append(identified)
    return kept


def _write_items(store: SheetStore | None, items: list[RawItem], *, dry_run: bool) -> int:
    """Append the collected rows. Returns how many were actually written.

    The stage checks `dry_run` itself rather than trusting the store's own flag. Both
    are set from the same CLI option in normal use, but a dry run that writes rows is
    the one failure this stage must never have, so it does not depend on a second
    object being configured correctly.
    """
    if not items or store is None:
        return 0
    if dry_run:
        log.info("collect.dry_run.skip_write", extra={"rows": len(items)})
        return 0
    rows = [item.to_sheet_row() for item in items]
    return store.append_rows(ITEMS.name, rows)


def _record_run(
    store: SheetStore | None,
    config: AppConfig,
    result: CollectResult,
    *,
    llm: LlmClient | None,
    dry_run: bool,
    source: str,
    category: str,
    run_stats_out: str | None = None,
) -> None:
    """Record this run's figures, so the budget ledger can see what it spent.

    Normally straight into the run's row. With `run_stats_out`, into a JSON file
    instead, for the pipeline's final job to merge — written even when the source
    failed, because a failed source may still have spent money. A dry run records
    nothing: it spent nothing and must not move the budget.
    """
    if dry_run or (store is None and not run_stats_out):
        return

    usage = llm.usage if llm is not None else None
    stats = RunStats(
        run_id=result.run_id,
        trigger="schedule" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "dispatch",
        inputs={"collect": {"source": source, "category": category}},
        status="partial" if result.failed_sources else "success",
        items_collected_by_source=dict(result.items_by_source),
        tokens_in=usage.tokens_in if usage else 0,
        tokens_out=usage.tokens_out if usage else 0,
        tokens_cache_read=usage.tokens_cache_read if usage else 0,
        est_cost_usd=result.est_cost_usd,
        api_calls_by_service={
            SERVICE_WEB_SEARCH: result.searches,
            SERVICE_MESSAGES: sum((usage.calls_by_model if usage else {}).values()),
            SERVICE_YOUTUBE_SEARCH: result.metrics.get("youtube.searches", 0),
            SERVICE_YOUTUBE_UNITS: result.metrics.get("youtube.units", 0),
        },
        actions_run_url=_actions_run_url(),
        git_sha=os.environ.get("GITHUB_SHA", "")[:12],
        errors=[line[:200] for line in (result.failures + result.warnings)[:10]],
        finished_at=now_utc(),
    )
    if run_stats_out:
        with open(run_stats_out, "w", encoding="utf-8") as handle:
            handle.write(stats.model_dump_json())
        return
    try:
        record_run(store, stats)
    except SheetStoreError as error:
        log.warning("collect.run_row_failed", extra={"error_type": type(error).__name__})
        result.warnings.append(f"Could not write the runs row: {error}")


def _actions_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""


# --- reporting -----------------------------------------------------------------------


def summary_lines(result: CollectResult) -> list[str]:
    """Human-readable summary. Counts only — no URLs, no titles."""
    lines = [f"Run {result.run_id}{' (dry run — nothing written)' if result.dry_run else ''}", ""]
    if result.items_by_source:
        for name, count in sorted(result.items_by_source.items()):
            lines.append(f"  {name:<20} {count} new item(s)")
    else:
        lines.append("  no items collected")
    lines += [
        "",
        f"  collected     {result.collected}",
        f"  duplicates    {result.duplicates} (already in the sheet)",
        f"  written       {result.written}",
        f"  web searches  {result.searches} (Claude)",
        f"  est. cost     ${result.est_cost_usd:.4f}",
    ]
    if result.metrics:
        lines += ["", "  Measured:"]
        lines += [f"    {name:<40} {value}" for name, value in sorted(result.metrics.items())]
    if result.skipped:
        lines += ["", "  Skipped by a budget cap:"]
        lines += [f"    - {s}" for s in result.skipped]
    if result.failures:
        lines += ["", "  Failed sources:"]
        lines += [f"    - {f}" for f in result.failures]
    if result.warnings:
        lines += ["", "  Warnings:"]
        lines += [f"    - {w}" for w in result.warnings[:10]]
    return lines


def write_job_summary(result: CollectResult) -> None:
    """Append the summary to the GitHub Actions job summary, when running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    rows = "\n".join(
        f"| {name} | {count} |" for name, count in sorted(result.items_by_source.items())
    ) or "| _(none)_ | 0 |"
    body = f"""### Collect — {result.run_id}

| Source | New items |
|---|---|
{rows}

| | |
|---|---|
| Collected | {result.collected} |
| Duplicates skipped | {result.duplicates} |
| Written | {result.written} |
| Claude web searches | {result.searches} |
| Estimated cost | ${result.est_cost_usd:.4f} |
"""
    if result.metrics:
        body += "\n| Measure | Count |\n|---|---|\n" + "".join(
            f"| {name} | {value} |\n" for name, value in sorted(result.metrics.items())
        )
    if result.skipped:
        body += "\n**Budget caps hit**\n" + "".join(f"- {s}\n" for s in result.skipped)
    if result.failed_sources:
        body += f"\n**Failed sources:** {', '.join(result.failed_sources)}\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except OSError as error:  # pragma: no cover
        log.warning("collect.job_summary_failed", extra={"error_type": type(error).__name__})
