"""How Python values become sheet cells, and back again.

One convention, used everywhere:

* **Lists** are joined with ``;`` (no surrounding spaces). Any ``;`` inside a value is
  percent-escaped to ``%3B`` first, so the round-trip is lossless even for URLs, which
  may legally contain a semicolon.
* **Columns named ``*_json``** hold compact JSON. Nothing else does.
* **Booleans** are written ``TRUE`` / ``FALSE`` so Sheets shows a real checkbox value
  rather than the Python words.
* **``None``** is written as an empty string, and reads back as ``None``.
* **Timestamps** are ISO 8601 in UTC with a ``Z`` suffix.

Everything written to the sheet goes through :func:`to_cell`; everything read comes back
through the typed readers. Keeping both halves here is what stops the convention from
drifting between modules.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

LIST_SEPARATOR = ";"
_ESCAPED_SEPARATOR = "%3B"

TRUE_STRINGS = frozenset({"true", "TRUE", "True", "yes", "y", "1"})
FALSE_STRINGS = frozenset({"false", "FALSE", "False", "no", "n", "0"})


# --- lists ---------------------------------------------------------------------------


def join_list(values: Iterable[Any] | None) -> str:
    """Join an iterable into a single ``;``-separated cell."""
    if values is None:
        return ""
    if isinstance(values, (str, bytes)):
        raise TypeError("join_list expects an iterable of values, not a string")
    parts = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(text.replace(LIST_SEPARATOR, _ESCAPED_SEPARATOR))
    return LIST_SEPARATOR.join(parts)


def split_list(cell: Any) -> list[str]:
    """Split a ``;``-separated cell back into a list, reversing the escaping."""
    if cell is None:
        return []
    text = str(cell).strip()
    if not text:
        return []
    return [
        part.strip().replace(_ESCAPED_SEPARATOR, LIST_SEPARATOR)
        for part in text.split(LIST_SEPARATOR)
        if part.strip()
    ]


# --- json ----------------------------------------------------------------------------


def dump_json(value: Mapping[str, Any] | list[Any] | None) -> str:
    """Compact JSON for a ``*_json`` column."""
    if value is None:
        return ""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def load_json(cell: Any, default: Any = None) -> Any:
    """Read a ``*_json`` column, returning `default` when it is blank or malformed."""
    if cell is None:
        return default
    text = str(cell).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


# --- scalars -------------------------------------------------------------------------


def to_cell(value: Any) -> str | int | float:
    """Convert any Python value to something safe to write into a cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return dump_json(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return join_list(value)
    return str(value)


def read_bool(cell: Any, default: bool | None = None) -> bool | None:
    """Read a boolean cell. Unrecognised text returns `default` rather than guessing."""
    if cell is None:
        return default
    if isinstance(cell, bool):
        return cell
    text = str(cell).strip()
    if not text:
        return default
    if text in TRUE_STRINGS:
        return True
    if text in FALSE_STRINGS:
        return False
    return default


def read_int(cell: Any, default: int | None = None) -> int | None:
    """Read an integer cell, tolerating floats written by Sheets (``3`` vs ``3.0``)."""
    if cell is None or cell == "":
        return default
    try:
        return int(float(str(cell).strip()))
    except (TypeError, ValueError):
        return default


def read_float(cell: Any, default: float | None = None) -> float | None:
    """Read a float cell."""
    if cell is None or cell == "":
        return default
    try:
        return float(str(cell).strip())
    except (TypeError, ValueError):
        return default


def read_str(cell: Any, default: str | None = None) -> str | None:
    """Read a text cell, treating blank as missing."""
    if cell is None:
        return default
    text = str(cell).strip()
    return text if text else default


# --- timestamps ----------------------------------------------------------------------


def iso_utc(moment: datetime) -> str:
    """Format a datetime as ISO 8601 UTC with a ``Z`` suffix."""
    from .clock import to_utc

    return to_utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_datetime(cell: Any) -> datetime | None:
    """Parse a timestamp cell written by :func:`iso_utc`, or any ISO 8601 string."""
    text = read_str(cell)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
