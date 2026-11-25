"""The cell conventions must round-trip, including the awkward cases."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from tula_insights.utils import serialise as ser


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["a"],
        ["a", "b", "c"],
        ["tag with spaces", "another"],
    ],
)
def test_list_round_trip(values):
    assert ser.split_list(ser.join_list(values)) == values


def test_semicolons_inside_values_survive():
    urls = ["https://example.com/a?x=1;y=2", "https://example.com/b"]
    assert ser.split_list(ser.join_list(urls)) == urls


def test_join_list_drops_blanks_and_none():
    assert ser.join_list(["a", "", None, "  ", "b"]) == "a;b"


def test_split_list_of_blank_is_empty():
    assert ser.split_list("") == []
    assert ser.split_list(None) == []
    assert ser.split_list("   ") == []


def test_join_list_rejects_a_bare_string():
    with pytest.raises(TypeError):
        ser.join_list("not-a-list")


def test_json_round_trip():
    payload = {"score": 3, "nested": {"a": [1, 2]}}
    assert ser.load_json(ser.dump_json(payload)) == payload


def test_load_json_tolerates_rubbish():
    assert ser.load_json("{not json", default={}) == {}
    assert ser.load_json("", default={}) == {}
    assert ser.load_json(None, default=None) is None


def test_booleans_are_sheet_shaped():
    assert ser.to_cell(True) == "TRUE"
    assert ser.to_cell(False) == "FALSE"
    assert ser.read_bool("TRUE") is True
    assert ser.read_bool("FALSE") is False
    assert ser.read_bool("yes") is True


def test_unreadable_boolean_returns_the_default_rather_than_guessing():
    assert ser.read_bool("maybe", default=None) is None
    assert ser.read_bool("", default=False) is False


def test_none_becomes_blank():
    assert ser.to_cell(None) == ""


def test_numbers_stay_numbers():
    assert ser.to_cell(3) == 3
    assert ser.to_cell(1.5) == 1.5


def test_read_int_tolerates_floats_from_sheets():
    assert ser.read_int("3") == 3
    assert ser.read_int("3.0") == 3
    assert ser.read_int("", default=0) == 0
    assert ser.read_int("nonsense", default=7) == 7


def test_timestamps_are_utc_with_a_z():
    moment = datetime(2026, 11, 25, 14, 30, 0, tzinfo=UTC)
    assert ser.iso_utc(moment) == "2026-11-25T14:30:00Z"
    assert ser.read_datetime("2026-11-25T14:30:00Z") == moment


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 11, 25, 14, 30, 0)
    assert ser.iso_utc(naive) == "2026-11-25T14:30:00Z"


def test_local_time_is_converted_not_truncated():
    from datetime import timedelta

    sast = datetime(2026, 11, 25, 16, 30, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ser.iso_utc(sast) == "2026-11-25T14:30:00Z"


def test_unparseable_timestamp_returns_none():
    assert ser.read_datetime("not a date") is None
    assert ser.read_datetime("") is None
