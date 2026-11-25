#!/usr/bin/env python3
"""Check the dedupe thresholds against the real embedding model.

How similar two questions look depends heavily on the model: some score every pet
question above 0.8 just for sharing a topic. This embeds a fixed set of question pairs
with known answers (tests/fixtures/dedupe_pairs.yaml) and checks that the thresholds
in config/scoring.yaml sort them correctly. The score workflow runs it before scoring,
so a bad threshold stops the run instead of quietly merging distinct questions.

    python scripts/calibrate_dedupe.py

Exit code 0: the thresholds behave. 1: they don't — the table shows where. Downloads
the model on first use (about 67 MB). Prints only the fixture questions, which are
invented, never collected data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tula_insights.config import load_config  # noqa: E402
from tula_insights.utils.embeddings import FastEmbedEmbedder  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "dedupe_pairs.yaml"


def similarities(embedder, pairs: list[list[str]]) -> list[float]:
    flat = [text for pair in pairs for text in pair]
    vectors = embedder.embed(flat)
    return [float(vectors[2 * i] @ vectors[2 * i + 1]) for i in range(len(pairs))]


def check(sims: dict[str, list[float]], merge: float, lower: float) -> list[str]:
    """Problems with the thresholds, in words. Empty means they behave."""
    problems = []
    for value in sims["same"]:
        if value < lower:
            problems.append(f"a 'same' pair scored {value:.3f}, below judge_lower_threshold "
                            f"{lower}: such merges could never happen")
    for value in sims["different"]:
        if value >= merge:
            problems.append(f"a 'different' pair scored {value:.3f}, at or above "
                            f"merge_threshold {merge}: it would merge without the judge")
    for value in sims["unrelated"]:
        if value >= lower:
            problems.append(f"an 'unrelated' pair scored {value:.3f}, at or above "
                            f"judge_lower_threshold {lower}: judge calls would be wasted")
    return problems


def main() -> int:
    config = load_config()
    cfg = config.scoring
    pairs = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    embedder = FastEmbedEmbedder(cfg.embeddings.model, cache_dir=cfg.embeddings.cache_dir)

    kinds = ("same", "different", "unrelated")
    sims = {kind: similarities(embedder, pairs[kind]) for kind in kinds}
    merge, lower = cfg.dedupe.merge_threshold, cfg.dedupe.judge_lower_threshold

    print(f"Model {cfg.embeddings.model}: merge >= {merge}, judge {lower}-{merge}, new < {lower}\n")
    for kind in ("same", "different", "unrelated"):
        values = sims[kind]
        print(f"  {kind:<10} min {min(values):.3f}  max {max(values):.3f}   "
              + " ".join(f"{v:.3f}" for v in values))

    problems = check(sims, merge, lower)
    if problems:
        print("\nThe thresholds do not suit this model:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nAdjust dedupe thresholds in config/scoring.yaml using the figures above.",
              file=sys.stderr)
        return 1
    print("\nThresholds behave on every fixture pair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
