"""The CLI must validate config, and must refuse unbuilt stages loudly."""

from __future__ import annotations

import pytest

from tula_insights.cli import PENDING_STAGES, build_parser, main


def test_validate_config_succeeds_on_the_real_config(config_dir, capsys):
    assert main(["validate-config"]) == 0
    out = capsys.readouterr().out
    assert "Config is valid" in out
    assert "reddit_search" in out


def test_validate_config_reports_missing_environment(config_dir, capsys):
    main(["validate-config"])
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_broken_config_exits_two_without_a_traceback(config_dir, capsys):
    (config_dir / "scoring.yaml").write_text("weights: {recurrence: 2.0}\n", encoding="utf-8")
    assert main(["validate-config"]) == 2
    assert "Config error" in capsys.readouterr().err


@pytest.mark.parametrize("stage", sorted(PENDING_STAGES))
def test_unbuilt_stages_say_which_stage_they_arrive_in(stage, config_dir, capsys):
    assert main([stage]) == 3
    err = capsys.readouterr().err
    assert "not built yet" in err
    assert str(PENDING_STAGES[stage]) in err


def test_unknown_stage_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["teleport"])


def test_the_documented_flags_all_parse():
    args = build_parser().parse_args(
        ["collect", "--source", "reddit_search", "--category", "gear_packing", "--dry-run", "--sync"]
    )
    assert args.source == "reddit_search"
    assert args.category == "gear_packing"
    assert args.dry_run is True
    assert args.sync is True
