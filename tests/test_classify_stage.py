"""The classify stage: statuses, retries, batches across runs, and the runs row."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fakes import FakeSpreadsheet, Obj
from test_sheets import SETTINGS
from tula_insights import schema
from tula_insights.config import load_config
from tula_insights.llm.client import BatchNotFound, LlmError, Usage
from tula_insights.models import RunStats
from tula_insights.stages import classify as stage
from tula_insights.store.sheets import SheetStore

GOOD = {"is_relevant": True, "primary_category": "airline_transport", "tags": [],
        "question": "Can my dog fly in the cabin?", "pain_point": None, "confidence": 0.9}
NOPE = {"is_relevant": False, "primary_category": None, "tags": [], "question": None,
        "pain_point": None, "confidence": 0.95}


def message(data, stop_reason="end_turn", tokens_in=1000, tokens_out=100):
    text = data if isinstance(data, str) else json.dumps(data)
    return Obj(
        content=[Obj(type="text", text=text)],
        stop_reason=stop_reason,
        usage=Obj(input_tokens=tokens_in, output_tokens=tokens_out, cache_read_input_tokens=0,
                  cache_creation_input_tokens=0, server_tool_use=None),
    )


class FakeLlm:
    """Sync replies in order; batches that end on demand."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.requests = []
        self.usage = Usage()
        self.batches = {}          # id -> {"status": str, "results": [...]}
        self.submitted = []
        self.missing = set()

    def create(self, **params):
        self.requests.append(params)
        if not self.replies:
            raise AssertionError("no reply queued")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        self.usage.add_response(reply, params["model"])
        return reply

    def create_batch(self, requests):
        batch_id = f"batch_{len(self.submitted) + 1}"
        self.submitted.append((batch_id, requests))
        self.batches[batch_id] = {"status": "in_progress", "results": []}
        return Obj(id=batch_id)

    def retrieve_batch(self, batch_id):
        if batch_id in self.missing:
            raise BatchNotFound("gone")
        return Obj(processing_status=self.batches[batch_id]["status"])

    def batch_results(self, batch_id):
        return self.batches[batch_id]["results"]

    def finish(self, batch_id, outcomes):
        """outcomes: {item_id: dict | 'expired' | 'server_error' | 'invalid_request'}"""
        results = []
        for item_id, outcome in outcomes.items():
            if isinstance(outcome, dict) or (isinstance(outcome, str) and outcome.startswith("{")):
                result = Obj(type="succeeded", message=message(outcome))
            elif outcome == "expired":
                result = Obj(type="expired")
            elif outcome == "server_error":
                result = Obj(type="errored", error=Obj(type="api_error"))
            elif outcome == "invalid_request":
                result = Obj(type="errored", error=Obj(type="invalid_request"))
            else:
                result = Obj(type="succeeded", message=message(outcome))
            results.append(Obj(custom_id=item_id, result=result))
        self.batches[batch_id] = {"status": "ended", "results": results}


@pytest.fixture
def config(config_dir):
    return load_config()


@pytest.fixture
def store():
    store = SheetStore(FakeSpreadsheet(), SETTINGS)
    for spec in schema.TABS:
        store.ensure_tab(spec)
    return store


def add_items(store, *ids, source="youtube", status="new"):
    store.append_rows("items", [
        {"item_id": i, "source": source, "title": f"Question {i}?", "snippet": f"Question {i}?",
         "status": status} for i in ids
    ])


def item(store, item_id):
    return next(r for r in store.read_rows("items") if r["item_id"] == item_id)


def run(config, store, llm, **kwargs):
    return stage.run(config, store=store, llm=llm, run_id="run-1", sleep=lambda _: None, **kwargs)


# --- sync ----------------------------------------------------------------------------


def test_sync_classifies_and_records_every_field(config, store):
    add_items(store, "a", "b")
    result = run(config, store, FakeLlm([message(GOOD), message(NOPE)]), sync=True)
    assert (result.relevant, result.irrelevant) == (1, 1)
    a, b = item(store, "a"), item(store, "b")
    assert a["status"] == "classified" and a["primary_category"] == "airline_transport"
    assert a["question"] == "Can my dog fly in the cabin?"
    assert a["model"] == "claude-haiku-5-5"
    assert a["prompt_version"] == "classify_extract:1/categories:1"
    assert b["status"] == "irrelevant" and b["is_relevant"] == "FALSE"


