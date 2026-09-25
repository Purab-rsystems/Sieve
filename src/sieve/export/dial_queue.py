"""Outbound dial queue provisioning (PRD module M8).

The priority formula, straight from the PRD::

    Dial Priority = (sv_icp_score      x 0.50)
                  + (Seniority Weight  x 0.30)
                  + (Direct Phone Verified x 0.20)

All three inputs are normalized to 0-100 before weighting, so the result also
lands on 0-100. Seniority weight comes from the taxonomy's rank table. Phone
verification scores 100 for a direct dial, 50 for a mobile, and 0 when the only
number available was flagged as a switchboard.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["dial_priority", "phone_verification_score", "DialInputs"]

WEIGHT_ICP = 0.50
WEIGHT_SENIORITY = 0.30
WEIGHT_PHONE = 0.20


@dataclass(frozen=True)
class DialInputs:
    icp_score: int
    seniority_rank: int
    has_direct_dial: bool
    has_mobile: bool
    phone_flagged: bool


def phone_verification_score(inputs: DialInputs) -> int:
    """0-100 for how reachable this contact's number is."""
    if inputs.phone_flagged:
        return 0
    if inputs.has_direct_dial:
        return 100
    if inputs.has_mobile:
        return 50
    return 0


def dial_priority(inputs: DialInputs) -> float:
    """The PRD's weighted priority, 0-100, rounded to two places."""
    value = (
        inputs.icp_score * WEIGHT_ICP
        + inputs.seniority_rank * WEIGHT_SENIORITY
        + phone_verification_score(inputs) * WEIGHT_PHONE
    )
    return round(max(0.0, min(100.0, value)), 2)
