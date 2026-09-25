"""Salesforce synchronization staging (PRD module M8, step iii).

Builds the two upsert payloads the Bulk API takes:

    Account   upserted on sv_account_id, carrying sv_icp_score,
              sv_target_tier and the technographic tags
    Contact   upserted on sv_contact_id, associated with the parent account,
              carrying sv_committee_role and the direct dials

Records blocked by M4 never reach the payload. DOMAIN_ANOMALY at Critical
severity blocks automatic write-back by name in the PRD, and anything else
marked DISQUALIFIED is held back with it.

Payloads are written as CSV in Bulk API 2.0 ingest form plus a JSON manifest.
Sending them needs credentials and a sandbox; everything up to the HTTP call
is here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..normalize.values import clean_str, normalize_phone, to_float, to_int

__all__ = [
    "SalesforcePayload", "build_payload", "load_approval",
    "ACCOUNT_FIELDS", "CONTACT_FIELDS",
]

#: Salesforce Account fields, mapped from roster columns.
ACCOUNT_FIELDS: dict[str, str] = {
    "Sieve_Account_Id__c": "sv_account_id",
    "Name": "Company",
    "Website": "Website URL",
    "Industry": "Industry",
    "NumberOfEmployees": "Number of Employees",
    "AnnualRevenue": "Revenue ($ M)",
    "Sieve_ICP_Score__c": "sv_icp_score",
    "Sieve_Target_Tier__c": "sv_target_tier",
    "Sieve_Score_Breakdown__c": "sv_score_breakdown",
}

#: Salesforce Contact fields, mapped from roster columns.
CONTACT_FIELDS: dict[str, str] = {
    "Sieve_Contact_Id__c": "sv_contact_id",
    "Sieve_Account_Id__c": "sv_account_id",
    "FirstName": "First Name",
    "LastName": "Last Name",
    "Title": "Title",
    "Sieve_Canonical_Title__c": "sv_canonical_title",
    "Sieve_Seniority__c": "sv_seniority",
    "Sieve_Committee_Role__c": "sv_committee_role",
    "Email": "Email",
    "Phone": "Phone",
    "MobilePhone": "Mobile",
    "MailingCity": "City",
    "MailingCountry": "Country",
    "Sieve_Flags__c": "sv_flags",
    "Sieve_Confidence__c": "sv_confidence",
    "Sieve_Validation_Status__c": "sv_validation_status",
    "Sieve_Dial_Priority__c": "sv_dial_priority",
}

#: Roster columns tried, in order, for a contact's direct dial.
DIRECT_DIAL_COLUMNS = ("Direct Dial 1", "Direct Dial", "Direct Phone")


@dataclass
class SalesforcePayload:
    """Two upsert frames plus the manifest describing them."""

    accounts: pd.DataFrame
    contacts: pd.DataFrame
    held_back: pd.DataFrame
    manifest: dict[str, Any] = field(default_factory=dict)

    def write(self, directory: Path | str) -> dict[str, Path]:
        """Write both payloads, the held-back records, and the manifest."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths = {
            "accounts": directory / "sf_account_upsert.csv",
            "contacts": directory / "sf_contact_upsert.csv",
            "held_back": directory / "sf_held_back.csv",
            "manifest": directory / "sf_manifest.json",
        }
        self.accounts.to_csv(paths["accounts"], index=False)
        self.contacts.to_csv(paths["contacts"], index=False)
        self.held_back.to_csv(paths["held_back"], index=False)
        paths["manifest"].write_text(
            json.dumps(self.manifest, indent=2, default=str)
        )
        return paths


def _pick(row: pd.Series, column: str | None) -> Any:
    if column is None or column not in row.index:
        return None
    value = row[column]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _direct_dial(row: pd.Series) -> str | None:
    for column in DIRECT_DIAL_COLUMNS:
        number = normalize_phone(_pick(row, column))
        if number:
            return number
    return None


def load_approval(path: Path | str) -> dict[str, Any]:
    """Read an approval file produced by the review console."""
    data = json.loads(Path(path).read_text())
    if not data.get("approved"):
        raise ValueError(f"{path} is not an approval: 'approved' is not true.")
    if "approved_contact_ids" not in data:
        raise ValueError(f"{path} carries no approved_contact_ids.")
    return data


