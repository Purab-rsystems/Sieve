"""Excel reading, one RawTable per worksheet.

The modernization workbook holds four sheets carrying three different schemas,
so emitting a table per sheet rather than per file is what lets the adapter
registry detect each one separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Iterator

from openpyxl import load_workbook

from .base import RawTable

__all__ = ["ExcelReader"]


class ExcelReader:
    extensions: ClassVar[tuple[str, ...]] = (".xlsx", ".xlsm", ".xltx")

    def __init__(self, header_scan_depth: int = 10) -> None:
        self.header_scan_depth = header_scan_depth

    def read(self, path: Path) -> Iterator[RawTable]:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                table = self._read_sheet(path, sheet)
                if table is not None:
                    yield table
        finally:
            workbook.close()

    def _read_sheet(self, path: Path, sheet: Any) -> RawTable | None:
        values = list(sheet.iter_rows(values_only=True))
        if not values:
            return None
        header_index = self._find_header(values)
        if header_index is None:
            return None
        header = values[header_index]
        columns = [self._label(cell, i) for i, cell in enumerate(header)]
        rows: list[dict[str, Any]] = []
        for raw in values[header_index + 1:]:
            if all(cell is None or str(cell).strip() == "" for cell in raw):
                continue
            record = {columns[i]: (raw[i] if i < len(raw) else None)
                      for i in range(len(columns))}
            rows.append(record)
        return RawTable(path=path, sheet=sheet.title, columns=columns, rows=rows)

    def _find_header(self, values: list[tuple[Any, ...]]) -> int | None:
        """Pick the first row that looks like a header.

        Sheets often carry a title row or a blank row above the real header,
        so the row with the most non-empty text cells within the scan window
        wins.
        """
        best_index, best_score = None, 0
        for index, row in enumerate(values[: self.header_scan_depth]):
            score = sum(
                1 for cell in row
                if cell is not None and str(cell).strip() != ""
            )
            if score > best_score:
                best_index, best_score = index, score
        return best_index if best_score >= 2 else None

    @staticmethod
    def _label(cell: Any, index: int) -> str:
        if cell is None or str(cell).strip() == "":
            return f"__unnamed_{index}"
        return str(cell).strip()
