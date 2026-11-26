"""The pipeline's housekeeping: the steps around the four stages.

* :func:`plan` — which sources this run collects from, as the collect job's matrix.
* :func:`purge` — the platform storage purge, run once per pipeline run rather than
  once per parallel collect job.
* :func:`record_collect_stats` — merges the figures each parallel collect job wrote to
  a file into the run's single `runs` row. Parallel jobs writing the row directly could
  both see no row and both append one.
* :func:`report` — one combined summary of the whole run, read back from its `runs`
  row, for the job summary and the counts-only `run_summary.json` artifact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..collectors.registry import plan_sources
from ..config import AppConfig
from ..models import RunStats
from ..schema import RUNS
from ..store.sheets import SheetStore, open_store
from ..utils.logging import get_logger
from .collect import make_run_id
from .retention import purge_expired, purge_expired_feed_links
from .runs import record_run

log = get_logger(__name__)


def plan(config: AppConfig, *, source: str = "all") -> list[str]:
    return [s.name for s in plan_sources(config, source=source)]


def write_github_output(values: dict[str, str]) -> None:
    """Hand values to later jobs through $GITHUB_OUTPUT, when running in Actions."""
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def purge(
    config: AppConfig, *, dry_run: bool = False, store: SheetStore | None = None
) -> dict[str, int]:
    store = store or open_store(config, dry_run=dry_run)
    return {
        "items_purged": purge_expired(store, config),
        "feed_rows_links_purged": purge_expired_feed_links(store, config),
    }


def record_collect_stats(
    config: AppConfig, paths: list[Path], *, store: SheetStore | None = None
) -> int:
    """Merge each collect job's stats file into the run's row. Returns files recorded."""
    files = [p for p in paths if p.is_file()]
    if not files:
        return 0
    store = store or open_store(config)
    for path in sorted(files):
        stats = RunStats.model_validate_json(path.read_text(encoding="utf-8"))
        record_run(store, stats)
    log.info("ops.collect_stats_recorded", extra={"files": len(files)})
    return len(files)


def report(
    config: AppConfig, *, run_id: str | None = None, store: SheetStore | None = None
) -> tuple[list[str], dict[str, object]]:
    """The whole run, summarised from its runs row. Counts only."""
    run_id = run_id or make_run_id()
    store = store or open_store(config, dry_run=True)
    row = next((r for r in store.read_rows(RUNS.name) if r.get("run_id") == run_id), None)
    if row is None:
        lines = [f"Run {run_id}: no runs row — nothing was written (a dry run, or nothing ran)."]
        return lines, {"run_id": run_id, "recorded": False}

    stats = RunStats.from_sheet_row(row)
    by_source = stats.items_collected_by_source or {}
    budget = config.budget
    summary: dict[str, object] = {
        "run_id": run_id,
        "recorded": True,
        "status": stats.status,
        "stages": sorted(stats.inputs),
        "items_collected_by_source": by_source,
        "items_collected": sum(by_source.values()),
        "items_classified": stats.items_classified,
        "items_irrelevant": stats.items_irrelevant,
        "new_insights": stats.new_insights,
        "merged_into_existing": stats.merged_into_existing,
        "llm_judge_calls": stats.llm_judge_calls,
        "served_today": stats.served_today,
        "tokens_in": stats.tokens_in,
        "tokens_out": stats.tokens_out,
        "tokens_cache_read": stats.tokens_cache_read,
        "est_cost_usd": round(stats.est_cost_usd, 4),
        "api_calls_by_service": stats.api_calls_by_service,
        "warnings": len(stats.errors),
        "monthly_claude_cap_usd": budget.monthly_caps.claude_usd,
    }
    lines = [
        f"Pipeline run {run_id} — {stats.status}",
        f"  stages run       {', '.join(summary['stages']) or '(none)'}",
        "",
        "  Collected by source:",
        *[f"    {name:<16} {count}" for name, count in sorted(by_source.items())],
        "",
        f"  classified       {stats.items_classified} ({stats.items_irrelevant} irrelevant)",
        f"  new insights     {stats.new_insights}",
        f"  merged           {stats.merged_into_existing} ({stats.llm_judge_calls} judge calls)",
        f"  served today     {stats.served_today}",
        f"  tokens           {stats.tokens_in} in / {stats.tokens_out} out "
        f"/ {stats.tokens_cache_read} cache reads",
        f"  est. cost        ${stats.est_cost_usd:.4f}",
    ]
    if stats.errors:
        lines += ["", f"  Warnings and errors ({len(stats.errors)}):"]
        lines += [f"    - {e}" for e in stats.errors]
    return lines, summary


def write_report(lines: list[str], summary: dict[str, object]) -> None:
    """Job summary and, if asked for, the counts-only run_summary.json."""
    step = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if step:
        with open(step, "a", encoding="utf-8") as handle:
            handle.write("### Pipeline run\n\n```\n" + "\n".join(lines) + "\n```\n")
    path = os.environ.get("TULA_RUN_SUMMARY", "").strip()
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
