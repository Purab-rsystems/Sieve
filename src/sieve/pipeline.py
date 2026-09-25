"""Orchestration.

`normalize` is the one public entry point M0 provides. It reads a file, picks
an adapter per table, maps everything onto the canonical model, and returns the
result together with a report describing what happened.

Everything here is a pure function of its arguments. A file is written only
when the caller asks for one, in the CLI layer above.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .io.adapters.base import (
    AdapterRegistry, NoAdapterMatched, SourceAdapter, default_registry as adapters,
)
from .io.readers.base import RawTable, ReaderRegistry, default_registry as readers
from .model.core import Account, CanonicalBatch, Contact, IssueLevel

__all__ = ["NormalizeResult", "TableOutcome", "normalize", "load_tables",
           "accounts_to_frame", "contacts_to_frame", "pair_by_source_row"]


@dataclass
class TableOutcome:
    """What happened to one table inside a file."""

    table: str
    sheet: str | None
    rows: int
    source_id: str | None
    confidence: float
    accounts: int
    contacts: int
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "sheet": self.sheet,
            "rows": self.rows,
            "source_id": self.source_id,
            "confidence": round(self.confidence, 3),
            "accounts": self.accounts,
            "contacts": self.contacts,
            "error": self.error,
        }


@dataclass
class NormalizeResult:
    """The canonical output of one or more files, plus a report."""

    batch: CanonicalBatch
    outcomes: list[TableOutcome] = field(default_factory=list)

    @property
    def accounts(self) -> list[Account]:
        return self.batch.accounts

    @property
    def contacts(self) -> list[Contact]:
        return self.batch.contacts

    def report(self) -> dict[str, Any]:
        issues: dict[str, int] = {}
        for issue in self.batch.issues:
            issues[issue.code] = issues.get(issue.code, 0) + 1
        return {
            "tables": [o.as_dict() for o in self.outcomes],
            "totals": {
                "tables": len(self.outcomes),
                "tables_recognised": sum(1 for o in self.outcomes if o.source_id),
                "rows": sum(o.rows for o in self.outcomes),
                "accounts": len(self.batch.accounts),
                "contacts": len(self.batch.contacts),
            },
            "issues": {
                "total": len(self.batch.issues),
                "errors": sum(
                    1 for i in self.batch.issues if i.level is IssueLevel.ERROR
                ),
                "by_code": dict(sorted(issues.items())),
            },
        }


def load_tables(
    path: Path,
    *,
    reader_registry: ReaderRegistry | None = None,
) -> list[RawTable]:
    """Read every table out of a file without interpreting any of it."""
    registry = reader_registry or readers()
    return list(registry.read(Path(path)))


def normalize(
    paths: Path | str | list[Path | str],
    *,
    source: str | None = None,
    reader_registry: ReaderRegistry | None = None,
    adapter_registry: AdapterRegistry | None = None,
    strict: bool = False,
) -> NormalizeResult:
    """Map one or more source files onto the canonical model.

    `source` forces a specific adapter. Leaving it as None runs detection,
    which is what handles a workbook whose sheets carry different schemas.

    With `strict` set, a table nothing recognises raises. Otherwise it is
    recorded as an outcome with an error and the run continues, so one
    unrecognised sheet cannot stop a five-sheet workbook.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    reader_registry = reader_registry or readers()
    adapter_registry = adapter_registry or adapters()

    combined = CanonicalBatch(source_id=source or "mixed", file="")
    outcomes: list[TableOutcome] = []

    for raw_path in paths:
        path = Path(raw_path)
        for table in reader_registry.read(path):
            adapter, confidence, error = _select(adapter_registry, table, source)
            if adapter is None:
                if strict:
                    raise NoAdapterMatched(error or f"No adapter for {table.name}")
                outcomes.append(TableOutcome(
                    table=table.name, sheet=table.sheet, rows=len(table),
                    source_id=None, confidence=0.0, accounts=0, contacts=0,
                    error=error,
                ))
                continue

            produced = adapter.to_canonical(table)
            combined.extend(produced)
            outcomes.append(TableOutcome(
                table=table.name, sheet=table.sheet, rows=len(table),
                source_id=adapter.source_id, confidence=confidence,
                accounts=len(produced.accounts), contacts=len(produced.contacts),
            ))

    return NormalizeResult(batch=combined, outcomes=outcomes)


def pair_by_source_row(contacts: list[Contact]) -> dict[int, list[Contact]]:
    """Group contacts that came from the same source row.

    The Apollo adapter emits two records per row, one holding what
    PhantomBuster reported and one holding what Apollo answered. Grouping them
    by `source_row` is how M4 finds the pairs to compare.

    Grouping by LinkedIn URL instead loses rows, because four matched records
    in the sample file carry no `Person Linkedin Url` at all.
    """
    groups: dict[int, list[Contact]] = {}
    for contact in contacts:
        if contact.source_row is not None:
            groups.setdefault(contact.source_row, []).append(contact)
    return groups


def _select(
    registry: AdapterRegistry,
    table: RawTable,
    source: str | None,
) -> tuple[SourceAdapter | None, float, str | None]:
    if source:
        adapter = registry.by_id(source)
        return adapter, adapter.detect(table), None
    try:
        adapter, confidence = registry.detect(table)
        return adapter, confidence, None
    except NoAdapterMatched as exc:
        return None, 0.0, str(exc)


# --------------------------------------------------------------------------
# Frame projections: flatten the canonical model for CSV export.
# --------------------------------------------------------------------------

_ACCOUNT_COLUMNS = [
    "account_id", "source_ref", "source_row", "name_raw", "name", "domain", "hq_country", "hq_city",
    "region", "industry", "sub_industry", "revenue_usd", "revenue_band",
    "employees", "employee_band", "it_budget_usd", "employee_growth_1y",
    "ownership_type", "parent_name", "is_acquired", "is_defunct",
    "certified_at", "source_status",
]

_CONTACT_COLUMNS = [
    "contact_id", "account_id", "source_ref", "source_row", "full_name", "first_name", "last_name",
    "title_raw", "canonical_role", "linkedin_url", "email", "phone", "mobile",
    "account_name_raw", "account_domain", "city", "state", "country",
    "tenure_role_months", "tenure_company_months", "match_status",
    "lead_status", "captured_at",
]


def accounts_to_frame(result: NormalizeResult) -> pd.DataFrame:
    """Flatten accounts for CSV export. List fields become ';'-joined."""
    rows = []
    for account in result.accounts:
        record = account.model_dump()
        record["sic_codes"] = ";".join(record.get("sic_codes") or [])
        record["naics_codes"] = ";".join(record.get("naics_codes") or [])
        record["subsidiary_domains"] = ";".join(record.get("subsidiary_domains") or [])
        record["signal_count"] = len(record.get("signals") or [])
        record.pop("signals", None)
        record.pop("platform_ids", None)
        rows.append(record)
    frame = pd.DataFrame(rows)
    ordered = [c for c in _ACCOUNT_COLUMNS if c in frame.columns]
    rest = [c for c in frame.columns if c not in ordered]
    return frame[ordered + rest] if len(frame) else frame


def contacts_to_frame(result: NormalizeResult) -> pd.DataFrame:
    """Flatten contacts for CSV export."""
    rows = []
    for contact in result.contacts:
        record = contact.model_dump()
        record["direct_dials"] = ";".join(record.get("direct_dials") or [])
        rows.append(record)
    frame = pd.DataFrame(rows)
    ordered = [c for c in _CONTACT_COLUMNS if c in frame.columns]
    rest = [c for c in frame.columns if c not in ordered]
    return frame[ordered + rest] if len(frame) else frame
