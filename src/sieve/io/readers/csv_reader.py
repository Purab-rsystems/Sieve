"""CSV and TSV reading."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, ClassVar, Iterator

from .base import RawTable

__all__ = ["CsvReader"]

# Cells in these files reach several kilobytes (LinkedIn bios, company
# descriptions), which exceeds the stdlib default.
csv.field_size_limit(10_000_000)

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


class CsvReader:
    extensions: ClassVar[tuple[str, ...]] = (".csv", ".tsv", ".txt")

    def read(self, path: Path) -> Iterator[RawTable]:
        text = self._decode(path)
        delimiter = "\t" if path.suffix.lower() == ".tsv" else self._sniff(text)
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        columns = [c.strip() for c in (reader.fieldnames or [])]
        rows: list[dict[str, Any]] = []
        for record in reader:
            rows.append({(k.strip() if k else k): v for k, v in record.items()})
        yield RawTable(path=path, sheet=None, columns=columns, rows=rows)

    @staticmethod
    def _decode(path: Path) -> str:
        raw = path.read_bytes()
        for encoding in _ENCODINGS:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _sniff(text: str) -> str:
        head = "\n".join(text.splitlines()[:5])
        try:
            return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","
