"""Buying committee assembly (PRD module M7).

For each Tier A account, pick the people who actually decide. The PRD names
three seats:

    Economic Buyer   CEO, CFO, COO. Budget sign-off, P&L, M&A sponsorship.
    Champion         CTO, VP Engineering, Chief Architect, VP Platform Eng.
                     Evaluates feasibility, Strangler Fig architecture,
                     database refactoring, developer velocity.
    Influencer       VP Product, Director of AdTech, Head of Infrastructure,
                     VP Corporate Development. Day-to-day stakeholders.

Eligibility is enforced before selection. A contact carrying GENERIC_EMAIL is
barred from every seat, because a routing address is not a person. A contact
carrying SENIORITY_AMBIGUITY is barred from Economic Buyer and Champion, which
is exactly the system action the PRD specifies for that flag. Anyone
disqualified by M4 is barred entirely.

Selection is deterministic. Candidates sort by seniority rank, then
confidence, then contact ID, so two runs over the same input produce the same
committee. Ties never resolve by list order, which would depend on how the
file happened to be sorted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "CommitteeCandidate", "CommitteeSeat", "Committee",
    "assemble_committee", "SEAT_ORDER", "SEAT_CAPACITY",
]

#: Seats filled in this order. Economic Buyer first, since it is the scarcest.
SEAT_ORDER = ("Economic Buyer", "Champion", "Influencer")

#: How many people may hold each seat. The PRD calls the committee
#: multi-threaded, so Influencer takes several while the two decision seats
#: stay single-threaded.
SEAT_CAPACITY = {"Economic Buyer": 1, "Champion": 1, "Influencer": 3}

#: Flags barring a contact from every seat.
BARRED_EVERYWHERE = frozenset({"GENERIC_EMAIL"})

#: Flags barring a contact from the two decision-making seats.
BARRED_FROM_DECISION_SEATS = frozenset({"SENIORITY_AMBIGUITY"})
DECISION_SEATS = frozenset({"Economic Buyer", "Champion"})


@dataclass(frozen=True)
class CommitteeCandidate:
    """One validated contact competing for a seat."""

    contact_id: str
    account_id: str
    full_name: str | None
    canonical_title: str | None
    seniority: str
    seniority_rank: int
    natural_seat: str | None            # what M6 mapped the title to
    is_authoritative: bool
    confidence: float
    flag_codes: frozenset[str] = frozenset()
    validation_status: str = "VERIFIED"

    def eligible_for(self, seat: str) -> bool:
        """Can this person hold this seat."""
        if self.validation_status == "DISQUALIFIED":
            return False
        if self.flag_codes & BARRED_EVERYWHERE:
            return False
        if seat in DECISION_SEATS and (self.flag_codes & BARRED_FROM_DECISION_SEATS):
            return False
        return self.natural_seat == seat

    def sort_key(self) -> tuple:
        """Deterministic ordering: seniority, then confidence, then ID."""
        return (-self.seniority_rank, -self.confidence, self.contact_id)


@dataclass(frozen=True)
class CommitteeSeat:
    """A filled seat."""

    seat: str
    contact_id: str
    full_name: str | None
    canonical_title: str | None
    seniority: str
    confidence: float


@dataclass
class Committee:
    """The assembled committee for one account."""

    account_id: str
    seats: list[CommitteeSeat] = field(default_factory=list)
    unassigned: list[str] = field(default_factory=list)

    @property
    def filled_seats(self) -> set[str]:
        return {seat.seat for seat in self.seats}

    @property
    def is_complete(self) -> bool:
        """A committee needs both decision seats to be workable."""
        return DECISION_SEATS.issubset(self.filled_seats)

    def role_for(self, contact_id: str) -> str:
        for seat in self.seats:
            if seat.contact_id == contact_id:
                return seat.seat
        return "Unassigned"

    def coverage(self) -> str:
        """A short readable summary, for the review console."""
        if not self.seats:
            return "empty"
        counts: dict[str, int] = {}
        for seat in self.seats:
            counts[seat.seat] = counts.get(seat.seat, 0) + 1
        return ", ".join(f"{name} x{counts[name]}" for name in SEAT_ORDER
                         if name in counts)


def assemble_committee(
    account_id: str,
    candidates: Iterable[CommitteeCandidate],
) -> Committee:
    """Fill each seat from the eligible candidates, best first."""
    pool = sorted(candidates, key=lambda c: c.sort_key())
    committee = Committee(account_id=account_id)
    taken: set[str] = set()

    for seat in SEAT_ORDER:
        remaining = SEAT_CAPACITY[seat]
        for candidate in pool:
            if remaining <= 0:
                break
            if candidate.contact_id in taken:
                continue
            if not candidate.eligible_for(seat):
                continue
            committee.seats.append(CommitteeSeat(
                seat=seat,
                contact_id=candidate.contact_id,
                full_name=candidate.full_name,
                canonical_title=candidate.canonical_title,
                seniority=candidate.seniority,
                confidence=candidate.confidence,
            ))
            taken.add(candidate.contact_id)
            remaining -= 1

    committee.unassigned = [c.contact_id for c in pool if c.contact_id not in taken]
    return committee
