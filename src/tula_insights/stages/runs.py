"""One `runs` row per pipeline run, shared by every stage of it.

In the scheduled pipeline (stage 6) collect, classify, score and publish run as
separate jobs under one run id. Each calls :func:`record_run` with what it did; the
first creates the row and the rest add to it — counts and costs are summed, errors
appended, the worst status kept. Run a stage on its own and it simply gets its own row.

Known limit, for stage 6: two jobs recording at the same instant could both see no
row and both append. The collect matrix will hand its counts to a single later job
rather than recording in parallel.
"""

from __future__ import annotations

from ..models import RunStats
from ..schema import RUNS
from ..store.sheets import SheetStore
from ..utils.logging import get_logger

log = get_logger(__name__)

_STATUS_RANK = {"running": 0, "success": 1, "partial": 2, "failed": 3}
MAX_ERRORS = 10


def _sum_dicts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for key, value in b.items():
        out[key] = int(out.get(key, 0) or 0) + int(value or 0)
    return out


def merge(old: RunStats, new: RunStats) -> RunStats:
    """Combine two stages' records of the same run."""
    starts = [t for t in (old.started_at, new.started_at) if t is not None]
    finishes = [t for t in (old.finished_at, new.finished_at) if t is not None]
    status = max((old.status, new.status), key=lambda s: _STATUS_RANK.get(s, 0))
    return RunStats(
        run_id=old.run_id,
        trigger=old.trigger or new.trigger,
        inputs={**old.inputs, **new.inputs},
        started_at=min(starts) if starts else None,
        finished_at=max(finishes) if finishes else None,
        status=status,
        items_collected_by_source=_sum_dicts(
            old.items_collected_by_source, new.items_collected_by_source
        ),
        items_classified=old.items_classified + new.items_classified,
        items_irrelevant=old.items_irrelevant + new.items_irrelevant,
        new_insights=old.new_insights + new.new_insights,
        merged_into_existing=old.merged_into_existing + new.merged_into_existing,
        llm_judge_calls=old.llm_judge_calls + new.llm_judge_calls,
        served_today=old.served_today + new.served_today,
        tokens_in=old.tokens_in + new.tokens_in,
        tokens_out=old.tokens_out + new.tokens_out,
        tokens_cache_read=old.tokens_cache_read + new.tokens_cache_read,
        est_cost_usd=round(old.est_cost_usd + new.est_cost_usd, 6),
        api_calls_by_service=_sum_dicts(old.api_calls_by_service, new.api_calls_by_service),
        actions_run_url=old.actions_run_url or new.actions_run_url,
        git_sha=old.git_sha or new.git_sha,
        errors=(old.errors + new.errors)[:MAX_ERRORS],
    )


def record_run(store: SheetStore, stats: RunStats) -> None:
    """Create this run's row, or add this stage's figures to it."""
    existing = next(
        (row for row in store.read_rows(RUNS.name) if row.get("run_id") == stats.run_id), None
    )
    if existing is None:
        store.append_rows(RUNS.name, [stats.to_sheet_row()])
        return
    combined = merge(RunStats.from_sheet_row(existing), stats).to_sheet_row()
    combined.pop("run_id")
    store.update_rows(RUNS.name, {stats.run_id: combined})
    log.info("runs.updated", extra={"run_id": stats.run_id})
