"""Format-DB contact sheets.

This is the delivery schema, and in the modernization workbook it appears three
times with headers that disagree between sheets:

    field            NA sheet            EU sheet            APAC sheet
    ---------------  ------------------  ------------------  ------------------
    mobile number    'Mobile '           'Mobile Phone'      'Mobile '
    second phone     'Phone-2'           'Phone 2'           'Phone-2'
    LinkedIn         'Linkedin Profile'  'LinkedIn Profile'  'Linkedin Profile'
    website          'Website URL'       'Website'           'Website URL'

Every `read.get` call below that touches one of those four fields lists all the
spellings. Anything the list misses gets caught by the resolver's fuzzy pass,
so a fourth sheet with a fifth spelling would still work.

Money arrives in millions of USD in this schema, unlike ZoomInfo's thousands.
"""

from __future__ import annotations

from typing import Any

from ...model.core import Account, CanonicalBatch, Contact
from ...normalize.values import (
    clean_str,
    normalize_company_name,
    normalize_domain,
    normalize_email,
    normalize_linkedin_url,
    normalize_person_name,
    normalize_phone,
    to_float,
    to_int,
)
from ..readers.base import RawTable
from .base import ColumnResolver, RowReader, SourceAdapter

__all__ = ["FormatDbAdapter"]


def _millions_usd(value: Any) -> float | None:
    """Format-DB reports money in millions, so scale it to plain USD."""
    number = to_float(value)
    return None if number is None else number * 1_000_000.0


class FormatDbAdapter(SourceAdapter):
    source_id = "formatdb"
    signature_columns = (
        "Lead Owner", "Lead Status", "First Name", "Last Name", "Title",
        "Email", "Company", "Industry", "Sub Industry", "Lead Source",
        "List Name", "Lead Tag", "Revenue ($ M)", "IT Budget ($ M)",
        "DNC Status", "Account Status",
    )
    required_columns = ("Lead Status", "DNC Status")

    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        batch = self._new_batch(table)
        resolver = ColumnResolver(table.columns)
        seen_accounts: dict[str, Account] = {}

        for index, row in enumerate(table.rows, start=1):
            read = self._reader(row, resolver, batch, index)

            contact = self._contact(read, index)
            batch.contacts.append(contact)
            batch.record_provenance(contact.source_ref, read.take())

            # The sheet name is the region: NA, EU or APAC.
            account = self._account(read, index, table.sheet, seen_accounts)
            if account is not None:
                batch.accounts.append(account)
                batch.record_provenance(account.source_ref, read.take())

        return batch

    @staticmethod
    def _contact(read: RowReader, index: int) -> Contact:
        first = read.get("First Name")
        last = read.get("Last Name")
        full_name = clean_str(" ".join(p for p in (first, last) if p))

        direct_dials = [
            number for number in (
                normalize_phone(read.raw(column))
                for column in ("Direct Dial 1", "Direct Dial 2", "Direct Dial 3")
            ) if number
        ]

        return Contact(
            source_row=index,
            source_ref=f"contact:{index}",
            first_name=first,
            last_name=last,
            full_name=full_name,
            name_key=normalize_person_name(full_name),
            title_raw=read.get("Title"),
            email=read.get("Email", parse=normalize_email),
            linkedin_url=read.get(
                "Linkedin Profile", "LinkedIn Profile",      # NA/APAC, then EU
                parse=normalize_linkedin_url,
            ),
            phone=read.get(
                "Phone", "Phone-2", "Phone 2",               # NA/APAC, then EU
                parse=normalize_phone,
            ),
            mobile=read.get(
                "Mobile", "Mobile Phone",                    # NA/APAC, then EU
                parse=normalize_phone,
            ),
            direct_dials=direct_dials,
            account_name_raw=read.get("Company"),
            account_domain=read.get(
                "Website URL", "Website",                    # NA/APAC, then EU
                parse=normalize_domain,
            ),
            city=read.get("City"),
            state=read.get("State/Province"),
            country=read.get("Country"),
            lead_status=read.get("Lead Status"),
        )

    @staticmethod
    def _account(
        read: RowReader,
        index: int,
        sheet: str | None,
        seen: dict[str, Account],
    ) -> Account | None:
        company_raw = read.get("Company")
        domain = read.get("Website URL", "Website", parse=normalize_domain)

        key = domain or normalize_company_name(company_raw) or f"row{index}"
        if key in seen:
            return None

        account = Account(
            source_row=index,
            source_ref=f"account:{index}",
            name_raw=company_raw,
            name=normalize_company_name(company_raw),
            domain=domain,
            hq_country=read.get("HQ Country"),
            region=sheet,
            industry=read.get("Industry"),
            sub_industry=read.get("Sub Industry"),
            employees=read.get("Number of Employees", parse=to_int),
            revenue_usd=read.get("Revenue ($ M)", parse=_millions_usd),
            it_budget_usd=read.get("IT Budget ($ M)", parse=_millions_usd),
        )
        seen[key] = account
        return account
