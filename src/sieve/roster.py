"""The Sieve v1.0 roster pipeline.

Four public functions, each taking a DataFrame and returning it with `sv_`
columns appended, plus `run` which chains all four.

    designate(df)   M6   sv_canonical_title, sv_seniority, sv_is_authoritative
    score(df)       M3   sv_icp_score, sv_target_tier, sv_score_breakdown
    validate(df)    M4   sv_flags, sv_confidence, sv_validation_status
    assemble(df)    M7   sv_committee_role
    run(df)         M0-M8, ending with sv_dial_priority

One deviation from the PRD's module diagram is worth stating plainly. The
diagram runs M4 before M6, but M4's SENIORITY_AMBIGUITY detector needs the
seniority mapping that M6 produces, and M7 needs both. So `run` orders them
M0, M3, M6, M4, M7, M8. Calling the stages individually in any order still
works; each one that depends on an earlier stage's columns degrades to
skipping the dependent check rather than failing.

Every function honours the additive contract: input columns are never touched,
row count and order never change, and every added column carries the `sv_`
prefix.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .committee.assembly import CommitteeCandidate, assemble_committee
from .export.audit import AuditLog, ExecutionContext
from .export.dial_queue import DialInputs, dial_priority
from .identity import account_id, contact_id
from .io.adapters.base import ColumnResolver
from .io.writers.additive import attach_columns
from .normalize.values import (
    clean_str, normalize_domain, normalize_email, normalize_phone, to_int,
)
from .scoring.icp import IcpRubric, load_rubric
from .taxonomy.designation import TitleTaxonomy, load_taxonomy
from .validation.engine import ContactUnderTest, validate_contact

__all__ = [
    "identify", "designate", "score", "validate", "assemble", "run",
    "RosterResult", "ROSTER_COLUMNS",
]

#: The 12 output columns the PRD's data contract specifies, in order.
ROSTER_COLUMNS = [
    "sv_account_id", "sv_contact_id", "sv_icp_score", "sv_target_tier",
    "sv_score_breakdown", "sv_canonical_title", "sv_seniority",
    "sv_committee_role", "sv_flags", "sv_confidence", "sv_validation_status",
    "sv_dial_priority",
]

#: Header spellings accepted for each logical input field. The resolver's fuzzy
#: pass catches anything absent from these lists.
FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["First Name", "firstName"],
    "last_name": ["Last Name", "lastName"],
    "full_name": ["Full Name", "fullName", "Name"],
    "title": ["Title", "Job Title"],
    "alternate_title": ["title"],           # Apollo's lowercase input column
    "company": ["Company", "Company Name", "companyName"],
    "website": ["Website", "Website URL", "Company Website"],
    "email": ["Email", "Email Address", "Work Email"],
    "phone": ["Phone", "Phone-2", "Phone 2", "Company Phone"],
    "mobile": ["Mobile", "Mobile Phone", "Cell"],
    "direct_dial": ["Direct Dial 1", "Direct Dial", "Direct Phone"],
    "linkedin": ["LinkedIn Profile", "Linkedin Profile", "Person Linkedin Url",
                 "defaultProfileUrl"],
    "industry": ["Industry", "Primary Industry"],
    "sub_industry": ["Sub Industry", "Primary Sub-Industry"],
    # The campaign's own vertical label. Kept separate from Sub Industry
    # because Format-DB's Industry and Sub Industry carry ZoomInfo's coarse
    # taxonomy ('Business Services', 'Software'), which says nothing about
    # AdTech fit. The Category column from the campaign sheet is what does.
    "category": ["Category", "Vertical", "Campaign Category"],
    "employees": ["Number of Employees", "Employees", "# Employees"],
    "revenue": ["Revenue ($ M)", "Revenue", "Annual Revenue"],
    "legacy_signal": ["Legacy Stack Signals", "sv_legacy_signal"],
    "modernization_signal": ["Modernization Intent Signal",
                             "sv_modernization_signal"],
    "scale_signal": ["Estimated Scale (Users / Revenue Tier)", "sv_scale_signal"],
    "platform_description": ["Platform / Product Description",
                             "sv_platform_description"],
    "tenure": ["durationInRole", "Tenure In Role"],
}


@dataclass
class RosterResult:
    """A processed frame plus the run's summary counts."""

    frame: pd.DataFrame
    summary: dict[str, Any]

    def to_csv(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.frame.to_csv(path, index=False)
        return path


class _Fields:
    """Resolves the logical fields a stage needs from whatever headers exist.

    A field can map onto SEVERAL columns at once, which happens whenever
    sheets with drifting headers get concatenated. Stack the modernization
    workbook's three region sheets and the result carries both `Website URL`
    (filled on NA and APAC rows) and `Website` (filled on EU rows) as separate
    columns, each empty for two thirds of the rows.

    So `value` coalesces: it walks every column this field resolved to and
    returns the first non-empty one for that row. Picking a single column per
    frame would silently blank the field on every row that spelled it the
    other way.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame_columns = list(frame.columns)
        self._resolver = ColumnResolver(self._frame_columns)
        self._cache: dict[str, list[str]] = {}

    def columns(self, field: str) -> list[str]:
        """Every column in the frame this logical field maps onto, in order."""
        if field not in self._cache:
            resolved: list[str] = []
            for alias in FIELD_ALIASES[field]:
                match = self._resolver.resolve([alias])
                if match is not None and match not in resolved:
                    resolved.append(match)
            self._cache[field] = resolved
        return self._cache[field]

    def column(self, field: str) -> str | None:
        """The first matching column, for callers needing a single name."""
        resolved = self.columns(field)
        return resolved[0] if resolved else None

    def value(self, row: Any, field: str) -> Any:
        """The first non-empty value across every column this field matched."""
        for column in self.columns(field):
            value = row.get(column)
            if value is None:
                continue
            if isinstance(value, float) and pd.isna(value):
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def present(self, field: str) -> bool:
        return bool(self.columns(field))


# --------------------------------------------------------------------------
# M0: deterministic identity
# --------------------------------------------------------------------------

def identify(frame: pd.DataFrame) -> pd.DataFrame:
    """Assign sv_account_id and sv_contact_id (PRD module M0)."""
    fields = _Fields(frame)
    account_ids, contact_ids, bases = [], [], []

    for position, (_, row) in enumerate(frame.iterrows()):
        account = account_id(
            fields.value(row, "website"),
            fields.value(row, "company"),
            fallback=f"row{position}",
        )
        contact = contact_id(
            account.value,
            fields.value(row, "email"),
            fields.value(row, "linkedin"),
            _assemble_name(fields, row),
            fallback=f"row{position}",
        )
        account_ids.append(account.value)
        contact_ids.append(contact.value)
        bases.append(f"{account.basis}/{contact.basis}")

    return attach_columns(frame, {
        "account_id": account_ids,
        "contact_id": contact_ids,
        "id_basis": bases,
    })


def _assemble_name(fields: _Fields, row: Any) -> str | None:
    full = clean_str(fields.value(row, "full_name"))
    if full:
        return full
    parts = [
        clean_str(fields.value(row, "first_name")),
        clean_str(fields.value(row, "last_name")),
    ]
    joined = " ".join(p for p in parts if p)
    return joined or None


# --------------------------------------------------------------------------
# M6: designation and authority
# --------------------------------------------------------------------------

def designate(
    frame: pd.DataFrame,
    taxonomy: TitleTaxonomy | None = None,
) -> pd.DataFrame:
    """Normalize titles and derive seniority and authority (PRD module M6)."""
    taxonomy = taxonomy or load_taxonomy()
    fields = _Fields(frame)
    title_column = fields.column("title")

    canonical, seniority, authority, ranks, basis = [], [], [], [], []
    for _, row in frame.iterrows():
        result = taxonomy.resolve(row.get(title_column) if title_column else None)
        canonical.append(result.canonical)
        seniority.append(result.seniority)
        authority.append(result.is_authoritative)
        ranks.append(result.seniority_rank)
        basis.append(result.match_basis)

    return attach_columns(frame, {
        "canonical_title": canonical,
        "seniority": seniority,
        "seniority_rank": ranks,
        "is_authoritative": authority,
        "title_match_basis": basis,
    })


# --------------------------------------------------------------------------
# M3: deterministic ICP scoring
# --------------------------------------------------------------------------

def score(
    frame: pd.DataFrame,
    rubric: IcpRubric | None = None,
    audit: AuditLog | None = None,
) -> pd.DataFrame:
    """Score every row against the ICP rubric (PRD module M3)."""
    rubric = rubric or load_rubric()
    fields = _Fields(frame)

    scores, tiers, breakdowns, reasons = [], [], [], []
    for _, row in frame.iterrows():
        result = rubric.score(
            industry=fields.value(row, "industry"),
            sub_industry=fields.value(row, "sub_industry"),
            category=fields.value(row, "category"),
            legacy_signal=fields.value(row, "legacy_signal"),
            platform_description=fields.value(row, "platform_description"),
            scale_signal=fields.value(row, "scale_signal"),
            modernization_signal=fields.value(row, "modernization_signal"),
            employees=fields.value(row, "employees"),
            revenue_usd=fields.value(row, "revenue"),
        )
        scores.append(result.score)
        tiers.append(result.tier)
        breakdowns.append(json.dumps(result.breakdown(), separators=(",", ":")))
        reasons.append(result.reasons())

        if audit is not None:
            audit.record(
                "icp_scored",
                account=clean_str(fields.value(row, "company")),
                score=result.score,
                tier=result.tier,
                breakdown=result.breakdown(),
            )

    return attach_columns(frame, {
        "icp_score": scores,
        "target_tier": tiers,
        "score_breakdown": breakdowns,
        "score_reasons": reasons,
    })


# --------------------------------------------------------------------------
# M4: validation and flagging
# --------------------------------------------------------------------------

def validate(
    frame: pd.DataFrame,
    taxonomy: TitleTaxonomy | None = None,
    audit: AuditLog | None = None,
) -> pd.DataFrame:
    """Flag suspect records without dropping any (PRD module M4)."""
    taxonomy = taxonomy or load_taxonomy()
    fields = _Fields(frame)
    has_designation = "sv_canonical_title" in frame.columns

    flags, confidences, statuses, details = [], [], [], []
    for _, row in frame.iterrows():
        designation = (
            taxonomy.resolve(fields.value(row, "title"))
            if not has_designation
            else taxonomy.resolve(row.get(fields.column("title")))
        )
        candidate = ContactUnderTest(
            email=fields.value(row, "email"),
            company_domain=fields.value(row, "website"),
            title=fields.value(row, "title"),
            alternate_title=(
                fields.value(row, "alternate_title")
                if fields.present("alternate_title")
                and fields.column("alternate_title") != fields.column("title")
                else None
            ),
            alternate_source="upstream enrichment",
            designation=designation,
            phone=normalize_phone(fields.value(row, "phone")),
            mobile=normalize_phone(fields.value(row, "mobile")),
            tenure_role_months=to_int(fields.value(row, "tenure")),
        )
        result = validate_contact(candidate)
        flags.append(result.codes)
        confidences.append(result.confidence)
        statuses.append(result.status)
        details.append(json.dumps(result.detail(), separators=(",", ":")))

        if audit is not None and result.flags:
            audit.record(
                "flags_assigned",
                contact=clean_str(fields.value(row, "email")),
                flags=result.codes,
                confidence=result.confidence,
                status=result.status,
            )

    return attach_columns(frame, {
        "flags": flags,
        "confidence": confidences,
        "validation_status": statuses,
        "flag_detail": details,
    })


# --------------------------------------------------------------------------
# M7: committee assembly
# --------------------------------------------------------------------------

def assemble(
    frame: pd.DataFrame,
    taxonomy: TitleTaxonomy | None = None,
    audit: AuditLog | None = None,
    tier_a_only: bool = True,
) -> pd.DataFrame:
    """Assign committee seats within qualified accounts (PRD module M7)."""
    taxonomy = taxonomy or load_taxonomy()
    required = {"sv_account_id", "sv_contact_id"}
    if not required.issubset(frame.columns):
        raise ValueError(
            "assemble needs sv_account_id and sv_contact_id. "
            "Run identify() first, or use run() which chains the stages."
        )

    fields = _Fields(frame)
    candidates_by_account: dict[str, list[CommitteeCandidate]] = {}

    for position, (_, row) in enumerate(frame.iterrows()):
        tier = row.get("sv_target_tier")
        if tier_a_only and tier != "Tier A":
            continue
        designation = taxonomy.resolve(
            row.get("sv_canonical_title") or fields.value(row, "title")
        )
        flag_codes = frozenset(
            code for code in str(row.get("sv_flags") or "").split(",") if code
        )
        candidate = CommitteeCandidate(
            contact_id=str(row["sv_contact_id"]),
            account_id=str(row["sv_account_id"]),
            full_name=_assemble_name(fields, row),
            canonical_title=designation.canonical,
            seniority=designation.seniority,
            seniority_rank=designation.seniority_rank,
            natural_seat=designation.committee_role,
            is_authoritative=designation.is_authoritative,
            confidence=float(row.get("sv_confidence") or 1.0),
            flag_codes=flag_codes,
            validation_status=str(row.get("sv_validation_status") or "VERIFIED"),
        )
        candidates_by_account.setdefault(candidate.account_id, []).append(candidate)

    committees = {
        account: assemble_committee(account, candidates)
        for account, candidates in candidates_by_account.items()
    }

    if audit is not None:
        for account, committee in committees.items():
            audit.record(
                "committee_assembled",
                account_id=account,
                coverage=committee.coverage(),
                complete=committee.is_complete,
                seats=[s.__dict__ for s in committee.seats],
            )

    roles = []
    for _, row in frame.iterrows():
        committee = committees.get(str(row.get("sv_account_id")))
        roles.append(
            committee.role_for(str(row.get("sv_contact_id")))
            if committee else "Unassigned"
        )

    return attach_columns(frame, {"committee_role": roles})


# --------------------------------------------------------------------------
# M8: dial priority and the full chain
# --------------------------------------------------------------------------

def _dial_priorities(frame: pd.DataFrame) -> list[float]:
    fields = _Fields(frame)
    priorities = []
    for _, row in frame.iterrows():
        flag_codes = str(row.get("sv_flags") or "")
        priorities.append(dial_priority(DialInputs(
            icp_score=int(row.get("sv_icp_score") or 0),
            seniority_rank=int(row.get("sv_seniority_rank") or 0),
            has_direct_dial=bool(normalize_phone(fields.value(row, "direct_dial"))),
            has_mobile=bool(normalize_phone(fields.value(row, "mobile"))),
            phone_flagged="DISPOSABLE_PHONE" in flag_codes,
        )))
    return priorities


def run(
    frame: pd.DataFrame,
    *,
    rubric: IcpRubric | None = None,
    taxonomy: TitleTaxonomy | None = None,
    audit_path: Path | str | None = None,
    run_id: str | None = None,
    tier_a_only: bool = True,
) -> RosterResult:
    """Run M0 through M8 and return the verified prospect roster.

    Stage order is M0, M3, M6, M4, M7, M8. M6 runs before M4 because
    SENIORITY_AMBIGUITY depends on the seniority mapping, and M7 depends on
    both.
    """
    rubric = rubric or load_rubric()
    taxonomy = taxonomy or load_taxonomy()
    context = ExecutionContext(
        run_id=run_id or str(uuid.uuid4()),
        rubric_version=rubric.version,
        taxonomy_version=taxonomy.version,
    )

    with AuditLog(audit_path, context) as audit:
        result = identify(frame)
        result = score(result, rubric=rubric, audit=audit)
        result = designate(result, taxonomy=taxonomy)
        result = validate(result, taxonomy=taxonomy, audit=audit)
        result = assemble(result, taxonomy=taxonomy, audit=audit,
                          tier_a_only=tier_a_only)
        result = attach_columns(result, {"dial_priority": _dial_priorities(result)})

    return RosterResult(frame=result, summary=_summarise(result, context))


def _summarise(frame: pd.DataFrame, context: ExecutionContext) -> dict[str, Any]:
    """Counts a reviewer wants before opening the file."""

    def counts(column: str) -> dict[str, int]:
        if column not in frame.columns:
            return {}
        return {str(k): int(v) for k, v in frame[column].value_counts().items()}

    flag_counts: dict[str, int] = {}
    for cell in frame.get("sv_flags", pd.Series(dtype=str)).fillna(""):
        for code in str(cell).split(","):
            if code:
                flag_counts[code] = flag_counts.get(code, 0) + 1

    return {
        "run_id": context.run_id,
        "rubric_version": context.rubric_version,
        "taxonomy_version": context.taxonomy_version,
        "rows": int(len(frame)),
        "accounts": int(frame["sv_account_id"].nunique())
        if "sv_account_id" in frame.columns else 0,
        "tiers": counts("sv_target_tier"),
        "validation_status": counts("sv_validation_status"),
        "committee_roles": counts("sv_committee_role"),
        "seniority": counts("sv_seniority"),
        "flags": dict(sorted(flag_counts.items())),
        "titles_resolved": int(frame["sv_canonical_title"].notna().sum())
        if "sv_canonical_title" in frame.columns else 0,
        "mean_confidence": round(float(frame["sv_confidence"].mean()), 3)
        if "sv_confidence" in frame.columns else None,
    }
