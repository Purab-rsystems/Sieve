"""Match contacts to a decided committee. Runs after the committee is fixed.

A contact fills a seat only when their own job title holds that seat's title
(reference.holds). Nobody is moved into a seat their title does not hold.
Per account, the report says which minimums are met and which seats are
missing. Nothing here changes the committee.
"""

from __future__ import annotations

import pandas as pd

from .reference import holds, load_rules

TITLE_COLUMNS = ("Title", "title", "Job Title", "job_title", "canonical_title")
ACCOUNT_COLUMNS = ("sv_account_id", "Website URL", "Website", "Company", "company", "Company Name", "account")


def _pick(df: pd.DataFrame, names) -> str:
    for n in names:
        if n in df.columns:
            return n
    raise KeyError(f"none of these columns found: {names}")


def match_contacts(contacts: pd.DataFrame, committee: dict, title_col: str | None = None,
                   account_col: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (contacts with sv_committee_title / sv_committee_role, per-account coverage)."""
    tcol = title_col or _pick(contacts, TITLE_COLUMNS)
    acol = account_col or _pick(contacts, ACCOUNT_COLUMNS)
    seats = {m["title"]: m["role"] for m in committee["members"]}
    order = {t: i for i, t in enumerate(seats)}
    out = contacts.copy()
    titles, roles = [], []
    for raw in out[tcol]:
        held = sorted((t for t in holds(str(raw)) if t in seats), key=order.get)
        titles.append(held[0] if held else "")
        roles.append(seats[held[0]] if held else "")
    out["sv_committee_title"] = titles
    out["sv_committee_role"] = roles

    minimums = load_rules()["minimums"]
    rows = []
    for account, g in out.groupby(acol, sort=True):
        filled = {r: 0 for r in minimums}
        seen = set()
        for t, r in zip(g["sv_committee_title"], g["sv_committee_role"]):
            if t and t not in seen:            # one seat per committee title per account
                seen.add(t)
                filled[r] = filled.get(r, 0) + 1
        missing = {r: n - filled.get(r, 0) for r, n in minimums.items() if filled.get(r, 0) < n}
        rows.append({"account": account, "contacts": len(g),
                     **{f"seats_{r}": filled.get(r, 0) for r in minimums},
                     "minimums_met": not missing,
                     "missing": "; ".join(f"{r} x{n}" for r, n in missing.items()),
                     "titles_present": "; ".join(sorted(seen))})
    return out, pd.DataFrame(rows)
