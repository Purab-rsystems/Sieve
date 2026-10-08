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
For each role: "which function's leader would be the <role>?", with the
functions as the options. Each model first writes its reasoning, then its
answer is read from its own probabilities right after that reasoning (see
models.py). The reasoning is stored with the answer and shown on every pick
and every rejection.

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
INSTRUCTION = ("First explain your thinking in 2 to 4 sentences. Then give your answer on its own "
               "line as \"Answer: <function>\", using one of the options exactly as written.")


def question_text(role: str, options) -> str:
    return (QUESTION.format(role=role) + "\nOptions:\n" + "\n".join(f"- {o}" for o in options)
            + "\n" + INSTRUCTION)
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


# ---------------------------------------------------------------- answers
def answer_key(model: str, campaign: str, role: str, options: Iterable[str], ctx: str = "") -> str:
    h = hashlib.sha1(campaign.encode()).hexdigest()[:12]
    return f"{ctx}{model}|{h}|{role}|{','.join(sorted(options))}"


def ctx_of(rules: dict | None) -> str:
    """Saved answers are kept apart per role list: the same question with other role
    definitions in the prompt may get another answer. The default list keeps no prefix."""
    if rules is None:
        return ""
    text = shared_text(rules)
    if text == shared_text():
        return ""
    return "ctx:" + hashlib.sha1(text.encode()).hexdigest()[:10] + "|"


