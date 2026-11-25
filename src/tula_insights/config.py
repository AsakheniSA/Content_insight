"""Loads and validates every YAML config file, plus the environment.

Nothing in the pipeline reads a YAML file directly. Everything goes through
:func:`load_config`, which parses the six files in ``config/`` into Pydantic models and
then runs the cross-file checks that a per-file schema cannot express — that every
keyword set names a real category, that the score weights add up, that the publish
rules are satisfiable.

A config mistake should fail loudly at startup, before a run spends money, so the
validators here are deliberately strict: unknown keys are rejected rather than ignored.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

CONFIG_FILES = (
    "categories.yaml",
    "sources.yaml",
    "keywords.yaml",
    "scoring.yaml",
    "budget.yaml",
    "settings.yaml",
)


class ConfigError(RuntimeError):
    """Raised when config is missing, malformed or internally inconsistent."""


class Base(BaseModel):
    """Strict base: an unknown key is a typo, not an extension point."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())


# --- categories.yaml -----------------------------------------------------------------


class Category(Base):
    id: str
    name: str
    description: str
    example_questions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not SNAKE_CASE.match(value):
            raise ValueError(f"category id {value!r} must be snake_case")
        return value

    @field_validator("description")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return " ".join(value.split())


class CategoriesConfig(Base):
    version: int
    categories: list[Category]

    @model_validator(mode="after")
    def _unique_ids(self) -> CategoriesConfig:
        if not self.categories:
            raise ValueError("at least one category is required")
        seen: set[str] = set()
        for category in self.categories:
            if category.id in seen:
                raise ValueError(f"duplicate category id {category.id!r}")
            seen.add(category.id)
        return self

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.categories]

    def by_id(self, category_id: str) -> Category:
        for category in self.categories:
            if category.id == category_id:
                return category
        raise KeyError(f"unknown category {category_id!r}")


# --- sources.yaml --------------------------------------------------------------------


class SourceDefaults(Base):
    enabled: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    requests_per_minute: int = 30
    daily_request_cap: int = 50
    max_items_per_run: int = 200


class SourceConfig(Base):
    name: str
    description: str = ""
    regions: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    enabled: bool | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    backoff_base_seconds: float | None = None
    backoff_max_seconds: float | None = None
    requests_per_minute: int | None = None
    daily_request_cap: int | None = None
    max_items_per_run: int | None = None

    @field_validator("name")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not SNAKE_CASE.match(value):
            raise ValueError(f"source name {value!r} must be snake_case")
        return value

    def resolved(self, defaults: SourceDefaults) -> ResolvedSource:
        """Fill unset fields from the defaults block."""
        values = defaults.model_dump()
        for key in list(values):
            override = getattr(self, key, None)
            if override is not None:
                values[key] = override
        return ResolvedSource(
            name=self.name,
            description=self.description,
            regions=self.regions,
            settings=self.settings,
            **values,
        )


class ResolvedSource(Base):
    """A source with every default filled in. This is what collectors receive."""

    name: str
    description: str
    regions: list[str]
    settings: dict[str, Any]
    enabled: bool
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    requests_per_minute: int
    daily_request_cap: int
    max_items_per_run: int


class SourcesConfig(Base):
    version: int
    defaults: SourceDefaults = Field(default_factory=SourceDefaults)
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def _unique_names(self) -> SourcesConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name {source.name!r}")
            seen.add(source.name)
        return self

    def resolve(self) -> list[ResolvedSource]:
        return [s.resolved(self.defaults) for s in self.sources]

    def enabled(self) -> list[ResolvedSource]:
        return [s for s in self.resolve() if s.enabled]


# --- keywords.yaml -------------------------------------------------------------------


class Region(Base):
    name: str
    modifiers: list[str] = Field(default_factory=list)


class KeywordSet(Base):
    id: str
    category: str
    regions: list[str]
    phrases: list[str]

    @field_validator("phrases")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("a keyword set needs at least one phrase")
        return value


class KeywordsConfig(Base):
    version: int
    regions: dict[str, Region]
    seed_phrases: list[str] = Field(default_factory=list)
    keyword_sets: list[KeywordSet]

    @model_validator(mode="after")
    def _unique_ids(self) -> KeywordsConfig:
        seen: set[str] = set()
        for keyword_set in self.keyword_sets:
            if keyword_set.id in seen:
                raise ValueError(f"duplicate keyword set id {keyword_set.id!r}")
            seen.add(keyword_set.id)
        return self

    def for_category(self, category_id: str) -> list[KeywordSet]:
        return [k for k in self.keyword_sets if k.category == category_id]


