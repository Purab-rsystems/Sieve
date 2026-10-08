"""Does a committee qualify against the list? With cited proof.

The list (the default one in rules.yaml, or the one set for a run) is
mandatory: a committee qualifies only when every line of it is met, e.g.
"1 Economic Buyer" needs at least one member in the Economic Buyer seat.

For every line the result cites the members that meet it, with the evidence
that put them there: what Qwen (judge) and Llama (checker) answered, how sure
each was, and the opening of each model's own reasoning, quoted. A line that
is not met is listed as failed, with how many are missing.

Works for the decided committee (members) and for the people picked at a
company (picks): both carry role, judge/checker probabilities and reasoning.
"""

from __future__ import annotations

import re


def _quote(text: str, sentences: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(parts[:sentences]).strip()


def qualify(people: list[dict], minimums: dict[str, int], who: str = "title") -> dict:
    """people: committee members or company picks. who: the field naming each one."""
    lines, failed = [], []
    for role, need in minimums.items():
        holders = [p for p in people if p.get("role") == role]
        proof = [{
            "who": p.get(who) or p.get("title"),
            "detail": p.get("function") if who == "title" else f"{p.get('title')} (seat: {p.get('seat_title')})",
            "judge_p": p.get("judge_p"), "checker_p": p.get("checker_p"), "chance": p.get("chance"),
            "judge_quote": _quote(p.get("judge_reasoning", "")),
            "checker_quote": _quote(p.get("checker_reasoning", "")),
        } for p in holders]
        met = len(holders) >= int(need)
        line = {"role": role, "need": int(need), "have": len(holders), "met": met, "proof": proof}
        lines.append(line)
        if not met:
            failed.append(f"{role}: {len(holders)} of {need}")
    return {"qualified": not failed, "lines": lines, "failed": failed}
