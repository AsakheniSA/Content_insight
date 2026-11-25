"""Asking Claude whether two questions are really the same.

Used only for the uncertain band of similarity (between `judge_lower_threshold` and
`merge_threshold` in scoring.yaml). Above the band, questions merge without asking;
below it, they stay separate. The reply is constrained to a candidate number or null.
"""

from __future__ import annotations

import json
from typing import Any

from .classifier import ClassificationInvalid
from .prompts import load_prompt

PROMPT_NAME = "dedupe_judge"


class DedupeJudge:
    def __init__(self, model: str, *, effort: str = "low", max_tokens: int = 1024) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.prompt = load_prompt(PROMPT_NAME)

    def request_params(self, question: str, candidates: list[str]) -> dict[str, Any]:
        numbered = "\n".join(f"{i}. {_clean(c)}" for i, c in enumerate(candidates, start=1))
        content = f"NEW question:\n{_clean(question)}\n\nCANDIDATE questions:\n{numbered}"
        schema = {
            "type": "object",
            "properties": {
                "same_as": {
                    "anyOf": [
                        {"type": "integer", "enum": list(range(1, len(candidates) + 1))},
                        {"type": "null"},
                    ]
                }
            },
            "required": ["same_as"],
            "additionalProperties": False,
        }
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {"type": "text", "text": self.prompt.text, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

    def parse(self, message: Any, candidate_count: int) -> int | None:
        """The zero-based index of the matching candidate, or None."""
        if getattr(message, "stop_reason", None) in {"refusal", "max_tokens"}:
            raise ClassificationInvalid(f"judge stopped: {message.stop_reason}")
        blocks = getattr(message, "content", []) or []
        text = next((b.text for b in blocks if getattr(b, "type", "") == "text"), None)
        try:
            choice = json.loads(text or "")["same_as"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            reason = f"judge reply unreadable ({type(error).__name__})"
            raise ClassificationInvalid(reason) from None
        if choice is None:
            return None
        if not isinstance(choice, int) or not 1 <= choice <= candidate_count:
            raise ClassificationInvalid(f"judge chose {choice!r}, not a listed candidate")
        return choice - 1


def _clean(text: str) -> str:
    return " ".join((text or "").split())
