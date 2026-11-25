"""The threshold check, and — with `-m model` — the real model against it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calibrate_dedupe

FIXTURE = Path(__file__).parent / "fixtures" / "dedupe_pairs.yaml"


def test_well_separated_figures_pass():
    sims = {"same": [0.93, 0.88], "different": [0.84, 0.81], "unrelated": [0.55, 0.62]}
    assert calibrate_dedupe.check(sims, merge=0.90, lower=0.80) == []


def test_a_same_pair_below_the_judge_band_is_caught():
    sims = {"same": [0.78], "different": [], "unrelated": []}
    assert "could never happen" in calibrate_dedupe.check(sims, 0.90, 0.80)[0]


def test_a_different_pair_that_would_auto_merge_is_caught():
    sims = {"same": [], "different": [0.92], "unrelated": []}
    assert "without the judge" in calibrate_dedupe.check(sims, 0.90, 0.80)[0]


def test_an_unrelated_pair_in_the_judge_band_is_caught():
    sims = {"same": [], "different": [], "unrelated": [0.83]}
    assert "wasted" in calibrate_dedupe.check(sims, 0.90, 0.80)[0]


def test_the_fixture_has_every_kind_of_pair():
    pairs = yaml.safe_load(FIXTURE.read_text())
    assert all(len(pairs[k]) >= 4 for k in ("same", "different", "unrelated"))


@pytest.mark.model
def test_the_real_model_respects_the_thresholds(config_dir):
    """Downloads the model. Run in the score workflow, not in CI."""
    from tula_insights.config import load_config
    from tula_insights.utils.embeddings import FastEmbedEmbedder

    cfg = load_config().scoring
    embedder = FastEmbedEmbedder(cfg.embeddings.model, cache_dir=cfg.embeddings.cache_dir)
    pairs = yaml.safe_load(FIXTURE.read_text())
    sims = {k: calibrate_dedupe.similarities(embedder, pairs[k]) for k in pairs}
    assert calibrate_dedupe.check(sims, cfg.dedupe.merge_threshold,
                                  cfg.dedupe.judge_lower_threshold) == []