# --- scoring.yaml --------------------------------------------------------------------


class EmbeddingsConfig(Base):
    model: str
    dimensions: int = Field(gt=0)
    cache_dir: str = ".cache/fastembed"
    batch_size: int = Field(default=64, gt=0)


class DedupeConfig(Base):
    judge_model: str = "claude-haiku-5-5"
    merge_threshold: float = Field(ge=0.0, le=1.0)
    judge_lower_threshold: float = Field(ge=0.0, le=1.0)
    max_judge_calls_per_run: int = Field(gt=0)
    judge_candidates_per_question: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> DedupeConfig:
        if self.judge_lower_threshold >= self.merge_threshold:
            raise ValueError(
                "judge_lower_threshold must be below merge_threshold, otherwise the "
                "judge band is empty and every near-duplicate is merged blindly"
            )
        return self


class Weights(Base):
    recurrence: float = Field(ge=0.0)
    cross_source: float = Field(ge=0.0)
    engagement: float = Field(ge=0.0)
    recency: float = Field(ge=0.0)
    trend: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Weights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"score weights must sum to 1.0, got {total:.6f}")
        return self


class RecurrenceConfig(Base):
    saturation_count: int = Field(gt=1)


class RecencyConfig(Base):
    half_life_days: float = Field(gt=0.0)


class TrendConfig(Base):
    window_days: int = Field(gt=0)
    baseline_days: int = Field(gt=0)
    ratio_cap: float = Field(gt=0.0)


class FeedbackConfig(Base):
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    penalty: float = Field(le=0.0)
    lookback_days: int = Field(gt=0)


class PublishConfig(Base):
    daily_feed_size: int = Field(gt=0)
    max_per_category: int = Field(gt=0)
    min_general_pet_care: int = Field(ge=0)
    max_general_pet_care: int = Field(ge=0)
    cooldown_days: int = Field(ge=0)
    min_total_score: float = Field(ge=0.0, le=1.0)
    angle_model: str

    @model_validator(mode="after")
    def _satisfiable(self) -> PublishConfig:
        if self.max_per_category > self.daily_feed_size:
            raise ValueError("max_per_category cannot exceed daily_feed_size")
        if self.min_general_pet_care > self.max_general_pet_care:
            raise ValueError("min_general_pet_care cannot exceed max_general_pet_care")
        if self.min_general_pet_care > self.daily_feed_size:
            raise ValueError("min_general_pet_care cannot exceed daily_feed_size")
        return self


class ScoringConfig(Base):
    version: int
    embeddings: EmbeddingsConfig
    dedupe: DedupeConfig
    weights: Weights
    recurrence: RecurrenceConfig
    recency: RecencyConfig
    trend: TrendConfig
    feedback: FeedbackConfig
    publish: PublishConfig


# --- budget.yaml ---------------------------------------------------------------------


class ModelPrice(Base):
    input_per_mtok: float = Field(ge=0.0)
    output_per_mtok: float = Field(ge=0.0)
    cache_read_per_mtok: float = Field(default=0.0, ge=0.0)
    batch_input_per_mtok: float | None = Field(default=None, ge=0.0)
    batch_output_per_mtok: float | None = Field(default=None, ge=0.0)


class Prices(Base):
    claude_web_search_per_1000_searches: float = Field(ge=0.0)
    models: dict[str, ModelPrice]


class MonthlyCaps(Base):
    claude_usd: float = Field(ge=0.0)
    claude_web_searches: int = Field(ge=0)
    youtube_searches: int = Field(ge=0)


class DailyCaps(Base):
    claude_web_searches: int = Field(ge=0)
    youtube_searches: int = Field(ge=0)


class BudgetLimits(Base):
    max_input_tokens_per_request: int = Field(gt=0)
    long_context_threshold_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _well_under(self) -> BudgetLimits:
        if self.max_input_tokens_per_request >= self.long_context_threshold_tokens:
            raise ValueError(
                "max_input_tokens_per_request must stay below "
                "long_context_threshold_tokens, where requests are billed at a "
                "multiple of standard rates"
            )
        return self


