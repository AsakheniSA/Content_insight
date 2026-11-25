"""The score components, each checked against hand-worked numbers."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from tula_insights import scoring
from tula_insights.config import load_config

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


@pytest.fixture
def cfg(config_dir):
    return load_config().scoring


def test_recurrence_is_log_scaled_and_saturates(cfg):
    assert scoring.recurrence(0, cfg) == 0.0
    assert scoring.recurrence(1, cfg) == pytest.approx(math.log1p(1) / math.log1p(20))
    assert scoring.recurrence(20, cfg) == pytest.approx(1.0)
    assert scoring.recurrence(500, cfg) == 1.0


def test_cross_source_counts_distinct_sources(cfg):
    assert scoring.cross_source(["youtube", "youtube"], 3) == pytest.approx(1 / 3)
    assert scoring.cross_source(["youtube", "web_questions", "manual"], 3) == 1.0


def test_recency_halves_every_half_life(cfg):
    assert scoring.recency(NOW, NOW, cfg) == 1.0
    assert scoring.recency(NOW - timedelta(days=14), NOW, cfg) == pytest.approx(0.5)
    assert scoring.recency(NOW - timedelta(days=28), NOW, cfg) == pytest.approx(0.25)
    assert scoring.recency(None, NOW, cfg) == 0.0


def test_a_future_date_does_not_score_above_one(cfg):
    assert scoring.recency(NOW + timedelta(days=3), NOW, cfg) == 1.0


def test_trend_compares_the_last_week_with_the_three_before(cfg):
    recent = [NOW - timedelta(days=1)] * 2
    base = [NOW - timedelta(days=10)] * 3
    # 2/7 a day recently vs 3/21 before: ratio 2.0, cap 3 -> 0.667
    assert scoring.trend(recent + base, NOW, cfg) == pytest.approx(2.0 / 3.0)


def test_trend_edges(cfg):
    assert scoring.trend([], NOW, cfg) == 0.0
    assert scoring.trend([NOW - timedelta(days=20)], NOW, cfg) == 0.0, "nothing recent"
    assert scoring.trend([NOW - timedelta(days=1)], NOW, cfg) == 1.0, "new and rising"


def test_trend_is_capped(cfg):
    recent = [NOW - timedelta(days=1)] * 50
    base = [NOW - timedelta(days=10)]
    assert scoring.trend(recent + base, NOW, cfg) == 1.0


def test_percentiles_rank_within_the_group_and_share_ties():
    pct = scoring.percentiles({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0, "none": None})
    assert pct == {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0, "none": 0.5}


def test_a_lone_value_is_neutral():
    assert scoring.percentiles({"only": 9.0}) == {"only": 0.5}


def test_youtube_replies_weigh_more_than_likes():
    likes = scoring.raw_engagement("youtube", {"comment_likes": 10, "comment_replies": 0})
    replies = scoring.raw_engagement("youtube", {"comment_likes": 0, "comment_replies": 10})
    assert replies > likes


def test_web_results_favour_recurrence_and_a_high_position():
    top = scoring.raw_engagement("web_questions", {"result_position": 0, "query_recurrence": 2})
    low = scoring.raw_engagement("web_questions", {"result_position": 9, "query_recurrence": 1})
    assert top > low


def test_sources_without_signals_are_neutral():
    assert scoring.raw_engagement("manual", {"platform": "reddit"}) is None
    assert scoring.raw_engagement("youtube", {}) is None


def test_the_total_is_the_weighted_sum_plus_feedback_clamped(cfg):
    ones = dict.fromkeys(("recurrence", "cross_source", "engagement", "recency", "trend"), 1.0)
    assert scoring.total(ones, 0.0, cfg) == pytest.approx(1.0)
    assert scoring.total(ones, -0.15, cfg) == pytest.approx(0.85)
    zeros = dict.fromkeys(ones, 0.0)
    assert scoring.total(zeros, -0.15, cfg) == 0.0