def test_only_new_items_are_classified(config, store):
    add_items(store, "done", status="classified")
    add_items(store, "fresh")
    llm = FakeLlm([message(GOOD)])
    run(config, store, llm, sync=True)
    assert len(llm.requests) == 1


def test_the_source_filter(config, store):
    add_items(store, "yt", source="youtube")
    add_items(store, "web", source="web_questions")
    llm = FakeLlm([message(GOOD)])
    run(config, store, llm, sync=True, source="web_questions")
    assert item(store, "web")["status"] == "classified"
    assert item(store, "yt")["status"] == "new"


def test_an_invalid_reply_is_retried_once_with_the_reason(config, store):
    add_items(store, "a")
    llm = FakeLlm([message({**GOOD, "question": None}), message(GOOD)])
    result = run(config, store, llm, sync=True)
    assert result.retried == 1 and item(store, "a")["status"] == "classified"
    assert "needs a question" in llm.requests[1]["messages"][0]["content"]


def test_two_invalid_replies_mark_the_item_as_an_error(config, store):
    add_items(store, "a")
    bad = message({**GOOD, "primary_category": "spaceships"})
    result = run(config, store, FakeLlm([bad, bad]), sync=True)
    row = item(store, "a")
    assert result.errors == 1 and row["status"] == "error"
    assert "listed ids" in row["error"]


def test_a_refusal_is_an_error_without_a_retry(config, store):
    add_items(store, "a")
    llm = FakeLlm([message(GOOD, stop_reason="refusal")])
    run(config, store, llm, sync=True)
    assert len(llm.requests) == 1 and item(store, "a")["status"] == "error"


def test_repeated_api_failures_stop_the_run_and_leave_items_new(config, store):
    add_items(store, *[f"i{n}" for n in range(8)])
    llm = FakeLlm([LlmError("down")] * 8)
    result = run(config, store, llm, sync=True)
    assert len(llm.requests) == stage.MAX_CONSECUTIVE_FAILURES
    assert all(r["status"] == "new" for r in store.read_rows("items"))
    assert any("API failures in a row" in w for w in result.warnings)


def test_sync_saves_progress_as_it_goes(config, store, monkeypatch):
    monkeypatch.setattr(config.settings.classify, "write_every", 2)
    add_items(store, "a", "b", "c")
    llm = FakeLlm([message(GOOD), message(GOOD), LlmError("x"), LlmError("x"), LlmError("x"),
                   LlmError("x"), LlmError("x")])
    run(config, store, llm, sync=True)
    assert item(store, "a")["status"] == "classified"


# --- dry run -------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(config, store):
    add_items(store, "a")
    result = run(config, store, FakeLlm([message(GOOD)]), dry_run=True)
    assert result.relevant == 1
    assert item(store, "a")["status"] == "new"
    assert store.read_rows("runs") == []


def test_a_dry_run_is_limited_to_the_sample(config, store):
    add_items(store, *[f"i{n}" for n in range(20)])
    llm = FakeLlm([message(GOOD)] * 20)
    run(config, store, llm, dry_run=True)
    assert len(llm.requests) == config.settings.classify.dry_run_sample


# --- batches -------------------------------------------------------------------------


def test_a_batch_is_submitted_remembered_and_collected(config, store):
    add_items(store, "a", "b")

    class EndsImmediately(FakeLlm):
        def create_batch(self, requests):
            batch = super().create_batch(requests)
            self.finish(batch.id, {"a": GOOD, "b": NOPE})
            return batch

    llm = EndsImmediately()
    result = run(config, store, llm)
    assert result.batches_submitted == 1 and result.batches_collected == 1
    assert item(store, "a")["status"] == "classified"
    assert item(store, "b")["status"] == "irrelevant"
    batch_row = store.read_rows("_batches")[0]
    assert batch_row["status"] == "completed" and batch_row["completed_at"]
    assert set(batch_row["item_ids"].split(";")) == {"a", "b"}


def test_an_unfinished_batch_is_left_for_the_next_run(config, store, monkeypatch):
    monkeypatch.setattr(config.settings.classify, "batch_poll_minutes", 0)
    add_items(store, "a")
    llm = FakeLlm()
    first = run(config, store, llm)
    assert first.batches_still_pending == 1
    assert item(store, "a")["status"] == "batch_pending"

    llm.finish("batch_1", {"a": GOOD})
    second = run(config, store, llm)
    assert second.batches_collected == 1 and second.batches_submitted == 0
    assert item(store, "a")["status"] == "classified"


