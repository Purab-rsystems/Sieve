"""Contact validation and anomaly flagging (PRD module M4, Gate 2).

Six detectors, each firing a standardized flag code at a declared severity.
Flags reduce a record's confidence from 1.00 by the penalty for their
severity, and a record is never dropped on account of one. The PRD calls this
non-destructive flagging, and it matters: a deleted record takes with it the
information a human would need in order to judge it.

    severity   penalty   PRD system action
    Critical    -0.40    disqualify or block CRM write-back
    High        -0.25    disqualify from the buying committee
    Medium      -0.15    retain, surface for human resolution
    Low         -0.05    prevent assignment to senior committee roles

Two detectors need data the file may not carry. TITLE_MISMATCH needs a second
opinion on the title, which exists when the row came through Apollo enrichment
and carries both halves. ROLE_DEPARTED needs a LinkedIn end-date, which no
export in this pipeline provides, so it falls back to a tenure heuristic and
declares which basis it used. Both say so in their evidence rather than
pretending to a certainty they lack.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..normalize.values import clean_str, normalize_domain, normalize_email
from ..taxonomy.designation import Designation

__all__ = [
    "Severity", "Flag", "ValidationResult", "ContactUnderTest",
    "EmailPatternIndex", "validate_contact", "SEVERITY_PENALTY",
]

#: PRD module M4: confidence = 1.00 - sum of penalties.
SEVERITY_PENALTY: dict[str, float] = {
    "Critical": 0.40,
    "High": 0.25,
    "Medium": 0.15,
    "Low": 0.05,
}

#: Local parts that route to a team rather than a person.
GENERIC_LOCAL_PARTS = frozenset({
    "info", "sales", "support", "contact", "admin", "hello", "help",
    "enquiries", "inquiries", "marketing", "team", "office", "mail",
    "general", "careers", "hr", "press", "media", "billing", "accounts",
    "noreply", "no-reply", "webmaster", "postmaster",
})

#: Consumer mailbox providers. An address on one of these attached to a
#: corporate contact record is a data-entry error rather than a second
#: corporate domain, so it earns the Critical severity.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "zoho.com", "yandex.com", "qq.com", "163.com",
})

#: Switchboard and VoIP patterns, PRD DISPOSABLE_PHONE.
SWITCHBOARD_PATTERNS = (
    re.compile(r"0{4,}$"),          # trailing zeros: a main line
    re.compile(r"(\d)\1{5,}"),      # six or more of the same digit
    re.compile(r"1234567|7654321"),
)

#: Tenure at or below this many months, with a title disagreement present,
#: reads as a possible recent move. Used only when no end-date exists.
RECENT_MOVE_MONTHS = 3


class Severity:
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


@dataclass(frozen=True)
class Flag:
    """One anomaly found on one record."""

    code: str
    severity: str
    evidence: str
    action: str

    @property
    def penalty(self) -> float:
        return SEVERITY_PENALTY[self.severity]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "evidence": self.evidence,
            "action": self.action,
            "penalty": self.penalty,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Everything M4 concluded about one record."""

    flags: list[Flag] = field(default_factory=list)
    confidence: float = 1.00
    status: str = "VERIFIED"        # VERIFIED | FLAGGED_REVIEW | DISQUALIFIED

    @property
    def codes(self) -> str:
        return ",".join(f.code for f in self.flags)

    def detail(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.flags]


@dataclass
class ContactUnderTest:
    """The inputs one validation pass needs.

    Assembled by the caller from whatever the row provides. Every field is
    optional, and a detector whose inputs are absent simply does not fire.
    """

    email: str | None = None
    company_domain: str | None = None
    title: str | None = None
    alternate_title: str | None = None      # a second source's opinion
    alternate_source: str | None = None
    designation: Designation | None = None
    phone: str | None = None
    mobile: str | None = None
    tenure_role_months: int | None = None
    role_end_date_days_ago: int | None = None


