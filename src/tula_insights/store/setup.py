"""Creating and validating the spreadsheet's tabs.

Lives in the package rather than in ``scripts/`` so that both the CLI
(``python -m tula_insights setup-sheet``) and the standalone
``scripts/setup_sheet.py`` call exactly the same code.
"""

from __future__ import annotations

import sys

from ..config import AppConfig, ConfigError
from ..schema import TABS
from .sheets import SheetStoreError, open_store


def ensure_schema(config: AppConfig, *, dry_run: bool = False) -> int:
    """Create missing tabs, validate existing ones. Returns a process exit code.

    Existing headers are reported, never rewritten: reordering a header on a tab the
    team is already using would move their data under new labels without telling anyone.
    """
    try:
        store = open_store(config, dry_run=dry_run)
    except (ConfigError, SheetStoreError) as error:
        print(f"Could not open the spreadsheet:\n  {error}", file=sys.stderr)
        return 2

    print(f"Spreadsheet: {store.book.title}")
    if dry_run:
        print("Dry run — nothing will be written.\n")

    created: list[str] = []
    checked: list[str] = []
    problems: list[str] = []

    for spec in TABS:
        try:
            was_created, tab_problems = store.ensure_tab(spec)
        except SheetStoreError as error:
            problems.append(f"{spec.name}: {error}")
            print(f"  failed   {spec.name:<20} {error}", file=sys.stderr)
            continue
        if was_created:
            created.append(spec.name)
            print(f"  created  {spec.name:<20} {len(spec.columns)} columns")
        else:
            checked.append(spec.name)
            print(f"  {'ok' if not tab_problems else '!!':<8} {spec.name:<20} already exists")
            problems.extend(tab_problems)

    print(f"\n{len(created)} tab(s) created, {len(checked)} checked.")

    if problems:
        print("\nHeader problems — fix these before running the pipeline:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("Every tab matches the schema.")
    if created:
        print(
            "\nNext: share the sheet with whoever reviews the feed, and leave the two "
            "tabs beginning with an underscore hidden — they are the pipeline's own memory."
        )
    return 0
