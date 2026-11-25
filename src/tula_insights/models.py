"""Domain models, and how each one maps to a row in its tab.

Each model owns its own conversion to and from a sheet row, so the column names in
:mod:`tula_insights.schema` are the only place a name is spelled. ``store/sheets.py``
moves rows; it never knows what a row means.

Identifiers are content hashes rather than counters: two collectors that find the same
page in the same run produce the same ``item_id``, so the append is idempotent without
a lock.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import schema
from .utils import serialise as ser
from .utils.clock import now_utc

MAX_SNIPPET_CHARS = 500

#: Query parameters stripped before an item_id is computed, so the same page found by
#: two different routes is one item.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_")


def canonical_url(url: str) -> str:
    """Normalise a URL for identity: lowercase host, no fragment, no tracking params."""
    parts = urlsplit(url.strip())
    query = "&".join(
        piece
        for piece in parts.query.split("&")
        if piece and not piece.lower().startswith(_TRACKING_PARAMS)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def make_item_id(source: str, external_id: str | None, url: str) -> str:
    """A stable 16-character id for an item.

    Keyed on the source's own id where it has one, and on the canonical URL otherwise,
    so re-collecting the same thing never creates a second row.
    """
    basis = f"{source}|{external_id}" if external_id else f"{source}|{canonical_url(url)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def make_insight_id(canonical_question: str) -> str:
    """A stable 16-character id for an insight, from its canonical question."""
    normalised = " ".join(canonical_question.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


# --- items ---------------------------------------------------------------------------


class RawItem(Model):
    """One thing a collector found, before classification."""

    item_id: str = ""
    source: str
    external_id: str | None = None
    url: str
    title: str = ""
    snippet: str = ""
    published_at: datetime | None = None
    collected_at: datetime = Field(default_factory=now_utc)
    query: str = ""
    region: str = ""
    engagement: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""

    @field_validator("snippet")
    @classmethod
    def _bounded(cls, value: str) -> str:
        value = " ".join(value.split())
        return value[:MAX_SNIPPET_CHARS]

    def with_id(self) -> RawItem:
        """Return a copy with `item_id` filled in, if it is not already."""
        if self.item_id:
            return self
        return self.model_copy(
            update={"item_id": make_item_id(self.source, self.external_id, self.url)}
        )

    def to_sheet_row(self) -> dict[str, Any]:
        item = self.with_id()
        return {
            "item_id": item.item_id,
            "source": item.source,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "snippet": item.snippet,
            "published_at": item.published_at,
            "collected_at": item.collected_at,
            "query": item.query,
            "region": item.region,
            "engagement_json": ser.dump_json(item.engagement),
            "engagement_pct": None,
            "run_id": item.run_id,
            "status": "new",
            "is_relevant": None,
            "primary_category": None,
            "tags": None,
            "question": None,
            "pain_point": None,
            "confidence": None,
            "model": None,
            "prompt_version": None,
            "insight_id": None,
            "error": None,
        }


class ItemRow(Model):
    """A row of the `items` tab, read back. Every field is optional: the tab holds items
    at every stage of the pipeline, and an unclassified row has most columns blank."""

    item_id: str
    source: str = ""
    external_id: str | None = None
    url: str = ""
    title: str = ""
    snippet: str = ""
    published_at: datetime | None = None
    collected_at: datetime | None = None
    query: str = ""
    region: str = ""
    engagement: dict[str, Any] = Field(default_factory=dict)
    engagement_pct: float | None = None
    run_id: str = ""
    status: str = "new"
    is_relevant: bool | None = None
    primary_category: str | None = None
    tags: list[str] = Field(default_factory=list)
    question: str | None = None
    pain_point: str | None = None
    confidence: float | None = None
    model: str | None = None
    prompt_version: str | None = None
    insight_id: str | None = None
    error: str | None = None

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> ItemRow:
        return cls(
            item_id=ser.read_str(row.get("item_id")) or "",
            source=ser.read_str(row.get("source")) or "",
            external_id=ser.read_str(row.get("external_id")),
            url=ser.read_str(row.get("url")) or "",
            title=ser.read_str(row.get("title")) or "",
            snippet=ser.read_str(row.get("snippet")) or "",
            published_at=ser.read_datetime(row.get("published_at")),
            collected_at=ser.read_datetime(row.get("collected_at")),
            query=ser.read_str(row.get("query")) or "",
            region=ser.read_str(row.get("region")) or "",
            engagement=ser.load_json(row.get("engagement_json"), default={}) or {},
            engagement_pct=ser.read_float(row.get("engagement_pct")),
            run_id=ser.read_str(row.get("run_id")) or "",
            status=ser.read_str(row.get("status")) or "new",
            is_relevant=ser.read_bool(row.get("is_relevant")),
            primary_category=ser.read_str(row.get("primary_category")),
            tags=ser.split_list(row.get("tags")),
            question=ser.read_str(row.get("question")),
            pain_point=ser.read_str(row.get("pain_point")),
            confidence=ser.read_float(row.get("confidence")),
            model=ser.read_str(row.get("model")),
            prompt_version=ser.read_str(row.get("prompt_version")),
            insight_id=ser.read_str(row.get("insight_id")),
            error=ser.read_str(row.get("error")),
        )


class Classification(Model):
    """What the classifier returns for one item. Validated against the model's JSON."""

    is_relevant: bool
    primary_category: str | None = None
    tags: list[str] = Field(default_factory=list)
    question: str | None = None
    pain_point: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    def to_updates(self, *, model: str, prompt_version: str) -> dict[str, Any]:
        """The `items` columns this classification sets."""
        return {
            "status": schema.ITEM_STATUSES[3] if not self.is_relevant else "classified",
            "is_relevant": self.is_relevant,
            "primary_category": self.primary_category,
            "tags": ser.join_list(self.tags),
            "question": self.question,
            "pain_point": self.pain_point,
            "confidence": round(self.confidence, 3),
            "model": model,
            "prompt_version": prompt_version,
            "error": "",
        }


