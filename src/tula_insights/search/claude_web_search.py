"""Search discovery through the Claude API web search tool.

**Why the results are read from the tool output, not from what the model writes.**

The brief's rule is that only URLs the search tool actually returned may be kept, so
that the model cannot invent one. This implementation takes that to its conclusion: the
model is never asked for a list of URLs at all. Every result is read out of the
`web_search_tool_result` blocks in the response, which are the search engine's own
output. There is no model-authored URL anywhere in the path, so there is nothing to
cross-check and nothing that can be invented. It is also cheaper — the model writes one
line instead of a JSON document.

**What the tool returns.** A `web_search_result` carries `url`, `title`, `page_age` and
an opaque `encrypted_content` blob. There is **no snippet field** (verified against the
API docs and the SDK's own `WebSearchResultBlock`). Where this module reports a snippet
it came from a citation — text the model quoted from the page — and it is labelled as
such. Pages the model did not cite simply have no snippet, and the classifier works
from the title alone.

**Tool version.** `web_search_20250305`, the basic tool, deliberately. The newer
`web_search_20260209` and `_20260318` add dynamic filtering, which runs the search
inside code execution so the model can discard results before they reach its context.
That is the right trade-off when the model is answering a question. Here the whole
point is to harvest every result, so filtering them away first would throw away the
output. The basic tool calls search directly and returns plain, top-level result
blocks.
"""

from __future__ import annotations

from typing import Any

import anthropic

from ..llm.client import LlmClient, LlmError
from ..llm.prompts import load_prompt
from ..utils.logging import get_logger
from .provider import SearchError, SearchRequest, SearchResponse, SearchResult

log = get_logger(__name__)

#: The basic web search tool. See the module docstring for why not the newer versions.
TOOL_TYPE = "web_search_20250305"
TOOL_NAME = "web_search"

#: How many times a `pause_turn` may be resumed before giving up on a request.
MAX_RESUMES = 4

#: Enough room for the short acknowledgement the prompt asks for, plus the server-side
#: tool loop's own bookkeeping. The results do not come out of this budget.
MAX_TOKENS = 2048


