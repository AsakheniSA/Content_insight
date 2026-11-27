"""The pipeline's housekeeping: plan, purge, merging collect figures, and the report."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeSearchProvider, FakeSpreadsheet
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.cli import main
from tula_insights.config import load_config
from tula_insights.models import RunStats
from tula_insights.search.provider import SearchResponse, SearchResult
from tula_insights.stages import collect, ops
from tula_insights.store.sheets import SheetStore


@pytest.fixture
def config(config_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    return load_config()


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


# --- plan ----------------------------------------------------------------------------


def test_plan_lists_the_enabled_sources(config):
    assert ops.plan(config) == ["web_questions", "manual", "youtube", "news_rss"]
    assert ops.plan(config, source="youtube") == ["youtube"]


def test_plan_hands_the_matrix_to_actions(config, tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert main(["plan", "--source", "manual"]) == 0
    text = out.read_text()
    assert 'sources=["manual"]' in text and "has_sources=true" in text


def test_plan_refuses_a_disabled_source(config):
    assert main(["plan", "--source", "reddit_search"]) == 1


# --- collect in a pipeline job -------------------------------------------------------


@pytest.fixture
def wired(monkeypatch, store):
    provider = FakeSearchProvider([SearchResponse(
        results=[SearchResult(url="https://example.com/q", title="A question?", query="q")],
        search_count=3,
    )])
    monkeypatch.setattr(collect, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(collect, "_build_search", lambda config, result: (None, provider))
    return provider


def test_a_pipeline_collect_job_writes_figures_to_a_file_not_the_sheet(config, store, wired, tmp_path):
    path = tmp_path / "web_questions.json"
    collect.run(config, source="web_questions", run_id="gha-1-1", purge=False,
                run_stats_out=str(path))
    assert store.read_rows("runs") == [], "parallel jobs must not race to create the row"
    stats = RunStats.model_validate_json(path.read_text())
    assert stats.run_id == "gha-1-1"
    assert stats.items_collected_by_source == {"web_questions": 1}
    assert stats.api_calls_by_service["claude_web_search"] == 3


def test_figures_are_written_even_when_the_source_fails(config, store, monkeypatch, tmp_path):
    from tula_insights.collectors.base import CollectorError
    from tula_insights.search.provider import SearchError

    monkeypatch.setattr(collect, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(collect, "_build_search", lambda config, result: (
        None, FakeSearchProvider(error=SearchError("down"))))
    path = tmp_path / "s.json"
    with pytest.raises(CollectorError):
        collect.run(config, source="web_questions", run_id="r", run_stats_out=str(path))
    assert RunStats.model_validate_json(path.read_text()).status == "partial"


def test_a_dry_run_writes_no_figures(config, store, wired, tmp_path):
    path = tmp_path / "s.json"
    collect.run(config, source="web_questions", dry_run=True, run_stats_out=str(path))
    assert not path.exists()


def test_no_purge_leaves_expired_rows_alone(config, store, wired):
    old = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.append_rows("items", [{"item_id": "yt", "source": "youtube", "title": "t",
                                 "collected_at": old, "status": "classified"}])
    collect.run(config, source="web_questions", purge=False)
    assert next(r for r in store.read_rows("items") if r["item_id"] == "yt")["title"] == "t"


# --- merging the figures, purge, report ----------------------------------------------


def stats_file(tmp_path, name, **fields):
    path = tmp_path / f"{name}.json"
    path.write_text(RunStats(run_id="gha-9-1", **fields).model_dump_json())
    return path


def test_every_collect_job_lands_in_one_runs_row(config, store, tmp_path):
    files = [
        stats_file(tmp_path, "web", items_collected_by_source={"web_questions": 150},
                   api_calls_by_service={"claude_web_search": 18}, est_cost_usd=0.2),
        stats_file(tmp_path, "yt", items_collected_by_source={"youtube": 244},
                   api_calls_by_service={"youtube_search": 6}),
    ]
    assert ops.record_collect_stats(config, files, store=store) == 2
    rows = store.read_rows("runs")
    assert len(rows) == 1
    stats = RunStats.from_sheet_row(rows[0])
    assert stats.items_collected_by_source == {"web_questions": 150, "youtube": 244}
    assert stats.api_calls_by_service == {"claude_web_search": 18, "youtube_search": 6}


def test_no_figures_is_not_an_error(config, store, tmp_path):
    assert ops.record_collect_stats(config, [tmp_path / "missing.json"], store=store) == 0


def test_purge_runs_both_purges(config, store):
    old = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.append_rows("items", [{"item_id": "yt", "source": "youtube", "title": "t",
                                 "collected_at": old, "status": "classified"}])
    store.append_rows("feed", [{"feed_date": "2026-10-01", "rank": 1, "insight_id": "i",
                                "evidence_urls": "https://youtu.be/x", "served_at": old}])
    assert ops.purge(config, store=store) == {"items_purged": 1, "feed_rows_links_purged": 1}


def test_the_report_summarises_the_whole_run(config, store, tmp_path, monkeypatch):
    store.append_rows("runs", [RunStats(
        run_id="gha-9-1", status="partial", inputs={"collect": {}, "publish": {}},
        items_collected_by_source={"youtube": 244}, items_classified=172, served_today=6,
        est_cost_usd=0.25, errors=["youtube: search failed"],
    ).to_sheet_row()])
    summary_path = tmp_path / "run_summary.json"
    monkeypatch.setenv("TULA_RUN_SUMMARY", str(summary_path))
    lines, summary = ops.report(config, run_id="gha-9-1", store=store)
    ops.write_report(lines, summary)
    text = "\n".join(lines)
    assert "served today     6" in text and "youtube" in text and "partial" in text
    data = json.loads(summary_path.read_text())
    assert data["served_today"] == 6 and data["items_collected"] == 244
    assert data["stages"] == ["collect", "publish"]


def test_a_run_with_no_row_says_so(config, store):
    lines, summary = ops.report(config, run_id="never", store=store)
    assert summary["recorded"] is False and "nothing was written" in lines[0]


# --- the backup schedule's check -----------------------------------------------------


def full_run(store, run_id, *, started, status="success",
             stages=("collect", "classify", "score", "publish")):
    store.append_rows("runs", [RunStats(
        run_id=run_id, started_at=started, status=status,
        inputs={s: {} for s in stages},
    ).to_sheet_row()])


MORNING = datetime(2026, 11, 27, 3, 20, tzinfo=UTC)  # 05:20 SAST


def test_a_full_run_today_means_the_backup_stops(config, store):
    full_run(store, "gha-1-1", started=MORNING)
    assert ops.full_run_done_today(config, store=store, now=MORNING + timedelta(hours=3)) == "gha-1-1"


def test_a_partial_run_counts_too(config, store):
    full_run(store, "gha-2-1", started=MORNING, status="partial")
    assert ops.full_run_done_today(config, store=store, now=MORNING) == "gha-2-1"


@pytest.mark.parametrize("kwargs", [
    {"status": "failed"},
    {"stages": ("collect", "classify")},
    {"started": datetime(2026, 11, 26, 21, 59, tzinfo=UTC)},  # 23:59 SAST yesterday
])
def test_anything_less_than_todays_full_run_lets_the_backup_go_ahead(config, store, kwargs):
    full_run(store, "x", **{"started": MORNING, **kwargs})
    assert ops.full_run_done_today(config, store=store, now=MORNING + timedelta(hours=5)) == ""


def test_the_day_turns_over_at_midnight_sast(config, store):
    full_run(store, "late", started=datetime(2026, 11, 26, 22, 30, tzinfo=UTC))  # 00:30 SAST on the 27th
    assert ops.full_run_done_today(config, store=store, now=MORNING) == "late"


def test_the_check_hands_skip_to_later_jobs(config, store, tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(ops, "open_store", lambda config, dry_run=False: store)
    full_run(store, "gha-3-1", started=datetime.now(UTC))
    assert main(["already-ran"]) == 0
    assert "skip=true" in out.read_text()


def test_an_unreadable_sheet_lets_the_run_go_ahead(config, tmp_path, monkeypatch):
    from tula_insights.store.sheets import SheetStoreError

    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def broken(config, dry_run=False):
        raise SheetStoreError("down")

    monkeypatch.setattr(ops, "open_store", broken)
    assert main(["already-ran"]) == 0
    assert "skip=false" in out.read_text()