# --- insights ------------------------------------------------------------------------


class Insight(Model):
    """A deduplicated question, with its evidence and its scores."""

    insight_id: str = ""
    canonical_question: str
    category: str
    tags: list[str] = Field(default_factory=list)
    item_count: int = 0
    source_count: int = 0
    sources: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    recurrence_score: float = 0.0
    cross_source_score: float = 0.0
    engagement_score: float = 0.0
    recency_score: float = 0.0
    trend_score: float = 0.0
    feedback_adjustment: float = 0.0
    total_score: float = 0.0
    suggested_angle: str | None = None
    angle_generated_at: datetime | None = None
    status: str = "active"
    refreshed_at: datetime | None = None
    last_served_at: datetime | None = None
    times_served: int = 0

    def with_id(self) -> Insight:
        if self.insight_id:
            return self
        return self.model_copy(update={"insight_id": make_insight_id(self.canonical_question)})

    def to_sheet_row(self) -> dict[str, Any]:
        insight = self.with_id()
        return {
            "insight_id": insight.insight_id,
            "canonical_question": insight.canonical_question,
            "category": insight.category,
            "tags": ser.join_list(insight.tags),
            "item_count": insight.item_count,
            "source_count": insight.source_count,
            "sources": ser.join_list(insight.sources),
            "evidence_urls": ser.join_list(insight.evidence_urls),
            "first_seen": insight.first_seen,
            "last_seen": insight.last_seen,
            "recurrence_score": round(insight.recurrence_score, 4),
            "cross_source_score": round(insight.cross_source_score, 4),
            "engagement_score": round(insight.engagement_score, 4),
            "recency_score": round(insight.recency_score, 4),
            "trend_score": round(insight.trend_score, 4),
            "feedback_adjustment": round(insight.feedback_adjustment, 4),
            "total_score": round(insight.total_score, 4),
            "suggested_angle": insight.suggested_angle,
            "angle_generated_at": insight.angle_generated_at,
            "status": insight.status,
            "refreshed_at": insight.refreshed_at,
            "last_served_at": insight.last_served_at,
            "times_served": insight.times_served,
        }

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> Insight:
        return cls(
            insight_id=ser.read_str(row.get("insight_id")) or "",
            canonical_question=ser.read_str(row.get("canonical_question")) or "",
            category=ser.read_str(row.get("category")) or "",
            tags=ser.split_list(row.get("tags")),
            item_count=ser.read_int(row.get("item_count"), 0) or 0,
            source_count=ser.read_int(row.get("source_count"), 0) or 0,
            sources=ser.split_list(row.get("sources")),
            evidence_urls=ser.split_list(row.get("evidence_urls")),
            first_seen=ser.read_datetime(row.get("first_seen")),
            last_seen=ser.read_datetime(row.get("last_seen")),
            recurrence_score=ser.read_float(row.get("recurrence_score"), 0.0) or 0.0,
            cross_source_score=ser.read_float(row.get("cross_source_score"), 0.0) or 0.0,
            engagement_score=ser.read_float(row.get("engagement_score"), 0.0) or 0.0,
            recency_score=ser.read_float(row.get("recency_score"), 0.0) or 0.0,
            trend_score=ser.read_float(row.get("trend_score"), 0.0) or 0.0,
            feedback_adjustment=ser.read_float(row.get("feedback_adjustment"), 0.0) or 0.0,
            total_score=ser.read_float(row.get("total_score"), 0.0) or 0.0,
            suggested_angle=ser.read_str(row.get("suggested_angle")),
            angle_generated_at=ser.read_datetime(row.get("angle_generated_at")),
            status=ser.read_str(row.get("status")) or "active",
            refreshed_at=ser.read_datetime(row.get("refreshed_at")),
            last_served_at=ser.read_datetime(row.get("last_served_at")),
            times_served=ser.read_int(row.get("times_served"), 0) or 0,
        )


