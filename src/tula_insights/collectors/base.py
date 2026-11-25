"""What every collector is, and what it is given.

A collector finds things. It does not judge relevance, deduplicate against history,
score anything or write to the sheet — the stage around it does all of that. Keeping
collectors this narrow is what makes adding a source a new module and a YAML entry
rather than a change to the pipeline.

The contract is deliberately small:

    class Collector(Protocol):
        name: str
        def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]: ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..config import AppConfig, ResolvedSource
from ..models import RawItem

if TYPE_CHECKING:  # pragma: no cover
    from ..budget import UsageLedger
    from ..llm.client import LlmClient
    from ..search.provider import SearchProvider
    from ..store.sheets import SheetStore


class CollectorError(RuntimeError):
    """A collector could not run. One failing source must not stop the others."""


@dataclass
class RunContext:
    """Everything a collector is allowed to reach."""

    run_id: str
    config: AppConfig
    ledger: UsageLedger
    store: SheetStore | None = None
    llm: LlmClient | None = None
    search: SearchProvider | None = None
    dry_run: bool = False
    #: Restrict collection to one category id, from `--category`. Empty means all.
    category: str = ""
    #: Item ids already in the sheet for this source, so a collector can skip them.
    known_item_ids: set[str] = field(default_factory=set)
    #: Non-fatal problems, surfaced in the job summary and the `runs` row.
    warnings: list[str] = field(default_factory=list)
    #: Named counts for the run summary. Numbers only — the summary is public.
    metrics: dict[str, int] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def count(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def require_search(self) -> SearchProvider:
        if self.search is None:
            raise CollectorError(
                "This collector needs a search provider, but none was configured. "
                "ANTHROPIC_API_KEY is probably missing."
            )
        return self.search


@runtime_checkable
class Collector(Protocol):
    """The whole collector interface."""

    name: str

    def collect(self, cfg: ResolvedSource, ctx: RunContext) -> list[RawItem]:
        """Find items. Return them; do not write them."""
        ...


@runtime_checkable
class AfterWrite(Protocol):
    """Optional. A collector that needs to record something once its items are safely
    in the sheet — such as marking manual submissions processed — implements this.

    It runs only after a successful, real write: never on a dry run, never if the
    write failed. That ordering is what stops a submission being marked processed and
    then lost.
    """

    def after_write(self, items: list[RawItem], ctx: RunContext) -> None: ...
