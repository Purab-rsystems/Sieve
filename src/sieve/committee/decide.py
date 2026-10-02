"""Decide the buying committee for a campaign: Qwen judges, Llama checks.

    committee = decide_committee("config/icp_adtech_modernization.yaml")

What the models read
--------------------
The buying-group roles in their sources' own words (rules.yaml), the role
reference sheet (every function at a target company with its real job titles
and their published descriptions), and the campaign: what is sold and to whom.
Never an account, a contact or a contact's title.

What they are asked
-------------------
For each role: "which function would supply the <role>?", with the functions
as the options. The judge's answer is read from its option-letter
probabilities over four orders, the checker's from the function names
themselves over two orders (see models.py).

Assembly (no tuned setting)
---------------------------
* Seats are filled in rules["seat_order"]. For each role the judge proposes
  every function it rates above chance (1 / number of options), best first.
* The checker must pass every pick: it passes a function it also rates above
  chance on the same question. Not passed means not in the committee,
  whatever the judge said.
* A passed function gives the seat its most senior title not yet used. One
  title, one role; a function can supply more than one title (CIO, then VP of
  IT) but only one per role.
* Influencer seats also take passed User and Ratifier picks: Forrester's
  Influencer covers anyone inside the organisation with a stake.
* A seat still short after a round sends the judge a follow-up: the same
  question with the rejected and already-seated functions taken off the
  list. Its new picks need the checker's pass like any other.
* Passed first-round picks for functions not yet in the committee join as
  extra members (more members are fine).
* A minimum nothing passes stays unfilled and is reported. Nothing is forced in.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .campaign import campaign_text as read_campaign
from .reference import TitleDef, functions, load_reference, load_rules

SYSTEM = "You are an expert in B2B enterprise buying groups."
QUESTION = ("Question: At a target company buying this offering, which function's leader "
            "would be the {role} in the buying group?")
DEFAULT_STORE = Path(__file__).resolve().parents[3] / "config" / "committee" / "answers.json"


# ---------------------------------------------------------------- prompt text
def _excerpt(t: TitleDef) -> str:
    if not t.summary:
        return f"- {t.title}: no published description is available for this title."
    sentences = re.split(r"(?<=[.!?])\s+", t.summary.strip())
    text = " ".join(sentences[:2])
    if t.duties:
        text += " Duties: " + "; ".join(d.rstrip(". ") for d in t.duties[:3]) + "."
    return f"- {t.title}: {text}"


def shared_text(rules: dict | None = None, reference: tuple[TitleDef, ...] | None = None) -> str:
    """Role definitions and the role reference sheet: the same for every campaign."""
    rules = rules or load_rules()
    roles = {**rules["roles"], **rules.get("also_asked", {})}
    parts = ["What a buying group is: " + " ".join(rules["intro"]["text"].split()), "", "Buying group roles:"]
    parts += [f"- {r}: {' '.join(v['text'].split())}" for r, v in roles.items()]
    parts += ["", "Functions at a target company, with their job titles and published job descriptions:"]
    for f, titles in sorted(functions(reference).items()):
        parts += ["", f"Function: {f}"] + [_excerpt(t) for t in titles]
    return "\n".join(parts) + "\n\n"


def campaign_block(text: str) -> str:
    return f"A seller is running this B2B campaign:\n{text}\n\n"


def letters_block(order, letters):
    return "\n".join(f"{l}) {o}" for l, o in zip(letters, order))


def names_block(order, _letters):
    return "Options:\n" + "\n".join(f"- {o}" for o in order)


# ---------------------------------------------------------------- answers
def answer_key(model: str, campaign: str, role: str, options: Iterable[str]) -> str:
    h = hashlib.sha1(campaign.encode()).hexdigest()[:12]
    return f"{model}|{h}|{role}|{','.join(sorted(options))}"


class AnswerStore:
    """Every model answer, saved to JSON so a decision reproduces without the models."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, float] | None:
        row = self.data.get(key)
        return row["probs"] if row else None

    def put(self, key: str, model: str, campaign: str, role: str, options, probs) -> None:
        with self._lock:
            self.data[key] = {"model": model, "campaign": campaign, "role": role,
                              "options": sorted(options), "probs": probs}

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=0, sort_keys=True))
            tmp.replace(self.path)


@dataclass(frozen=True)
class Ask:
    model: str            # "judge" | "checker"
    campaign: str
    role: str
    options: tuple[str, ...]


