"""The search interface every collector talks to.

Only `claude_web_search` implements it today — the Claude API web search tool is the
one search provider the project is allowed to pay for (§2 of the brief). The interface
exists so that adding another provider later is a new module and a config value, not a
change to any collector.

A provider's job is narrow: given phrases, return the results the search engine
actually returned. It does not judge relevance, deduplicate across runs or write
anything. Collectors do that.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class SearchError(RuntimeError):
    """Raised when a search cannot be performed at all.

    A search that runs and returns nothing is not an error — it is an empty result
    list. This is for a provider that could not be reached or was refused.
    """


class SearchRequest(BaseModel):
    """One unit of search work: a set of phrases, run under one set of restrictions."""

    model_config = ConfigDict(extra="forbid")

    phrases: list[str]
    #: Free-text context for the model about what counts as a useful result.
    intent: str = ""
    #: Only return results from these domains. Bare domains, optional path, no scheme.
    allowed_domains: list[str] = Field(default_factory=list)
    #: Never return results from these domains. Mutually exclusive with allowed_domains.
    blocked_domains: list[str] = Field(default_factory=list)
    #: Hard ceiling on searches for this request.
    max_uses: int = 3
    #: Region label carried through onto every result, for the item row.
    region: str = ""
    #: Optional locality hint passed to the provider.
    country: str | None = None


class SearchResult(BaseModel):
    """One result, exactly as the search engine returned it.

    Note what is *not* here: there is no snippet. The Claude web search tool returns
    `url`, `title`, `page_age` and an opaque `encrypted_content` blob — no readable
    excerpt. Where a snippet exists it came from a citation, which is text the model
    quoted from the page, and `snippet_source` records that.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    #: Raw page-age string from the provider, e.g. "April 30, 2025". Not parsed here.
    page_age: str | None = None
    #: Zero-based position within the search that returned it. The only ranking signal.
    position: int = 0
    #: The query that found it.
    query: str = ""
    #: How many distinct queries in this request returned it.
    recurrence: int = 1
    #: Quoted text from the page, when the model cited it. Empty otherwise.
    snippet: str = ""
    snippet_source: str = ""


class SearchResponse(BaseModel):
    """Everything one :class:`SearchRequest` produced."""

    model_config = ConfigDict(extra="forbid")

    results: list[SearchResult] = Field(default_factory=list)
    #: The queries the provider actually ran, in order.
    queries: list[str] = Field(default_factory=list)
    #: Billable searches, from the provider's own usage report — not len(queries).
    search_count: int = 0
    #: Provider error codes encountered. Non-fatal: some searches may still have worked.
    error_codes: list[str] = Field(default_factory=list)
    stop_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def ok(self) -> bool:
        return not self.error_codes


@runtime_checkable
class SearchProvider(Protocol):
    """What a collector may assume about a search provider."""

    name: str

    def search(self, request: SearchRequest) -> SearchResponse:
        """Run the request and return what the engine returned."""
        ...
