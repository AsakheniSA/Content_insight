"""Small helpers for talking to the GitHub Actions runner."""

from __future__ import annotations

import os

from .pii import scrub


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def _escape(text: str) -> str:
    # The runner's workflow-command escaping for message and property values.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(level: str, title: str, message: str, *, max_chars: int = 300) -> str:
    """A `::error::` / `::warning::` line. Scrubbed and cut short: the repo is public."""
    flat = " ".join(scrub(message).split())
    if len(flat) > max_chars:
        flat = flat[: max_chars - 1].rstrip() + "…"
    safe_title = _escape(title).replace(",", "%2C").replace(":", "%3A")
    return f"::{level} title={safe_title}::{_escape(flat)}"


def emit(level: str, title: str, message: str) -> None:
    """Print an annotation when running in Actions; do nothing locally."""
    if in_actions():
        print(annotation(level, title, message), flush=True)
