"""The score stage: merging, judging, new insights, and what gets written."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from fakes import FakeSpreadsheet, Obj
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.config import load_config
from tula_insights.llm.client import LlmError, Usage
from tula_insights.models import Insight, RunStats
from tula_insights.stages import score as stage
from tula_insights.store.sheets import SheetStore
from tula_insights.utils import embeddings as emb

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def unit(*values):
    v = np.zeros(8, dtype=np.float32)
    v[: len(values)] = values
    return emb.normalise(v)[0]


def at(similarity):
    """A vector with exactly this cosine similarity to unit(1)."""
    return unit(similarity, np.sqrt(max(0.0, 1 - similarity**2)))


class FakeEmbedder:
    model = "fake-model"

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return np.vstack([self.mapping[t] for t in texts]) if texts else np.zeros((0, 8))


class FakeLlm:
    def __init__(self, choices=()):
        self.choices = list(choices)
        self.requests = []
        self.usage = Usage()

    def create(self, **params):
        self.requests.append(params)
        choice = self.choices.pop(0)
        if isinstance(choice, Exception):
            raise choice
        reply = Obj(content=[Obj(type="text", text=json.dumps({"same_as": choice}))],
                    stop_reason="end_turn",
                    usage=Obj(input_tokens=300, output_tokens=10, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, server_tool_use=None))
        self.usage.add_response(reply, params["model"])
        return reply


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def add_item(store, item_id, question, *, source="youtube", category="airline_transport",
             days_ago=1, status="classified", insight_id="", engagement=None, url=None):
    store.append_rows("items", [{
        "item_id": item_id, "source": source, "status": status, "question": question,
        "primary_category": category, "insight_id": insight_id,
        "url": url if url is not None else f"https://example.com/{item_id}",
        "collected_at": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engagement_json": json.dumps(engagement or {"comment_likes": 1, "comment_replies": 0}),
    }])


def add_insight(store, insight_id, question, vector, model="fake-model"):
    store.append_rows("insights", [Insight(insight_id=insight_id, canonical_question=question,
                                           category="airline_transport").to_sheet_row()])
    store.append_rows("_embeddings", [{"insight_id": insight_id, "model": model,
                                       "vector": emb.encode(vector)}])


def run(config, store, embedder, llm=None, **kw):
    return stage.run(config, store=store, embedder=embedder, llm=llm, run_id="run-1",
                     now=NOW, **kw)


def items(store):
    return {r["item_id"]: r for r in store.read_rows("items")}


def insights(store):
    return {r["insight_id"]: r for r in store.read_rows("insights")}


# --- dedupe decisions ----------------------------------------------------------------


def test_the_first_question_becomes_an_insight(config, store):
    add_item(store, "i1", "Can my dog fly?")
    result = run(config, store, FakeEmbedder({"Can my dog fly?": unit(1)}))
    assert result.new_insights == 1
    insight_id = items(store)["i1"]["insight_id"]
    assert insights(store)[insight_id]["canonical_question"] == "Can my dog fly?"
    assert int(insights(store)[insight_id]["item_count"]) == 1


def test_a_near_identical_question_merges_without_the_judge(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Is my dog allowed to fly?")
    llm = FakeLlm()
    result = run(config, store, FakeEmbedder({"Is my dog allowed to fly?": at(0.95)}), llm)
    assert result.merged == 1 and result.judge_calls == 0
    assert items(store)["i1"]["insight_id"] == "ins1"


def test_a_distant_question_starts_a_new_insight(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "How often should I bathe my dog?")
    result = run(config, store, FakeEmbedder({"How often should I bathe my dog?": at(0.5)}), FakeLlm())
    assert result.new_insights == 1 and result.judge_calls == 0
    assert items(store)["i1"]["insight_id"] != "ins1"


def test_the_judge_decides_the_middle_band_yes(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Can I take my dog on a plane?")
    llm = FakeLlm([1])
    result = run(config, store, FakeEmbedder({"Can I take my dog on a plane?": at(0.85)}), llm)
    assert result.judge_calls == 1 and result.judge_merges == 1
    assert items(store)["i1"]["insight_id"] == "ins1"
    assert "Can my dog fly?" in llm.requests[0]["messages"][0]["content"]


def test_the_judge_decides_the_middle_band_no(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "What crate does my dog need for cargo?")
    result = run(config, store, FakeEmbedder({"What crate does my dog need for cargo?": at(0.85)}),
                 FakeLlm([None]))
    assert result.new_insights == 1 and result.judge_merges == 0


def test_without_a_judge_the_middle_band_waits(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Can I take my dog on a plane?")
    result = run(config, store, FakeEmbedder({"Can I take my dog on a plane?": at(0.85)}), None)
    assert result.deferred == 1
    assert items(store)["i1"]["insight_id"] == "", "never guessed at"


def test_a_failed_judge_call_defers_rather_than_guesses(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Can I take my dog on a plane?")
    result = run(config, store, FakeEmbedder({"Can I take my dog on a plane?": at(0.85)}),
                 FakeLlm([LlmError("down")]))
    assert result.deferred == 1 and items(store)["i1"]["insight_id"] == ""


def test_the_judge_call_cap_is_honoured(config, store, monkeypatch):
    monkeypatch.setattr(config.scoring.dedupe, "max_judge_calls_per_run", 1)
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "i1", "Q one", days_ago=2)
    add_item(store, "i2", "Q two", days_ago=1)
    # Both in the judge band with ins1, but far from each other, so the second cannot
    # simply merge into the insight the first one creates.
    one, two = unit(0.85, 0.527), unit(0.85, -0.527)
    result = run(config, store, FakeEmbedder({"Q one": one, "Q two": two}), FakeLlm([None]))
    assert result.judge_calls == 1 and result.deferred == 1


def test_two_copies_of_a_new_question_in_one_run_end_up_together(config, store):
    add_item(store, "i1", "Can my dog fly?", days_ago=2)
    add_item(store, "i2", "Can my dog fly on a plane?", days_ago=1)
    result = run(config, store, FakeEmbedder({"Can my dog fly?": unit(1),
                                              "Can my dog fly on a plane?": at(0.97)}))
    rows = items(store)
    assert rows["i1"]["insight_id"] == rows["i2"]["insight_id"]
    assert result.new_insights == 1 and result.merged == 1


def test_only_classified_items_without_an_insight_are_deduped(config, store):
    add_item(store, "irr", "x", status="irrelevant")
    add_item(store, "new", "y", status="new")
    embedder = FakeEmbedder({})
    run(config, store, embedder)
    assert embedder.calls == []


# --- aggregates and scores -----------------------------------------------------------


def test_aggregates_are_recomputed_from_items(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "a", "q", source="youtube", insight_id="ins1", days_ago=3)
    add_item(store, "b", "q", source="web_questions", insight_id="ins1", days_ago=1,
             engagement={"result_position": 0, "query_recurrence": 2})
    run(config, store, FakeEmbedder({}))
    row = insights(store)["ins1"]
    assert int(row["item_count"]) == 2 and int(row["source_count"]) == 2
    assert set(row["sources"].split(";")) == {"web_questions", "youtube"}
    assert float(row["total_score"]) > 0


def test_purged_items_still_count_but_give_no_evidence_link(config, store):
    """YouTube links leave an insight once the 30-day purge blanks the item."""
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "live", "q", insight_id="ins1", url="https://www.youtube.com/watch?v=a")
    add_item(store, "gone", "", insight_id="ins1", status="purged", url="")
    run(config, store, FakeEmbedder({}))
    row = insights(store)["ins1"]
    assert int(row["item_count"]) == 2
    assert row["evidence_urls"] == "https://www.youtube.com/watch?v=a"


def test_evidence_links_are_the_most_engaged_three(config, store):
    add_insight(store, "ins1", "Q", unit(1))
    for n, likes in enumerate([1, 50, 5, 200]):
        add_item(store, f"i{n}", "q", insight_id="ins1",
                 engagement={"comment_likes": likes, "comment_replies": 0})
    run(config, store, FakeEmbedder({}))
    links = insights(store)["ins1"]["evidence_urls"].split(";")
    assert links == ["https://example.com/i3", "https://example.com/i1", "https://example.com/i2"]


def test_engagement_percentiles_are_written_to_items(config, store):
    for n, likes in enumerate([1, 10, 100]):
        add_item(store, f"i{n}", f"q{n}", engagement={"comment_likes": likes, "comment_replies": 0})
    run(config, store, FakeEmbedder({"q0": unit(1), "q1": unit(0, 1), "q2": unit(0, 0, 1)}))
    rows = items(store)
    assert [float(rows[f"i{n}"]["engagement_pct"]) for n in range(3)] == [0.0, 0.5, 1.0]


def test_a_rejected_feed_insight_is_penalised(config, store):
    add_insight(store, "ins1", "Q", unit(1))
    add_item(store, "a", "q", insight_id="ins1")
    store.append_rows("feed", [{"feed_date": "2026-11-20", "rank": 1, "insight_id": "ins1",
                                "question": "Q", "status": "rejected",
                                "served_at": "2026-11-20T03:00:00Z"}])
    run(config, store, FakeEmbedder({}))
    assert float(insights(store)["ins1"]["feedback_adjustment"]) == -0.15


def test_publish_owned_columns_are_never_written(config, store):
    add_insight(store, "ins1", "Q", unit(1))
    sheet = store.book.worksheet("insights")
    sheet.values[1][schema.INSIGHTS.index_of("suggested_angle")] = "Team's angle"
    sheet.values[1][schema.INSIGHTS.index_of("times_served")] = "4"
    add_item(store, "a", "q", insight_id="ins1")
    run(config, store, FakeEmbedder({}))
    row = insights(store)["ins1"]
    assert row["suggested_angle"] == "Team's angle" and row["times_served"] == "4"


# --- vectors -------------------------------------------------------------------------


def test_new_insight_vectors_are_stored(config, store):
    add_item(store, "i1", "Can my dog fly?")
    run(config, store, FakeEmbedder({"Can my dog fly?": unit(1)}))
    stored = store.read_rows("_embeddings")
    assert len(stored) == 1 and stored[0]["model"] == "fake-model"
    assert np.allclose(emb.decode(stored[0]["vector"]), unit(1))


def test_a_model_change_re_embeds_everything(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(0, 1), model="old-model")
    result = run(config, store, FakeEmbedder({"Can my dog fly?": unit(1)}))
    assert result.reembedded == 1
    row = store.read_rows("_embeddings")[0]
    assert row["model"] == "fake-model" and np.allclose(emb.decode(row["vector"]), unit(1))


# --- dry run, runs row, summary ------------------------------------------------------


def test_a_dry_run_writes_nothing(config, store):
    add_item(store, "i1", "Can my dog fly?")
    result = run(config, store, FakeEmbedder({"Can my dog fly?": unit(1)}), dry_run=True)
    assert result.new_insights == 1
    assert store.read_rows("insights") == [] and store.read_rows("_embeddings") == []
    assert items(store)["i1"]["insight_id"] == ""


def test_the_runs_row_records_new_and_merged(config, store):
    add_insight(store, "ins1", "Can my dog fly?", unit(1))
    add_item(store, "a", "A")
    add_item(store, "b", "B")
    run(config, store, FakeEmbedder({"A": at(0.95), "B": at(0.2)}), FakeLlm())
    stats = RunStats.from_sheet_row(store.read_rows("runs")[0])
    assert stats.new_insights == 1 and stats.merged_into_existing == 1


def test_the_summary_shows_relevance_by_source_and_no_text(config, store):
    add_item(store, "y", "Secret question text?", source="youtube")
    add_item(store, "w", "", source="web_questions", status="irrelevant")
    result = run(config, store, FakeEmbedder({"Secret question text?": unit(1)}))
    text = "\n".join(stage.summary_lines(result))
    assert "Secret question text" not in text
    assert "youtube" in text and "web_questions" in text and "relevant" in text