class EmailPatternIndex:
    """Learns each domain's dominant email pattern from the file itself.

    With three or more addresses on a domain, the shape they agree on becomes
    that domain's expected pattern. An address departing from it is worth a
    look. Below three addresses there is no pattern to speak of, so the
    detector stays quiet rather than guessing.
    """

    MIN_SAMPLES = 3

    def __init__(self, emails: Iterable[str | None]) -> None:
        shapes: dict[str, Counter[str]] = {}
        for raw in emails:
            email = normalize_email(raw)
            if email is None:
                continue
            local, _, domain = email.partition("@")
            shapes.setdefault(domain, Counter())[self.shape(local)] += 1

        self._dominant: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        for domain, counter in shapes.items():
            total = sum(counter.values())
            if total < self.MIN_SAMPLES:
                continue
            self._dominant[domain] = counter.most_common(1)[0][0]
            self._counts[domain] = total

    @staticmethod
    def shape(local_part: str) -> str:
        """Reduce a local part to its structural shape.

        'vaidas.dzedulionis' -> 'word.word'
        'jsmith'             -> 'word'
        'j.smith2'           -> 'word.word#'
        """
        # Digits are dropped rather than marked, so 'j.smith2' and 'a.b'
        # share a shape. Keeping them would leave 'smith2' looking structurally
        # different from 'smith' and split one real pattern into two.
        text = re.sub(r"\d+", "", local_part.lower())
        parts = re.split(r"([._\-+])", text)
        return "".join("word" if p and p.isalnum() else p for p in parts)

    def expected(self, domain: str) -> str | None:
        return self._dominant.get(domain)

    def sample_size(self, domain: str) -> int:
        return self._counts.get(domain, 0)


# --------------------------------------------------------------------------
# the six detectors
# --------------------------------------------------------------------------

def _title_mismatch(c: ContactUnderTest) -> Flag | None:
    """PRD TITLE_MISMATCH, Medium. Two sources disagree about the title."""
    primary = clean_str(c.title)
    alternate = clean_str(c.alternate_title)
    if primary is None or alternate is None:
        return None
    if primary.strip().lower() == alternate.strip().lower():
        return None
    source = c.alternate_source or "second source"
    return Flag(
        code="TITLE_MISMATCH",
        severity=Severity.MEDIUM,
        evidence=f"{primary!r} here against {alternate!r} from {source}",
        action="Retain record; surface both titles for human resolution",
    )


def _role_departed(c: ContactUnderTest) -> Flag | None:
    """PRD ROLE_DEPARTED, Critical. The person appears to have left.

    The specified heuristic reads a LinkedIn end-date within 90 days. No export
    in this pipeline carries one, so when it is absent the detector falls back
    to a weaker signal: a title disagreement alongside very short tenure, which
    is what a recent move looks like in the data available. The evidence string
    names which basis fired, so nobody mistakes the fallback for the real test.
    """
    if c.role_end_date_days_ago is not None:
        if c.role_end_date_days_ago <= 90:
            return Flag(
                code="ROLE_DEPARTED",
                severity=Severity.CRITICAL,
                evidence=f"role end-date {c.role_end_date_days_ago} days ago",
                action="Disqualify contact; trigger replacement backfill",
            )
        return None

    tenure = c.tenure_role_months
    disagrees = bool(
        clean_str(c.alternate_title)
        and clean_str(c.title)
        and clean_str(c.title).strip().lower()
        != clean_str(c.alternate_title).strip().lower()
    )
    if tenure is not None and tenure <= RECENT_MOVE_MONTHS and disagrees:
        return Flag(
            code="ROLE_DEPARTED",
            severity=Severity.CRITICAL,
            evidence=(
                f"no end-date available; inferred from {tenure} months tenure "
                f"plus a cross-source title disagreement"
            ),
            action="Disqualify contact; trigger replacement backfill",
        )
    return None


def _generic_email(c: ContactUnderTest) -> Flag | None:
    """PRD GENERIC_EMAIL, High. The address routes to a team."""
    email = normalize_email(c.email)
    if email is None:
        return None
    local = email.split("@")[0]
    base = re.split(r"[._\-+]", local)[0]
    if local in GENERIC_LOCAL_PARTS or base in GENERIC_LOCAL_PARTS:
        return Flag(
            code="GENERIC_EMAIL",
            severity=Severity.HIGH,
            evidence=f"local part {local!r} is a routing address",
            action="Disqualify from buying committee; tag as general routing",
        )
    return None


