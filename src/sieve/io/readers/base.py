"""The reader layer.

A reader answers one question: how do I get rows out of these bytes. It knows
about file formats and stays ignorant of what any column means, which is the
adapter's job. Keeping the two apart is what lets the same ZoomInfo schema
arrive as CSV today and XLSX tomorrow without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterator, Protocol, runtime_checkable

__all__ = ["RawTable", "Reader", "ReaderRegistry", "UnsupportedFormat"]


class UnsupportedFormat(Exception):
    """Raised when no reader claims a file extension."""


@dataclass(frozen=True)
class RawTable:
    """One rectangular table of strings, exactly as the file presented it."""

    path: Path
    sheet: str | None
    columns: list[str]
    rows: list[dict[str, Any]] = field(repr=False, default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.path.name}::{self.sheet}" if self.sheet else self.path.name

    def __len__(self) -> int:
        return len(self.rows)


@runtime_checkable
class Reader(Protocol):
    """Turns a path into one or more tables."""

    extensions: ClassVar[tuple[str, ...]]

    def read(self, path: Path) -> Iterator[RawTable]: ...


class ReaderRegistry:
    """Dispatches a path to the reader that claims its extension."""

    def __init__(self, readers: list[Reader] | None = None) -> None:
        self._readers: list[Reader] = list(readers or [])

    def register(self, reader: Reader) -> None:
        self._readers.append(reader)

    def for_path(self, path: Path) -> Reader:
        suffix = path.suffix.lower()
        for reader in self._readers:
            if suffix in reader.extensions:
                return reader
        supported = sorted({e for r in self._readers for e in r.extensions})
        raise UnsupportedFormat(
            f"No reader handles '{suffix}'. Supported extensions: {', '.join(supported)}"
        )

    def read(self, path: Path) -> Iterator[RawTable]:
        yield from self.for_path(path).read(path)


def default_registry() -> ReaderRegistry:
    """The readers shipped with version one."""
    from .csv_reader import CsvReader
    from .excel_reader import ExcelReader

    return ReaderRegistry([CsvReader(), ExcelReader()])
