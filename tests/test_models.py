"""Item identity, row mapping and the classification contract."""

from __future__ import annotations

from datetime import UTC, datetime

from tula_insights import schema
from tula_insights.models import (
    Classification,
    FeedRow,
    Insight,
    ItemRow,
    RawItem,
    RunStats,
    canonical_url,
    make_item_id,
)


def test_canonical_url_strips_tracking_case_and_fragment():
    assert canonical_url("https://EXAMPLE.com/a/?utm_source=x&q=1#top") == (
        "https://example.com/a?q=1"
    )


def test_same_page_found_twice_is_one_item():
    a = make_item_id("reddit_search", None, "https://reddit.com/r/dogs/abc/?utm_campaign=x")
    b = make_item_id("reddit_search", None, "https://Reddit.com/r/dogs/abc")
    assert a == b


def test_external_id_wins_over_url_when_present():
    with_id = make_item_id("youtube", "vid123", "https://youtube.com/watch?v=vid123")
    moved = make_item_id("youtube", "vid123", "https://youtube.com/shorts/vid123")
    assert with_id == moved


def test_different_sources_never_collide():
    assert make_item_id("a", None, "https://e.com") != make_item_id("b", None, "https://e.com")


def test_item_id_is_stable_across_runs():
    assert make_item_id("s", None, "https://e.com/x") == make_item_id("s", None, "https://e.com/x")


def test_snippet_is_bounded_and_whitespace_collapsed():
    item = RawItem(source="s", url="https://e.com", snippet="a   b\n\nc " + "x" * 1000)
    assert len(item.snippet) == 500
    assert item.snippet.startswith("a b c")


def test_every_model_row_matches_its_tab_exactly():
    pairs = [
        (RawItem(source="s", url="https://e.com").to_sheet_row(), schema.ITEMS),
        (Insight(canonical_question="q?", category="c").to_sheet_row(), schema.INSIGHTS),
        (
            FeedRow(feed_date="2026-11-25", rank=1, insight_id="i", question="q", category="c")
            .to_sheet_row(),
            schema.FEED,
        ),
        (RunStats(run_id="r").to_sheet_row(), schema.RUNS),
    ]
    for row, spec in pairs:
        assert tuple(row) == spec.columns, f"{spec.name} row does not match the schema"


def test_new_items_start_as_new():
    assert RawItem(source="s", url="https://e.com").to_sheet_row()["status"] == "new"


def test_feed_rows_never_carry_reviewer_values():
    row = FeedRow(
        feed_date="2026-11-25",
        rank=1,
        insight_id="i",
        question="q",
        category="c",
        status="served",
        rejection_reason="should not be written",
        post_ref="should not be written",
    ).to_sheet_row()
    assert row["rejection_reason"] == ""
    assert row["post_ref"] == ""
    assert row["reviewed_by"] == ""


def test_irrelevant_classification_marks_the_item_irrelevant():
    updates = Classification(is_relevant=False, confidence=0.9).to_updates(
        model="claude-haiku-5-5", prompt_version="1"
    )
    assert updates["status"] == "irrelevant"
    assert updates["is_relevant"] is False


def test_relevant_classification_marks_the_item_classified():
    updates = Classification(
        is_relevant=True,
        primary_category="airline_transport",
        tags=["health"],
        question="Can my dog fly in the cabin?",
        confidence=0.82,
    ).to_updates(model="claude-haiku-5-5", prompt_version="1")
    assert updates["status"] == "classified"
    assert updates["tags"] == "health"
    assert updates["confidence"] == 0.82


def test_item_row_survives_a_mostly_empty_sheet_row():
    row = ItemRow.from_sheet_row({"item_id": "abc"})
    assert row.item_id == "abc"
    assert row.status == "new"
    assert row.tags == []
    assert row.is_relevant is None


def test_insight_round_trips_through_a_sheet_row():
    original = Insight(
        canonical_question="How soon after the rabies shot can my dog travel?",
        category="vaccinations_health_certs",
        tags=["health"],
        sources=["reddit_search", "youtube"],
        evidence_urls=["https://e.com/a", "https://e.com/b"],
        item_count=4,
        source_count=2,
        first_seen=datetime(2026, 11, 1, tzinfo=UTC),
        total_score=0.6123,
    ).with_id()
    restored = Insight.from_sheet_row(
        {k: str(v) if v is not None else "" for k, v in original.to_sheet_row().items()}
    )
    assert restored.insight_id == original.insight_id
    assert restored.canonical_question == original.canonical_question
    assert restored.sources == original.sources
    assert restored.evidence_urls == original.evidence_urls
    assert restored.first_seen == original.first_seen
    assert round(restored.total_score, 4) == 0.6123


def test_run_errors_are_truncated_for_a_public_tab():
    stats = RunStats(run_id="r", errors=["x" * 500])
    assert len(stats.to_sheet_row()["errors"]) == 200