# ---------------------------------------------------------------- assembly
@dataclass
class Committee:
    campaign: str
    members: list[dict] = field(default_factory=list)
    unfilled: dict[str, int] = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)
    missing: list[Ask] = field(default_factory=list)
    judge: str = ""
    checker: str = ""

    @property
    def complete(self) -> bool:
        return not self.missing

    def titles(self) -> list[str]:
        return [m["title"] for m in self.members]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("missing")
        d["seat_counts"] = {}
        for m in self.members:
            d["seat_counts"][m["role"]] = d["seat_counts"].get(m["role"], 0) + 1
        return d


def assemble(campaign: str, answers: Callable[[Ask], dict | None], rules: dict | None = None,
             reference: tuple[TitleDef, ...] | None = None, use_checker: bool = True,
             judge_name: str = "", checker_name: str = "") -> Committee:
    rules = rules or load_rules()
    funcs = functions(reference)
    all_functions = sorted(funcs)
    minimums = rules["minimums"]
    order = [r for r in rules["seat_order"] if r in minimums] + [r for r in minimums if r not in rules["seat_order"]]
    feeds = {r: [r] for r in minimums}
    for extra, v in rules.get("also_asked", {}).items():
        feeds.setdefault(v["fills"], [v["fills"]]).append(extra)
    rounds = 1 + int(rules.get("follow_up_rounds", 0))

    c = Committee(campaign=campaign, judge=judge_name, checker=checker_name if use_checker else "none")
    used: set[str] = set()
    seated: dict[str, set[str]] = {r: set() for r in minimums}
    first_round: list[tuple[float, str, str, tuple[str, ...]]] = []

    def free_title(f: str) -> TitleDef | None:
        return next((t for t in funcs[f] if t.title not in used), None)

    def ask(model: str, role: str, options: tuple[str, ...]) -> dict | None:
        a = Ask(model, campaign, role, options)
        got = answers(a)
        if got is None:
            c.missing.append(a)
        return got

    def passes(role: str, f: str, options: tuple[str, ...]) -> tuple[bool | None, float | None]:
        if not use_checker:
            return True, None
        pc = ask("checker", role, options)
        if pc is None:
            return None, None
        return pc[f] > 1.0 / len(options), round(pc[f], 4)

    for role in order:
        need = minimums[role]
        excluded: set[str] = set()
        for rnd in range(rounds):
            have = sum(1 for m in c.members if m["role"] == role)
            if have >= need:
                break
            options = tuple(f for f in all_functions
                            if f not in excluded and f not in seated[role] and free_title(f))
            if len(options) < 2:
                break
            proposals = []
            stalled = False
            for asked_as in feeds[role]:
                pj = ask("judge", asked_as, options)
                if pj is None:
                    stalled = True
                    continue
                proposals += [(p, asked_as, f) for f, p in pj.items() if p > 1.0 / len(options)]
            if stalled:
                return c
            best: dict[str, tuple[float, str]] = {}
            for p, asked_as, f in proposals:
                if f not in best or p > best[f][0]:
                    best[f] = (p, asked_as)
            ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
            if not ranked:
                break
            if rnd == 0:
                first_round += [(p, a, f, options) for f, (p, a) in ranked]
            for f, (p, asked_as) in ranked:
                if sum(1 for m in c.members if m["role"] == role) >= need:
                    break
                ok, pc = passes(asked_as, f, options)
                if ok is None:
                    return c
                if not ok:
                    excluded.add(f)
                    c.rejected.append({"role": role, "asked_as": asked_as, "function": f, "round": rnd,
                                       "judge_p": round(p, 4), "checker_p": pc,
                                       "chance": round(1.0 / len(options), 4)})
                    continue
                t = free_title(f)
                if t is None:
                    continue
                used.add(t.title)
                seated[role].add(f)
                c.members.append({"title": t.title, "function": f, "level": t.level, "role": role,
                                  "asked_as": asked_as, "round": rnd, "extra": False,
                                  "judge_p": round(p, 4), "checker_p": pc,
                                  "chance": round(1.0 / len(options), 4)})
        short = need - sum(1 for m in c.members if m["role"] == role)
        if short > 0:
            c.unfilled[role] = short

    # Extra members: passed first-round picks for functions not yet in the committee.
    role_of = {a: r for r, asks in feeds.items() for a in asks}
    for p, asked_as, f, options in sorted(first_round, key=lambda x: -x[0]):
        if any(m["function"] == f for m in c.members):
            continue
        if any(r["function"] == f and r["asked_as"] == asked_as and r["round"] == 0 for r in c.rejected):
            continue                                    # already turned down on this same question
        ok, pc = passes(asked_as, f, options)
        if ok is None:
            return c
        if not ok:
            c.rejected.append({"role": role_of[asked_as], "asked_as": asked_as, "function": f, "round": 0,
                               "extra": True, "judge_p": round(p, 4), "checker_p": pc,
                               "chance": round(1.0 / len(options), 4)})
            continue
        t = free_title(f)
        if t is None:
            continue
        used.add(t.title)
        c.members.append({"title": t.title, "function": f, "level": t.level, "role": role_of[asked_as],
                          "asked_as": asked_as, "round": 0, "extra": True,
                          "judge_p": round(p, 4), "checker_p": pc, "chance": round(1.0 / len(options), 4)})
    return c


