"""The adapter layer: what do these columns mean.

Four pieces live here.

`ColumnResolver` finds the actual header a table used for a field. Source files
spell the same field several ways, so this is where drift gets absorbed.

`RowReader` reads one source row. It bundles the four things every field read
needs (the row, the resolver, the batch, the row number) so an adapter can say
`read.get("Email", parse=normalize_email)` instead of passing those four
arguments thirty times over.

`SourceAdapter` is the base class each source schema subclasses. It declares
which headers identify the schema, scores an incoming table against them, and
maps rows onto the canonical model.

`AdapterRegistry` picks the best-fitting adapter for a table.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, Iterable

from rapidfuzz import fuzz

from ...model.core import CanonicalBatch, IssueLevel, Provenance
from ...normalize.values import clean_str
from ..readers.base import RawTable

__all__ = [
    "SourceAdapter",
    "AdapterRegistry",
    "NoAdapterMatched",
    "ColumnResolver",
    "RowReader",
    "canonical_header",
]

#: An adapter scoring below this refuses to claim a table, so an unrecognised
#: schema raises instead of being mapped wrongly.
DETECTION_FLOOR = 0.55

#: Two header spellings scoring at or above this count as the same field.
HEADER_SIMILARITY_FLOOR = 88.0


class NoAdapterMatched(Exception):
    """Raised when no adapter recognises a table's schema."""


def canonical_header(name: str) -> str:
    """Reduce a header to a comparable form.

    Lowercases, collapses separators, and drops punctuation that is never
    meaningful. Note that this deliberately makes 'title' and 'Title'
    identical, which is why ColumnResolver checks literal names first.
    """
    text = str(name or "").strip().lower()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^\w\s()./#]", "", text)
    return text.strip()


