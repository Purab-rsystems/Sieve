"""Apollo enrichment export.

This file is structurally unusual, and the adapter reflects that.

Each row holds TWO descriptions of the same person, side by side:

    columns 1-6    what PhantomBuster reported, sent TO Apollo as input
                   defaultProfileUrl, fullName, firstName, lastName,
                   companyName, title

    column 7       Result: 'Matched' or 'N/A'

    columns 8+     what Apollo answered
                   First Name, Last Name, Title, Person Linkedin Url,
                   Email, Company Name, and the firmographics

Watch the casing. `title` is the input and `Title` is Apollo's answer. Same for
`firstName` against `First Name`. Every column reference below is marked with
which half it belongs to, because a single letter of case is the only thing
separating them.

The adapter emits TWO contact records per row, one per half. In the sample file
181 of 532 matched rows carry a different title on each side, and that
disagreement is precisely the signal module M4 exists to detect. Merging the
halves at ingestion would destroy it, so this behaviour has a regression test
on it and should survive any refactor.

The two records share a `source_row`, which is how `pair_by_source_row` finds
them again. Pairing on LinkedIn URL instead loses 61 rows, because four matched
records carry no `Person Linkedin Url` at all.
"""

from __future__ import annotations

from ...model.core import Account, CanonicalBatch, Contact
from ...normalize.values import (
    clean_str,
    normalize_company_name,
    normalize_domain,
    normalize_email,
    normalize_linkedin_url,
    normalize_person_name,
    normalize_phone,
    split_multi,
    to_float,
    to_int,
)
from ..readers.base import RawTable
from .base import ColumnResolver, RowReader, SourceAdapter

__all__ = ["ApolloEnrichAdapter"]


class ApolloEnrichAdapter(SourceAdapter):
    source_id = "apollo_enrich"
    signature_columns = (
        "defaultProfileUrl", "fullName", "companyName", "title", "Result",
        "Person Linkedin Url", "Email", "Company Name", "Annual Revenue",
        "Ultimate Parent Company Name",
    )
    required_columns = ("Result",)

    #: Setting this False emits only Apollo's answer and drops the input half,
    #: which also drops the ability to detect title disagreement.
    emit_input_side = True

    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        batch = self._new_batch(table)
        resolver = ColumnResolver(table.columns)
        seen_accounts: dict[str, Account] = {}

        for index, row in enumerate(table.rows, start=1):
            read = self._reader(row, resolver, batch, index)

            if self.emit_input_side:
                contact = self._input_half(read, index)
                batch.contacts.append(contact)
                batch.record_provenance(contact.source_ref, read.take())

            contact = self._apollo_half(read, index)
            batch.contacts.append(contact)
            batch.record_provenance(contact.source_ref, read.take())

            account = self._account(read, index, seen_accounts)
            if account is not None:
                batch.accounts.append(account)
                batch.record_provenance(account.source_ref, read.take())

        return batch

    # -- the input half: lowercase columns, what PhantomBuster reported ------

    @staticmethod
    def _input_half(read: RowReader, index: int) -> Contact:
        first = read.get("firstName")               # input
        last = read.get("lastName")                 # input
        return Contact(
            source_row=index,
            source_ref=f"contact:{index}:input",
            first_name=first,
            last_name=last,
            full_name=read.get("fullName"),          # input
            name_key=normalize_person_name(read.raw("fullName")),
            title_raw=read.get("title"),             # input, NOT 'Title'
            linkedin_url=read.get(
                "defaultProfileUrl", parse=normalize_linkedin_url  # input
            ),
            account_name_raw=read.get("companyName"),  # input
            match_status="input",
        )

    # -- Apollo's answer: capitalised columns --------------------------------

    @staticmethod
    def _apollo_half(read: RowReader, index: int) -> Contact:
        first = read.get("First Name")              # Apollo
        last = read.get("Last Name")                # Apollo
        full_name = clean_str(" ".join(p for p in (first, last) if p))
        return Contact(
            source_row=index,
            source_ref=f"contact:{index}:apollo",
            first_name=first,
            last_name=last,
            full_name=full_name,
            name_key=normalize_person_name(full_name),
            title_raw=read.get("Title"),             # Apollo, NOT 'title'
            linkedin_url=read.get(
                "Person Linkedin Url", parse=normalize_linkedin_url  # Apollo
            ),
            email=read.get("Email", parse=normalize_email),          # Apollo
            phone=read.get("Company Phone", parse=normalize_phone),  # Apollo
            account_name_raw=read.get("Company Name"),               # Apollo
            account_domain=read.get("Website", parse=normalize_domain),
            city=read.get("City"),
            state=read.get("State"),
            country=read.get("Country"),
            match_status=read.get("Result"),
        )

    # -- the account, built from Apollo's firmographic columns ---------------

    @staticmethod
    def _account(
        read: RowReader,
        index: int,
        seen: dict[str, Account],
    ) -> Account | None:
        company_raw = read.get("Company Name")
        domain = read.get("Website", parse=normalize_domain)
        if company_raw is None and domain is None:
            return None

        key = domain or normalize_company_name(company_raw) or f"row{index}"
        if key in seen:
            return None

        subsidiary_domains = [
            resolved for resolved in (
                normalize_domain(part)
                for part in split_multi(read.raw("Suborganization Domains"))
            ) if resolved
        ]

        account = Account(
            source_row=index,
            source_ref=f"account:{index}",
            name_raw=company_raw,
            name=normalize_company_name(company_raw),
            domain=domain,
            industry=read.get("Industry"),
            employees=read.get("# Employees", parse=to_int),
            revenue_usd=read.get("Annual Revenue", parse=to_float),
            hq_city=read.get("Company City"),
            hq_state=read.get("Company State"),
            hq_country=read.get("Company Country"),
            founded_year=read.get("Company Founded Year", parse=to_int),
            parent_name=read.get("Ultimate Parent Company Name"),
            subsidiary_domains=subsidiary_domains,
            linkedin_company_url=read.get("Company Linkedin Url"),
        )
        seen[key] = account
        return account
