"""The classify stage: every new item gets a category and an underlying question.

**Two modes.**

* **Batch** (the default, for scheduled runs): new items are sent as one Message Batch
  at half price. The run first collects any batches earlier runs left behind, then
  submits its own and waits for it up to `batch_poll_minutes`. Whatever is unfinished
  is picked up by the next run — the `_batches` tab is the memory between runs.
* **Sync** (`--sync`): items are classified one by one with the standard API, and saved
  every `write_every` items so a crash loses little. For manual runs and testing.

**Dry run** classifies a small sample one by one and writes nothing.

**Item status** moves `new` → `batch_pending` (batch only) → `classified`, `irrelevant`
or `error`. A batch result that expired or failed on Anthropic's side puts the item
back to `new`, to be sent again. A reply that fails validation is retried once with the
reason stated, then marked `error` with a short, text-free reason.

Logs and the run summary carry counts only — no item text — because they are public.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..budget import SERVICE_MESSAGES, UsageLedger
from ..config import AppConfig, ConfigError
from ..llm.classifier import ClassificationInvalid, Classifier
from ..llm.client import BatchNotFound, LlmClient, LlmError, estimate_tokens
from ..models import Classification, ItemRow, RunStats
from ..schema import BATCHES, ITEMS
from ..store.sheets import SheetStore, SheetStoreError, open_store
from ..utils import serialise as ser
from ..utils.clock import now_utc
from ..utils.logging import get_logger
from .collect import make_run_id
from .runs import record_run

log = get_logger(__name__)

STAGE = "classify"
#: Give up a sync run after this many API failures in a row — the API is likely down,
#: and carrying on would only mark items as failed that will succeed tomorrow.
MAX_CONSECUTIVE_FAILURES = 5
#: Rough output size of one classification, for the pre-run cost estimate.
EST_OUTPUT_TOKENS = 400


class ClassifyError(RuntimeError):
    """The stage could not run at all."""


@dataclass
class ClassifyResult:
    run_id: str
    mode: str
    relevant: int = 0
    irrelevant: int = 0
    errors: int = 0
    retried: int = 0
    requeued: int = 0
    batches_submitted: int = 0
    batches_collected: int = 0
    batches_still_pending: int = 0
    items_submitted: int = 0
    est_cost_usd: float = 0.0
    skipped_reason: str = ""
    #: Set when Claude rejected the stage's requests; the CLI then fails the job so the
    #: alert opens an issue. Items are kept and retried by the next run.
    api_failure: str = ""
    dry_run: bool = False
    categories: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def classified(self) -> int:
        return self.relevant + self.irrelevant


def run(
    config: AppConfig,
    *,
    sync: bool = False,
    dry_run: bool = False,
    source: str = "all",
    run_id: str | None = None,
    store: SheetStore | None = None,
    llm: LlmClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ClassifyResult:
    mode = "dry_run" if dry_run else ("sync" if sync else "batch")
    result = ClassifyResult(run_id=run_id or make_run_id(), mode=mode, dry_run=dry_run)

    if store is None:
        try:
            store = open_store(config, dry_run=dry_run)
        except (ConfigError, SheetStoreError) as error:
            raise ClassifyError(f"Cannot open the spreadsheet: {error}") from error
    if llm is None:
        if not config.env.anthropic_api_key:
            raise ClassifyError(
                "ANTHROPIC_API_KEY is not set, so nothing can be classified."
            )
        llm = LlmClient(config.env.anthropic_api_key, config.budget)

    stage = _Stage(config, store, llm, result, sleep=sleep)

    rows = [ItemRow.from_sheet_row(r) for r in store.read_rows(ITEMS.name)]
    stage.by_id = {r.item_id: r for r in rows}
    new = [r for r in rows if r.status == "new" and (source in ("", "all") or r.source == source)]
    new = new[: config.settings.classify.max_items_per_run]

    if dry_run:
        stage.classify_sync(new[: config.settings.classify.dry_run_sample], write=False)
        result.est_cost_usd = llm.usage.estimate_usd(config.budget)
        return result

    ledger = UsageLedger.from_store(config, store)
    estimate = stage.estimate_usd(len(new), batch=not sync)
    decision = ledger.allows_spend(estimate)
    if new and not decision:
        result.skipped_reason = decision.reason
        if ledger.fail_on_cap:
            raise ClassifyError(f"Classify stopped: {decision.reason}")
        log.warning("classify.skipped", extra={"reason": decision.reason})
        new = []

    try:
        if sync:
            stage.classify_sync(new, write=True)
        else:
            stage.collect_pending_batches()
            batch_id = stage.submit_batch(new)
            if batch_id:
                stage.wait_and_collect(batch_id)
    finally:
        stage.flush()
        result.est_cost_usd = llm.usage.estimate_usd(config.budget)
        _record(store, config, result, llm)
    return result


class _Stage:
    """The moving parts of one classify run, sharing the store, client and tallies."""

    def __init__(
        self,
        config: AppConfig,
        store: SheetStore,
        llm: LlmClient,
        result: ClassifyResult,
        *,
        sleep: Callable[[float], None],
    ) -> None:
        self.config = config
        self.settings = config.settings.classify
        self.store = store
        self.llm = llm
        self.result = result
        self.sleep = sleep
        self.classifier = Classifier(config)
        self.by_id: dict[str, ItemRow] = {}
        self.pending_updates: dict[str, dict[str, Any]] = {}

    # --- cost ------------------------------------------------------------------------

    def estimate_usd(self, count: int, *, batch: bool) -> float:
        if count == 0:
            return 0.0
        price = self.config.budget.price_for(self.settings.model)
        sample = self.classifier.request_params(ItemRow(item_id="x", source="web_questions"))
        tokens_in = estimate_tokens(sample) + 300  # plus a typical item
        per_in = (price.batch_input_per_mtok if batch else None) or price.input_per_mtok
        per_out = (price.batch_output_per_mtok if batch else None) or price.output_per_mtok
        per_item = tokens_in / 1e6 * per_in + EST_OUTPUT_TOKENS / 1e6 * per_out
        return round(per_item * count, 4)

    # --- sync ------------------------------------------------------------------------

    def classify_sync(self, items: list[ItemRow], *, write: bool) -> None:
        failures_in_a_row = 0
        for item in items:
            try:
                classification = self._ask(item)
            except ClassificationInvalid as error:
                self._mark_error(item, str(error), write=write)
                failures_in_a_row = 0
                continue
            except LlmError as error:
                failures_in_a_row += 1
                log.warning("classify.call_failed", extra={"in_a_row": failures_in_a_row})
                if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                    self.result.warnings.append(
                        f"Stopped after {failures_in_a_row} API failures in a row; the "
                        f"remaining items stay 'new' for the next run. Last error: {error}"
                    )
                    return
                continue
            failures_in_a_row = 0
            self._mark_classified(item, classification, write=write)
            if write and len(self.pending_updates) >= self.settings.write_every:
                self.flush()

    def _ask(self, item: ItemRow) -> Classification:
        """One item, with a single retry if the reply fails validation."""
        reply = self.llm.create(**self.classifier.request_params(item))
        try:
            return self.classifier.parse(reply)
        except ClassificationInvalid as error:
            if not error.retryable:
                raise
            self.result.retried += 1
            retry = self.llm.create(**self.classifier.request_params(item, problem=str(error)))
            return self.classifier.parse(retry)

    # --- batch -----------------------------------------------------------------------

    def submit_batch(self, items: list[ItemRow]) -> str | None:
        if not items:
            return None
        requests = [
            {"custom_id": item.item_id, "params": self.classifier.request_params(item)}
            for item in items
        ]
        try:
            batch = self.llm.create_batch(requests)
        except LlmError as error:
            self.result.warnings.append(f"Could not submit the batch; items stay 'new': {error}")
            self.result.api_failure = str(error)
            return None
        batch_id = batch.id
        # Recorded straight away: a batch nobody remembers is paid for and never read.
        self.store.append_rows(BATCHES.name, [{
            "batch_id": batch_id,
            "stage": STAGE,
            "submitted_at": now_utc(),
            "item_ids": ser.join_list(i.item_id for i in items),
            "status": "submitted",
            "completed_at": "",
        }])
        self.store.update_rows(ITEMS.name, {i.item_id: {"status": "batch_pending"} for i in items})
        self.result.batches_submitted += 1
        self.result.items_submitted += len(items)
        log.info("classify.batch_submitted", extra={"items": len(items)})
        return batch_id

    def collect_pending_batches(self) -> None:
        """Read results of batches that earlier runs submitted but did not finish."""
        for row in self.store.read_rows(BATCHES.name):
            if row.get("stage") != STAGE or row.get("status") != "submitted":
                continue
            batch_id = row.get("batch_id", "")
            try:
                batch = self.llm.retrieve_batch(batch_id)
            except BatchNotFound:
                self._lose_batch(batch_id, ser.split_list(row.get("item_ids")))
                continue
            except LlmError as error:
                self.result.warnings.append(f"Could not check an earlier batch: {error}")
                continue
            if getattr(batch, "processing_status", "") == "ended":
                self._collect(batch_id)
            else:
                self.result.batches_still_pending += 1

    def wait_and_collect(self, batch_id: str) -> None:
        deadline = time.monotonic() + self.settings.batch_poll_minutes * 60
        while True:
            try:
                batch = self.llm.retrieve_batch(batch_id)
            except LlmError as error:
                self.result.warnings.append(f"Could not check this run's batch: {error}")
                self.result.batches_still_pending += 1
                return
            if getattr(batch, "processing_status", "") == "ended":
                self._collect(batch_id)
                return
            if time.monotonic() >= deadline:
                self.result.batches_still_pending += 1
                log.info("classify.batch_left_for_next_run")
                return
            self.sleep(self.settings.batch_poll_interval_seconds)

    def _collect(self, batch_id: str) -> None:
        try:
            results = self.llm.batch_results(batch_id)
        except LlmError as error:
            self.result.warnings.append(f"Could not read a finished batch's results: {error}")
            return
        for entry in results:
            self._apply_batch_result(entry)
        self.flush()
        self.store.update_rows(
            BATCHES.name, {batch_id: {"status": "completed", "completed_at": now_utc()}}
        )
        self.result.batches_collected += 1

    def _apply_batch_result(self, entry: Any) -> None:
        item = self.by_id.get(getattr(entry, "custom_id", ""))
        if item is None:
            return  # deleted from the sheet since it was submitted
        outcome = getattr(entry, "result", None)
        kind = getattr(outcome, "type", "")

        if kind == "succeeded":
            message = outcome.message
            self.llm.usage.add_response(message, self.settings.model, batch=True)
            try:
                classification = self.classifier.parse(message)
            except ClassificationInvalid as error:
                if not error.retryable:
                    self._mark_error(item, str(error), write=True)
                    return
                # One retry, sent on its own with the problem stated.
                self.result.retried += 1
                try:
                    retry = self.llm.create(
                        **self.classifier.request_params(item, problem=str(error))
                    )
                    classification = self.classifier.parse(retry)
                except (ClassificationInvalid, LlmError) as again:
                    self._mark_error(item, str(again), write=True)
                    return
            self._mark_classified(item, classification, write=True)
            return

        if kind == "errored" and _error_type(outcome) == "invalid_request":
            self._mark_error(item, "the API rejected this item's request", write=True)
            return
        # errored on Anthropic's side, canceled or expired: send it again next run.
        self.pending_updates[item.item_id] = {"status": "new"}
        self.result.requeued += 1

    def _lose_batch(self, batch_id: str, item_ids: list[str]) -> None:
        """A batch the API no longer knows (results last 29 days): resend its items."""
        self.store.update_rows(
            ITEMS.name,
            {i: {"status": "new"} for i in item_ids
             if self.by_id.get(i) and self.by_id[i].status == "batch_pending"},
        )
        self.store.update_rows(
            BATCHES.name, {batch_id: {"status": "lost", "completed_at": now_utc()}}
        )
        self.result.warnings.append("An earlier batch had expired; its items were re-queued.")

    # --- recording -------------------------------------------------------------------

    def _mark_classified(
        self, item: ItemRow, classification: Classification, *, write: bool
    ) -> None:
        if classification.is_relevant:
            self.result.relevant += 1
            category = classification.primary_category or "unknown"
            self.result.categories[category] = self.result.categories.get(category, 0) + 1
        else:
            self.result.irrelevant += 1
        if write:
            self.pending_updates[item.item_id] = classification.to_updates(
                model=self.settings.model, prompt_version=self.classifier.prompt_version
            )

    def _mark_error(self, item: ItemRow, reason: str, *, write: bool) -> None:
        self.result.errors += 1
        if write:
            self.pending_updates[item.item_id] = {
                "status": "error",
                "error": reason[:200],
                "model": self.settings.model,
                "prompt_version": self.classifier.prompt_version,
            }

    def flush(self) -> None:
        if not self.pending_updates:
            return
        updates, self.pending_updates = self.pending_updates, {}
        self.store.update_rows(ITEMS.name, updates)


def _error_type(outcome: Any) -> str:
    error = getattr(outcome, "error", None)
    return getattr(error, "type", None) or getattr(getattr(error, "error", None), "type", "") or ""


def _record(store: SheetStore, config: AppConfig, result: ClassifyResult, llm: LlmClient) -> None:
    if result.dry_run:
        return
    import os

    usage = llm.usage
    stats = RunStats(
        run_id=result.run_id,
        trigger="schedule" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "dispatch",
        inputs={"classify": {"mode": result.mode}},
        status="partial" if (result.errors or result.warnings) else "success",
        items_classified=result.classified,
        items_irrelevant=result.irrelevant,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        tokens_cache_read=usage.tokens_cache_read,
        est_cost_usd=result.est_cost_usd,
        api_calls_by_service={SERVICE_MESSAGES: sum(usage.calls_by_model.values())},
        git_sha=os.environ.get("GITHUB_SHA", "")[:12],
        errors=[w[:200] for w in result.warnings[:10]],
        finished_at=now_utc(),
    )
    try:
        record_run(store, stats)
    except SheetStoreError as error:
        result.warnings.append(f"Could not write the runs row: {error}")


# --- reporting -----------------------------------------------------------------------


def _by_count(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda pair: -pair[1])


def summary_lines(result: ClassifyResult) -> list[str]:
    """Counts only — this is printed in public logs."""
    suffix = " (dry run — nothing written)" if result.dry_run else ""
    lines = [f"Classify run {result.run_id} — {result.mode}{suffix}", ""]
    if result.skipped_reason:
        lines += [f"  SKIPPED: {result.skipped_reason}", ""]
    lines += [
        f"  relevant      {result.relevant}",
        f"  irrelevant    {result.irrelevant}",
        f"  errors        {result.errors}",
        f"  retried       {result.retried}",
    ]
    if result.mode == "batch":
        lines += [
            f"  submitted     {result.items_submitted} item(s) in "
            f"{result.batches_submitted} batch(es)",
            f"  collected     {result.batches_collected} batch(es)",
            f"  still pending {result.batches_still_pending} batch(es) — collected next run",
            f"  re-queued     {result.requeued}",
        ]
    lines.append(f"  est. cost     ${result.est_cost_usd:.4f}")
    if result.categories:
        lines += ["", "  By category:"]
        lines += [f"    {c:<32} {n}" for c, n in _by_count(result.categories)]
    if result.warnings:
        lines += ["", "  Warnings:"] + [f"    - {w}" for w in result.warnings[:10]]
    return lines


def write_job_summary(result: ClassifyResult) -> None:
    import os

    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    rows = "".join(f"| {c} | {n} |\n" for c, n in _by_count(result.categories))
    body = (
        f"### Classify — {result.run_id} ({result.mode})\n\n"
        f"| | |\n|---|---|\n| Relevant | {result.relevant} |\n"
        f"| Irrelevant | {result.irrelevant} |\n"
        f"| Errors | {result.errors} |\n| Retried | {result.retried} |\n"
        f"| Batches submitted / collected / pending | {result.batches_submitted} / "
        f"{result.batches_collected} / {result.batches_still_pending} |\n"
        f"| Estimated cost | ${result.est_cost_usd:.4f} |\n"
    )
    if rows:
        body += f"\n| Category | Items |\n|---|---|\n{rows}"
    if result.skipped_reason:
        body += f"\n**Skipped:** {result.skipped_reason}\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except OSError:  # pragma: no cover
        pass
