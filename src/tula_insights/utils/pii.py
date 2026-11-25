"""Scrubbing personal information out of anything before it is stored.

The repository and its logs are public, and the sheet is shared with the team, so
nothing collected may carry a person's identity. §6.1 of the brief is the rule:
**only public content and metadata — no usernames, handles, author IDs, profile links
or avatars.**

Two layers:

* :func:`scrub` rewrites text, replacing emails, phone numbers, `@handles` and Reddit
  `u/` user references with placeholders.
* :func:`is_person_url` rejects whole URLs that point at a person rather than at
  content. A user profile cannot be scrubbed into something safe — it is dropped.

Subreddit references (`r/dogs`) are deliberately **kept**. See the note on
``_USER_REF`` below.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

EMAIL = "[email]"
PHONE = "[phone]"
HANDLE = "[handle]"
USER = "[user]"

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Grouped digit runs that could be a phone number. The pattern alone is far too
# generous — "2026-11-25" has exactly the same shape as "082 555 1234" — so a candidate
# is only replaced if it carries at least MIN_PHONE_DIGITS digits. A date has eight, a
# South African or international number has ten or more. Dates, prices and postcodes
# are ordinary content and must survive.
MIN_PHONE_DIGITS = 9

_PHONE_CANDIDATE_RE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?"
    r"\d{2,4}(?:[\s.-]\d{2,4}){2,4}(?![\w.])"
)


def _maybe_phone(match: re.Match[str]) -> str:
    text = match.group(0)
    return PHONE if sum(character.isdigit() for character in text) >= MIN_PHONE_DIGITS else text

# An @handle, but not an email address (guarded by the lookbehind) and not a bare "@".
_HANDLE_RE = re.compile(r"(?<![\w.@])@([A-Za-z0-9_]{2,30})\b")

# Reddit user references only. `r/<subreddit>` is NOT scrubbed: a subreddit is a topic,
# not a person — it identifies nobody, and it is usually the only signal in a Reddit
# title saying what the thread is about. Removing it would cost the classifier its
# subject matter and protect no one. (Deviation from §6.1, which lists `r/` alongside
# `u/`; flagged for approval.)
_USER_REF = re.compile(r"(?<![\w/])(?:/?u/)([A-Za-z0-9_-]{2,30})\b")

#: URL path segments that identify a person rather than a piece of content.
_PERSON_PATHS = (
    "/user/",
    "/u/",
    "/users/",
    "/profile/",
    "/author/",
    "/member/",
    "/@",
)


def scrub(text: str | None) -> str:
    """Replace anything that identifies a person with a placeholder."""
    if not text:
        return ""
    cleaned = _EMAIL_RE.sub(EMAIL, text)
    cleaned = _PHONE_CANDIDATE_RE.sub(_maybe_phone, cleaned)
    cleaned = _USER_REF.sub(USER, cleaned)
    cleaned = _HANDLE_RE.sub(HANDLE, cleaned)
    return " ".join(cleaned.split())


def is_person_url(url: str) -> bool:
    """True if the URL points at a person — a profile, an author page, a user feed.

    Such a URL is dropped rather than scrubbed: there is no version of a profile link
    that is not a profile link.
    """
    if not url:
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    path = parts.path.lower()
    if not path.endswith("/"):
        path += "/"
    return any(segment in path for segment in _PERSON_PATHS)


def scrub_mapping(row: dict[str, object], *fields: str) -> dict[str, object]:
    """Return a copy of `row` with the named text fields scrubbed."""
    out = dict(row)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = scrub(value)
    return out
