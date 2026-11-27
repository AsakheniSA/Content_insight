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


class BatchNotFound(LlmError):
    """The API no longer knows this batch — results are kept for 29 days."""


def estimate_tokens(payload: Any) -> int:
    """A rough, deliberately generous token estimate: about 3 characters per token."""
    import json

    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return len(text) // 3 + 1


#: Writing to the prompt cache costs this multiple of the input price (5-minute TTL).
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class ModelUsage:
    """Token counts for one model under one pricing mode."""

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    calls: int = 0


@dataclass
class Usage:
    """Running totals for one run. Counts only — never prompt or response text.

    Tokens are kept per model and per pricing mode (standard or batch), because a run
    can mix both — the classifier sends a batch, then retries a few items one by one —
    and each is priced differently. An earlier version kept one pool of tokens and
    priced it once per model used, which would double-count as soon as a second model
    or mode appeared.
    """

    standard: dict[str, ModelUsage] = field(default_factory=dict)
    batch: dict[str, ModelUsage] = field(default_factory=dict)
    web_search_requests: int = 0

    def add_response(self, response: Any, model: str, *, batch: bool = False) -> None:
        """Accumulate the usage block of one API response (or one batch result)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        bucket = (self.batch if batch else self.standard).setdefault(model, ModelUsage())
        bucket.tokens_in += getattr(usage, "input_tokens", 0) or 0
        bucket.tokens_out += getattr(usage, "output_tokens", 0) or 0
        bucket.tokens_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        bucket.tokens_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        bucket.calls += 1
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_search_requests += getattr(server, "web_search_requests", 0) or 0

    def _all(self) -> list[ModelUsage]:
        return [*self.standard.values(), *self.batch.values()]

    @property
    def tokens_in(self) -> int:
        return sum(u.tokens_in for u in self._all())

    @property
    def tokens_out(self) -> int:
        return sum(u.tokens_out for u in self._all())

    @property
    def tokens_cache_read(self) -> int:
        return sum(u.tokens_cache_read for u in self._all())

    @property
    def tokens_cache_write(self) -> int:
        return sum(u.tokens_cache_write for u in self._all())

    @property
    def calls_by_model(self) -> dict[str, int]:
        calls: dict[str, int] = {}
        for pool in (self.standard, self.batch):
            for model, used in pool.items():
                calls[model] = calls.get(model, 0) + used.calls
        return calls

    def estimate_usd(self, budget: BudgetConfig) -> float:
        """Estimated spend, from the price table in budget.yaml.

        Input tokens reported by the API exclude cached ones, which are priced
        separately: cache reads at the cache-read rate, cache writes at
        CACHE_WRITE_MULTIPLIER x the input rate. Batch tokens use the batch rates.
        """
        total = (
            self.web_search_requests / 1000.0 * budget.prices.claude_web_search_per_1000_searches
        )
        for pool, is_batch in ((self.standard, False), (self.batch, True)):
            for model, used in pool.items():
                try:
                    price = budget.price_for(model)
                except Exception:
                    log.warning("llm.unpriced_model", extra={"model": model})
                    continue
                per_in = (price.batch_input_per_mtok if is_batch else None) or price.input_per_mtok
                per_out = (
                    (price.batch_output_per_mtok if is_batch else None) or price.output_per_mtok
                )
                total += used.tokens_in / 1_000_000 * per_in
                total += used.tokens_out / 1_000_000 * per_out
                total += used.tokens_cache_read / 1_000_000 * price.cache_read_per_mtok
                total += (
                    used.tokens_cache_write / 1_000_000 * per_in * CACHE_WRITE_MULTIPLIER
                )
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


def _api_message(error: BaseException, limit: int = 300) -> str:
    """Anthropic's own explanation, one line, cut short.

    Its error messages describe the request's problem (credit, a bad parameter) and do
    not echo the prompt, so they are safe in public logs and issues.
    """
    body = getattr(error, "body", None)
    message = ""
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            message = str(inner.get("message") or "")
    message = message or str(getattr(error, "message", "") or error)
    message = " ".join(message.split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


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
        rates. Rather than discover that on the invoice, count first and refuse — but
        only when a rough local estimate says it could matter, so that hundreds of
        small classification calls don't each cost an extra round trip.
        """
        cap = self.budget.limits.max_input_tokens_per_request
        countable = {k: v for k, v in kwargs.items() if k in {"messages", "system", "tools"}}
        if "messages" not in countable:
            return
        if estimate_tokens(countable) < cap // 2:
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

    # --- Message Batches -------------------------------------------------------------

    def create_batch(self, requests: list[dict[str, Any]]) -> Any:
        """Submit a Message Batch. `requests` are {"custom_id", "params"} dicts."""
        cap = self.budget.limits.max_input_tokens_per_request
        for request in requests:
            if estimate_tokens(request["params"]) > cap:
                raise LlmError(
                    f"Batch request {request['custom_id']} is estimated over the "
                    f"{cap}-token input cap; refusing to submit."
                )
        return self._with_retry(
            lambda: self.client.messages.batches.create(requests=requests),
            what="messages.batches.create",
        )

    def retrieve_batch(self, batch_id: str) -> Any:
        return self._with_retry(
            lambda: self.client.messages.batches.retrieve(batch_id),
            what="messages.batches.retrieve",
        )

    def batch_results(self, batch_id: str) -> list[Any]:
        """Every result of an ended batch, read in full. Order is not guaranteed."""
        return self._with_retry(
            lambda: list(self.client.messages.batches.results(batch_id)),
            what="messages.batches.results",
        )

    def _with_retry(self, func, *, what: str) -> Any:
        try:
            return call_with_retry(
                func,
                what=what,
                attempts=self.max_retries + 1,
                retry_on=(anthropic.APIError, anthropic.APIConnectionError),
                should_retry=_is_retryable,
            )
        except anthropic.NotFoundError as error:
            raise BatchNotFound(f"{what}: batch not found (it may have expired)") from error
        except anthropic.BadRequestError as error:
            raise LlmError(f"{what} failed: {self._explain_bad_request(error)}") from error
        except anthropic.APIError as error:
            raise LlmError(
                f"{what} failed (HTTP {_status_of(error)}): {_api_message(error)}"
            ) from error

    @staticmethod
    def _explain_bad_request(error: anthropic.BadRequestError) -> str:
        message = _api_message(error)
        if "credit balance" in message.lower():
            return (
                "The Claude API account has run out of credit. Top it up in the Claude "
                "Console (Billing). Nothing is lost: new items wait as 'new' and the next "
                f"run catches up.\nAPI said: {message}"
            )
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
