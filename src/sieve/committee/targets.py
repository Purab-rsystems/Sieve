"""Pick the people to target at each Tier A company, once the committee is decided.

For every Tier A account and every seat of the decided committee, Qwen (judge)
reads the campaign, the committee, the company and its contacts, writes its
reasoning, and picks one contact for the seat, or "nobody here fits it".
Llama (checker) answers the same question; the pick stands only if Llama also
rates that contact above chance. Same rules as the committee:

* one person, one seat;
* a rejected pick, or a person already seated, is taken off the list and the
  judge is asked again (follow-up rounds from rules.yaml);
* "nobody fits" is an answer, not a failure: the seat stays empty and the
  judge's reasoning says why. Nobody is forced into a seat.

Round 1 asks every seat at once, so each model's questions are answered in one
batch; clashes (one person top for two seats) go to the earlier seat in the
committee's seat order, and the later seat is asked again without that person.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Callable

import pandas as pd

from .reference import load_rules

NOBODY = "Nobody here fits this seat"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXY"          # Z is always NOBODY
CHUNK = len(LETTERS)


@dataclass(frozen=True)
class PickAsk:
    model: str                 # "judge" | "checker"
    prompt: str                # the account- and seat-specific part of the question
    options: tuple[str, ...]   # letters, Z = nobody

    @property
    def campaign(self) -> str:          # used by the answer store key
        return self.prompt

    @property
    def role(self) -> str:
        return "target"

    label = "picking a contact"

    def store_key(self, model_name: str) -> str:
        return pick_key(model_name, self)

    def prompt_text(self) -> str:
        return self.prompt


@dataclass
class AccountTargets:
    account_id: str
    company: str
    picks: list[dict] = field(default_factory=list)       # seat filled by a person
    empty: list[dict] = field(default_factory=list)       # nobody fits (judge's reasoning)
    rejected: list[dict] = field(default_factory=list)    # judge's pick the checker turned down
    missing: list[PickAsk] = field(default_factory=list)


def seats_of(committee: dict) -> list[dict]:
    order = committee.get("seat_order") or load_rules()["seat_order"]
    members = sorted(committee["members"], key=lambda m: (order.index(m["role"]) if m["role"] in order else 99,
                                                          m.get("extra", False)))
    return [{"seat": i, "role": m["role"], "title": m["title"], "function": m["function"]}
            for i, m in enumerate(members)]


def committee_block(committee: dict) -> str:
    return "The buying committee decided for this campaign:\n" + "\n".join(
        f"- {s['role']}: {s['title']} ({s['function']})" for s in seats_of(committee)) + "\n\n"


def seat_prompt(company: dict, seat: dict, contacts: list[dict]) -> tuple[str, tuple[str, ...]]:
    letters = LETTERS[:len(contacts)]
    lines = [f"{l}) {c['name'] or '(no name)'} - {c['title'] or '(no title)'}" for l, c in zip(letters, contacts)]
    lines.append(f"Z) {NOBODY}")
    text = (f"Target company: {company['company']}" + (f" ({company['website']})" if company.get("website") else "")
            + f", TAL score {company['score']} ({company['tier']}).\nIts contacts:\n" + "\n".join(lines) + "\n\n"
            f"Question: Which of these contacts should the seller target for the {seat['role']} seat, the "
            f"committee's {seat['title']} ({seat['function']})? Pick the person at this company who would "
            f"actually play that part in deciding on this offering, judging from their job title. If none of "
            f"them would, answer Z.\n"
            f"First explain your thinking in 2 to 4 sentences. Then give your answer on its own line as "
            f"\"Answer: <letter>\".")
    return text, tuple(letters) + ("Z",)


def account_contacts(roster: pd.DataFrame) -> list[dict]:
    out = []
    for _, r in roster.iterrows():
        name = " ".join(str(x) for x in (r.get("First Name"), r.get("Last Name"))
                        if isinstance(x, str) and x.strip())
        out.append({"contact_id": str(r["sv_contact_id"]), "name": name,
                    "title": r.get("Title") if isinstance(r.get("Title"), str) else ""})
    return out


def pick_for_account(company: dict, contacts: list[dict], committee: dict, campaign: str,
                     answers: Callable[[PickAsk], dict | None], use_checker: bool = True) -> AccountTargets:
    rules = load_rules()
    rounds = 1 + int(rules.get("follow_up_rounds", 0))
    head = campaign + committee_block(committee)
    res = AccountTargets(company["account_id"], company["company"])
    seats = seats_of(committee)
    seated: dict[int, str] = {}                  # seat -> contact_id
    taken: set[str] = set()
    excluded: dict[int, set[str]] = {s["seat"]: set() for s in seats}
    closed: set[int] = set()

    def ask(model, prompt, options):
        a = PickAsk(model, prompt, options)
        got = answers(a)
        if got is None:
            res.missing.append(a)
        return got

    for rnd in range(rounds):
        open_seats = [s for s in seats if s["seat"] not in seated and s["seat"] not in closed]
        if not open_seats:
            break
        tops = []                                     # (seat, contact or None, p, prompt, options, by_letter, reasoning)
        stalled = False
        for s in open_seats:
            cands = [c for c in contacts if c["contact_id"] not in taken
                     and c["contact_id"] not in excluded[s["seat"]]]
            got = judge_pick(s, cands, head, company, ask)
            if got is None:
                stalled = True
                continue
            tops.append((s, *got))
        if stalled:
            return res
        claimed: set[str] = set()
        progress = False
        for s, person, p, prompt, options, by_letter, why_j in tops:     # seats in seat order
            base = {"seat": s["seat"], "role": s["role"], "seat_title": s["title"], "function": s["function"],
                    "round": rnd, "judge_p": round(p, 4), "chance": round(1 / len(options), 4),
                    "judge_reasoning": why_j}
            if person is None:
                res.empty.append(base)
                closed.add(s["seat"])
                continue
            if person["contact_id"] in claimed:          # an earlier seat took this person this round
                continue
            if use_checker:
                pc = ask("checker", prompt, options)
                if pc is None:
                    return res
                letter = next(k for k, v in by_letter.items() if v is person)
                cp = pc["probs"][letter]
                base.update(checker_p=round(cp, 4), checker_reasoning=pc.get("reasoning", ""))
                if cp <= 1 / len(options):
                    res.rejected.append({**base, **person})
                    excluded[s["seat"]].add(person["contact_id"])
                    progress = True
                    continue
            else:
                base.update(checker_p=None, checker_reasoning="")
            claimed.add(person["contact_id"])
            taken.add(person["contact_id"])
            seated[s["seat"]] = person["contact_id"]
            res.picks.append({**base, **person})
            progress = True
        if not progress and all(s["seat"] in closed or s["seat"] in seated for s in open_seats):
            break
    for s in seats:                                   # seats that ran out of rounds or people
        if s["seat"] not in seated and s["seat"] not in closed:
            res.empty.append({"seat": s["seat"], "role": s["role"], "seat_title": s["title"],
                              "function": s["function"], "round": rounds, "judge_p": None, "chance": None,
                              "judge_reasoning": "No contact left that both models accept for this seat."})
    res.picks.sort(key=lambda x: x["seat"])
    res.empty.sort(key=lambda x: x["seat"])
    return res


def judge_pick(seat, cands, head, company, ask):
    """The judge's pick for a seat among cands: groups of 25 when there are more,
    then a final question between the group winners. Returns (person or None,
    p, prompt, options, by_letter, reasoning) for the question that decided it,
    or None while answers are missing."""
    groups = [cands[i:i + CHUNK] for i in range(0, len(cands), CHUNK)] or [[]]
    winners, last = [], None
    for grp in groups:
        prompt, options = seat_prompt(company, seat, grp)
        prompt = head + prompt
        got = ask("judge", prompt, options)
        if got is None:
            return None
        probs = got["probs"]
        letter = max(probs, key=probs.get)
        by_letter = dict(zip(LETTERS, grp))
        last = (by_letter.get(letter), probs[letter], prompt, options, by_letter, got.get("reasoning", ""))
        if by_letter.get(letter) is not None:
            winners.append(by_letter[letter])
    if len(groups) == 1 or not winners:
        return last
    return judge_pick(seat, winners, head, company, ask)   # final question between the group winners


def pick_targets(roster: pd.DataFrame, committee: dict, campaign: str,
                 answers: Callable[[PickAsk], dict | None], tiers=("Tier A",),
                 use_checker: bool = True) -> dict[str, AccountTargets]:
    out = {}
    frame = roster[roster["sv_target_tier"].isin(tiers)]
    for aid, g in frame.groupby("sv_account_id", sort=False):
        first = g.iloc[0]
        web = first.get("Website URL") if isinstance(first.get("Website URL"), str) else first.get("Website")
        company = {"account_id": str(aid), "company": str(first.get("Company") or aid),
                   "website": web if isinstance(web, str) else "",
                   "score": int(float(first.get("sv_icp_score") or 0)), "tier": first.get("sv_target_tier")}
        out[str(aid)] = pick_for_account(company, account_contacts(g), committee,
                                         f"A seller is running this B2B campaign:\n{campaign}\n\n", answers, use_checker)
    return out


def pick_key(model_name: str, a: PickAsk) -> str:
    return f"{model_name}|target|{hashlib.sha1(a.prompt.encode()).hexdigest()[:16]}"


def to_dict(t: AccountTargets) -> dict:
    d = asdict(t)
    d.pop("missing")
    return d