class BudgetConfig(Base):
    version: int
    prices_checked_on: str
    prices_source: str = ""
    monthly_caps: MonthlyCaps
    daily_caps: DailyCaps
    on_cap_exceeded: str = "skip"
    prices: Prices
    limits: BudgetLimits

    @field_validator("on_cap_exceeded")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in {"skip", "fail"}:
            raise ValueError("on_cap_exceeded must be 'skip' or 'fail'")
        return value

    @model_validator(mode="after")
    def _daily_within_monthly(self) -> BudgetConfig:
        if self.daily_caps.claude_web_searches > self.monthly_caps.claude_web_searches:
            raise ValueError("daily claude_web_searches cap exceeds the monthly cap")
        if self.daily_caps.youtube_searches > self.monthly_caps.youtube_searches:
            raise ValueError("daily youtube_searches cap exceeds the monthly cap")
        return self

    def price_for(self, model: str) -> ModelPrice:
        try:
            return self.prices.models[model]
        except KeyError:
            raise ConfigError(
                f"no price table entry for model {model!r}. Add it to "
                f"config/budget.yaml under prices.models."
            ) from None


# --- settings.yaml -------------------------------------------------------------------


class SheetSettings(Base):
    spreadsheet_name: str
    cell_limit: int = Field(gt=0)
    archive_threshold_fraction: float = Field(gt=0.0, le=1.0)
    reads_per_minute: int = Field(gt=0, le=60)
    writes_per_minute: int = Field(gt=0, le=60)
    max_retries: int = Field(ge=0)
    backoff_base_seconds: float = Field(gt=0.0)
    backoff_max_seconds: float = Field(gt=0.0)
    new_tab_rows: int = Field(gt=0)


class RetentionSettings(Base):
    items_archive_after_days: int = Field(gt=0)
    #: Source name -> the longest its platform lets us keep what its API returned.
    #: Rows from these sources are purged at that age instead of being archived.
    api_data_max_days: dict[str, int] = Field(default_factory=dict)

    @field_validator("api_data_max_days")
    @classmethod
    def _positive(cls, value: dict[str, int]) -> dict[str, int]:
        for source, days in value.items():
            if days <= 0:
                raise ValueError(f"api_data_max_days.{source} must be a positive number of days")
        return value


class RunSettings(Base):
    local_utc_offset_hours: int
    local_timezone_label: str


class LoggingSettings(Base):
    level: str = "INFO"
    format: str = "json"
    allow_item_text: bool = False

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        if value.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level {value!r}")
        return value.upper()

    @field_validator("format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in {"json", "plain"}:
            raise ValueError("log format must be 'json' or 'plain'")
        return value


class ClassifySettings(Base):
    model: str
    effort: str = "low"
    max_tokens: int = Field(default=2048, gt=0)
    max_items_per_run: int = Field(default=500, gt=0)
    batch_poll_minutes: float = Field(default=15.0, ge=0.0)
    batch_poll_interval_seconds: float = Field(default=30.0, gt=0.0)
    dry_run_sample: int = Field(default=10, gt=0)
    write_every: int = Field(default=50, gt=0)
    max_question_chars: int = Field(default=300, gt=20)

    @field_validator("effort")
    @classmethod
    def _known_effort(cls, value: str) -> str:
        if value not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("effort must be one of low, medium, high, xhigh, max")
        return value


class Settings(Base):
    version: int
    sheet: SheetSettings
    retention: RetentionSettings
    run: RunSettings
    logging: LoggingSettings
    classify: ClassifySettings


# --- environment ---------------------------------------------------------------------


class Env(BaseModel):
    """Secrets and IDs from the environment. Never logged, never written to the sheet."""

    model_config = ConfigDict(extra="ignore")

    anthropic_api_key: str | None = None
    google_service_account_json: str | None = None
    sheet_id: str | None = None
    youtube_api_key: str | None = None

    def require(self, *names: str) -> None:
        """Fail with an actionable message if any named variable is missing."""
        missing = [n for n in names if not getattr(self, n, None)]
        if not missing:
            return
        env_names = ", ".join(n.upper() for n in missing)
        where = (
            "Add them under Settings > Secrets and variables > Actions > Secrets."
            if os.environ.get("GITHUB_ACTIONS") == "true"
            else "Copy .env.example to .env and fill them in."
        )
        raise ConfigError(f"Missing required environment variable(s): {env_names}. {where}")

    def service_account_info(self) -> dict[str, Any]:
        """Decode the service-account key, accepting base64 (preferred) or raw JSON."""
        raw = (self.google_service_account_json or "").strip()
        if not raw:
            raise ConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is empty. It holds the service-account key, "
                "base64-encoded to a single line. See .env.example for the encode command."
            )
        if not raw.lstrip().startswith("{"):
            try:
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ConfigError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is neither valid base64 nor raw JSON. "
                    "Re-encode the key file: base64 -w0 service_account.json"
                ) from exc
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON decoded to text that is not valid JSON. "
                "It must be the whole key file, from the first { to the last }."
            ) from exc
        for required in ("client_email", "private_key", "token_uri"):
            if required not in info:
                raise ConfigError(
                    f"The service-account key is missing {required!r}. Download a fresh "
                    "JSON key from the service account's Keys tab."
                )
        return info


