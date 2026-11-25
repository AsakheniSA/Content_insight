"""An in-memory stand-in for gspread, so the store can be tested without a network.

It implements only the handful of methods `SheetStore` actually uses, and it stores
values exactly as they are written, so a test can assert on what reached the sheet.
"""

from __future__ import annotations

from typing import Any

from gspread.exceptions import WorksheetNotFound
from gspread.utils import a1_range_to_grid_range


class FakeWorksheet:
    def __init__(self, title: str, rows: int = 1000, cols: int = 26) -> None:
        self.title = title
        self.row_count = rows
        self.col_count = cols
        self.values: list[list[Any]] = []
        self.hidden = False
        self.frozen_rows = 0
        self.calls: list[str] = []

    # --- helpers ---------------------------------------------------------------------

    def _ensure(self, row: int, col: int) -> None:
        while len(self.values) < row:
            self.values.append([])
        line = self.values[row - 1]
        while len(line) < col:
            line.append("")

    # --- gspread surface -------------------------------------------------------------

    def update(self, values, range_name=None, **kwargs):
        self.calls.append("update")
        grid = a1_range_to_grid_range(range_name or "A1")
        top = grid.get("startRowIndex", 0)
        left = grid.get("startColumnIndex", 0)
        for r, line in enumerate(values):
            for c, value in enumerate(line):
                self._ensure(top + r + 1, left + c + 1)
                self.values[top + r][left + c] = value
        return {}

    def append_rows(self, values, **kwargs):
        self.calls.append("append_rows")
        self.values.extend([list(line) for line in values])
        return {}

    def batch_update(self, data, **kwargs):
        self.calls.append("batch_update")
        for entry in data:
            self.update(entry["values"], entry["range"])
        return {}

    def get_all_values(self, *args, **kwargs):
        self.calls.append("get_all_values")
        width = max((len(line) for line in self.values), default=0)
        return [list(line) + [""] * (width - len(line)) for line in self.values]

    def get_values(self, range_name=None, **kwargs):
        self.calls.append("get_values")
        grid = a1_range_to_grid_range(range_name)
        top = grid.get("startRowIndex", 0)
        left = grid.get("startColumnIndex", 0)
        right = grid.get("endColumnIndex", left + 1)
        out = []
        for line in self.values[top:]:
            out.append([line[i] if i < len(line) else "" for i in range(left, right)])
        while out and not any(str(c).strip() for c in out[-1]):
            out.pop()
        return out

    def row_values(self, row: int, **kwargs):
        self.calls.append("row_values")
        if row > len(self.values):
            return []
        return [str(c) for c in self.values[row - 1]]

    def freeze(self, rows=None, cols=None):
        self.calls.append("freeze")
        self.frozen_rows = rows or 0
        return {}

    def hide(self):
        self.calls.append("hide")
        self.hidden = True
        return {}


class FakeSpreadsheet:
    def __init__(self, title: str = "Tula Insights") -> None:
        self.title = title
        self._sheets: dict[str, FakeWorksheet] = {}

    def worksheets(self, exclude_hidden: bool = False):
        return [w for w in self._sheets.values() if not (exclude_hidden and w.hidden)]

    def worksheet(self, title: str) -> FakeWorksheet:
        try:
            return self._sheets[title]
        except KeyError:
            raise WorksheetNotFound(title) from None

    def add_worksheet(self, title: str, rows: int, cols: int, index=None) -> FakeWorksheet:
        sheet = FakeWorksheet(title, rows, cols)
        self._sheets[title] = sheet
        return sheet