def build_payload(
    roster: pd.DataFrame,
    *,
    approved: bool = False,
    approval: dict[str, Any] | None = None,
    tier_filter: tuple[str, ...] = ("Tier A", "Tier B"),
) -> SalesforcePayload:
    """Turn a validated roster into Bulk API upsert payloads.

    `approved` records the human sign-off from the review console. An
    unapproved payload is still built, and its manifest says so, because the
    console needs something to render before anyone can approve it.
    """
    required = {"sv_account_id", "sv_contact_id", "sv_validation_status"}
    missing = required - set(roster.columns)
    if missing:
        raise ValueError(
            f"Roster is missing {sorted(missing)}. Run `sieve run` first."
        )

    if approval is not None:
        approved = True
        allowed = set(approval.get("approved_contact_ids", []))
        rejected = ~roster["sv_contact_id"].astype(str).isin(allowed)
    else:
        rejected = pd.Series(False, index=roster.index)

    blocked = roster["sv_validation_status"] == "DISQUALIFIED"
    out_of_tier = ~roster.get(
        "sv_target_tier", pd.Series(["Tier A"] * len(roster))
    ).isin(tier_filter)
    held = blocked | out_of_tier | rejected

    eligible = roster.loc[~held]
    held_back = roster.loc[held]

    account_rows: dict[str, dict[str, Any]] = {}
    for _, row in eligible.iterrows():
        key = str(row["sv_account_id"])
        if key in account_rows:
            continue
        record = {
            field: _pick(row, column) for field, column in ACCOUNT_FIELDS.items()
        }
        record["NumberOfEmployees"] = to_int(record["NumberOfEmployees"])
        revenue = to_float(record["AnnualRevenue"])
        record["AnnualRevenue"] = None if revenue is None else revenue * 1_000_000
        record["Website"] = clean_str(
            _pick(row, "Website URL") or _pick(row, "Website")
        )
        account_rows[key] = record

    contact_rows = []
    for _, row in eligible.iterrows():
        record = {
            field: _pick(row, column) for field, column in CONTACT_FIELDS.items()
        }
        record["Phone"] = normalize_phone(record["Phone"])
        record["MobilePhone"] = normalize_phone(
            record["MobilePhone"] or _pick(row, "Mobile Phone")
        )
        record["Sieve_Direct_Dial__c"] = _direct_dial(row)
        contact_rows.append(record)

    accounts = pd.DataFrame(list(account_rows.values()))
    contacts = pd.DataFrame(contact_rows)

    # The Bulk API rejects a batch carrying the same external ID twice, so the
    # payload must hold one row per contact. Duplicates arrive legitimately:
    # the campaign file lists the same person under 'Criteo' and
    # 'Criteo (EU HQ)', and the UUID5 scheme correctly collapses them onto one
    # id. Keeping the first occurrence is what the upsert would have done
    # anyway, minus the error.
    duplicate_contacts = 0
    if len(contacts):
        before = len(contacts)
        contacts = contacts.drop_duplicates(
            subset="Sieve_Contact_Id__c", keep="first"
        ).reset_index(drop=True)
        duplicate_contacts = before - len(contacts)

    manifest = {
        "approved": approved,
        "operation": "upsert",
        "account_external_id": "Sieve_Account_Id__c",
        "contact_external_id": "Sieve_Contact_Id__c",
        "tier_filter": list(tier_filter),
        "accounts": int(len(accounts)),
        "contacts": int(len(contacts)),
        "duplicate_contacts_collapsed": int(duplicate_contacts),
        "held_back": int(len(held_back)),
        # Reasons are reported without overlap, in the order they apply, so
        # the three figures sum to held_back. A Tier C row absent from the
        # approval list is out of tier, rather than something a rep dropped.
        "held_back_reasons": {
            "disqualified": int(blocked.sum()),
            "out_of_tier": int((out_of_tier & ~blocked).sum()),
            "dropped_in_review": int(
                (rejected & ~blocked & ~out_of_tier).sum()
            ),
        },
        "committee_seats": (
            contacts["Sieve_Committee_Role__c"].value_counts().to_dict()
            if "Sieve_Committee_Role__c" in contacts.columns else {}
        ),
        "ready_to_send": approved,
    }

    return SalesforcePayload(
        accounts=accounts, contacts=contacts, held_back=held_back,
        manifest=manifest,
    )
