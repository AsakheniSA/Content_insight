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


# --- Claude API fakes ----------------------------------------------------------------


class Obj:
    """A tiny attribute bag standing in for an SDK response object."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Obj({self.__dict__})"


def search_result(url, title="", page_age=None):
    return Obj(
        type="web_search_result",
        url=url,
        title=title,
        page_age=page_age,
        encrypted_content="enc-" + url,
    )


def tool_use(block_id, query):
    return Obj(type="server_tool_use", id=block_id, name="web_search", input={"query": query})


def tool_result(block_id, results):
    return Obj(type="web_search_tool_result", tool_use_id=block_id, content=list(results))


def tool_error(block_id, error_code):
    return Obj(
        type="web_search_tool_result",
        tool_use_id=block_id,
        content=Obj(type="web_search_tool_result_error", error_code=error_code),
    )


def text(body, citations=None):
    return Obj(type="text", text=body, citations=list(citations or []))


def citation(url, cited_text, title=""):
    return Obj(
        type="web_search_result_location", url=url, cited_text=cited_text, title=title
    )


def message(content, *, stop_reason="end_turn", searches=1, tokens_in=100, tokens_out=10):
    return Obj(
        content=list(content),
        stop_reason=stop_reason,
        usage=Obj(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=Obj(web_search_requests=searches, web_fetch_requests=0),
        ),
    )


class FakeLlm:
    """Stands in for LlmClient: returns queued responses and records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        from tula_insights.llm.client import Usage

        self.usage = Usage()

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeLlm ran out of queued responses")
        response = self.responses.pop(0)
        self.usage.add_response(response, kwargs.get("model", "fake"))
        return response


class FakeSearchProvider:
    """Stands in for a SearchProvider: returns queued responses per call."""

    name = "fake_search"

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        from tula_insights.search.provider import SearchResponse

        return self.responses.pop(0) if self.responses else SearchResponse()