class AnswerStore:
    """Every model answer, saved to JSON so a decision reproduces without the models."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        """{"probs": {function: p}, "reasoning": str} or None."""
        return self.data.get(key)

    def put(self, key: str, model: str, campaign: str, role: str, options, probs,
            reasoning: str = "", answer_token: str = "") -> None:
        with self._lock:
            self.data[key] = {"model": model, "campaign": campaign, "role": role,
                              "options": sorted(options), "probs": probs,
                              "reasoning": reasoning, "answer_token": answer_token}

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
             judge_name: str = "", checker_name: str = "", minimums: dict | None = None) -> Committee:
    rules = dict(rules or load_rules())
    if minimums is not None:                     # a list generated for this campaign replaces the default
        rules["minimums"] = {r: int(n) for r, n in minimums.items()}
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

    reasons: dict[tuple[str, str, tuple[str, ...]], str] = {}

    def ask(model: str, role: str, options: tuple[str, ...]) -> dict | None:
        a = Ask(model, campaign, role, options)
        got = answers(a)
        if got is None:
            c.missing.append(a)
            return None
        if "probs" in got:                              # a stored answer: probs + reasoning
            reasons[(model, role, options)] = got.get("reasoning", "")
            return got["probs"]
        return got

    def passes(role: str, f: str, options: tuple[str, ...]) -> tuple[bool | None, float | None]:
        if not use_checker:
            return True, None
        pc = ask("checker", role, options)
        if pc is None:
            return None, None
        return pc[f] > 1.0 / len(options), round(pc[f], 4)

    def why(role: str, options: tuple[str, ...]) -> dict:
        return {"judge_reasoning": reasons.get(("judge", role, options), ""),
                "checker_reasoning": reasons.get(("checker", role, options), "") if use_checker else ""}

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
                                       "chance": round(1.0 / len(options), 4), **why(asked_as, options)})
                    continue
                t = free_title(f)
                if t is None:
                    continue
                used.add(t.title)
                seated[role].add(f)
                c.members.append({"title": t.title, "function": f, "level": t.level, "role": role,
                                  "asked_as": asked_as, "round": rnd, "extra": False,
                                  "judge_p": round(p, 4), "checker_p": pc,
                                  "chance": round(1.0 / len(options), 4), **why(asked_as, options)})
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
                               "chance": round(1.0 / len(options), 4), **why(asked_as, options)})
            continue
        t = free_title(f)
        if t is None:
            continue
        used.add(t.title)
        c.members.append({"title": t.title, "function": f, "level": t.level, "role": role_of[asked_as],
                          "asked_as": asked_as, "round": 0, "extra": True,
                          "judge_p": round(p, 4), "checker_p": pc, "chance": round(1.0 / len(options), 4),
                          **why(asked_as, options)})
    return c


# ---------------------------------------------------------------- running the models
class ModelRunner:
    """Answers missing questions through Ollama, one model's batch at a time
    (so Ollama does not keep swapping the two models in and out of memory)."""

    def __init__(self, store: AnswerStore, log=print, progress=None, rules: dict | None = None) -> None:
        from .models import CHECKER, JUDGE, OllamaModel
        self.checked = False                # Ollama is checked only when a question is really asked
        self.specs = {"judge": JUDGE, "checker": CHECKER}
        judge, checker = OllamaModel(JUDGE), OllamaModel(CHECKER)
        judge.other, checker.other = checker, judge
        self.models = {"judge": judge, "checker": checker}
        self.store = store
        self.log = log
        self.progress = progress            # called with (answered_so_far, label) for the UI
        self.loaded: str | None = None
        self.shared = shared_text(rules)
        self.ctx = ctx_of(rules)
        self.answered = 0

    def name(self, which: str) -> str:
        return self.specs[which].name

    def key(self, a) -> str:
        if hasattr(a, "store_key"):
            return self.ctx + a.store_key(self.name(a.model))
        return answer_key(self.name(a.model), a.campaign, a.role, a.options, self.ctx)

    def user_text(self, a) -> str:
        if hasattr(a, "prompt_text"):
            return self.shared + a.prompt_text()
        return self.shared + campaign_block(a.campaign) + question_text(a.role, a.options)

    def lookup(self, a) -> dict | None:
        return self.store.get(self.key(a))

    def answer(self, asks: Iterable) -> None:
        if not self.checked:
            from .models import OllamaError, check_ollama
            problems = check_ollama()
            if problems:
                raise OllamaError("; ".join(problems))
            self.checked = True
        todo = {}
        for a in asks:
            todo.setdefault(self.key(a), a)
        for which in ("judge", "checker"):
            batch = [a for a in todo.values() if a.model == which]
            if not batch:
                continue
            self.loaded = which
            model = self.models[which]
            batch.sort(key=lambda a: a.campaign)            # similar prompts back to back: prompt cache
            for a in batch:
                what = getattr(a, "label", a.role)
                label = f"{'Qwen (judge)' if which == 'judge' else 'Llama (checker)'}: {what}"
                if self.progress:
                    self.progress(self.answered, label)
                got = model.ask(SYSTEM, self.user_text(a), a.options)
                self.store.put(self.key(a), self.name(which), a.campaign, a.role, a.options, got["probs"],
                               got["reasoning"], got["answer_token"])
                self.store.save()                                # resumable after a restart
                self.answered += 1
            self.log(f"  {which}: {len(batch)} questions answered ({model.seconds:.0f}s so far)")

    def close(self) -> None:
        pass


def decide_many(campaigns: dict[str, str], store: AnswerStore, use_checker: bool = True,
                run_models: bool = True, log=print, runner: "ModelRunner | None" = None,
                progress=None, minimums: dict | None = None, rules: dict | None = None) -> dict[str, Committee]:
    """Decide several committees together, batching each model's questions.
    campaigns maps a name to its campaign text. Without run_models, only stored answers are used."""
    from .models import CHECKER, JUDGE
    runner = runner or (ModelRunner(store, log, progress, rules=rules) if run_models else None)
    ctx = ctx_of(rules)

    def lookup(a: Ask):
        name = (JUDGE if a.model == "judge" else CHECKER).name
        return store.get(answer_key(name, a.campaign, a.role, a.options, ctx))

    while True:
        out = {k: assemble(t, lookup, rules=rules, use_checker=use_checker, judge_name=JUDGE.name,
                           checker_name=CHECKER.name, minimums=minimums) for k, t in campaigns.items()}
        missing = [a for c in out.values() for a in c.missing]
        if not missing or runner is None:
            return out
        # The model used last answers first, to avoid swapping models.
        first = runner.loaded if runner.loaded in {a.model for a in missing} else missing[0].model
        runner.answer([a for a in missing if a.model == first])


def decide_committee(campaign_file: str | Path | dict, store_path: str | Path | None = DEFAULT_STORE,
                     run_models: bool = True, log=print, progress=None) -> Committee:
    text = read_campaign(campaign_file)
    store = AnswerStore(store_path)
    return decide_many({"campaign": text}, store, run_models=run_models, log=log,
                       progress=progress)["campaign"]


def decide_targets(roster, committee: dict, campaign: str, store: AnswerStore, run_models: bool = True,
                   log=print, progress=None, runner: "ModelRunner | None" = None, use_checker: bool = True,
                   rules: dict | None = None):
    """Pick the contacts to target at each Tier A company (see targets.py), batching
    each model's questions across every company and seat."""
    from .models import CHECKER, JUDGE
    from .targets import pick_key, pick_targets
    runner = runner or (ModelRunner(store, log, progress, rules=rules) if run_models else None)
    ctx = ctx_of(rules)

    def lookup(a):
        return store.get(ctx + pick_key((JUDGE if a.model == "judge" else CHECKER).name, a))

    while True:
        out = pick_targets(roster, committee, campaign, lookup, use_checker=use_checker)
        missing = [a for t in out.values() for a in t.missing]
        if not missing or runner is None:
            return out
        first = runner.loaded if runner.loaded in {a.model for a in missing} else missing[0].model
        runner.answer([a for a in missing if a.model == first])


def decide_list(campaign: str, store: AnswerStore, run_models: bool = True, log=print, progress=None,
                runner: "ModelRunner | None" = None, use_checker: bool = True,
                exclude: dict | None = None, roles: list | None = None, rules: dict | None = None) -> dict | None:
    """The committee list generated for this campaign (see required.py)."""
    from .models import CHECKER, JUDGE
    from .required import generate_list
    runner = runner or (ModelRunner(store, log, progress, rules=rules) if run_models else None)
    ctx = ctx_of(rules)

    def lookup(a):
        return store.get(ctx + a.store_key((JUDGE if a.model == "judge" else CHECKER).name))

    while True:
        out, missing = generate_list(campaign, lookup, use_checker=use_checker, exclude=exclude, roles=roles,
                                     rules=rules)
        if not missing or runner is None:
            return out
        first = runner.loaded if runner.loaded in {a.model for a in missing} else missing[0].model
        runner.answer([a for a in missing if a.model == first])
