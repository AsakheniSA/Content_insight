"""The publish stage: what is eligible, what is chosen, and no double-serving."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest

from fakes import FakeSpreadsheet, Obj
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.config import load_config
from tula_insights.llm.angle import AngleWriter
from tula_insights.llm.classifier import ClassificationInvalid
from tula_insights.llm.client import LlmError, Usage
from tula_insights.models import FeedRow, Insight, RunStats
from tula_insights.stages import publish as stage
from tula_insights.store.sheets import SheetStore

NOW = datetime(2026, 11, 26, 3, 30, tzinfo=UTC)
DAY = date(2026, 11, 26)


def insight(iid, score, category="airline_transport", **kw):
    kw.setdefault("status", "active")
    kw.setdefault("item_count", 1)
    return Insight(insight_id=iid, canonical_question=f"Question {iid}?", category=category,
                   total_score=score, **kw)


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def cfg(config):
    return config.scoring.publish


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


class FakeLlm:
    def __init__(self, angles=None):
        self.angles = list(angles or [])
        self.requests = []
        self.usage = Usage()

    def create(self, **params):
        self.requests.append(params)
        angle = self.angles.pop(0) if self.angles else "Help owners plan ahead with confidence."
        if isinstance(angle, Exception):
            raise angle
        reply = Obj(content=[Obj(type="text", text=json.dumps({"angle": angle}))],
                    stop_reason="end_turn",
                    usage=Obj(input_tokens=400, output_tokens=30, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, server_tool_use=None))
        self.usage.add_response(reply, params["model"])
        return reply


def seed(store, *insights):
    store.append_rows("insights", [i.to_sheet_row() for i in insights])


def run(config, store, llm=None, **kw):
    return stage.run(config, store=store, llm=llm if llm is not None else FakeLlm(),
                     run_id="run-1", now=kw.pop("now", NOW), feed_date=kw.pop("day", DAY), **kw)


def feed(store):
    return [FeedRow.from_sheet_row(r) for r in store.read_rows("feed")]


# --- selection -----------------------------------------------------------------------


def test_the_best_are_chosen_best_first(cfg):
    pool = [insight("a", 0.5, "costs_planning"), insight("b", 0.9, "gear_packing"),
            insight("c", 0.7, "pet_friendly_stays"), insight("g", 0.4, "general_pet_care")]
    chosen = stage.select(pool, slots=3, already=Counter(), cfg=cfg)
    assert [i.insight_id for i in chosen] == ["b", "c", "g"], "one general-care slot is reserved"


def test_no_category_takes_more_than_its_share(cfg):
    pool = [insight(f"air{n}", 0.9 - n / 100) for n in range(5)] + [
        insight("g", 0.3, "general_pet_care"), insight("p", 0.2, "gear_packing")]
    chosen = stage.select(pool, slots=6, already=Counter(), cfg=cfg)
    cats = Counter(i.category for i in chosen)
    assert cats["airline_transport"] == cfg.max_per_category == 2


def test_general_pet_care_is_capped(cfg):
    pool = [insight(f"g{n}", 0.9, "general_pet_care") for n in range(5)] + [
        insight("t", 0.2, "gear_packing")]
    chosen = stage.select(pool, slots=6, already=Counter(), cfg=cfg)
    assert Counter(i.category for i in chosen)["general_pet_care"] == cfg.max_general_pet_care


def test_the_rules_hold_across_the_whole_day(cfg):
    pool = [insight("air", 0.9), insight("gear", 0.5, "gear_packing")]
    chosen = stage.select(pool, slots=2, already=Counter({"airline_transport": 2}), cfg=cfg)
    assert [i.insight_id for i in chosen] == ["gear"]


def test_a_thin_day_serves_fewer_rather_than_padding(cfg):
    assert len(stage.select([insight("a", 0.9)], slots=6, already=Counter(), cfg=cfg)) == 1


# --- eligibility ---------------------------------------------------------------------


def eligible(i, cfg, served_at=None, today=frozenset()):
    return stage.is_eligible(i, served_at=served_at, served_today=set(today), cfg=cfg, now=NOW)


def test_never_served_is_eligible(cfg):
    assert eligible(insight("a", 0.5), cfg)


def test_low_scores_retired_and_blank_questions_are_not(cfg):
    assert not eligible(insight("a", 0.05), cfg)
    assert not eligible(insight("a", 0.9, status="retired"), cfg)
    assert not eligible(insight("a", 0.9).model_copy(update={"canonical_question": " "}), cfg)


def test_served_without_new_evidence_is_never_eligible_again(cfg):
    long_ago = NOW - timedelta(days=90)
    assert not eligible(insight("a", 0.9, refreshed_at=long_ago - timedelta(days=1)), cfg, long_ago)


def test_new_evidence_alone_is_not_enough_inside_the_cooldown(cfg):
    served = NOW - timedelta(days=5)
    assert not eligible(insight("a", 0.9, refreshed_at=NOW - timedelta(days=1)), cfg, served)


def test_new_evidence_after_the_cooldown_makes_it_eligible(cfg):
    served = NOW - timedelta(days=cfg.cooldown_days + 1)
    assert eligible(insight("a", 0.9, refreshed_at=NOW - timedelta(days=1)), cfg, served)


def test_last_served_reads_the_feed_too():
    """A run that died after writing the feed but before marking insights can't cause a repeat."""
    insights = {"a": insight("a", 0.9)}
    rows = [FeedRow(feed_date="2026-11-20", rank=1, insight_id="a", question="Q", category="c",
                    served_at=NOW - timedelta(days=6))]
    assert stage.last_served(insights, rows)["a"] == NOW - timedelta(days=6)


