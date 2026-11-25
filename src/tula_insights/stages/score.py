"""The score stage: merge repeat questions into insights, then rank every insight.

**Dedupe.** Each newly classified item's question is embedded locally and compared
with every insight's question vector:

* similarity >= `merge_threshold` → merged into that insight, no API call;
* between `judge_lower_threshold` and `merge_threshold` → Claude judges whether it is
  really the same question (up to `max_judge_calls_per_run`; the rest wait for the
  next run rather than being guessed at);
* below `judge_lower_threshold` → a new insight.

Items are processed oldest first and new insights join the comparison set at once, so
two copies of a new question in the same run end up together. Judging is done with
standard requests rather than a batch: each decision can change what the next
question is compared against.

**Score.** Every insight's aggregates — item count, sources, evidence links, dates, the
five score components — are recomputed from its items on every run. The items are the
source of truth. One consequence: evidence links to YouTube comments disappear from an
insight once the 30-day purge blanks those items, with no separate step.

Columns the publish stage owns (`suggested_angle`, `status`, `last_served_at`,
`times_served`) are never written here.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .. import scoring
from ..budget import SERVICE_MESSAGES, UsageLedger
from ..config import AppConfig, ConfigError
from ..llm.classifier import ClassificationInvalid
from ..llm.client import LlmClient, LlmError, estimate_tokens
from ..llm.judge import DedupeJudge
from ..models import FeedRow, Insight, ItemRow, RunStats, make_insight_id
from ..schema import EMBEDDINGS, FEED, INSIGHTS, ITEMS
from ..store.sheets import SheetStore, SheetStoreError, open_store
from ..utils import embeddings as emb
from ..utils.clock import now_utc
from ..utils.logging import get_logger
from .collect import make_run_id
from .runs import record_run

log = get_logger(__name__)

#: The insight columns this stage owns. Everything else on the tab belongs to publish.
SCORE_COLUMNS = (
    "category", "tags", "item_count", "source_count", "sources", "evidence_urls",
    "first_seen", "last_seen", "recurrence_score", "cross_source_score", "engagement_score",
    "recency_score", "trend_score", "feedback_adjustment", "total_score", "refreshed_at",
)
EVIDENCE_LINKS = 3


class ScoreError(RuntimeError):
    """The stage could not run at all."""


@dataclass
class ScoreResult:
    run_id: str
    dry_run: bool = False
    new_insights: int = 0
    merged: int = 0
    judge_calls: int = 0
    judge_merges: int = 0
    deferred: int = 0
    reembedded: int = 0
    insights_total: int = 0
    engagement_updates: int = 0
    est_cost_usd: float = 0.0
    relevance_by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    top: list[tuple[float, str, int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run(
    config: AppConfig,
    *,
    dry_run: bool = False,
    run_id: str | None = None,
    store: SheetStore | None = None,
    llm: LlmClient | None = None,
    embedder: emb.Embedder | None = None,
    now: datetime | None = None,
) -> ScoreResult:
    result = ScoreResult(run_id=run_id or make_run_id(), dry_run=dry_run)
    now = now or now_utc()
    cfg = config.scoring

    if store is None:
        try:
            store = open_store(config, dry_run=dry_run)
        except (ConfigError, SheetStoreError) as error:
            raise ScoreError(f"Cannot open the spreadsheet: {error}") from error
    if embedder is None:
        embedder = emb.FastEmbedEmbedder(
            cfg.embeddings.model, cache_dir=cfg.embeddings.cache_dir,
            batch_size=cfg.embeddings.batch_size,
        )
    if llm is None and config.env.anthropic_api_key:
        llm = LlmClient(config.env.anthropic_api_key, config.budget)

    judge = DedupeJudge(cfg.dedupe.judge_model) if llm is not None else None
    if judge is not None and not dry_run:
        ledger = UsageLedger.from_store(config, store)
        candidates = ["y" * 120] * cfg.dedupe.judge_candidates_per_question
        sample = judge.request_params("x" * 120, candidates)
        price = config.budget.price_for(cfg.dedupe.judge_model)
        estimate = cfg.dedupe.max_judge_calls_per_run * (
            estimate_tokens(sample) / 1e6 * price.input_per_mtok + 50 / 1e6 * price.output_per_mtok
        )
        decision = ledger.allows_spend(estimate)
        if not decision:
            result.warnings.append(f"Judge disabled this run: {decision.reason}")
            judge = None
    if judge is None and llm is None:
        result.warnings.append(
            "ANTHROPIC_API_KEY is not set: questions in the judge band wait for a later run."
        )

    items = [ItemRow.from_sheet_row(r) for r in store.read_rows(ITEMS.name)]
    insights = {
        i.insight_id: i
        for i in (Insight.from_sheet_row(r) for r in store.read_rows(INSIGHTS.name))
        if i.insight_id
    }
    stored_vectors = {
        r["insight_id"]: (r.get("model", ""), r.get("vector", ""))
        for r in store.read_rows(EMBEDDINGS.name) if r.get("insight_id")
    }
    had_vector_row = set(stored_vectors)
    result.relevance_by_source = _relevance_by_source(items)

    # --- the comparison set: every insight's vector, re-embedding any that are stale --
    ids: list[str] = []
    rows: list[np.ndarray] = []
    vector_writes: dict[str, str] = {}
    stale = [
        insight_id for insight_id in insights
        if stored_vectors.get(insight_id, ("", ""))[0] != embedder.model
        or not stored_vectors.get(insight_id, ("", ""))[1]
    ]
    if stale:
        fresh = embedder.embed([insights[i].canonical_question for i in stale])
        for insight_id, vector in zip(stale, fresh, strict=True):
            vector_writes[insight_id] = emb.encode(vector)
            stored_vectors[insight_id] = (embedder.model, vector_writes[insight_id])
        result.reembedded = len(stale)
    for insight_id in insights:
        ids.append(insight_id)
        rows.append(emb.decode(stored_vectors[insight_id][1]))
    vectors_by_id = dict(zip(ids, rows, strict=True))

    # --- dedupe ----------------------------------------------------------------------
    pending = sorted(
        (i for i in items if i.status == "classified" and not i.insight_id and i.question),
        key=lambda i: (i.collected_at is None, i.collected_at or now),
    )
    assignments: dict[str, str] = {}
    touched: set[str] = set()
    new_ids: list[str] = []
    if pending:
        pending_vectors = embedder.embed([i.question or "" for i in pending])
        for item, vector in zip(pending, pending_vectors, strict=True):
            target = _match(
                item, vector, ids, rows, insights, cfg, judge, llm, result
            )
            if target == "defer":
                result.deferred += 1
                continue
            if target is None:
                insight_id = make_insight_id(item.question or "")
                if insight_id not in insights:
                    insights[insight_id] = Insight(
                        insight_id=insight_id,
                        canonical_question=item.question or "",
                        category=item.primary_category or "",
                        first_seen=scoring.item_date(item),
                        status="active",
                    )
                    new_ids.append(insight_id)
                    vector_writes[insight_id] = emb.encode(vector)
                    ids.append(insight_id)
                    rows.append(np.asarray(vector, dtype=np.float32))
                    vectors_by_id[insight_id] = rows[-1]
                    result.new_insights += 1
                else:
                    result.merged += 1  # same wording as an existing insight
                target = insight_id
            assignments[item.item_id] = target
            touched.add(target)

    # --- score -----------------------------------------------------------------------
    for item in items:
        if item.item_id in assignments:
            item.insight_id = assignments[item.item_id]
    linked: dict[str, list[ItemRow]] = {}
    for item in items:
        if item.insight_id and item.insight_id in insights:
            linked.setdefault(item.insight_id, []).append(item)

    engagement_pct = _engagement_percentiles(items)
    rejected = _rejected_vectors(store, vectors_by_id, now, cfg)
    enabled = len(config.enabled_sources)
    allowed_tags = {c.id: set(c.tags) for c in config.categories.categories}

    scored: dict[str, Insight] = {}
    for insight_id, members in linked.items():
        scored[insight_id] = _score(
            insights[insight_id], members, engagement_pct, rejected,
            vectors_by_id.get(insight_id), enabled, allowed_tags, cfg, now,
            refreshed=insight_id in touched,
        )
    result.insights_total = len(scored)
    result.top = sorted(
        ((i.total_score, i.category, i.item_count, i.source_count) for i in scored.values()),
        reverse=True,
    )[:10]

    item_updates: dict[str, dict[str, Any]] = {}
    for item in items:
        updates: dict[str, Any] = {}
        if item.item_id in assignments:
            updates["insight_id"] = assignments[item.item_id]
        pct = engagement_pct.get(item.item_id)
        changed = item.engagement_pct is None or (
            pct is not None and round(item.engagement_pct, 3) != round(pct, 3)
        )
        if pct is not None and item.status in {"classified", "purged"} and changed:
            updates["engagement_pct"] = round(pct, 3)
        if updates:
            item_updates[item.item_id] = updates
    result.engagement_updates = sum(1 for u in item_updates.values() if "engagement_pct" in u)

    if llm is not None:
        result.est_cost_usd = llm.usage.estimate_usd(config.budget)
    if dry_run:
        return result

    # --- write: vectors first, then insights, then the items that point at them -------
    store.append_rows(EMBEDDINGS.name, [
        {"insight_id": i, "model": embedder.model, "vector": v}
        for i, v in vector_writes.items() if i not in had_vector_row
    ])
    store.update_rows(EMBEDDINGS.name, {
        i: {"model": embedder.model, "vector": v}
        for i, v in vector_writes.items() if i in had_vector_row
    })

    new_rows = [scored[i].to_sheet_row() for i in new_ids if i in scored]
    store.append_rows(INSIGHTS.name, new_rows)
    updates = {
        insight_id: {k: v for k, v in insight.to_sheet_row().items() if k in SCORE_COLUMNS}
        for insight_id, insight in scored.items() if insight_id not in new_ids
    }
    store.update_rows(INSIGHTS.name, updates)
    store.update_rows(ITEMS.name, item_updates)

    _record(store, result, llm)
    return result


# --- dedupe helpers ------------------------------------------------------------------


def _match(
    item: ItemRow,
    vector: np.ndarray,
    ids: list[str],
    rows: list[np.ndarray],
    insights: dict[str, Insight],
    cfg,
    judge: DedupeJudge | None,
    llm: LlmClient | None,
    result: ScoreResult,
) -> str | None:
    """The insight this item belongs to, None for a new insight, or "defer"."""
    if not rows:
        return None
    sims = np.vstack(rows) @ np.asarray(vector, dtype=np.float32)
    order = np.argsort(-sims)
    best = float(sims[order[0]])
    if best >= cfg.dedupe.merge_threshold:
        result.merged += 1
        return ids[int(order[0])]
    if best < cfg.dedupe.judge_lower_threshold:
        return None

    candidates = [
        ids[int(j)] for j in order[: cfg.dedupe.judge_candidates_per_question]
        if float(sims[int(j)]) >= cfg.dedupe.judge_lower_threshold
    ]
    if judge is None or llm is None or result.judge_calls >= cfg.dedupe.max_judge_calls_per_run:
        return "defer"
    result.judge_calls += 1
    questions = [insights[c].canonical_question for c in candidates]
    try:
        reply = llm.create(**judge.request_params(item.question or "", questions))
        choice = judge.parse(reply, len(candidates))
    except (LlmError, ClassificationInvalid) as error:
        log.warning("score.judge_failed", extra={"error_type": type(error).__name__})
        return "defer"
    if choice is None:
        return None
    result.merged += 1
    result.judge_merges += 1
    return candidates[choice]


# --- scoring helpers -----------------------------------------------------------------


def _relevance_by_source(items: list[ItemRow]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for item in items:
        if item.status in {"classified", "irrelevant"}:
            bucket = counts.setdefault(item.source, {"relevant": 0, "irrelevant": 0})
            bucket["relevant" if item.status == "classified" else "irrelevant"] += 1
    return counts


def _engagement_percentiles(items: list[ItemRow]) -> dict[str, float]:
    by_source: dict[str, dict[str, float | None]] = {}
    for item in items:
        if item.status not in {"classified", "purged"}:
            continue
        by_source.setdefault(item.source, {})[item.item_id] = scoring.raw_engagement(
            item.source, item.engagement
        )
    out: dict[str, float] = {}
    for values in by_source.values():
        out.update(scoring.percentiles(values))
    return out


def _rejected_vectors(
    store: SheetStore, vectors: dict[str, np.ndarray], now: datetime, cfg
) -> list[np.ndarray]:
    since = now - timedelta(days=cfg.feedback.lookback_days)
    rejected = []
    for row in store.read_rows(FEED.name):
        feed = FeedRow.from_sheet_row(row)
        if feed.status != "rejected" or feed.insight_id not in vectors:
            continue
        if feed.served_at is not None and feed.served_at < since:
            continue
        rejected.append(vectors[feed.insight_id])
    return rejected


def _score(
    insight: Insight,
    members: list[ItemRow],
    engagement_pct: dict[str, float],
    rejected: list[np.ndarray],
    vector: np.ndarray | None,
    enabled_sources: int,
    allowed_tags: dict[str, set[str]],
    cfg,
    now: datetime,
    *,
    refreshed: bool,
) -> Insight:
    dates = [d for d in (scoring.item_date(m) for m in members) if d is not None]
    categories = Counter(m.primary_category for m in members if m.primary_category)
    category = categories.most_common(1)[0][0] if categories else insight.category
    tags = sorted({t for m in members for t in m.tags} & allowed_tags.get(category, set()))
    sources = sorted({m.source for m in members})
    with_urls = sorted(
        (m for m in members if m.url),
        key=lambda m: -engagement_pct.get(m.item_id, 0.5),
    )
    components = {
        "recurrence": scoring.recurrence(len(members), cfg),
        "cross_source": scoring.cross_source(sources, enabled_sources),
        "engagement": float(np.mean([engagement_pct.get(m.item_id, 0.5) for m in members])),
        "recency": scoring.recency(max(dates) if dates else None, now, cfg),
        "trend": scoring.trend(dates, now, cfg),
    }
    feedback = 0.0
    if rejected and vector is not None:
        closest = float(max(np.vstack(rejected) @ vector))
        if closest >= cfg.feedback.similarity_threshold:
            feedback = cfg.feedback.penalty
    return insight.model_copy(update={
        "category": category,
        "tags": tags,
        "item_count": len(members),
        "source_count": len(sources),
        "sources": sources,
        "evidence_urls": [m.url for m in with_urls[:EVIDENCE_LINKS]],
        "first_seen": min(dates) if dates else insight.first_seen,
        "last_seen": max(dates) if dates else insight.last_seen,
        "recurrence_score": components["recurrence"],
        "cross_source_score": components["cross_source"],
        "engagement_score": components["engagement"],
        "recency_score": components["recency"],
        "trend_score": components["trend"],
        "feedback_adjustment": feedback,
        "total_score": scoring.total(components, feedback, cfg),
        "refreshed_at": now if refreshed else insight.refreshed_at,
    })


def _record(store: SheetStore, result: ScoreResult, llm: LlmClient | None) -> None:
    usage = llm.usage if llm is not None else None
    stats = RunStats(
        run_id=result.run_id,
        trigger="schedule" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "dispatch",
        inputs={"score": {}},
        status="partial" if result.warnings else "success",
        new_insights=result.new_insights,
        merged_into_existing=result.merged,
        llm_judge_calls=result.judge_calls,
        tokens_in=usage.tokens_in if usage else 0,
        tokens_out=usage.tokens_out if usage else 0,
        tokens_cache_read=usage.tokens_cache_read if usage else 0,
        est_cost_usd=result.est_cost_usd,
        api_calls_by_service=(
            {SERVICE_MESSAGES: sum(usage.calls_by_model.values())} if usage else {}
        ),
        git_sha=os.environ.get("GITHUB_SHA", "")[:12],
        errors=[w[:200] for w in result.warnings[:10]],
        finished_at=now_utc(),
    )
    try:
        record_run(store, stats)
    except SheetStoreError as error:
        result.warnings.append(f"Could not write the runs row: {error}")


# --- reporting -----------------------------------------------------------------------


def summary_lines(result: ScoreResult) -> list[str]:
    """Counts and scores only — no question text, because logs are public."""
    suffix = " (dry run — nothing written)" if result.dry_run else ""
    lines = [
        f"Score run {result.run_id}{suffix}", "",
        f"  new insights      {result.new_insights}",
        f"  merged            {result.merged} (of which {result.judge_merges} after a judge call)",
        f"  judge calls       {result.judge_calls}",
        f"  deferred          {result.deferred} — wait for the next run",
        f"  insights scored   {result.insights_total}",
        f"  re-embedded       {result.reembedded}",
        f"  est. cost         ${result.est_cost_usd:.4f}",
    ]
    if result.relevance_by_source:
        lines += ["", "  Relevance by source (classified items):"]
        for source, counts in sorted(result.relevance_by_source.items()):
            total = counts["relevant"] + counts["irrelevant"]
            share = counts["relevant"] / total if total else 0.0
            lines.append(
                f"    {source:<16} {counts['relevant']:>4} of {total:<4} relevant ({share:.0%})"
            )
    if result.top:
        lines += ["", "  Top insights (score, category, items, sources):"]
        lines += [f"    {score:.3f}  {cat:<32} {n:>3}  {s}" for score, cat, n, s in result.top]
    if result.warnings:
        lines += ["", "  Warnings:"] + [f"    - {w}" for w in result.warnings[:10]]
    return lines


def write_job_summary(result: ScoreResult) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    body = "\n".join(["```", *summary_lines(result), "```", ""])
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"### Score — {result.run_id}\n\n{body}\n")
    except OSError:  # pragma: no cover
        pass
