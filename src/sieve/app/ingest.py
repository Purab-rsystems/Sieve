"""Turn uploaded files into the one contact table Sieve's pipeline reads.

Every uploaded CSV, and every sheet of every uploaded Excel workbook, is read
and sorted into one of two kinds:

    contacts   has a job-title column (Title / Job Title / title). These are
               stacked into one table (NA, EU and APAC sheets, several
               exports, ...).
    research   no title column, but a company or website column and research
               columns (Category, signals, scale, description), like the
               campaign workbook's Status sheet. Its research columns are
               joined onto the contacts by website domain, falling back to
               company name.

Anything else is listed as skipped, with the reason. Values are read as text
and "NA" stays "NA" (North America), never a missing value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..normalize.values import normalize_company_name, normalize_domain

TITLE_COLUMNS = ("Title", "Job Title", "title")
COMPANY_COLUMNS = ("Company", "Company Name", "companyName")
WEBSITE_COLUMNS = ("Website URL", "Website", "Company Website", "Domain")
RESEARCH_HINTS = ("Signal", "Scale", "Description", "Category", "Vertical")


@dataclass
class Ingested:
    frame: pd.DataFrame
    contact_tables: list[str] = field(default_factory=list)
    research_tables: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    research_matched: int = 0

    def summary(self) -> dict:
        return {"contacts": int(len(self.frame)), "contact_tables": self.contact_tables,
                "research_tables": self.research_tables, "skipped": self.skipped,
                "research_matched": self.research_matched}


def _read(path: Path) -> list[tuple[str, pd.DataFrame]]:
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False, na_values=[""])
        return [(f"{path.name} / {name}", df) for name, df in sheets.items()]
    if path.suffix.lower() in (".csv", ".txt"):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return [(path.name, pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""],
                                                encoding=enc, low_memory=False))]
            except UnicodeDecodeError:
                continue
    raise ValueError(f"{path.name}: not a CSV or Excel file")


def _first(df: pd.DataFrame, names) -> str | None:
    return next((c for c in names if c in df.columns), None)


def _key(df: pd.DataFrame) -> pd.Series:
    web = _first(df, WEBSITE_COLUMNS)
    comp = _first(df, COMPANY_COLUMNS)
    by_web = df[web].map(normalize_domain) if web else pd.Series([None] * len(df), index=df.index)
    by_comp = df[comp].map(normalize_company_name) if comp else pd.Series([None] * len(df), index=df.index)
    return by_web.where(by_web.notna() & (by_web != ""), by_comp)


def ingest(paths: list[Path]) -> Ingested:
    contacts, research, skipped = [], [], []
    for p in paths:
        for name, df in _read(Path(p)):
            df = df.dropna(how="all")
            df.columns = [str(c).strip() if not str(c).startswith("Estimated Scale") else str(c) for c in df.columns]
            if df.empty:
                skipped.append(f"{name}: empty")
            elif _first(df, TITLE_COLUMNS):
                contacts.append((name, df))
            elif (_first(df, COMPANY_COLUMNS) or _first(df, WEBSITE_COLUMNS)) and \
                    any(h.lower() in c.lower() for c in df.columns for h in RESEARCH_HINTS):
                research.append((name, df))
            else:
                skipped.append(f"{name}: no job-title column and no research columns")
    if not contacts:
        raise ValueError("No contact table found: a contact file needs a Title (or Job Title) column.")

    frame = pd.concat([df for _, df in contacts], ignore_index=True)
    out = Ingested(frame=frame, contact_tables=[n for n, _ in contacts],
                   research_tables=[n for n, _ in research], skipped=skipped)
    if research:
        keys = _key(frame)
        matched = pd.Series(False, index=frame.index)
        for _, rdf in research:
            cols = [c for c in rdf.columns
                    if any(h.lower() in c.lower() for h in RESEARCH_HINTS) and c not in frame.columns]
            if not cols:
                continue
            rdf = rdf.assign(_key=_key(rdf)).dropna(subset=["_key"]).drop_duplicates("_key")
            lookup = rdf.set_index("_key")[cols].to_dict("index")
            for c in cols:
                frame[c] = [(lookup.get(k) or {}).get(c) for k in keys]
            matched |= keys.isin(lookup.keys())
        out.research_matched = int(matched.sum())
        out.frame = frame
    return out
