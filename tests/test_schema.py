"""The schema is the contract. These tests guard the parts that are easy to break."""

from __future__ import annotations

import pytest

from tula_insights import schema


def test_every_tab_name_is_unique():
    names = [tab.name for tab in schema.TABS]
    assert len(names) == len(set(names))


def test_no_tab_has_duplicate_columns():
    for tab in schema.TABS:
        assert len(tab.columns) == len(set(tab.columns)), f"{tab.name} repeats a column"


def test_internal_tabs_are_hidden_and_named_with_an_underscore():
    for tab in schema.TABS:
        if tab.name.startswith("_"):
            assert tab.hidden, f"{tab.name} should be hidden"
        if tab.hidden:
            assert tab.name.startswith("_"), f"{tab.name} is hidden but not named as internal"


def test_key_columns_exist_on_their_tab():
    for tab in schema.TABS:
        if tab.key_column:
            assert tab.key_column in tab.columns


def test_human_editable_columns_exist_on_their_tab():
    for tab in schema.TABS:
        for column in tab.human_editable:
            assert column in tab.columns, f"{tab.name}.{column} is not a real column"


def test_feed_protects_exactly_the_four_reviewer_columns():
    assert schema.FEED.human_editable == (
        "status",
        "rejection_reason",
        "post_ref",
        "reviewed_by",
    )


def test_the_archive_mirrors_items_exactly():
    assert schema.ITEMS_ARCHIVE.columns == schema.ITEMS.columns


def test_index_of_is_zero_based():
    assert schema.ITEMS.index_of("item_id") == 0
    assert schema.ITEMS.index_of("error") == len(schema.ITEMS.columns) - 1


def test_unknown_column_fails_with_a_helpful_message():
    with pytest.raises(KeyError, match="no column"):
        schema.ITEMS.index_of("nope")


def test_unknown_tab_fails_with_a_helpful_message():
    with pytest.raises(KeyError, match="Unknown tab"):
        schema.tab("nope")
