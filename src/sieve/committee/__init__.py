"""Buying committee.

assembly    M7: fills per-account seats from validated contacts (v1).
decide      the committee decided from the campaign file by a judge model
            (Qwen2.5-7B) and a checker model (Llama-3.1-8B), before any
            contact is looked at.
match       contacts matched to the decided committee's seats.
reference   the role reference sheet and the committee rules.
"""

from .assembly import (
    SEAT_CAPACITY, SEAT_ORDER, Committee, CommitteeCandidate, CommitteeSeat,
    assemble_committee,
)

__all__ = [
    "SEAT_CAPACITY", "SEAT_ORDER", "Committee", "CommitteeCandidate",
    "CommitteeSeat", "assemble_committee",
]