# --- the stage -----------------------------------------------------------------------


def test_todays_feed_is_written_ranked_with_angles(config, store):
    seed(store, insight("a", 0.8), insight("b", 0.6, "gear_packing"),
         insight("g", 0.5, "general_pet_care"))
    result = run(config, store)
    rows = feed(store)
    assert result.served == 3 and [r.rank for r in rows] == [1, 2, 3]
    assert all(r.feed_date == "2026-11-26" and r.status == "served" for r in rows)
    assert all(r.suggested_angle for r in rows)
    assert all(r.rejection_reason == "" and r.reviewed_by == "" for r in rows)


def test_running_twice_serves_nothing_new(config, store):
    """The brief's checkpoint for this stage."""
    seed(store, *[insight(f"i{n}", 0.9 - n / 100, cat)
                  for n, cat in enumerate(["airline_transport", "gear_packing", "costs_planning",
                                           "pet_friendly_stays", "sitters_boarding",
                                           "general_pet_care", "documents_permits"])])
    first = run(config, store)
    second = run(config, store)
    assert first.served == 6 and second.served == 0
    assert len(feed(store)) == 6


def test_a_second_run_tops_up_a_short_day(config, store):
    seed(store, insight("a", 0.8))
    run(config, store)
    seed(store, insight("b", 0.7, "gear_packing"))
    second = run(config, store)
    assert second.served == 1 and second.already_today == 1
    assert [r.rank for r in feed(store)] == [1, 2]


def test_served_insights_are_marked(config, store):
    seed(store, insight("a", 0.8))
    run(config, store)
    row = Insight.from_sheet_row(store.read_rows("insights")[0])
    assert row.times_served == 1 and row.last_served_at == NOW
    assert row.suggested_angle and row.angle_generated_at == NOW


def test_tomorrow_serves_different_insights(config, store):
    seed(store, insight("a", 0.9), insight("b", 0.8, "gear_packing"))
    run(config, store)
    seed(store, insight("c", 0.7, "costs_planning"))
    tomorrow = run(config, store, day=DAY + timedelta(days=1), now=NOW + timedelta(days=1))
    assert [r.insight_id for r in feed(store) if r.feed_date == "2026-11-27"] == ["c"]
    assert tomorrow.served == 1


def test_an_angle_with_a_number_is_retried(config, store):
    seed(store, insight("a", 0.8))
    llm = FakeLlm(["Explain the 30-day rabies window.", "Help owners plan vaccinations early."])
    run(config, store, llm)
    assert feed(store)[0].suggested_angle == "Help owners plan vaccinations early."
    assert "number" in llm.requests[1]["messages"][0]["content"]


def test_a_failed_angle_still_serves_the_insight(config, store):
    seed(store, insight("a", 0.8))
    result = run(config, store, FakeLlm([LlmError("down")]))
    assert result.served == 1 and feed(store)[0].suggested_angle == ""
    assert result.angles_failed == 1 and result.warnings


def test_a_dry_run_writes_nothing(config, store):
    seed(store, insight("a", 0.8))
    result = run(config, store, dry_run=True)
    assert result.served == 1
    assert store.read_rows("feed") == [] and store.read_rows("runs") == []
    assert Insight.from_sheet_row(store.read_rows("insights")[0]).times_served == 0


def test_the_runs_row_counts_what_was_served(config, store):
    seed(store, insight("a", 0.8), insight("b", 0.7, "gear_packing"))
    run(config, store)
    stats = RunStats.from_sheet_row(store.read_rows("runs")[0])
    assert stats.served_today == 2 and stats.inputs["publish"]["feed_date"] == "2026-11-26"


def test_the_summary_file_carries_counts_only(config, store, tmp_path, monkeypatch):
    path = tmp_path / "run_summary.json"
    monkeypatch.setenv("TULA_RUN_SUMMARY", str(path))
    seed(store, insight("a", 0.8))
    run(config, store)
    data = json.loads(path.read_text())
    assert data["served"] == 1 and data["by_category"] == {"airline_transport": 1}
    assert "Question" not in path.read_text()


def test_the_summary_carries_no_question_text(config, store):
    seed(store, insight("a", 0.8))
    result = run(config, store)
    assert "Question a?" not in "\n".join(stage.summary_lines(result))


# --- the angle writer ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle", "reason"),
    [("Fly within 10 days of the certificate.", "number"),
     ("", "empty"),
     ("Long " * 60, "limit")],
)
def test_bad_angles_are_rejected(angle, reason):
    with pytest.raises(ClassificationInvalid, match=reason):
        AngleWriter("claude-haiku-5-5").validate(angle)


def test_a_good_angle_is_tidied_and_kept():
    writer = AngleWriter("claude-haiku-5-5")
    assert writer.validate("  Help owners   plan ahead.\n") == "Help owners plan ahead."
