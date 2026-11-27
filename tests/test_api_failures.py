"""When Claude rejects requests: the real reason is shown, and the stage fails visibly."""

from __future__ import annotations

import anthropic
import pytest

from test_classify_stage import FakeLlm, add_items, item
from test_classify_stage import config as config  # pytest fixture
from test_classify_stage import store as store  # pytest fixture
from test_publish import FakeLlm as AngleLlm
from test_publish import insight
from tula_insights.cli import main
from tula_insights.llm.client import LlmClient, LlmError
from tula_insights.stages import classify as classify_stage
from tula_insights.stages import publish as publish_stage

CREDIT = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."


def bad_request(message: str) -> anthropic.BadRequestError:
    """A 400 as the SDK raises it, without depending on its HTTP library's internals."""
    error = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
    Exception.__init__(error, message)
    error.message = message
    error.status_code = 400
    error.body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return error


def client_raising(error, config):
    client = LlmClient("sk-ant-test", config.budget, max_retries=0)  # pragma: allowlist secret

    class Batches:
        def create(self, **kwargs):
            raise error

    client.client = type("C", (), {"messages": type("M", (), {"batches": Batches()})()})()
    return client


def test_a_credit_problem_is_explained_in_plain_words(config):
    with pytest.raises(LlmError) as caught:
        client_raising(bad_request(CREDIT), config).create_batch([])
    text = str(caught.value)
    assert "run out of credit" in text and "Billing" in text
    assert "credit balance is too low" in text, "Anthropic's own words are kept"


def test_other_rejections_carry_the_apis_reason(config):
    with pytest.raises(LlmError) as caught:
        client_raising(bad_request("max_tokens: must be at most 8192"), config).create_batch([])
    assert "max_tokens: must be at most 8192" in str(caught.value)


class FailingBatch(FakeLlm):
    def create_batch(self, requests):
        raise LlmError("messages.batches.create failed: The Claude API account has run out of credit.")


def test_classify_fails_the_job_but_keeps_the_items(config_dir, store, monkeypatch, capsys):
    add_items(store, "a", "b")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    monkeypatch.setattr(classify_stage, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(classify_stage, "LlmClient", lambda *a, **k: FailingBatch())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["classify"]) == 1
    out, err = capsys.readouterr()
    assert "::error title=Classify could not reach Claude::" in out, "the alert quotes this"
    assert "run out of credit" in err
    assert item(store, "a")["status"] == "new" and item(store, "b")["status"] == "new"


class FailingAngles(AngleLlm):
    def create(self, **params):
        raise LlmError("The Claude API rejected the request: credit balance is too low")


def test_publish_still_serves_the_feed_then_fails_if_no_angle_could_be_written(
    config_dir, store, monkeypatch, capsys
):
    store.append_rows("insights", [insight("a", 0.8).to_sheet_row()])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    monkeypatch.setattr(publish_stage, "open_store", lambda config, dry_run=False: store)
    monkeypatch.setattr(publish_stage, "LlmClient", lambda *a, **k: FailingAngles())
    assert main(["publish"]) == 1
    assert len(store.read_rows("feed")) == 1, "the feed goes out first"
    assert "credit balance is too low" in capsys.readouterr().err


def test_one_failed_angle_among_good_ones_does_not_fail_the_job(config_dir, store, monkeypatch):
    store.append_rows("insights", [insight("a", 0.8).to_sheet_row(),
                                   insight("b", 0.7, "gear_packing").to_sheet_row()])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pragma: allowlist secret
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    monkeypatch.setattr(publish_stage, "open_store", lambda config, dry_run=False: store)
    llm = AngleLlm([LlmError("temporary"), "Help owners plan ahead with confidence."])
    monkeypatch.setattr(publish_stage, "LlmClient", lambda *a, **k: llm)
    assert main(["publish"]) == 0