# --- feed ----------------------------------------------------------------------------


class FeedRow(Model):
    """One published row. The contract with the content pipeline."""

    feed_date: str
    rank: int
    insight_id: str
    question: str
    category: str
    tags: list[str] = Field(default_factory=list)
    suggested_angle: str = ""
    evidence_urls: list[str] = Field(default_factory=list)
    total_score: float = 0.0
    served_at: datetime | None = None
    status: str = "served"
    rejection_reason: str = ""
    post_ref: str = ""
    reviewed_by: str = ""

    def to_sheet_row(self) -> dict[str, Any]:
        return {
            "feed_date": self.feed_date,
            "rank": self.rank,
            "insight_id": self.insight_id,
            "question": self.question,
            "category": self.category,
            "tags": ser.join_list(self.tags),
            "suggested_angle": self.suggested_angle,
            "evidence_urls": ser.join_list(self.evidence_urls),
            "total_score": round(self.total_score, 4),
            "served_at": self.served_at or now_utc(),
            "status": self.status,
            # Reviewer columns are written blank on creation and never touched again.
            "rejection_reason": "",
            "post_ref": "",
            "reviewed_by": "",
        }

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> FeedRow:
        return cls(
            feed_date=ser.read_str(row.get("feed_date")) or "",
            rank=ser.read_int(row.get("rank"), 0) or 0,
            insight_id=ser.read_str(row.get("insight_id")) or "",
            question=ser.read_str(row.get("question")) or "",
            category=ser.read_str(row.get("category")) or "",
            tags=ser.split_list(row.get("tags")),
            suggested_angle=ser.read_str(row.get("suggested_angle")) or "",
            evidence_urls=ser.split_list(row.get("evidence_urls")),
            total_score=ser.read_float(row.get("total_score"), 0.0) or 0.0,
            served_at=ser.read_datetime(row.get("served_at")),
            status=ser.read_str(row.get("status")) or "served",
            rejection_reason=ser.read_str(row.get("rejection_reason")) or "",
            post_ref=ser.read_str(row.get("post_ref")) or "",
            reviewed_by=ser.read_str(row.get("reviewed_by")) or "",
        )


# --- manual submissions --------------------------------------------------------------


