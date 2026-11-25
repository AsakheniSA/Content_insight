"""The Claude API client, with token accounting and cost estimation.

Every request in the project goes through :class:`LlmClient` so that three things are
true everywhere: requests are retried on the failures worth retrying, every token and
every server-tool call is counted, and no request is sent that would exceed the input
cap in `budget.yaml`.

Cost is *estimated* from the price table in `config/budget.yaml`, which carries the
date it was last checked. It is a guide for the job summary and the `runs` tab, not an
invoice — the Claude Console is the source of truth for what was actually spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic

from ..config import BudgetConfig
from ..utils.logging import get_logger
from ..utils.retry import call_with_retry

log = get_logger(__name__)

#: Status codes worth retrying. 400 and 401 never are — they mean the request is wrong.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LlmError(RuntimeError):
    """Raised when a Claude API request cannot be completed."""


@dataclass
class Usage:
    """Running totals for one run. Counts only — never prompt or response text."""

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    web_search_requests: int = 0
    calls_by_model: dict[str, int] = field(default_factory=dict)

    def add_response(self, response: Any, model: str) -> None:
        """Accumulate the usage block of one API response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.tokens_in += getattr(usage, "input_tokens", 0) or 0
        self.tokens_out += getattr(usage, "output_tokens", 0) or 0
        self.tokens_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.tokens_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_search_requests += getattr(server, "web_search_requests", 0) or 0
        self.calls_by_model[model] = self.calls_by_model.get(model, 0) + 1

    def merge(self, other: Usage) -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.tokens_cache_read += other.tokens_cache_read
        self.tokens_cache_write += other.tokens_cache_write
        self.web_search_requests += other.web_search_requests
        for model, count in other.calls_by_model.items():
            self.calls_by_model[model] = self.calls_by_model.get(model, 0) + count

    def estimate_usd(self, budget: BudgetConfig, *, batch: bool = False) -> float:
        """Estimated spend, from the price table in budget.yaml."""
        total = (
            self.web_search_requests
            / 1000.0
            * budget.prices.claude_web_search_per_1000_searches
        )
        for model, _calls in self.calls_by_model.items():
            try:
                price = budget.price_for(model)
            except Exception:
                log.warning("llm.unpriced_model", extra={"model": model})
                continue
            per_in = (price.batch_input_per_mtok if batch else None) or price.input_per_mtok
            per_out = (price.batch_output_per_mtok if batch else None) or price.output_per_mtok
            total += self.tokens_in / 1_000_000 * per_in
            total += self.tokens_out / 1_000_000 * per_out
            total += self.tokens_cache_read / 1_000_000 * price.cache_read_per_mtok
        return round(total, 6)

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cache_read": self.tokens_cache_read,
            "tokens_cache_write": self.tokens_cache_write,
            "web_search_requests": self.web_search_requests,
        }


def _status_of(error: BaseException) -> int | None:
    return getattr(error, "status_code", None)


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return True
    status = _status_of(error)
    return status in RETRYABLE_STATUS if status is not None else False


class LlmClient:
    """A thin, counted wrapper around the Anthropic SDK."""

    def __init__(
        self,
        api_key: str,
        budget: BudgetConfig,
        *,
        max_retries: int = 4,
        timeout_seconds: float = 180.0,
    ) -> None:
        # The SDK retries too; its own retrier is disabled so that one policy — ours,
        # which honours Retry-After and logs every attempt — is in charge.
        self.client = anthropic.Anthropic(
            api_key=api_key, max_retries=0, timeout=timeout_seconds
        )
        self.budget = budget
        self.max_retries = max_retries
        self.usage = Usage()

    def create(self, *, model: str, **kwargs: Any) -> Any:
        """Send one Messages request, retried, counted and input-capped."""
        self._guard_input_size(model=model, **kwargs)

        def send() -> Any:
            return self.client.messages.create(model=model, **kwargs)

        try:
            response = call_with_retry(
                send,
                what=f"messages.create({model})",
                attempts=self.max_retries + 1,
                retry_on=(anthropic.APIError, anthropic.APIConnectionError),
                should_retry=_is_retryable,
            )
        except anthropic.BadRequestError as error:
            raise LlmError(self._explain_bad_request(error)) from error
        except anthropic.AuthenticationError as error:
            raise LlmError(
                "ANTHROPIC_API_KEY was rejected. Check the key in .env (locally) or in "
                "the repository's Actions secrets."
            ) from error
        except anthropic.APIError as error:
            raise LlmError(
                f"Claude API request failed (HTTP {_status_of(error)}) after "
                f"{self.max_retries + 1} attempt(s)."
            ) from error

        self.usage.add_response(response, model)
        return response

    def _guard_input_size(self, *, model: str, **kwargs: Any) -> None:
        """Refuse a request whose input is near the long-context price cliff.

        Prompts over the threshold in budget.yaml are billed at a multiple of standard
        rates. Rather than discover that on the invoice, count first and refuse.
        """
        cap = self.budget.limits.max_input_tokens_per_request
        countable = {
            key: value
            for key, value in kwargs.items()
            if key in {"messages", "system", "tools"}
        }
        if "messages" not in countable:
            return
        try:
            counted = self.client.messages.count_tokens(model=model, **countable)
            tokens = counted.input_tokens
        except Exception as error:
            log.warning("llm.count_tokens_failed", extra={"error_type": type(error).__name__})
            return
        if tokens > cap:
            raise LlmError(
                f"Request would send {tokens} input tokens, over the "
                f"{cap} cap in config/budget.yaml (limits.max_input_tokens_per_request). "
                f"Reduce the batch size rather than raising the cap: the long-context "
                f"threshold is {self.budget.limits.long_context_threshold_tokens} tokens, "
                f"beyond which requests are billed at a multiple of standard rates."
            )

    @staticmethod
    def _explain_bad_request(error: anthropic.BadRequestError) -> str:
        message = str(getattr(error, "message", "") or error)
        if "not accessible to our user agent" in message.lower():
            return (
                "A domain in this source's allowed_domains blocks Anthropic's crawler, so "
                "the Claude web search tool cannot search it. Remove that domain from the "
                "source in config/sources.yaml — or disable the source. Content from that "
                "site can still come in by hand through the manual_submissions tab.\n"
                f"API said: {message}"
            )
        if "web search" in message.lower() or "web_search" in message.lower():
            return (
                "The Claude API rejected the web search tool. An organisation admin must "
                "enable web search in the Claude Console (Settings > Capabilities) before "
                "the discovery stages can run.\n"
                f"API said: {message}"
            )
        return f"The Claude API rejected the request: {message}"
