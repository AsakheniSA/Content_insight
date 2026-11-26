"""Budget caps under pressure: a simulated breach, in skip mode and in fail mode."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from fakes import FakeSearchProvider, FakeSpreadsheet
from test_publish import FakeLlm as AngleLlm
from test_publish import insight
from test_score_stage import FakeEmbedder, FakeLlm, add_insight, add_item, at, unit
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.budget import (
    SERVICE_WEB_SEARCH,
    SERVICE_YOUTUBE_SEARCH,
    UsageLedger,
    cap_lines,
)
from tula_insights.cli import main
from tula_insights.config import load_config
from tula_insights.models import RunStats
from tula_insights.search.provider import SearchResponse, SearchResult
from tula_insights.stages import collect, ops
from tula_insights.stages import publish as publish_stage
from tula_insights.stages import score as score_stage
from tula_insights.store.sheets import SheetStore
from tula_insights.utils.actions import annotation


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")


@pytest.fixture
def config(config_dir, env):
    return load_config()


def set_policy(config_dir, policy: str) -> None:
    path = config_dir / "budget.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on_cap_exceeded"] = policy
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def spent_already(store, *, searches=0, usd=0.0, youtube=0, when=None):
    """A runs row for earlier today: the history the ledger reads."""
    when = when or datetime.now(UTC).replace(microsecond=0)
    store.append_rows("runs", [RunStats(
        run_id=f"earlier-{searches}-{usd}-{youtube}", started_at=when, est_cost_usd=usd,
        api_calls_by_service={SERVICE_WEB_SEARCH: searches, SERVICE_YOUTUBE_SEARCH: youtube},
    ).to_sheet_row()])


@pytest.fixture
def wired(monkeypatch, store):
    """collect.run against the fake sheet, with a search provider that always answers."""
    responses = [
        SearchResponse(
            results=[SearchResult(url=f"https://example.com/q{n}", title=f"Question {n}?",
                                  query="q")],
            search_count=3,
        )
        for n in range(10)
    ]
    provider = FakeSearchProvider(responses)
    monkeypatch.setattr(collect, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(collect, "_build_search", lambda config, result: (None, provider))
    return provider


# --- the simulated breach, in collect ------------------------------------------------


def test_a_cap_hit_mid_run_keeps_what_was_collected_and_flags_it(config, store, wired):
    spent_already(store, searches=37)  # daily cap is 40: room for one 3-search request
    result = collect.run(config, source="web_questions", purge=False, run_id="r1")
    assert result.items_by_source["web_questions"] == 1
    assert result.cap_breaches and "daily Claude web search cap" in result.cap_breaches[0]
    row = next(r for r in store.read_rows("runs") if r["run_id"] == "r1")
    assert any(e.startswith("budget: web_questions") for e in RunStats.from_sheet_row(row).errors)


def test_skip_mode_does_not_fail_the_job(config_dir, env, store, wired, capsys):
    spent_already(store, searches=40)
    assert main(["collect", "--source", "web_questions", "--no-purge"]) == 0
    assert "daily Claude web search cap" in capsys.readouterr().out


def test_fail_mode_fails_the_job_after_saving_the_items(config_dir, env, store, wired, capsys,
                                                       monkeypatch):
    set_policy(config_dir, "fail")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    spent_already(store, searches=37)
    assert main(["collect", "--source", "web_questions", "--no-purge"]) == 1
    out, err = capsys.readouterr()
    assert "Budget cap reached" in err
    assert "::error title=Budget cap reached" in out, "the alert job quotes this annotation"
    assert len([r for r in store.read_rows("items") if r["source"] == "web_questions"]) == 1


def test_a_sources_own_cap_is_not_a_budget_breach(config_dir, env, store, wired, monkeypatch):
    set_policy(config_dir, "fail")
    path = config_dir / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = next(s for s in data["sources"] if s["name"] == "web_questions")
    entry["daily_request_cap"] = 3
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["collect", "--source", "web_questions", "--no-purge"]) == 0


def test_the_monthly_spend_cap_stops_web_searches(config, store, wired):
    spent_already(store, usd=25.0)
    result = collect.run(config, source="web_questions", purge=False, run_id="r2")
    assert result.items_by_source["web_questions"] == 0
    assert "monthly Claude spend cap" in result.cap_breaches[0]
    assert wired.requests == []


# --- score and publish ---------------------------------------------------------------


def test_score_without_budget_disables_the_judge_and_flags_it(config, store):
    spent_already(store, usd=24.999)
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Is flying ok for dogs?")
    llm = FakeLlm()
    result = score_stage.run(config, store=store, llm=llm, run_id="r",
                             embedder=FakeEmbedder({"Is flying ok for dogs?": at(0.85)}))
    assert llm.requests == [], "no judge call once the cap would be passed"
    assert result.deferred == 1
    assert result.cap_breaches and result.cap_breaches[0].startswith("judge:")


def test_publish_without_budget_serves_the_feed_without_angles(config, store):
    spent_already(store, usd=25.0)
    store.append_rows("insights", [insight("a", 0.8).to_sheet_row()])
    llm = AngleLlm()
    result = publish_stage.run(config, store=store, llm=llm, run_id="r")
    assert result.served == 1, "the feed still goes out"
    assert llm.requests == []
    assert result.cap_breaches and result.cap_breaches[0].startswith("angles:")


@pytest.mark.parametrize("policy, code", [("skip", 0), ("fail", 1)])
def test_publish_cli_follows_the_policy(config_dir, env, store, monkeypatch, policy, code):
    set_policy(config_dir, policy)
    spent_already(store, usd=25.0)
    store.append_rows("insights", [insight("a", 0.8).to_sheet_row()])
    monkeypatch.setattr(publish_stage, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(publish_stage, "LlmClient", lambda *a, **k: AngleLlm())
    assert main(["publish"]) == code
    assert len(store.read_rows("feed")) == 1


# --- reporting -----------------------------------------------------------------------


def test_the_budget_status_shows_every_cap_and_warns_near_one(config, store):
    spent_already(store, searches=35, usd=3.0, youtube=10)
    lines, warnings = ops.budget_status(config, store=store)
    text = "\n".join(lines)
    assert "$3.00 of $25.00" in text
    assert "35 of 40" in text and "35 of 1500" in text
    assert warnings == ["Web searches, today is at 88% of its cap"]
    assert "skip and flag" in text


def test_cap_lines_cover_every_cap(config):
    names = [c.name for c in cap_lines(UsageLedger(config=config))]
    assert len(names) == 5 and all(c.fraction == 0 for c in cap_lines(UsageLedger(config=config)))


def test_the_report_ends_with_the_budget_and_sheet_size(config, store):
    store.append_rows("runs", [RunStats(run_id="gha-5-1", started_at=datetime.now(UTC),
                                        est_cost_usd=0.21).to_sheet_row()])
    lines, summary = ops.report(config, run_id="gha-5-1", store=store)
    text = "\n".join(lines)
    assert "Budget (budget.yaml caps):" in text and "$0.21 of $25.00" in text
    assert "Sheet size" in text and summary["sheet_cells"] > 0
    assert summary["budget_warnings"] == []


def test_the_budget_command_runs(config_dir, env, store, monkeypatch, capsys):
    monkeypatch.setattr(ops, "open_store", lambda config, dry_run=False: store)
    assert main(["budget"]) == 0
    assert "Claude spend, this month" in capsys.readouterr().out


def test_annotations_are_escaped_scrubbed_and_short():
    line = annotation("error", "Collect failed: x", "line one\nmail me at a@b.co " + "z" * 400)
    assert line.startswith("::error title=Collect failed%3A x::line one mail me at")
    assert "a@b.co" not in line and "\n" not in line
    assert len(line.split("::", 2)[2]) <= 300