class ClaudeWebSearchProvider:
    """Runs searches with the Claude API's server-side web search tool."""

    name = "claude_web_search"

    def __init__(
        self,
        llm: LlmClient,
        *,
        model: str = "claude-haiku-5-5",
        effort: str = "low",
        prompt_name: str = "discover_search",
    ) -> None:
        self.llm = llm
        self.model = model
        self.effort = effort
        self.prompt = load_prompt(prompt_name)

    # --- public ----------------------------------------------------------------------

    def search(self, request: SearchRequest) -> SearchResponse:
        if not request.phrases:
            return SearchResponse()
        if request.allowed_domains and request.blocked_domains:
            raise SearchError(
                "allowed_domains and blocked_domains cannot both be set — the Claude "
                "API rejects a request that sends both. Fix the source's settings in "
                "config/sources.yaml."
            )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._user_message(request)}
        ]
        tools = [self._tool_definition(request)]

        results_by_url: dict[str, SearchResult] = {}
        queries_by_id: dict[str, str] = {}
        queries: list[str] = []
        error_codes: list[str] = []
        search_count = 0
        tokens_in = tokens_out = 0
        stop_reason: str | None = None

        for resume in range(MAX_RESUMES + 1):
            try:
                response = self.llm.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=self.prompt.text,
                    messages=messages,
                    tools=tools,
                    output_config={"effort": self.effort},
                )
            except LlmError as error:
                raise SearchError(str(error)) from error
            except anthropic.APIError as error:  # pragma: no cover - LlmClient wraps these
                raise SearchError(f"Claude API request failed: {error}") from error

            stop_reason = getattr(response, "stop_reason", None)
            usage = getattr(response, "usage", None)
            if usage is not None:
                tokens_in += getattr(usage, "input_tokens", 0) or 0
                tokens_out += getattr(usage, "output_tokens", 0) or 0
                server = getattr(usage, "server_tool_use", None)
                if server is not None:
                    search_count += getattr(server, "web_search_requests", 0) or 0

            self._harvest(
                response,
                request=request,
                results_by_url=results_by_url,
                queries_by_id=queries_by_id,
                queries=queries,
                error_codes=error_codes,
            )

            if stop_reason != "pause_turn":
                break

            # A paused turn is resumed by sending the assistant message back unchanged.
            # Nothing may be added — the encrypted_content on each result has to arrive
            # exactly as it left, or the API rejects the request.
            messages = [*messages, {"role": "assistant", "content": response.content}]
            log.info(
                "search.resumed",
                extra={"provider": self.name, "resume": resume + 1, "results": len(results_by_url)},
            )
        else:
            log.warning(
                "search.resume_limit",
                extra={"provider": self.name, "limit": MAX_RESUMES},
            )

        results = self._ranked(results_by_url)
        log.info(
            "search.completed",
            extra={
                "provider": self.name,
                "phrases": len(request.phrases),
                "queries": len(queries),
                "searches_billed": search_count,
                "results": len(results),
                "errors": len(error_codes),
                "region": request.region,
            },
        )
        return SearchResponse(
            results=results,
            queries=queries,
            search_count=search_count,
            error_codes=error_codes,
            stop_reason=stop_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # --- request shaping -------------------------------------------------------------

    def _tool_definition(self, request: SearchRequest) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": TOOL_TYPE,
            "name": TOOL_NAME,
            "max_uses": max(1, request.max_uses),
        }
        if request.allowed_domains:
            tool["allowed_domains"] = list(request.allowed_domains)
        elif request.blocked_domains:
            tool["blocked_domains"] = list(request.blocked_domains)
        if request.country:
            tool["user_location"] = {"type": "approximate", "country": request.country}
        return tool

    def _user_message(self, request: SearchRequest) -> str:
        lines = []
        if request.intent:
            lines.append(request.intent)
        if request.region:
            lines.append(f"Region: {request.region}")
        lines.append(f"Search limit: {request.max_uses} searches.")
        lines.append("")
        lines.append("Phrases to search for:")
        lines.extend(f"- {phrase}" for phrase in request.phrases)
        return "\n".join(lines)

    # --- response parsing ------------------------------------------------------------

    def _harvest(
        self,
        response: Any,
        *,
        request: SearchRequest,
        results_by_url: dict[str, SearchResult],
        queries_by_id: dict[str, str],
        queries: list[str],
        error_codes: list[str],
    ) -> None:
        """Read results, queries, citations and errors out of one response."""
        citations = self._citations(response)

        for block in getattr(response, "content", []) or []:
            kind = getattr(block, "type", None)

            if kind == "server_tool_use" and getattr(block, "name", None) == TOOL_NAME:
                query = (getattr(block, "input", None) or {}).get("query", "")
                if query:
                    queries_by_id[block.id] = query
                    queries.append(query)
                continue

            if kind != "web_search_tool_result":
                continue

            content = getattr(block, "content", None)
            query = queries_by_id.get(getattr(block, "tool_use_id", ""), "")

            # On success `content` is a list of results; on failure it is a single error
            # object. The API returns HTTP 200 either way, so this branch is the only
            # thing standing between an error and a crash on iteration.
            if not isinstance(content, list):
                code = getattr(content, "error_code", None) or "unknown"
                error_codes.append(str(code))
                log.warning(
                    "search.tool_error",
                    extra={"provider": self.name, "error_code": str(code)},
                )
                continue

            for position, entry in enumerate(content):
                if getattr(entry, "type", None) != "web_search_result":
                    continue
                url = (getattr(entry, "url", "") or "").strip()
                if not url:
                    continue
                existing = results_by_url.get(url)
                if existing is not None:
                    # Seen by another query in the same request: that recurrence is a
                    # real signal, and the best position is the one worth keeping.
                    existing.recurrence += 1
                    existing.position = min(existing.position, position)
                    continue
                snippet = citations.get(url, "")
                results_by_url[url] = SearchResult(
                    url=url,
                    title=(getattr(entry, "title", "") or "").strip(),
                    page_age=getattr(entry, "page_age", None),
                    position=position,
                    query=query,
                    recurrence=1,
                    snippet=snippet,
                    snippet_source="citation" if snippet else "",
                )

    @staticmethod
    def _citations(response: Any) -> dict[str, str]:
        """Map each cited URL to the text the model quoted from it.

        This is the only readable excerpt available: a search result has no snippet
        field. `cited_text` is capped at 150 characters by the API and is quoted from
        the page itself, so it is evidence rather than model prose.
        """
        quoted: dict[str, str] = {}
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) != "text":
                continue
            for citation in getattr(block, "citations", None) or []:
                url = (getattr(citation, "url", "") or "").strip()
                text = (getattr(citation, "cited_text", "") or "").strip()
                if url and text and url not in quoted:
                    quoted[url] = text
        return quoted

    @staticmethod
    def _ranked(results_by_url: dict[str, SearchResult]) -> list[SearchResult]:
        """Most recurrent first, then best position — the only ranking signals there are."""
        return sorted(
            results_by_url.values(),
            key=lambda result: (-result.recurrence, result.position, result.url),
        )
