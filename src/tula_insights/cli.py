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

#: Stage name -> why it is not available yet.
PENDING_STAGES = {
    "archive": (
        "deferred — the sheet is far below its size threshold, and archiving `items` "
        "needs a design decision first (see README, Known limitations)"
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tula_insights",
        description="Tula Insights Layer — research and insights feed.",
    )
    parser.add_argument(
        "stage",
        choices=["validate-config", "setup-sheet", "collect", "classify", "score",
                 "publish", "plan", "purge", "record-run", "report", "budget", "alert",
                 "keepalive", "already-ran", *PENDING_STAGES],
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
    if args.stage == "budget":
        return _budget(config)
    if args.stage == "already-ran":
        return _already_ran(config)
    if args.stage == "alert":
        return _alert(config)
    if args.stage == "keepalive":
        return _keepalive(config, dry_run=args.dry_run)
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

    print(f"The {args.stage!r} stage is not available: {PENDING_STAGES[args.stage]}.",
          file=sys.stderr)
    return 3


def _fail(what: str, error: object) -> int:
    """Report a stage failure on stderr and, in Actions, as an error annotation.

    The annotation is what the alert job quotes in the failure issue.
    """
    from .utils.actions import emit

    print(f"{what}:\n  {error}", file=sys.stderr)
    emit("error", what, str(error))
    return 1


def _cap_failure(config, breaches: list[str]) -> int:
    """With `on_cap_exceeded: fail`, a budget cap that was hit fails the stage.

    Called after the stage has saved its work, so nothing collected is lost.
    """
    if not breaches or config.budget.on_cap_exceeded != "fail":
        return 0
    return _fail(
        "Budget cap reached (budget.yaml: on_cap_exceeded is fail)", "; ".join(breaches)
    )


def _publish(config, *, dry_run: bool) -> int:
    from .stages import publish as publish_stage

    try:
        result = publish_stage.run(config, dry_run=dry_run)
    except publish_stage.PublishError as error:
        return _fail("Publish failed", error)
    print("\n".join(publish_stage.summary_lines(result)))
    publish_stage.write_job_summary(result)
    return _cap_failure(config, result.cap_breaches)


def _score(config, *, dry_run: bool) -> int:
    from .stages import score as score_stage

    try:
        result = score_stage.run(config, dry_run=dry_run)
    except score_stage.ScoreError as error:
        return _fail("Score failed", error)
    print("\n".join(score_stage.summary_lines(result)))
    score_stage.write_job_summary(result)
    return _cap_failure(config, result.cap_breaches)


def _classify(config, *, source: str, sync: bool, dry_run: bool) -> int:
    from .stages import classify as classify_stage

    try:
        result = classify_stage.run(config, sync=sync, dry_run=dry_run, source=source)
    except classify_stage.ClassifyError as error:
        return _fail("Classify failed", error)
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
        return _fail("Plan failed", error)
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
        return _fail("Purge failed", error)
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
        return _fail("Recording the collect figures failed", error)
    print(f"Recorded figures from {recorded} collect job(s).")
    return 0


def _report(config) -> int:
    from .stages import ops
    from .store.sheets import SheetStoreError

    try:
        lines, summary = ops.report(config)
    except (SheetStoreError, ConfigError) as error:
        return _fail("Report failed", error)
    print("\n".join(lines))
    ops.write_report(lines, summary)
    return 0


def _budget(config) -> int:
    from .stages import ops
    from .store.sheets import SheetStoreError

    try:
        lines, warnings = ops.budget_status(config)
    except (SheetStoreError, ConfigError) as error:
        return _fail("Budget check failed", error)
    print("\n".join(lines))
    ops.write_budget_summary(lines, warnings)
    return 0


def _already_ran(config) -> int:
    """Hand `skip=true` to later jobs if today's full run is already done."""
    from .stages import ops
    from .store.sheets import SheetStoreError

    try:
        run_id = ops.full_run_done_today(config)
    except (SheetStoreError, ConfigError) as error:
        # Better to risk a second run than to miss a day.
        print(f"Could not check today's runs, so this run goes ahead: {error}")
        run_id = ""
    ops.write_github_output({"skip": "true" if run_id else "false"})
    if run_id:
        print(f"Today's full run is already done ({run_id}); this backup run stops here.")
    else:
        print("No full run yet today; this run goes ahead.")
    return 0


def _alert(config) -> int:
    import os

    from . import alerts
    from .github_api import GitHubClient, GitHubError, RunInfo

    try:
        outcome = alerts.RunOutcome.from_json(
            os.environ.get("NEEDS_JSON", ""),
            event=os.environ.get("GITHUB_EVENT_NAME", ""),
            inputs_json=os.environ.get("INPUTS_JSON", ""),
        )
        result = alerts.handle(
            GitHubClient.from_env(), RunInfo.from_env(), outcome, config.settings.alerts
        )
    except (GitHubError, ValueError) as error:
        return _fail("Alert failed", error)
    label = config.settings.alerts.label
    messages = {
        "opened": f"Opened issue #{result.issue_number} ({label}).",
        "commented": f"Commented on open issue #{result.issue_number} ({label}).",
        "closed": f"Closed issue #{result.issue_number} ({label}): fully successful run.",
        "none": "Nothing to do: no failure, and no open issue this run could close.",
    }
    if result.failed_jobs:
        print(f"Failed jobs: {', '.join(result.failed_jobs)}")
    print(messages[result.action])
    return 0


def _keepalive(config, *, dry_run: bool) -> int:
    from . import keepalive
    from .github_api import GitHubClient, GitHubError

    try:
        result = keepalive.run(GitHubClient.from_env(), config.settings.keepalive,
                               dry_run=dry_run)
    except GitHubError as error:
        return _fail("Keep-alive failed", error)
    print("\n".join(keepalive.summary_lines(result, config.settings.keepalive)))
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
        return _fail(f"Collect failed ({source})", error)
    except SheetStoreError as error:
        return _fail(f"Collect failed ({source}) — the spreadsheet could not be reached", error)
    except ConfigError as error:
        print(f"Config error:\n{error}", file=sys.stderr)
        return 2

    print("\n".join(collect_stage.summary_lines(result)))
    collect_stage.write_job_summary(result)
    return _cap_failure(config, result.cap_breaches)


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
