"""Nothing that identifies a person may reach the sheet or the logs."""

from __future__ import annotations

import pytest

from tula_insights.utils.pii import is_person_url, scrub, scrub_mapping


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ask u/johnsmith", "ask [user]"),
        ("ask /u/johnsmith", "ask [user]"),
        ("mail me at a.b@c.co.za", "mail me at [email]"),
        ("call +27 82 555 1234", "call [phone]"),
        ("call 082 555 1234", "call [phone]"),
        ("follow @petguru", "follow [handle]"),
    ],
)
def test_identifying_details_are_replaced(raw, expected):
    assert scrub(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Posted 2026-11-25",
        "collected at 2026-11-25T10:00:00Z",
        "price R1 250.00",
        "flight BA0056 departs 14:30",
        "crate size 48 x 32 x 35",
    ],
)
def test_ordinary_content_survives(raw):
    """A date, a price or a measurement is content, not a phone number."""
    assert scrub(raw) == raw


def test_subreddits_are_kept():
    """A subreddit is a topic, not a person — and often the only subject signal."""
    assert scrub("great advice in r/dogs and r/CapeTown") == (
        "great advice in r/dogs and r/CapeTown"
    )


def test_a_user_and_a_subreddit_in_one_line():
    assert scrub("u/someone posted in r/dogs") == "[user] posted in r/dogs"


def test_whitespace_is_collapsed():
    assert scrub("  lots   of \n space ") == "lots of space"


def test_blank_input():
    assert scrub(None) == ""
    assert scrub("") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://reddit.com/user/bob",
        "https://reddit.com/u/bob",
        "https://www.reddit.com/users/bob/",
        "https://example.com/profile/42",
        "https://example.com/author/jane",
        "https://x.com/@someone",
        "",
    ],
)
def test_person_urls_are_rejected(url):
    assert is_person_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/dogs/comments/1abc2de/can_i_fly_my_dog/",
        "https://example.com/blog/pet-travel",
        "https://example.com/r/useful",
    ],
)
def test_content_urls_are_kept(url):
    assert is_person_url(url) is False


def test_scrub_mapping_only_touches_named_fields():
    row = {"title": "ask u/bob", "url": "https://reddit.com/u/bob", "count": 3}
    out = scrub_mapping(row, "title")
    assert out["title"] == "ask [user]"
    assert out["url"] == row["url"], "url is handled by is_person_url, not scrubbed"
    assert out["count"] == 3
