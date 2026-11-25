#!/usr/bin/env python3
"""Create and validate the tabs of the Tula Insights spreadsheet.

Idempotent: run it as often as you like. It creates any tab that is missing, writes and
freezes the header row, and hides the two internal tabs. For tabs that already exist it
only reports header differences — it never rewrites a header.

    python scripts/setup_sheet.py             # create and validate
    python scripts/setup_sheet.py --dry-run   # say what it would do, change nothing

Needs GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID (see .env.example), and the spreadsheet
shared with the service account's email address as an Editor.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running straight from a checkout, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tula_insights.config import ConfigError, load_config
from tula_insights.store.setup import ensure_schema
from tula_insights.utils.logging import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and validate the spreadsheet tabs.")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"Config error:\n{error}", file=sys.stderr)
        return 2

    setup_logging(level=config.settings.logging.level, fmt="plain")
    return ensure_schema(config, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
