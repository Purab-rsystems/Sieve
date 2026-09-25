"""Outbound dial queue provisioning (PRD module M8, step iv).

Turns an approved roster into a dialer-ready queue for Compass Max, ordered by
the PRD's dial priority.

Only phone-verified contacts enter the queue, which the PRD states directly:
the engine "evaluates phone-verified contacts". A record with no usable number,
or whose only number M4 flagged as a switchboard, has nothing to dial.
Disqualified records never enter either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..normalize.values import normalize_phone

__all__ = ["build_queue", "write_queue", "QUEUE_COLUMNS"]

QUEUE_COLUMNS = [
    "queue_position", "sv_contact_id", "sv_account_id", "company",
    "full_name", "sv_canonical_title", "sv_seniority", "sv_committee_role",
    "dial_number", "number_type", "sv_icp_score", "sv_target_tier",
    "sv_confidence", "sv_dial_priority",
]

#: Columns tried, in order, for the number to dial.
_DIAL_SOURCES = (
    ("Direct Dial 1", "direct"), ("Direct Dial", "direct"),
    ("Direct Phone", "direct"), ("Mobile", "mobile"),
    ("Mobile Phone", "mobile"), ("Phone", "switchboard"),
)


def _dial_number(row: pd.Series) -> tuple[str | None, str | None]:
    """The best number available for this contact, and what kind it is."""
    flagged = "DISPOSABLE_PHONE" in str(row.get("sv_flags") or "")
    for column, kind in _DIAL_SOURCES:
        if column not in row.index:
            continue
        number = normalize_phone(row[column])
        if not number:
            continue
        if kind == "switchboard" and flagged:
            continue
        return number, kind
    return None, None


def _name(row: pd.Series) -> str | None:
    parts = [row.get("First Name"), row.get("Last Name")]
    joined = " ".join(str(p) for p in parts if p and not pd.isna(p))
    return joined or None


def build_queue(
    roster: pd.DataFrame,
    *,
    tier_filter: tuple[str, ...] = ("Tier A",),
    committee_only: bool = True,
    min_confidence: float = 0.60,
) -> pd.DataFrame:
    """Build the prioritized dial queue.

    Defaults follow the PRD's priority cadence: Tier A accounts, committee
    members, and nothing M4 scored below the confidence floor.
    """
    if "sv_dial_priority" not in roster.columns:
        raise ValueError("Roster has no sv_dial_priority. Run `sieve run` first.")

    frame = roster.copy()
    keep = frame["sv_validation_status"] != "DISQUALIFIED"
    keep &= frame["sv_confidence"].fillna(0) >= min_confidence
    if tier_filter:
        keep &= frame["sv_target_tier"].isin(tier_filter)
    if committee_only:
        keep &= frame["sv_committee_role"] != "Unassigned"
    frame = frame.loc[keep]

    rows = []
    for _, row in frame.iterrows():
        number, kind = _dial_number(row)
        if number is None:
            continue
        rows.append({
            "sv_contact_id": row["sv_contact_id"],
            "sv_account_id": row["sv_account_id"],
            "company": row.get("Company"),
            "full_name": _name(row),
            "sv_canonical_title": row.get("sv_canonical_title"),
            "sv_seniority": row.get("sv_seniority"),
            "sv_committee_role": row.get("sv_committee_role"),
            "dial_number": number,
            "number_type": kind,
            "sv_icp_score": row.get("sv_icp_score"),
            "sv_target_tier": row.get("sv_target_tier"),
            "sv_confidence": row.get("sv_confidence"),
            "sv_dial_priority": row.get("sv_dial_priority"),
        })

    queue = pd.DataFrame(rows)
    if queue.empty:
        return pd.DataFrame(columns=QUEUE_COLUMNS)

    # Sort by priority, then contact id, so the order repeats run to run.
    queue = queue.sort_values(
        ["sv_dial_priority", "sv_contact_id"], ascending=[False, True]
    ).reset_index(drop=True)
    queue.insert(0, "queue_position", range(1, len(queue) + 1))
    return queue[QUEUE_COLUMNS]


def write_queue(queue: pd.DataFrame, directory: Path | str) -> dict[str, Path]:
    """Write the queue CSV and a summary."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "queue": directory / "compass_max_dial_queue.csv",
        "summary": directory / "dial_queue_summary.json",
    }
    queue.to_csv(paths["queue"], index=False)

    summary: dict[str, Any] = {
        "contacts": int(len(queue)),
        "accounts": int(queue["sv_account_id"].nunique()) if len(queue) else 0,
        "by_committee_role": (
            queue["sv_committee_role"].value_counts().to_dict() if len(queue) else {}
        ),
        "by_number_type": (
            queue["number_type"].value_counts().to_dict() if len(queue) else {}
        ),
        "priority_range": (
            [float(queue["sv_dial_priority"].min()),
             float(queue["sv_dial_priority"].max())] if len(queue) else []
        ),
    }
    paths["summary"].write_text(json.dumps(summary, indent=2))
    return paths