def load_env(root: Path | None = None) -> Env:
    """Read the environment, loading a local ``.env`` first if one exists."""
    _load_dotenv(root or ROOT)
    return Env(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        google_service_account_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
        sheet_id=os.environ.get("SHEET_ID") or None,
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY") or None,
    )


def _load_dotenv(root: Path) -> None:
    """Minimal ``.env`` reader, so the project needs no extra dependency.

    Existing environment variables always win, so a GitHub secret is never shadowed by
    a stray local file.
    """
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --- the whole thing -----------------------------------------------------------------


class AppConfig(Base):
    categories: CategoriesConfig
    sources: SourcesConfig
    keywords: KeywordsConfig
    scoring: ScoringConfig
    budget: BudgetConfig
    settings: Settings
    env: Env

    @model_validator(mode="after")
    def _cross_file_checks(self) -> AppConfig:
        category_ids = set(self.categories.ids)
        region_ids = set(self.keywords.regions)

        for keyword_set in self.keywords.keyword_sets:
            if keyword_set.category not in category_ids:
                raise ValueError(
                    f"keyword set {keyword_set.id!r} names category "
                    f"{keyword_set.category!r}, which is not in categories.yaml"
                )
            for region in keyword_set.regions:
                if region not in region_ids:
                    raise ValueError(
                        f"keyword set {keyword_set.id!r} names region {region!r}, "
                        f"which is not in keywords.yaml regions"
                    )

        for source in self.sources.sources:
            for region in source.regions:
                if region not in region_ids:
                    raise ValueError(
                        f"source {source.name!r} names region {region!r}, which is not "
                        f"in keywords.yaml regions"
                    )

        angle_model = self.scoring.publish.angle_model
        if angle_model not in self.budget.prices.models:
            raise ValueError(
                f"scoring.publish.angle_model is {angle_model!r} but budget.yaml has no "
                f"price for it, so its cost could not be tracked"
            )

        judge_model = self.scoring.dedupe.judge_model
        if judge_model not in self.budget.prices.models:
            raise ValueError(
                f"scoring.dedupe.judge_model is {judge_model!r} but budget.yaml has no "
                f"price for it, so its cost could not be tracked"
            )

        classify_model = self.settings.classify.model
        if classify_model not in self.budget.prices.models:
            raise ValueError(
                f"settings.classify.model is {classify_model!r} but budget.yaml has no "
                f"price for it, so its cost could not be tracked"
            )

        uncovered = category_ids - {k.category for k in self.keywords.keyword_sets}
        if uncovered:
            raise ValueError(
                "every category needs at least one keyword set, otherwise it can never "
                f"be discovered. Missing: {', '.join(sorted(uncovered))}"
            )

        return self

    @property
    def enabled_sources(self) -> list[ResolvedSource]:
        return self.sources.enabled()


def config_dir(root: Path | None = None) -> Path:
    """Where the YAML lives. ``TULA_CONFIG_DIR`` overrides it, for tests."""
    override = os.environ.get("TULA_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return (root or ROOT) / "config"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if data is None:
        raise ConfigError(f"{path.name} is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping at the top level")
    return data


def _parse(model: type[BaseModel], path: Path) -> Any:
    try:
        return model.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path.name} is invalid:\n{_explain(exc)}") from exc


def _explain(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(root: Path | None = None) -> AppConfig:
    """Load, parse and validate every config file plus the environment."""
    directory = config_dir(root)
    try:
        app = AppConfig(
            categories=_parse(CategoriesConfig, directory / "categories.yaml"),
            sources=_parse(SourcesConfig, directory / "sources.yaml"),
            keywords=_parse(KeywordsConfig, directory / "keywords.yaml"),
            scoring=_parse(ScoringConfig, directory / "scoring.yaml"),
            budget=_parse(BudgetConfig, directory / "budget.yaml"),
            settings=_parse(Settings, directory / "settings.yaml"),
            env=load_env(root),
        )
    except ValidationError as exc:
        raise ConfigError(f"Config is internally inconsistent:\n{_explain(exc)}") from exc
    return app


@lru_cache(maxsize=1)
def cached_config() -> AppConfig:
    """`load_config` memoised, for code paths that need it repeatedly."""
    return load_config()
