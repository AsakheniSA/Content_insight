"""Planning a run: which sources, and do they have modules."""

from __future__ import annotations

import pytest
import yaml

from tula_insights.budget import UsageLedger
from tula_insights.collectors.base import CollectorError, RunContext
from tula_insights.collectors.registry import build_collector, known_collectors, plan_sources
from tula_insights.config import load_config


@pytest.fixture
def config(config_dir):
    return load_config()


def ctx_for(config):
    return RunContext(run_id="r", config=config, ledger=UsageLedger(config=config))


def test_the_default_plan_is_every_enabled_source(config):
    assert [s.name for s in plan_sources(config)] == ["reddit_search"]


def test_a_named_source_narrows_the_plan(config):
    assert [s.name for s in plan_sources(config, source="reddit_search")] == ["reddit_search"]


def test_an_unknown_source_names_the_real_ones(config):
    with pytest.raises(CollectorError, match="Unknown source"):
        plan_sources(config, source="facebook")


def test_asking_for_a_disabled_source_is_an_error_not_silence(config):
    """Silently collecting nothing from a source you asked for is the worse failure."""
    with pytest.raises(CollectorError, match="disabled"):
        plan_sources(config, source="youtube")


def test_an_enabled_source_with_no_module_fails_at_planning_time(config_dir):
    path = config_dir / "sources.yaml"
    data = yaml.safe_load(path.read_text())
    for source in data["sources"]:
        if source["name"] == "news_rss":
            source["enabled"] = True
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(CollectorError, match="no collector module"):
        plan_sources(load_config())


def test_building_a_known_collector(config):
    collector = build_collector("reddit_search", ctx_for(config))
    assert collector.name == "reddit_search"
    assert hasattr(collector, "collect")


def test_building_an_unknown_collector_lists_what_exists(config):
    with pytest.raises(CollectorError, match="reddit_search"):
        build_collector("nope", ctx_for(config))


def test_every_registered_collector_is_a_real_source(config):
    """The registry must not drift away from the config file."""
    names = {s.name for s in config.sources.resolve()}
    assert known_collectors() <= names
