"""Command line entry point: ``python -m tula_insights <stage> [options]``.

Stages land one at a time (see IMPLEMENTATION_BRIEF.md section 11). A stage that is not
built yet exits with a clear message naming the stage it arrives in, rather than a
traceback or, worse, a silent success in a scheduled run.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .config import ConfigError, load_config
from .utils.logging import get_logger, setup_logging

log = get_logger(__name__)

#: Stage name -> the build stage it arrives in. Filled in as each one lands.
PENDING_STAGES = {
    "archive": 8,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tula_insights",
        description="Tula Insights Layer — research and insights feed.",
    )
    parser.add_argument(
        "stage",
        choices=["validate-config", "setup-sheet", "collect", "classify", "score",
                 "publish", "plan", "purge", "record-run", "report", *PENDING_STAGES],
        help="which pipeline stage to run",
    )
    parser.add_argument(
        "--source",
        default="all",
        help="limit a stage to one source name from config/sources.yaml (default: all)",
    )
    parser.add_argument(
        "--category",
        default="all",
        help="limit a stage to one category id from config/categories.yaml (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="process everything but write nothing to Sheets; print a summary instead",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="use the standard Claude API instead of the Batch API (classify stage)",
    )
    parser.add_argument(
        "--no-purge",
        action="store_true",
        help="collect: skip the platform storage purge (the pipeline runs it once, up front)",
    )
    parser.add_argument(
        "--run-stats-out",
        default=None,
        help="collect: write the run's figures to this JSON file instead of the runs tab",
    )
    parser.add_argument(
        "--from",
        dest="from_dir",
        default=None,
        help="record-run: the folder holding the collect jobs' stats files",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "plain"],
        default=None,
        help="override the log format from settings.yaml (plain is easier to read locally)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"Config error:\n{error}", file=sys.stderr)
        return 2

    setup_logging(
        level=config.settings.logging.level,
        fmt=args.log_format or config.settings.logging.format,
        allow_item_text=config.settings.logging.allow_item_text,
    )

    if args.stage == "validate-config":
        return _validate_config(config)
    if args.stage == "setup-sheet":
        return _setup_sheet(config, dry_run=args.dry_run)
    if args.stage == "plan":
        return _plan(config, source=args.source)
    if args.stage == "purge":
        return _purge(config, dry_run=args.dry_run)
    if args.stage == "record-run":
        return _record_run(config, from_dir=args.from_dir)
    if args.stage == "report":
        return _report(config)
    if args.stage == "publish":
        return _publish(config, dry_run=args.dry_run)
    if args.stage == "score":
        return _score(config, dry_run=args.dry_run)
    if args.stage == "classify":
        return _classify(config, source=args.source, sync=args.sync, dry_run=args.dry_run)
    if args.stage == "collect":
        return _collect(
            config, source=args.source, category=args.category, dry_run=args.dry_run,
            purge=not args.no_purge, run_stats_out=args.run_stats_out,
        )

    stage_number = PENDING_STAGES[args.stage]
    print(
        f"The {args.stage!r} stage is not built yet — it lands in build stage "
        f"{stage_number} (see IMPLEMENTATION_BRIEF.md section 11).",
        file=sys.stderr,
    )
    return 3


def _publish(config, *, dry_run: bool) -> int:
    from .stages import publish as publish_stage

    try:
        result = publish_stage.run(config, dry_run=dry_run)
    except publish_stage.PublishError as error:
        print(f"Publish failed:\n  {error}", file=sys.stderr)
        return 1
    print("\n".join(publish_stage.summary_lines(result)))
    publish_stage.write_job_summary(result)
    return 0


def _score(config, *, dry_run: bool) -> int:
    from .stages import score as score_stage

    try:
        result = score_stage.run(config, dry_run=dry_run)
    except score_stage.ScoreError as error:
        print(f"Score failed:\n  {error}", file=sys.stderr)
        return 1
    print("\n".join(score_stage.summary_lines(result)))
    score_stage.write_job_summary(result)
    return 0


def _classify(config, *, source: str, sync: bool, dry_run: bool) -> int:
    from .stages import classify as classify_stage

    try:
        result = classify_stage.run(config, sync=sync, dry_run=dry_run, source=source)
    except classify_stage.ClassifyError as error:
        print(f"Classify failed:\n  {error}", file=sys.stderr)
        return 1
    print("\n".join(classify_stage.summary_lines(result)))
    classify_stage.write_job_summary(result)
    return 0


def _plan(config, *, source: str) -> int:
    import json

    from .collectors.base import CollectorError
    from .stages import ops

    try:
        sources = ops.plan(config, source=source)
    except CollectorError as error:
        print(f"Plan failed:\n  {error}", file=sys.stderr)
        return 1
    ops.write_github_output({
        "sources": json.dumps(sources),
        "has_sources": "true" if sources else "false",
    })
    print(f"Sources to collect: {', '.join(sources) or '(none)'}")
    return 0


def _purge(config, *, dry_run: bool) -> int:
    from .stages import ops
    from .store.sheets import SheetStoreError

    try:
        counts = ops.purge(config, dry_run=dry_run)
    except (SheetStoreError, ConfigError) as error:
        print(f"Purge failed:\n  {error}", file=sys.stderr)
        return 1
    suffix = " (dry run — nothing changed)" if dry_run else ""
    print(f"Platform storage purge{suffix}: {counts['items_purged']} item row(s), "
          f"{counts['feed_rows_links_purged']} feed row(s)")
    return 0


def _record_run(config, *, from_dir: str | None) -> int:
    from pathlib import Path

    from .stages import ops
    from .store.sheets import SheetStoreError

    folder = Path(from_dir or "collect-stats")
    paths = list(folder.rglob("*.json")) if folder.exists() else []
    try:
        recorded = ops.record_collect_stats(config, paths)
    except (SheetStoreError, ConfigError, ValueError) as error:
        print(f"Recording the collect figures failed:\n  {error}", file=sys.stderr)
        return 1
    print(f"Recorded figures from {recorded} collect job(s).")
    return 0


def _report(config) -> int:
    from .stages import ops
    from .store.sheets import SheetStoreError

    try:
        lines, summary = ops.report(config)
    except (SheetStoreError, ConfigError) as error:
        print(f"Report failed:\n  {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    ops.write_report(lines, summary)
    return 0


def _collect(
    config, *, source: str, category: str, dry_run: bool, purge: bool = True,
    run_stats_out: str | None = None,
) -> int:
    from .collectors.base import CollectorError
    from .stages import collect as collect_stage
    from .store.sheets import SheetStoreError

    try:
        result = collect_stage.run(
            config, source=source, category=category, dry_run=dry_run,
            purge=purge, run_stats_out=run_stats_out,
        )
    except CollectorError as error:
        print(f"Collect failed:\n  {error}", file=sys.stderr)
        return 1
    except SheetStoreError as error:
        print(f"Collect failed — the spreadsheet could not be reached:\n  {error}",
              file=sys.stderr)
        return 1
    except ConfigError as error:
        print(f"Config error:\n{error}", file=sys.stderr)
        return 2

    print("\n".join(collect_stage.summary_lines(result)))
    collect_stage.write_job_summary(result)
    return 0


def _validate_config(config) -> int:
    enabled = [s.name for s in config.enabled_sources]
    disabled = [s.name for s in config.sources.resolve() if not s.enabled]
    print("Config is valid.\n")
    category_ids = ", ".join(config.categories.ids)
    print(f"  categories    {len(config.categories.categories)}: {category_ids}")
    print(f"  keyword sets  {len(config.keywords.keyword_sets)}")
    print(f"  regions       {', '.join(config.keywords.regions)}")
    print(f"  sources on    {', '.join(enabled) or '(none)'}")
    print(f"  sources off   {', '.join(disabled) or '(none)'}")
    print(f"  feed size     {config.scoring.publish.daily_feed_size}/day, "
          f"max {config.scoring.publish.max_per_category} per category")
    print(f"  budget        ${config.budget.monthly_caps.claude_usd:.2f}/month Claude, "
          f"{config.budget.daily_caps.claude_web_searches} searches/day")
    print(f"  prices dated  {config.budget.prices_checked_on}")

    missing = [
        name
        for name, value in (
            ("ANTHROPIC_API_KEY", config.env.anthropic_api_key),
            ("GOOGLE_SERVICE_ACCOUNT_JSON", config.env.google_service_account_json),
            ("SHEET_ID", config.env.sheet_id),
            ("YOUTUBE_API_KEY", config.env.youtube_api_key),
        )
        if not value
    ]
    if missing:
        print(f"\n  environment   not set: {', '.join(missing)}")
        print("                (fine until the stage that needs them)")
    else:
        print("\n  environment   all four variables are set")
    return 0


def _setup_sheet(config, *, dry_run: bool) -> int:
    from .store.setup import ensure_schema

    return ensure_schema(config, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