class ManualSubmission(Model):
    """A row somebody typed into `manual_submissions`."""

    submitted_at: datetime | None = None
    submitted_by: str = ""
    platform: str = ""
    url: str = ""
    question_or_quote: str = ""
    notes: str = ""
    processed: bool = False

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> ManualSubmission:
        return cls(
            submitted_at=ser.read_datetime(row.get("submitted_at")),
            submitted_by=ser.read_str(row.get("submitted_by")) or "",
            platform=ser.read_str(row.get("platform")) or "",
            url=ser.read_str(row.get("url")) or "",
            question_or_quote=ser.read_str(row.get("question_or_quote")) or "",
            notes=ser.read_str(row.get("notes")) or "",
            processed=bool(ser.read_bool(row.get("processed"), False)),
        )


# --- runs ----------------------------------------------------------------------------


class RunStats(Model):
    """Everything the `runs` tab records about one pipeline run."""

    run_id: str
    trigger: str = "dispatch"
    inputs: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None
    status: str = "running"
    items_collected_by_source: dict[str, int] = Field(default_factory=dict)
    items_classified: int = 0
    items_irrelevant: int = 0
    new_insights: int = 0
    merged_into_existing: int = 0
    llm_judge_calls: int = 0
    served_today: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    est_cost_usd: float = 0.0
    api_calls_by_service: dict[str, int] = Field(default_factory=dict)
    actions_run_url: str = ""
    git_sha: str = ""
    errors: list[str] = Field(default_factory=list)

    def to_sheet_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "inputs": ser.dump_json(self.inputs),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "items_collected_by_source": ser.dump_json(self.items_collected_by_source),
            "items_classified": self.items_classified,
            "items_irrelevant": self.items_irrelevant,
            "new_insights": self.new_insights,
            "merged_into_existing": self.merged_into_existing,
            "llm_judge_calls": self.llm_judge_calls,
            "served_today": self.served_today,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cache_read": self.tokens_cache_read,
            "est_cost_usd": round(self.est_cost_usd, 4),
            "api_calls_by_service": ser.dump_json(self.api_calls_by_service),
            "actions_run_url": self.actions_run_url,
            "git_sha": self.git_sha,
            # Short error lines only — this tab is as public as the repo.
            "errors": ser.join_list(e[:200] for e in self.errors),
        }

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> RunStats:
        return cls(
            run_id=ser.read_str(row.get("run_id")) or "",
            trigger=ser.read_str(row.get("trigger")) or "dispatch",
            inputs=ser.load_json(row.get("inputs"), default={}) or {},
            started_at=ser.read_datetime(row.get("started_at")) or now_utc(),
            finished_at=ser.read_datetime(row.get("finished_at")),
            status=ser.read_str(row.get("status")) or "running",
            items_collected_by_source=ser.load_json(
                row.get("items_collected_by_source"), default={}
            )
            or {},
            items_classified=ser.read_int(row.get("items_classified"), 0) or 0,
            items_irrelevant=ser.read_int(row.get("items_irrelevant"), 0) or 0,
            new_insights=ser.read_int(row.get("new_insights"), 0) or 0,
            merged_into_existing=ser.read_int(row.get("merged_into_existing"), 0) or 0,
            llm_judge_calls=ser.read_int(row.get("llm_judge_calls"), 0) or 0,
            served_today=ser.read_int(row.get("served_today"), 0) or 0,
            tokens_in=ser.read_int(row.get("tokens_in"), 0) or 0,
            tokens_out=ser.read_int(row.get("tokens_out"), 0) or 0,
            tokens_cache_read=ser.read_int(row.get("tokens_cache_read"), 0) or 0,
            est_cost_usd=ser.read_float(row.get("est_cost_usd"), 0.0) or 0.0,
            api_calls_by_service=ser.load_json(row.get("api_calls_by_service"), default={})
            or {},
            actions_run_url=ser.read_str(row.get("actions_run_url")) or "",
            git_sha=ser.read_str(row.get("git_sha")) or "",
            errors=ser.split_list(row.get("errors")),
        )