def test_failed_and_expired_results_go_back_to_new(config, store, monkeypatch):
    monkeypatch.setattr(config.settings.classify, "batch_poll_minutes", 0)
    add_items(store, "exp", "srv", "bad")
    llm = FakeLlm()
    run(config, store, llm)
    llm.finish("batch_1", {"exp": "expired", "srv": "server_error", "bad": "invalid_request"})
    result = run(config, store, llm)
    assert item(store, "exp")["status"] == "new"
    assert item(store, "srv")["status"] == "new"
    assert result.requeued == 2
    assert item(store, "bad")["status"] == "error"


def test_an_invalid_batch_result_is_retried_on_its_own(config, store, monkeypatch):
    monkeypatch.setattr(config.settings.classify, "batch_poll_minutes", 0)
    add_items(store, "a")
    llm = FakeLlm([message(GOOD)])
    run(config, store, llm)
    llm.finish("batch_1", {"a": {**GOOD, "question": None}})
    result = run(config, store, llm)
    assert result.retried == 1 and item(store, "a")["status"] == "classified"


def test_an_expired_batch_requeues_its_items(config, store, monkeypatch):
    monkeypatch.setattr(config.settings.classify, "batch_poll_minutes", 0)
    add_items(store, "a")
    llm = FakeLlm()
    run(config, store, llm)
    llm.missing.add("batch_1")
    llm.submitted.clear()
    run(config, store, llm)
    assert store.read_rows("_batches")[0]["status"] == "lost"
    # Back to new; the next run submits it again.
    assert item(store, "a")["status"] == "new"
    assert llm.submitted == []


def test_batch_tokens_are_priced_at_batch_rates(config, store):
    add_items(store, "a")

    class EndsImmediately(FakeLlm):
        def create_batch(self, requests):
            batch = super().create_batch(requests)
            self.finish(batch.id, {"a": GOOD})
            return batch

    llm = EndsImmediately()
    run(config, store, llm)
    assert llm.usage.batch["claude-haiku-5-5"].calls == 1
    assert llm.usage.standard == {}


# --- budget and the runs row ---------------------------------------------------------


def test_the_runs_row_records_what_was_classified(config, store):
    add_items(store, "a", "b")
    run(config, store, FakeLlm([message(GOOD), message(NOPE)]), sync=True)
    stats = RunStats.from_sheet_row(store.read_rows("runs")[0])
    assert stats.items_classified == 2 and stats.items_irrelevant == 1
    assert stats.tokens_in == 2000 and stats.est_cost_usd > 0
    assert stats.inputs["classify"]["mode"] == "sync"


def test_classify_adds_to_the_same_runs_row_as_collect(config, store):
    store.append_rows("runs", [RunStats(
        run_id="run-1", items_collected_by_source={"youtube": 5}, tokens_in=500,
        est_cost_usd=0.2, inputs={"collect": {"source": "all"}}, status="success",
    ).to_sheet_row()])
    add_items(store, "a")
    run(config, store, FakeLlm([message(GOOD)]), sync=True)
    rows = store.read_rows("runs")
    assert len(rows) == 1
    stats = RunStats.from_sheet_row(rows[0])
    assert stats.items_collected_by_source == {"youtube": 5}
    assert stats.items_classified == 1
    assert stats.tokens_in == 1500
    assert set(stats.inputs) == {"collect", "classify"}


def test_the_monthly_cap_skips_the_stage(config, store):
    store.append_rows("runs", [RunStats(
        run_id="old", started_at=datetime.now(UTC), est_cost_usd=25.0,
    ).to_sheet_row()])
    add_items(store, "a")
    llm = FakeLlm()
    result = run(config, store, llm, sync=True)
    assert "spend cap" in result.skipped_reason
    assert llm.requests == [] and item(store, "a")["status"] == "new"


def test_the_summary_carries_no_item_text(config, store):
    add_items(store, "a")
    result = run(config, store, FakeLlm([message(GOOD)]), sync=True)
    text = "\n".join(stage.summary_lines(result))
    assert "Can my dog fly" not in text and "airline_transport" in text
