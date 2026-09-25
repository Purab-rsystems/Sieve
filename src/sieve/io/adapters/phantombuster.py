"""PhantomBuster Sales Navigator export.

Raw scrape output, one person per row. It carries no email at all, and its
value lies in three things nothing else provides: the LinkedIn URL, how long
the person has been in the role, and their free-text bio.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...model.core import Account, CanonicalBatch, Contact
from ...normalize.values import (
    clean_str,
    normalize_company_name,
    normalize_domain,
    normalize_linkedin_url,
    normalize_person_name,
    parse_tenure_months,
)
from ..readers.base import RawTable
from .base import ColumnResolver, SourceAdapter

__all__ = ["PhantomBusterAdapter"]


def _timestamp(value: Any) -> datetime | None:
    """Parse the ISO timestamp PhantomBuster stamps on every scraped row."""
    text = clean_str(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class PhantomBusterAdapter(SourceAdapter):
    source_id = "phantombuster"
    signature_columns = (
        "profileUrl", "fullName", "companyName", "title", "vmid",
        "durationInRole", "durationInCompany", "defaultProfileUrl", "query",
    )
    required_columns = ("defaultProfileUrl",)

    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        batch = self._new_batch(table)
        resolver = ColumnResolver(table.columns)

        # One company appears on many rows, so accounts are deduplicated here
        # by domain or normalized name. Full resolution is module M2's job.
        seen_accounts: dict[str, Account] = {}

        for index, row in enumerate(table.rows, start=1):
            read = self._reader(row, resolver, batch, index)

            company_raw = read.get("companyName")
            domain = read.get("regularCompanyUrl", "companyUrl", parse=normalize_domain)

            contact = Contact(
                source_row=index,
                source_ref=f"contact:{index}",
                first_name=read.get("firstName"),
                last_name=read.get("lastName"),
                full_name=read.get("fullName", "name"),
                name_key=normalize_person_name(read.raw("fullName")),
                title_raw=read.get("title"),
                linkedin_url=read.get(
                    "defaultProfileUrl", "linkedInProfileUrl", "profileUrl",
                    parse=normalize_linkedin_url,
                ),
                account_name_raw=company_raw,
                account_domain=domain,
                country=read.get("location", "companyLocation"),
                tenure_role_months=read.get("durationInRole", parse=parse_tenure_months),
                tenure_company_months=read.get(
                    "durationInCompany", parse=parse_tenure_months
                ),
                bio=read.get("summary"),
                role_description=read.get("titleDescription"),
                captured_at=read.get("timestamp", parse=_timestamp),
            )
            batch.contacts.append(contact)
            batch.record_provenance(contact.source_ref, read.take())

            if company_raw is None and domain is None:
                continue

            key = domain or normalize_company_name(company_raw) or f"row{index}"
            if key in seen_accounts:
                continue

            account = Account(
                source_row=index,
                source_ref=f"account:{index}",
                name_raw=company_raw,
                name=normalize_company_name(company_raw),
                domain=domain,
                industry=read.get("industry"),
                hq_city=read.get("companyLocation"),
            )
            seen_accounts[key] = account
            batch.accounts.append(account)
            batch.record_provenance(account.source_ref, read.take())

        return batch
