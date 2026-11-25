"""Structured logging.

The repository is public, so GitHub Actions logs, artifacts and job summaries are
public too. That makes logging a safety boundary, not a convenience: **counts and IDs
only — never item text, questions, snippets or URLs.**

:func:`setup_logging` installs a JSON formatter and a redacting filter. The filter is
the backstop, not the plan: pass counts to ``extra=`` rather than formatting text into
the message and hoping the filter catches it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

#: Keys that may appear in `extra=` and be emitted as-is.
_SAFE_EXTRA_TYPES = (str, int, float, bool, type(None))

#: Patterns redacted from every log record when `allow_item_text` is false.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so Actions logs stay greppable."""

    def __init__(self, *, redact: bool = True) -> None:
        super().__init__()
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self.redact:
            message = redact(message)
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, _SAFE_EXTRA_TYPES) else str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable output for local runs."""

    def __init__(self, *, redact: bool = False) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s  %(message)s", "%H:%M:%S")
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact(text) if self.redact else text


def redact(text: str) -> str:
    """Replace URLs and email addresses in a log line with placeholders."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    *,
    allow_item_text: bool = False,
) -> None:
    """Configure the root logger. Safe to call more than once.

    ``TULA_LOG_LEVEL`` overrides `level`, so a single run can be made verbose without
    editing config.
    """
    level = os.environ.get("TULA_LOG_LEVEL", level).upper()
    redacting = not allow_item_text

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter(redact=redacting) if fmt == "json" else PlainFormatter(redact=redacting)
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These two are chatty at DEBUG and can echo request URLs.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """A logger for a module, named under the package."""
    return logging.getLogger(name)
