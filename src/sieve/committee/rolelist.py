"""An uploaded committee list: roles, how many of each, and what each role is.

File: CSV or Excel with the columns
    Role         the role's name (any name: Economic Buyer, Security Reviewer, ...)
    Count        how many people the committee needs in it (0 or more)
    Description  what the role does in the buying decision (optional)

A role with no description gets one written by Qwen from its own knowledge,
marked as such. The uploaded list then replaces the default list for that
run: the committee, the qualification check, "Generate committee" and the
Tier A picks all use these roles and descriptions.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from .reference import load_rules

TEMPLATE_ROWS = [
    ("Economic Buyer", 1, ""),
    ("Champion", 1, ""),
    ("Technical Evaluator", 1, ""),
    ("Influencer", 3, ""),
]

COLUMN_NAMES = {
    "role": ("role", "role name", "seat", "name"),
    "count": ("count", "number", "minimum", "min", "how many", "seats"),
    "description": ("description", "definition", "what they do", "details"),
}


class ListError(ValueError):
    pass


def template_csv() -> str:
    return pd.DataFrame(TEMPLATE_ROWS, columns=["Role", "Count", "Description"]).to_csv(index=False)


def _col(df: pd.DataFrame, key: str) -> str | None:
    lower = {str(c).strip().lower(): c for c in df.columns}
    return next((lower[n] for n in COLUMN_NAMES[key] if n in lower), None)


def parse_list(data: bytes, filename: str) -> list[dict]:
    """Rows of {role, count, description, source}. Raises ListError with a plain reason."""
    name = filename.lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            df = pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
        elif name.endswith((".csv", ".txt")):
            df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")
        else:
            raise ListError("The committee list must be a CSV or Excel file.")
    except ListError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ListError(f"The committee list could not be read: {e}") from e
    rc, cc, dc = _col(df, "role"), _col(df, "count"), _col(df, "description")
    if not rc or not cc:
        raise ListError("The committee list needs a Role column and a Count column (Description is optional).")
    rows, seen = [], set()
    for i, r in df.iterrows():
        role = str(r[rc]).strip()
        if not role:
            continue
        if role.lower() in seen:
            raise ListError(f"Role '{role}' appears twice in the committee list.")
        seen.add(role.lower())
        try:
            count = int(float(str(r[cc]).strip()))
        except ValueError:
            raise ListError(f"Row {i + 2}: Count for '{role}' must be a whole number.") from None
        if count < 0 or count > 10:
            raise ListError(f"Row {i + 2}: Count for '{role}' must be between 0 and 10.")
        desc = " ".join(str(r[dc]).split()) if dc else ""
        rows.append({"role": role, "count": count, "description": desc,
                     "source": "uploaded" if desc else ""})
    if not rows:
        raise ListError("The committee list has no roles.")
    return rows


def describe_prompt(role: str, all_roles: list[str]) -> str:
    return (f"In a B2B enterprise buying group (the people at a company who decide on a purchase), the roles "
            f"are: {', '.join(all_roles)}.\n\nIn 2 to 3 sentences, describe the {role} role: who usually holds it "
            f"and what they do in the buying decision. Write only the description.")


def fill_descriptions(rows: list[dict], write) -> list[dict]:
    """Ask Qwen for every missing description. write(prompt) -> text."""
    names = [r["role"] for r in rows]
    out = []
    for r in rows:
        if not r["description"]:
            text = " ".join(write(describe_prompt(r["role"], names)).split())
            r = {**r, "description": text, "source": "written by Qwen from its own knowledge (no source)"}
        out.append(r)
    return out


def rules_for(rows: list[dict] | None) -> dict:
    """The rules a run uses: the default ones, or the uploaded list's."""
    base = load_rules()
    if not rows:
        return base
    names = [r["role"] for r in rows]
    rules = dict(base)
    rules["minimums"] = {r["role"]: int(r["count"]) for r in rows}
    rules["seat_order"] = names
    rules["roles"] = {r["role"]: {"text": r["description"], "source": r["source"]} for r in rows}
    # Forrester's User and Ratifier feed the Influencer seats only when the list has an Influencer role.
    rules["also_asked"] = {k: v for k, v in (base.get("also_asked") or {}).items() if v.get("fills") in names}
    return rules
