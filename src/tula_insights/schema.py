"""The Google Sheets schema — the single source of truth for tabs and columns.

Every tab the pipeline reads or writes is declared here. `scripts/setup_sheet.py`
creates missing tabs from these specs and validates the headers of existing ones, and
`store/sheets.py` refuses to write a column that is not declared. Changing a column
means changing it here and nowhere else.

Column order is part of the contract: tabs are append-only in practice, and the content
team reads `feed` by column name, so columns are added at the end rather than inserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- status vocabularies -------------------------------------------------------------

#: `purged` marks a row whose platform-supplied text and numbers were deleted to meet a
#: platform's storage limit (YouTube allows 30 days). The row stays, so its insight
#: keeps its evidence count, but it carries nothing the platform supplied.
ITEM_STATUSES = ("new", "batch_pending", "classified", "irrelevant", "error", "purged")
INSIGHT_STATUSES = ("active", "retired")
FEED_STATUSES = ("served", "used", "rejected")
RUN_STATUSES = ("running", "success", "partial", "failed")
RUN_TRIGGERS = ("schedule", "dispatch")


@dataclass(frozen=True)
class TabSpec:
    """One tab in the Tula Insights spreadsheet."""

    name: str
    columns: tuple[str, ...]
    description: str = ""
    hidden: bool = False
    #: Columns a human reviewer or the content pipeline may edit. The pipeline must
    #: never overwrite these — see `store.sheets.SheetStore.update_rows`.
    human_editable: tuple[str, ...] = field(default_factory=tuple)
    #: Column holding the row's primary key, where the tab has one.
    key_column: str | None = None

    def index_of(self, column: str) -> int:
        """Zero-based position of `column`, raising a clear error if it is unknown."""
        try:
            return self.columns.index(column)
        except ValueError:
            raise KeyError(
                f"{self.name!r} has no column {column!r}. Known columns: "
                f"{', '.join(self.columns)}"
            ) from None


ITEMS = TabSpec(
    name="items",
    description="Every collected item and its classification.",
    key_column="item_id",
    columns=(
        # identity and provenance
        "item_id",
        "source",
        "external_id",
        "url",
        "title",
        "snippet",
        "published_at",
        "collected_at",
        "query",
        "region",
        # engagement
        "engagement_json",
        "engagement_pct",
        # pipeline state
        "run_id",
        "status",
        # classification
        "is_relevant",
        "primary_category",
        "tags",
        "question",
        "pain_point",
        "confidence",
        "model",
        "prompt_version",
        # linkage
        "insight_id",
        "error",
    ),
)

ITEMS_ARCHIVE = TabSpec(
    name="items_archive",
    description="Items older than the retention window, moved out of `items`.",
    key_column="item_id",
    columns=ITEMS.columns,
)

INSIGHTS = TabSpec(
    name="insights",
    description="Deduplicated questions, scored and ranked.",
    key_column="insight_id",
    columns=(
        "insight_id",
        "canonical_question",
        "category",
        "tags",
        "item_count",
        "source_count",
        "sources",
        "evidence_urls",
        "first_seen",
        "last_seen",
        "recurrence_score",
        "cross_source_score",
        "engagement_score",
        "recency_score",
        "trend_score",
        "feedback_adjustment",
        "total_score",
        "suggested_angle",
        "angle_generated_at",
        "status",
        "refreshed_at",
        "last_served_at",
        "times_served",
    ),
)

FEED = TabSpec(
    name="feed",
    description=(
        "The daily ranked output. This is the contract with the content pipeline: "
        "reviewers and the pipeline edit only the human_editable columns."
    ),
    key_column="insight_id",
    human_editable=("status", "rejection_reason", "post_ref", "reviewed_by"),
    columns=(
        "feed_date",
        "rank",
        "insight_id",
        "question",
        "category",
        "tags",
        "suggested_angle",
        "evidence_urls",
        "total_score",
        "served_at",
        "status",
        "rejection_reason",
        "post_ref",
        "reviewed_by",
    ),
)

MANUAL_SUBMISSIONS = TabSpec(
    name="manual_submissions",
    description=(
        "Compliant input path for Facebook, Instagram and anything else without a "
        "collector. People fill in every column except `processed`."
    ),
    human_editable=(
        "submitted_at",
        "submitted_by",
        "platform",
        "url",
        "question_or_quote",
        "notes",
    ),
    columns=(
        "submitted_at",
        "submitted_by",
        "platform",
        "url",
        "question_or_quote",
        "notes",
        "processed",
    ),
)

RUNS = TabSpec(
    name="runs",
    description="One row per pipeline run.",
    key_column="run_id",
    columns=(
        "run_id",
        "trigger",
        "inputs",
        "started_at",
        "finished_at",
        "status",
        "items_collected_by_source",
        "items_classified",
        "items_irrelevant",
        "new_insights",
        "merged_into_existing",
        "llm_judge_calls",
        "served_today",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "est_cost_usd",
        "api_calls_by_service",
        "actions_run_url",
        "git_sha",
        "errors",
    ),
)

EMBEDDINGS = TabSpec(
    name="_embeddings",
    description="Insight question vectors, base64-encoded float32. Do not edit.",
    hidden=True,
    key_column="insight_id",
    columns=("insight_id", "model", "vector"),
)

BATCHES = TabSpec(
    name="_batches",
    description="In-flight Claude Message Batches. Do not edit.",
    hidden=True,
    key_column="batch_id",
    columns=("batch_id", "stage", "submitted_at", "item_ids", "status", "completed_at"),
)

#: Every tab, in the order `setup_sheet.py` creates them.
TABS: tuple[TabSpec, ...] = (
    ITEMS,
    INSIGHTS,
    FEED,
    MANUAL_SUBMISSIONS,
    RUNS,
    ITEMS_ARCHIVE,
    EMBEDDINGS,
    BATCHES,
)

TABS_BY_NAME: dict[str, TabSpec] = {tab.name: tab for tab in TABS}


def tab(name: str) -> TabSpec:
    """Look up a tab spec by name."""
    try:
        return TABS_BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"Unknown tab {name!r}. Known tabs: {', '.join(TABS_BY_NAME)}"
        ) from None
