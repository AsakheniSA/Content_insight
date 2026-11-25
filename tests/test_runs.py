"""Several stages, one runs row."""

from __future__ import annotations

from datetime import UTC, datetime

from tula_insights.models import RunStats
from tula_insights.stages.runs import MAX_ERRORS, merge

T1 = datetime(2026, 11, 25, 3, 17, tzinfo=UTC)
T2 = datetime(2026, 11, 25, 3, 40, tzinfo=UTC)


def test_counts_and_costs_add_up():
    old = RunStats(run_id="r", tokens_in=100, est_cost_usd=0.2,
                   api_calls_by_service={"claude_web_search": 18})
    new = RunStats(run_id="r", tokens_in=50, est_cost_usd=0.05, items_classified=7,
                   api_calls_by_service={"claude_web_search": 2, "claude_messages": 7})
    merged = merge(old, new)
    assert merged.tokens_in == 150 and merged.est_cost_usd == 0.25
    assert merged.items_classified == 7
    assert merged.api_calls_by_service == {"claude_web_search": 20, "claude_messages": 7}


def test_the_worst_status_wins():
    assert merge(RunStats(run_id="r", status="success"), RunStats(run_id="r", status="partial")).status == "partial"
    assert merge(RunStats(run_id="r", status="failed"), RunStats(run_id="r", status="success")).status == "failed"


def test_the_run_spans_from_first_start_to_last_finish():
    merged = merge(RunStats(run_id="r", started_at=T1, finished_at=T1),
                   RunStats(run_id="r", started_at=T2, finished_at=T2))
    assert merged.started_at == T1 and merged.finished_at == T2


def test_errors_are_appended_and_capped():
    merged = merge(RunStats(run_id="r", errors=["a"] * 8), RunStats(run_id="r", errors=["b"] * 8))
    assert len(merged.errors) == MAX_ERRORS and merged.errors[0] == "a"


def test_each_stage_keeps_its_own_inputs():
    merged = merge(RunStats(run_id="r", inputs={"collect": {"source": "all"}}),
                   RunStats(run_id="r", inputs={"classify": {"mode": "batch"}}))
    assert set(merged.inputs) == {"collect", "classify"}
