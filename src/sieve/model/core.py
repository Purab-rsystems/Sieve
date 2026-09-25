"""The canonical model.

Every field here appears in at least one of the real source files. Nothing is
speculative. Records are Pydantic models so that construction validates, and
the batch that holds them also carries provenance and any issues raised while
reading, which is what lets a malformed row travel through the pipeline
instead of vanishing.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Provenance",
    "Issue",
    "IssueLevel",
    "SignalKind",
    "Signal",
    "Account",
    "Contact",
    "CanonicalBatch",
]


class _Frozen(BaseModel):
    """Base for facts that must never change once recorded.

    Provenance and Issue describe what a file said and when. Rewriting either
    of them after the fact would make the audit trail a lie, so they are
    immutable and hashable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class _Record(BaseModel):
    """Base for entities that later modules enrich.

    Account and Contact travel through the pipeline picking up fields. M2
    assigns their IDs, M1 fills in canonical_role, M4 attaches flags. So they
    stay mutable, with validate_assignment keeping every later write type-safe.

    Both bases forbid extra fields, which turns a typo in an adapter into an
    immediate error rather than a silently dropped value.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# provenance and issues
# --------------------------------------------------------------------------

class Provenance(_Frozen):
    """Where a single value came from."""

    source_id: str
    file: str
    row: int
    field: str
    captured_at: datetime | None = None

    def describe(self) -> str:
        return f"{self.source_id}:{self.file}#{self.row}.{self.field}"


class IssueLevel(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class Issue(_Frozen):
    """A data problem worth surfacing without stopping the run."""

    level: IssueLevel
    code: str
    detail: str
    source_id: str | None = None
    file: str | None = None
    row: int | None = None
    field: str | None = None


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

class SignalKind(str, Enum):
    LEGACY_STACK = "legacy_stack"
    MODERNIZATION_INTENT = "modernization_intent"
    CLOUD_MIGRATION = "cloud_migration"
    LICENSE_COST_PRESSURE = "license_cost_pressure"
    ESTATE_SIZE = "estate_size"
    REGULATED_ENVIRONMENT = "regulated_environment"
    HIRING = "hiring"
    SCALE = "scale"
    PITCH_ANGLE = "pitch_angle"
    OTHER = "other"


class Signal(_Record):
    kind: SignalKind
    text: str
    source_url: str | None = None
    observed_at: datetime | None = None
    confidence: float | None = None


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

class Account(_Record):
    """A company. `account_id` stays None until M2 assigns one."""

    account_id: str | None = None
    #: 1-based index of the source row this record came from.
    source_row: int | None = None
    #: Provenance key, e.g. 'account:14'. Unique within one batch.
    source_ref: str | None = None
    name_raw: str | None = None
    name: str | None = None                    # normalized matching key
    domain: str | None = None

    hq_country: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    region: str | None = None

    industry: str | None = None
    sub_industry: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    naics_codes: list[str] = Field(default_factory=list)

    revenue_usd: float | None = None
    revenue_band: str | None = None
    employees: int | None = None
    employee_band: str | None = None
    it_budget_usd: float | None = None
    employee_growth_1y: float | None = None
    employee_growth_2y: float | None = None

    ownership_type: str | None = None
    business_model: str | None = None
    founded_year: int | None = None

    parent_name: str | None = None
    parent_account_id: str | None = None
    parent_relationship: str | None = None
    subsidiary_domains: list[str] = Field(default_factory=list)

    is_acquired: bool | None = None
    is_defunct: bool | None = None
    certified_at: datetime | None = None

    linkedin_company_url: str | None = None
    platform_ids: dict[str, str] = Field(default_factory=dict)

    signals: list[Signal] = Field(default_factory=list)
    source_status: str | None = None           # campaign_status free text

    def match_keys(self) -> list[tuple[str, str]]:
        """Ordered candidate keys for resolution, strongest first."""
        keys: list[tuple[str, str]] = []
        if self.domain:
            keys.append(("domain", self.domain))
        for platform, value in sorted(self.platform_ids.items()):
            keys.append((f"platform:{platform}", value))
        if self.name:
            suffix = f"|{self.hq_country.lower()}" if self.hq_country else ""
            keys.append(("name", f"{self.name}{suffix}"))
        return keys


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------

class Contact(_Record):
    """A person. `title_raw` is preserved verbatim, forever."""

    contact_id: str | None = None
    account_id: str | None = None
    #: 1-based index of the source row this record came from. Two records
    #: sharing a source_row came from the same line of the same file, which
    #: is how the Apollo input and output halves get paired.
    source_row: int | None = None
    #: Provenance key, e.g. 'contact:14:apollo'. Unique within one batch.
    source_ref: str | None = None
    account_name_raw: str | None = None
    account_domain: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    name_key: str | None = None                # normalized matching key

    title_raw: str | None = None
    canonical_role: str | None = None          # assigned by M1
    role_tier: int | None = None
    seniority: str | None = None
    function: str | None = None

    linkedin_url: str | None = None
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    direct_dials: list[str] = Field(default_factory=list)

    city: str | None = None
    state: str | None = None
    country: str | None = None

    tenure_role_months: int | None = None
    tenure_company_months: int | None = None
    bio: str | None = None
    role_description: str | None = None

    match_status: str | None = None            # 'Matched', 'Company Match', ...
    lead_status: str | None = None
    captured_at: datetime | None = None

    def match_keys(self) -> list[tuple[str, str]]:
        """Ordered candidate keys for resolution, strongest first."""
        keys: list[tuple[str, str]] = []
        if self.linkedin_url:
            keys.append(("linkedin", self.linkedin_url))
        if self.email:
            keys.append(("email", self.email))
        if self.name_key and (self.account_domain or self.account_name_raw):
            anchor = self.account_domain or self.account_name_raw
            keys.append(("name_account", f"{self.name_key}|{anchor}"))
        return keys


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------

class CanonicalBatch(BaseModel):
    """Everything one adapter produced from one table."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    file: str
    sheet: str | None = None
    accounts: list[Account] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    provenance: dict[str, list[Provenance]] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)

    def record_provenance(self, ref: str, entries: list[Provenance]) -> None:
        self.provenance.setdefault(ref, []).extend(entries)

    def add_issue(self, **kwargs: Any) -> None:
        self.issues.append(Issue(**kwargs))

    def extend(self, other: "CanonicalBatch") -> None:
        self.accounts.extend(other.accounts)
        self.contacts.extend(other.contacts)
        for ref, entries in other.provenance.items():
            self.provenance.setdefault(ref, []).extend(entries)
        self.issues.extend(other.issues)

    def summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "file": self.file,
            "sheet": self.sheet,
            "accounts": len(self.accounts),
            "contacts": len(self.contacts),
            "issues": len(self.issues),
            "errors": sum(1 for i in self.issues if i.level is IssueLevel.ERROR),
        }
