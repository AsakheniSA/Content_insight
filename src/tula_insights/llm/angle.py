"""Writing the one-line `suggested_angle` for each insight served in the feed.

An angle is a direction for a post, never a claim. Tula publishes only facts checked
against a verified source (the team's verified-claims ledger); an angle is not
checked, so it must carry no facts. The prompt says so, and this module enforces the
part that can be enforced mechanically: **an angle containing a digit is rejected**,
because numbers — durations, weights, prices, intervals — are where unverified claims
hide. A rejected angle is retried once with the reason stated.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import Insight
from ..utils.pii import scrub
from .classifier import ClassificationInvalid
from .prompts import load_prompt

PROMPT_NAME = "angle"

_SCHEMA = {
    "type": "object",
    "properties": {"angle": {"type": "string"}},
    "required": ["angle"],
    "additionalProperties": False,
}
_DIGIT = re.compile(r"\d")


class AngleWriter:
    def __init__(self, model: str, *, max_chars: int = 160, effort: str = "low") -> None:
        self.model = model
        self.max_chars = max_chars
        self.effort = effort
        self.prompt = load_prompt(PROMPT_NAME)

    @property
    def prompt_version(self) -> str:
        return self.prompt.stamp

    def request_params(
        self, insight: Insight, *, pain_points: list[str], problem: str | None = None
    ) -> dict[str, Any]:
        lines = [
            f"Question: {_clean(insight.canonical_question)}",
            f"Category: {insight.category}",
        ]
        if pain_points:
            worries = "; ".join(_clean(p) for p in pain_points[:3])
            lines.append(f"Worries owners mentioned: {worries}")
        lines.append(
            f"Asked by {insight.item_count} owner(s) across {insight.source_count} source(s)."
        )
        if problem:
            lines.append(f"\nYour previous angle was rejected: {problem}. Write a new one.")
        return {
            "model": self.model,
            "max_tokens": 1024,
            "system": [
                {"type": "text", "text": self.prompt.text, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": "\n".join(lines)}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
        }

    def parse(self, message: Any) -> str:
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            raise ClassificationInvalid("the model declined", retryable=False)
        if stop == "max_tokens":
            raise ClassificationInvalid("the reply was cut off")
        blocks = getattr(message, "content", []) or []
        text = next((b.text for b in blocks if getattr(b, "type", "") == "text"), None)
        try:
            angle = json.loads(text or "")["angle"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ClassificationInvalid("the reply was not the expected JSON") from None
        return self.validate(angle)

    def validate(self, angle: Any) -> str:
        if not isinstance(angle, str):
            raise ClassificationInvalid("the angle was not text")
        cleaned = scrub(" ".join(angle.split()))
        if not cleaned:
            raise ClassificationInvalid("the angle was empty")
        if _DIGIT.search(cleaned):
            raise ClassificationInvalid(
                "it contained a number; angles must carry no numbers, rules or facts"
            )
        if len(cleaned) > self.max_chars:
            raise ClassificationInvalid(
                f"it was {len(cleaned)} characters; the limit is {self.max_chars}"
            )
        return cleaned


def _clean(text: str) -> str:
    return " ".join((text or "").split())
