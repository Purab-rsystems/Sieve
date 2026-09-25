"""ZoomInfo enrichment export.

The deepest firmographic source available, at 67 columns. It alone supplies SIC
and NAICS codes, estimated IT department budget, employee growth rates, and the
defunct and acquired status flags, which is why modules M3 and M4 both lean on
it.

Money arrives in thousands of USD, so `_thousands_usd` multiplies by 1,000
before storing. A revenue of 1261389 in the file becomes 1,261,389,000 here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...model.core import Account, CanonicalBatch, Contact
from ...normalize.values import (
    clean_str,
    normalize_company_name,
    normalize_domain,
    normalize_email,
    normalize_person_name,
    normalize_phone,
    split_multi,
    to_bool,
    to_float,
    to_int,
)
from ..readers.base import RawTable
from .base import ColumnResolver, RowReader, SourceAdapter

__all__ = ["ZoomInfoEnrichAdapter"]

#: Certification dates appear as 'September 24, 2024' and occasionally in
#: other layouts, so each is tried in turn.
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y")


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = clean_str(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _thousands_usd(value: Any) -> float | None:
    """ZoomInfo reports money in thousands, so scale it to plain USD."""
    number = to_float(value)
    return None if number is None else number * 1_000.0


class ZoomInfoEnrichAdapter(SourceAdapter):
    source_id = "zoominfo_enrich"
    signature_columns = (
        "ZoomInfo Company ID", "Revenue (in 000s USD)", "Employee Range",
        "Est. IT Department Budget (in 000s USD)", "SIC Code 1", "NAICS Code 1",
        "Primary Industry", "Certified Active Company",
        "Company ID (Ultimate Parent)", "Match status",
    )
    required_columns = ("ZoomInfo Company ID",)

    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        batch = self._new_batch(table)
        resolver = ColumnResolver(table.columns)
        seen_accounts: dict[str, Account] = {}

        for index, row in enumerate(table.rows, start=1):
            read = self._reader(row, resolver, batch, index)

            contact = self._contact(read, index)
            if contact is not None:
                batch.contacts.append(contact)
                batch.record_provenance(contact.source_ref, read.take())

            account = self._account(read, index, seen_accounts)
            if account is not None:
                batch.accounts.append(account)
                batch.record_provenance(account.source_ref, read.take())

        return batch

    @staticmethod
    def _contact(read: RowReader, index: int) -> Contact | None:
        """Rows on a company-only match carry no person, so return None."""
        first = read.get("First Name")
        last = read.get("Last Name")
        if first is None and last is None:
            return None

        full_name = clean_str(" ".join(p for p in (first, last) if p))
        return Contact(
            source_row=index,
            source_ref=f"contact:{index}",
            first_name=first,
            last_name=last,
            full_name=full_name,
            name_key=normalize_person_name(full_name),
            title_raw=read.get("Title"),
            email=read.get("Email", parse=normalize_email),
            phone=read.get("Company HQ Phone", parse=normalize_phone),
            account_name_raw=read.get("Company Name", "Company"),
            account_domain=read.get("Website.1", "Website", parse=normalize_domain),
            city=read.get("Company City"),
            state=read.get("Company State"),
            country=read.get("Company Country"),
            match_status=read.get("Match status"),
        )

    @staticmethod
    def _account(
        read: RowReader,
        index: int,
        seen: dict[str, Account],
    ) -> Account | None:
        company_raw = read.get("Company Name", "Company")
        domain = read.get("Website.1", "Website", parse=normalize_domain)
        zoominfo_id = read.get("ZoomInfo Company ID")

        key = (
            domain
            or zoominfo_id
            or normalize_company_name(company_raw)
            or f"row{index}"
        )
        if key in seen:
            return None

        platform_ids = {}
        if zoominfo_id:
            platform_ids["zoominfo"] = zoominfo_id
        parent_id = read.get("Company ID (Ultimate Parent)")
        if parent_id:
            platform_ids["zoominfo_parent"] = parent_id

        account = Account(
            source_row=index,
            source_ref=f"account:{index}",
            name_raw=company_raw,
            name=normalize_company_name(company_raw),
            domain=domain,
            hq_city=read.get("Company City"),
            hq_state=read.get("Company State"),
            hq_country=read.get("Company Country"),
            industry=read.get("Primary Industry"),
            sub_industry=read.get("Primary Sub-Industry"),
            sic_codes=split_multi(read.raw("SIC Codes")),
            naics_codes=split_multi(read.raw("NAICS Codes")),
            revenue_usd=read.get("Revenue (in 000s USD)", parse=_thousands_usd),
            revenue_band=read.get("Revenue Range (in USD)"),
            employees=read.get("Employees", parse=to_int),
            employee_band=read.get("Employee Range"),
            it_budget_usd=read.get(
                "Est. IT Department Budget (in 000s USD)", parse=_thousands_usd
            ),
            employee_growth_1y=read.get(
                "Past 1 Year Employee Growth Rate", parse=to_float
            ),
            employee_growth_2y=read.get(
                "Past 2 Year Employee Growth Rate", parse=to_float
            ),
            ownership_type=read.get("Ownership Type"),
            business_model=read.get("Business Model"),
            founded_year=read.get("Founded Year", parse=to_int),
            parent_name=read.get("Entity Name (Ultimate Parent)"),
            parent_relationship=read.get("Relationship (Immediate Parent)"),
            is_acquired=read.get("Company Is Acquired", parse=to_bool),
            is_defunct=read.get("Defunct Company", parse=to_bool),
            certified_at=read.get("Certification Date", parse=_date),
            linkedin_company_url=read.get("LinkedIn Company Profile URL"),
            platform_ids=platform_ids,
        )
        seen[key] = account
        return account