class ColumnResolver:
    """Finds which header a table used for a given field.

    Resolution runs three passes, in this order:

      1. Literal, case-sensitive. 'Title' matches only 'Title'.
      2. Canonical. 'LinkedIn Profile' matches 'Linkedin Profile'.
      3. Fuzzy. 'Phone 2' matches 'Phone-2' at 88% similarity or better.

    The order matters and is load-bearing. The Apollo export carries both
    `title` (what PhantomBuster reported) and `Title` (what Apollo answered),
    holding different values. Canonicalizing first would collapse them and hand
    back the same column for both halves, silently destroying the disagreement
    that module M4 exists to detect.
    """

    def __init__(self, columns: Iterable[str]) -> None:
        # Pass 1 lookup: exact header text -> the column as it appears.
        self._literal: dict[str, str] = {}
        # Passes 2 and 3 lookup: canonical form -> every column sharing it.
        self._by_canonical: dict[str, list[str]] = {}

        for column in columns:
            self._literal.setdefault(str(column).strip(), column)
            self._by_canonical.setdefault(canonical_header(column), []).append(column)

    def resolve(self, candidates: list[str]) -> str | None:
        """Return the header this table uses for a field, or None."""
        return (
            self._match_literal(candidates)
            or self._match_canonical(candidates)
            or self._match_fuzzy(candidates)
        )

    def _match_literal(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            column = self._literal.get(str(candidate).strip())
            if column is not None:
                return column
        return None

    def _match_canonical(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            columns = self._by_canonical.get(canonical_header(candidate))
            if columns:
                return columns[0]
        return None

    def _match_fuzzy(self, candidates: list[str]) -> str | None:
        best_column, best_score = None, 0.0
        for candidate in candidates:
            wanted = canonical_header(candidate)
            for existing, columns in self._by_canonical.items():
                score = fuzz.ratio(wanted, existing)
                if score > best_score:
                    best_column, best_score = columns[0], score
        return best_column if best_score >= HEADER_SIMILARITY_FLOOR else None

    def coverage(self, expected: Iterable[str]) -> float:
        """Fraction of the expected headers this table can satisfy."""
        expected = list(expected)
        if not expected:
            return 0.0
        found = sum(1 for header in expected if self.resolve([header]) is not None)
        return found / len(expected)

    def ambiguous(self) -> dict[str, list[str]]:
        """Headers sharing a canonical form, such as 'title' and 'Title'."""
        return {k: v for k, v in self._by_canonical.items() if len(v) > 1}


class RowReader:
    """Reads fields out of one source row.

    An adapter creates one of these per row, then uses it like this::

        read = self._reader(row, resolver, batch, index)

        contact = Contact(
            title_raw=read.get("Title"),
            email=read.get("Email", parse=normalize_email),
            phone=read.get("Phone", "Phone-2", "Phone 2", parse=normalize_phone),
        )
        batch.record_provenance(f"contact:{index}", read.take())

    Extra positional arguments are alternative spellings of the same field,
    tried in order. The first one the table actually has wins, which is how one
    adapter serves three sheets whose headers disagree.

    `parse` defaults to `clean_str`, which trims whitespace and maps
    placeholder text to None.

    Every successful read appends to `self.provenance`, so the adapter hands
    the whole list to the batch once the record is built. A value the parser
    cannot interpret returns None and logs a warning on the batch, leaving the
    row intact. That is the soft-failure rule: bad data gets flagged and
    carried rather than silently dropped.
    """

    def __init__(
        self,
        row: dict[str, Any],
        resolver: ColumnResolver,
        batch: CanonicalBatch,
        row_index: int,
        source_id: str,
    ) -> None:
        self.row = row
        self.resolver = resolver
        self.batch = batch
        self.row_index = row_index
        self.source_id = source_id
        self.provenance: list[Provenance] = []

    def get(
        self,
        *candidates: str,
        parse: Callable[[Any], Any] = clean_str,
    ) -> Any:
        """Read one field: find its column, parse it, record where it came from."""
        column = self.resolver.resolve(list(candidates))
        if column is None:
            return None

        raw = self.row.get(column)
        value = parse(raw)

        if value is None:
            self._warn_if_value_was_present(column, raw)
            return None

        self.provenance.append(
            Provenance(
                source_id=self.source_id,
                file=self.batch.file,
                row=self.row_index,
                field=column,
            )
        )
        return value

    def raw(self, *candidates: str) -> Any:
        """Read a cell without parsing, for callers doing their own handling."""
        column = self.resolver.resolve(list(candidates))
        return None if column is None else self.row.get(column)

    def take(self) -> list[Provenance]:
        """Hand over the provenance collected so far and start a fresh list.

        Adapters that build more than one record from a single row call this
        between records, so each record gets its own provenance entries.
        """
        collected, self.provenance = self.provenance, []
        return collected

    def _warn_if_value_was_present(self, column: str, raw: Any) -> None:
        """A cell holding text the parser rejected is worth reporting."""
        if raw in (None, ""):
            return
        text = str(raw).strip()
        if not text:
            return
        self.batch.add_issue(
            level=IssueLevel.WARNING,
            code="unparsed_value",
            detail=f"Could not parse {column!r} value {text[:60]!r}",
            source_id=self.source_id,
            file=self.batch.file,
            row=self.row_index,
            field=column,
        )


class SourceAdapter(ABC):
    """Base class for every source schema.

    A subclass declares three things and implements one method:

        source_id           short name used in reports and on the CLI
        signature_columns   headers identifying this schema, used for scoring
        required_columns    headers without which the adapter refuses the table
        to_canonical()      maps rows onto Accounts and Contacts
    """

    source_id: ClassVar[str]
    signature_columns: ClassVar[tuple[str, ...]] = ()
    required_columns: ClassVar[tuple[str, ...]] = ()

    def detect(self, table: RawTable) -> float:
        """Score 0.0 to 1.0 for how well this adapter fits the table.

        A missing required column scores zero outright. Otherwise the score is
        the fraction of signature columns the table can supply.
        """
        resolver = ColumnResolver(table.columns)
        for header in self.required_columns:
            if resolver.resolve([header]) is None:
                return 0.0
        if not self.signature_columns:
            return 0.0
        return resolver.coverage(self.signature_columns)

    @abstractmethod
    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        """Map the table onto accounts and contacts."""

    def _new_batch(self, table: RawTable) -> CanonicalBatch:
        """Start an empty batch tagged with this adapter and this file."""
        return CanonicalBatch(
            source_id=self.source_id,
            file=table.path.name,
            sheet=table.sheet,
        )

    def _reader(
        self,
        row: dict[str, Any],
        resolver: ColumnResolver,
        batch: CanonicalBatch,
        index: int,
    ) -> RowReader:
        """Build a RowReader for one row of this adapter's table."""
        return RowReader(row, resolver, batch, index, self.source_id)


class AdapterRegistry:
    """Chooses the best-fitting adapter for a table."""

    def __init__(self, adapters: list[SourceAdapter] | None = None) -> None:
        self._adapters: list[SourceAdapter] = list(adapters or [])

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters.append(adapter)

    def by_id(self, source_id: str) -> SourceAdapter:
        """Look up an adapter by name, for when the caller forces a source."""
        for adapter in self._adapters:
            if adapter.source_id == source_id:
                return adapter
        known = ", ".join(sorted(a.source_id for a in self._adapters))
        raise NoAdapterMatched(f"Unknown source id {source_id!r}. Known: {known}")

    def score_all(self, table: RawTable) -> list[tuple[str, float]]:
        """Every adapter's score for this table, best first. Used by `inspect`."""
        scores = [(a.source_id, a.detect(table)) for a in self._adapters]
        return sorted(scores, key=lambda pair: pair[1], reverse=True)

    def detect(self, table: RawTable) -> tuple[SourceAdapter, float]:
        """Return the winning adapter and its score, or raise if nothing fits."""
        best_adapter, best_score = None, 0.0
        for adapter in self._adapters:
            score = adapter.detect(table)
            if score > best_score:
                best_adapter, best_score = adapter, score

        if best_adapter is None or best_score < DETECTION_FLOOR:
            ranked = self.score_all(table)[:3]
            detail = ", ".join(f"{sid}={score:.2f}" for sid, score in ranked)
            raise NoAdapterMatched(
                f"No adapter reached the detection floor of {DETECTION_FLOOR} "
                f"for {table.name}. Best candidates: {detail}. "
                f"Pass --source explicitly, or add an adapter."
            )
        return best_adapter, best_score


def default_registry() -> AdapterRegistry:
    """The five adapters shipped with version one."""
    from .apollo import ApolloEnrichAdapter
    from .campaign_status import CampaignStatusAdapter
    from .formatdb import FormatDbAdapter
    from .phantombuster import PhantomBusterAdapter
    from .zoominfo import ZoomInfoEnrichAdapter

    return AdapterRegistry([
        PhantomBusterAdapter(),
        ApolloEnrichAdapter(),
        ZoomInfoEnrichAdapter(),
        FormatDbAdapter(),
        CampaignStatusAdapter(),
    ])