# ---------------------------------------------------------------- running the models
class ModelRunner:
    """Answers missing questions, loading one model at a time."""

    def __init__(self, store: AnswerStore, state_dir: Path | None = None, log=print) -> None:
        from .models import CHECKER, JUDGE
        self.specs = {"judge": JUDGE, "checker": CHECKER}
        self.store = store
        self.state_dir = state_dir
        self.log = log
        self.loaded = None
        self.model = None
        self.shared = shared_text()

    def name(self, which: str) -> str:
        return self.specs[which].name

    def lookup(self, a: Ask) -> dict | None:
        return self.store.get(answer_key(self.name(a.model), a.campaign, a.role, a.options))

    def _load(self, which: str) -> None:
        if self.loaded == which:
            return
        from .models import LocalModel
        if self.model is not None:
            self.model.close()
            self.model = None
        spec = self.specs[which]
        self.log(f"loading {which}: {spec.name}")
        m = LocalModel(spec, n_ctx=8192, state_dir=self.state_dir)
        n_shared = len(m.tok(self.shared))
        m.set_prefix(SYSTEM, self.shared)
        self.log(f"  loaded in {m.load_seconds:.0f}s, shared prompt {n_shared} tokens")
        self.model, self.loaded = m, which

    def answer(self, asks: Iterable[Ask]) -> None:
        todo = {}
        for a in asks:
            todo.setdefault(answer_key(self.name(a.model), a.campaign, a.role, a.options), a)
        for which in ("judge", "checker"):
            batch = [a for a in todo.values() if a.model == which]
            if not batch:
                continue
            self._load(which)
            batch.sort(key=lambda a: a.campaign)            # reuse each campaign's cached text
            for a in batch:
                if self.specs[which].style == "letters":
                    probs = self.model.letter_probs(campaign_block(a.campaign), QUESTION.format(role=a.role),
                                                    a.options, letters_block)
                else:
                    probs = self.model.cloze_probs(campaign_block(a.campaign), QUESTION.format(role=a.role),
                                                   a.options, names_block)
                self.store.put(answer_key(self.name(which), a.campaign, a.role, a.options),
                               self.name(which), a.campaign, a.role, a.options, probs)
                self.store.save()                                # resumable after a restart
            self.log(f"  {which}: {len(batch)} questions answered ({self.model.seconds:.0f}s so far)")

    def close(self) -> None:
        if self.model is not None:
            self.model.close()
            self.model = None
            self.loaded = None


def decide_many(campaigns: dict[str, str], store: AnswerStore, use_checker: bool = True,
                run_models: bool = True, state_dir: Path | None = None, log=print,
                runner: "ModelRunner | None" = None) -> dict[str, Committee]:
    """Decide several committees together, so each model is loaded as few times as possible.
    campaigns maps a name to its campaign text. Without run_models, only stored answers are used."""
    from .models import CHECKER, JUDGE
    own = runner is None and run_models
    runner = runner or (ModelRunner(store, state_dir, log) if run_models else None)

    def lookup(a: Ask):
        name = (JUDGE if a.model == "judge" else CHECKER).name
        return store.get(answer_key(name, a.campaign, a.role, a.options))

    try:
        while True:
            out = {k: assemble(t, lookup, use_checker=use_checker, judge_name=JUDGE.name,
                               checker_name=CHECKER.name) for k, t in campaigns.items()}
            missing = [a for c in out.values() for a in c.missing]
            if not missing or runner is None:
                return out
            # Answer the loaded model's questions first, to avoid swapping models.
            first = runner.loaded if runner.loaded in {a.model for a in missing} else missing[0].model
            runner.answer([a for a in missing if a.model == first])
    finally:
        if own and runner is not None:
            runner.close()


def decide_committee(campaign_file: str | Path, store_path: str | Path | None = DEFAULT_STORE,
                     run_models: bool = True, state_dir: Path | None = None, log=print) -> Committee:
    text = read_campaign(campaign_file)
    store = AnswerStore(store_path)
    return decide_many({"campaign": text}, store, run_models=run_models, state_dir=state_dir, log=log)["campaign"]