def _domain_anomaly(c: ContactUnderTest) -> Flag | None:
    """PRD DOMAIN_ANOMALY, Critical. Email domain differs from the company's."""
    email = normalize_email(c.email)
    company = normalize_domain(c.company_domain)
    if email is None or company is None:
        return None
    email_domain = normalize_domain(email.split("@")[1])
    if email_domain is None or email_domain == company:
        return None
    # A shared registrable root covers regional and brand domains, so
    # 'adform.com' and 'adform.de' agree.
    if email_domain.split(".")[0] == company.split(".")[0]:
        return None

    # Severity splits on what kind of mismatch this is, because the two carry
    # very different consequences.
    #
    # A consumer mailbox on a corporate record is a data-entry error, and the
    # PRD's Critical action of blocking CRM write-back is right for it.
    #
    # A different CORPORATE domain is usually an acquisition or a rebrand, and
    # the record is fine. In the modernization campaign this covers Outbrain
    # against teads.com, Equativ against smartadserver.com, Basis against
    # centro.net, and MiQ against miqdigital.com. Treating those as Critical
    # would disqualify 9% of the file over renamings, so they flag at Medium
    # for review and for adding the alias to the account's domain set.
    if email_domain in FREEMAIL_DOMAINS:
        return Flag(
            code="DOMAIN_ANOMALY",
            severity=Severity.CRITICAL,
            evidence=(
                f"consumer mailbox {email_domain} on a record for {company}"
            ),
            action="Flag for manual verification; block automatic CRM write-back",
        )

    return Flag(
        code="DOMAIN_ANOMALY",
        severity=Severity.MEDIUM,
        evidence=(
            f"email domain {email_domain} against company domain {company}; "
            f"both corporate, so likely an acquisition, rebrand or alias"
        ),
        action="Verify and register the alias domain on the account",
    )


def _seniority_ambiguity(c: ContactUnderTest) -> Flag | None:
    """PRD SENIORITY_AMBIGUITY, Low. No distinct executive rank."""
    if c.designation is None or not c.designation.is_ambiguous:
        return None
    return Flag(
        code="SENIORITY_AMBIGUITY",
        severity=Severity.LOW,
        evidence=(
            f"title {c.designation.title_raw!r} resolves to "
            f"{c.designation.seniority}"
        ),
        action="Prevent assignment to Economic Buyer or Champion roles",
    )


def _disposable_phone(c: ContactUnderTest) -> Flag | None:
    """PRD DISPOSABLE_PHONE, Medium. A switchboard rather than a direct dial."""
    for number in (c.phone, c.mobile):
        digits = clean_str(number)
        if digits is None:
            continue
        for pattern in SWITCHBOARD_PATTERNS:
            if pattern.search(digits):
                return Flag(
                    code="DISPOSABLE_PHONE",
                    severity=Severity.MEDIUM,
                    evidence=f"{digits} matches a switchboard pattern",
                    action="Suppress direct-dial tag; mark as HQ switchboard",
                )
    return None


#: Detector order is fixed, so flag strings are reproducible run to run.
DETECTORS = (
    _title_mismatch,
    _role_departed,
    _generic_email,
    _domain_anomaly,
    _seniority_ambiguity,
    _disposable_phone,
)

#: A record carrying any of these at Critical severity is disqualified.
#: Severity matters as well as the code, since DOMAIN_ANOMALY fires at two
#: levels depending on whether the mismatched domain is corporate or consumer.
DISQUALIFYING_CODES = frozenset({"ROLE_DEPARTED", "DOMAIN_ANOMALY"})


def validate_contact(contact: ContactUnderTest) -> ValidationResult:
    """Run all six detectors and compute confidence and status."""
    flags = [flag for detector in DETECTORS if (flag := detector(contact))]

    confidence = 1.00 - sum(flag.penalty for flag in flags)
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    if any(
        flag.code in DISQUALIFYING_CODES and flag.severity == Severity.CRITICAL
        for flag in flags
    ):
        status = "DISQUALIFIED"
    elif flags:
        status = "FLAGGED_REVIEW"
    else:
        status = "VERIFIED"

    return ValidationResult(flags=flags, confidence=confidence, status=status)
