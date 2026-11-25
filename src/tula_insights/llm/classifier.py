"""Turning one collected item into a validated classification.

Everything the classifier needs that depends on configuration is built here from the
YAML — the category list in the prompt, the allowed values in the output schema — so
adding a category is still a YAML change: the prompt and the schema follow.

**Output is constrained, then checked.** Requests use structured outputs
(`output_config.format`, supported on Claude Haiku 5.5 and inside the Batch API), so
the reply is always JSON of the right shape with category ids and tags drawn from the
allowed lists. Structured outputs cannot enforce numeric ranges, so Pydantic and the
rules below check what the schema cannot: confidence within 0 to 1, a category and a
question whenever an item is marked relevant, tags only where the category allows
them. A reply that fails is retried once with the problem stated; then it is an error.

**The prompt prefix is cached.** The system prompt — rules plus every category — is
identical on every request and marked with `cache_control`. Claude Haiku 5.5 caches a
prefix from 512 tokens, which this comfortably exceeds.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ..config import AppConfig
from ..models import Classification, ItemRow
from ..utils.pii import scrub
from .prompts import Prompt, load_prompt

PROMPT_NAME = "classify_extract"

#: How an item's source is described to the model.
SOURCE_LABELS = {
    "web_questions": "web page",
    "youtube": "YouTube comment",
    "manual": "note pasted in by the team",
    "reddit_search": "Reddit thread",
}

#: Item text is cut to this before it is sent. The collectors already bound it; this
#: keeps a request small and well under the input cap whatever reaches the sheet.
MAX_ITEM_CHARS = 1200


class ClassificationInvalid(ValueError):
    """The model's reply could not be turned into a valid classification."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Classifier:
    """Builds requests and validates replies. Holds no client: callers send."""

    def __init__(self, config: AppConfig, prompt: Prompt | None = None) -> None:
        self.config = config
        self.settings = config.settings.classify
        self.prompt = prompt or load_prompt(PROMPT_NAME)
        self.system_text = self._render_system()
        self.schema = self._schema()
        self._category_tags = {c.id: set(c.tags) for c in config.categories.categories}

    @property
    def prompt_version(self) -> str:
        """Written onto every row: which prompt, and which category list, made it."""
        return f"{self.prompt.stamp}/categories:{self.config.categories.version}"

    # --- requests --------------------------------------------------------------------

    def request_params(self, item: ItemRow, *, problem: str | None = None) -> dict[str, Any]:
        """Messages API parameters for one item. Also valid as a batch request's params."""
        content = self.item_message(item)
        if problem:
            content += (
                "\n\nYour previous answer for this item was rejected: "
                f"{problem}\nAnswer again, fixing that."
            )
        return {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": [
                {"type": "text", "text": self.system_text, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": self.schema},
            },
        }

    @staticmethod
    def item_message(item: ItemRow) -> str:
        label = SOURCE_LABELS.get(item.source, "item")
        title = _defuse(item.title)
        excerpt = _defuse(item.snippet)
        lines = ["<item>", f"type: {label}"]
        if title and excerpt and title.rstrip("…") in excerpt:
            lines.append(f"text: {excerpt[:MAX_ITEM_CHARS]}")
        else:
            if title:
                lines.append(f"title: {title[:MAX_ITEM_CHARS]}")
            if excerpt:
                lines.append(f"excerpt: {excerpt[:MAX_ITEM_CHARS]}")
            if not title and not excerpt:
                lines.append("text: (empty)")
        lines.append("</item>")
        return "\n".join(lines)

    # --- replies ---------------------------------------------------------------------

    def parse(self, message: Any) -> Classification:
        """Validate one reply. Raises ClassificationInvalid with a reason."""
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            raise ClassificationInvalid("the model declined to classify this item", retryable=False)
        if stop == "max_tokens":
            raise ClassificationInvalid("the reply was cut off before it finished")

        blocks = getattr(message, "content", []) or []
        text = next((b.text for b in blocks if getattr(b, "type", "") == "text"), None)
        if not text:
            raise ClassificationInvalid("the reply contained no JSON")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ClassificationInvalid(f"the reply was not valid JSON ({error.msg})") from None
        return self.validate(data)

    def validate(self, data: Any) -> Classification:
        if not isinstance(data, dict):
            raise ClassificationInvalid("the reply was not a JSON object")
        try:
            result = Classification.model_validate(data)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'reply'}: {e['msg']}"
                for e in error.errors()
            )
            raise ClassificationInvalid(problems) from None

        if not result.is_relevant:
            # Normalise rather than reject: the decision that matters was made.
            return result.model_copy(
                update={"primary_category": None, "tags": [], "question": None, "pain_point": None}
            )

        if result.primary_category not in self._category_tags:
            raise ClassificationInvalid(
                f"primary_category {result.primary_category!r} is not one of the listed ids"
            )
        question = scrub(result.question or "").strip()
        if not question:
            raise ClassificationInvalid("a relevant item needs a question")
        limit = self.settings.max_question_chars
        if len(question) > limit:
            question = question[: limit - 1].rstrip() + "…"
        allowed = self._category_tags[result.primary_category]
        pain = scrub(result.pain_point or "").strip() or None
        return result.model_copy(
            update={
                "question": question,
                "pain_point": pain,
                "tags": [t for t in dict.fromkeys(result.tags) if t in allowed],
            }
        )

    # --- building blocks -------------------------------------------------------------

    def _render_system(self) -> str:
        categories = []
        for category in self.config.categories.categories:
            examples = "; ".join(f'"{q}"' for q in category.example_questions[:3])
            line = f"- `{category.id}` — {category.name}: {category.description}"
            if examples:
                line += f" Examples: {examples}"
            categories.append(line)
        tags = sorted({t for c in self.config.categories.categories for t in c.tags})
        text = self.prompt.text.replace("{{categories}}", "\n".join(categories))
        return text.replace("{{tags}}", "\n".join(f"- `{t}`" for t in tags) or "(none)")

    def _schema(self) -> dict[str, Any]:
        ids = [c.id for c in self.config.categories.categories]
        tags = sorted({t for c in self.config.categories.categories for t in c.tags})
        tag_item: dict[str, Any] = {"type": "string", "enum": tags} if tags else {"type": "string"}
        nullable_text = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        return {
            "type": "object",
            "properties": {
                "is_relevant": {"type": "boolean"},
                "primary_category": {"anyOf": [{"type": "string", "enum": ids}, {"type": "null"}]},
                "tags": {"type": "array", "items": tag_item},
                "question": nullable_text,
                "pain_point": nullable_text,
                # Range enforced by Pydantic: structured outputs ignore minimum/maximum.
                "confidence": {"type": "number"},
            },
            "required": ["is_relevant", "primary_category", "tags", "question", "pain_point",
                         "confidence"],
            "additionalProperties": False,
        }


def _defuse(text: str | None) -> str:
    """Stop item text from closing the <item> wrapper early."""
    if not text:
        return ""
    return " ".join(text.replace("<item>", "").replace("</item>", "").split())
