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
    "collect": 2,
    "classify": 3,
    "score": 4,
    "publish": 5,
    "archive": 8,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tula_insights",
        description="Tula Insights Layer — research and insights feed.",
    )
    parser.add_argument(
        "stage",
        choices=["validate-config", "setup-sheet", *PENDING_STAGES],
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

    stage_number = PENDING_STAGES[args.stage]
    print(
        f"The {args.stage!r} stage is not built yet — it lands in build stage "
        f"{stage_number} (see IMPLEMENTATION_BRIEF.md section 11).",
        file=sys.stderr,
    )
    return 3


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
