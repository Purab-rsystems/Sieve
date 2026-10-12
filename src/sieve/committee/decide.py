"""Decide the buying committee for a campaign: Qwen judges, Llama checks.

    committee = decide_committee("config/icp_adtech_modernization.yaml")

What the models read
--------------------
The buying-group roles in their sources' own words (rules.yaml), the role
reference sheet (every function at a target company with its real job titles
and their published descriptions), and the campaign: what is sold and to whom.
Never an account, a contact or a contact's title.

Steps
-----
1. Role briefs. Each model writes its own campaign-specific description of
   every role, from the sourced definition, the reference sheet and the
   campaign. Each model's questions then carry its own brief: both are
   anchored to the same sourced text, and neither reads the other's words.
2. Qwen's candidates. For every role question (Economic Buyer, Champion,
   Technical Evaluator, Influencer, and Forrester's User and Ratifier, which
   fill Influencer seats) Qwen is asked "which function's leader would be the
   <role>?" up to `candidates_per_question` times, each time with its earlier
   answers taken off the options. One option is always "None" (no other
   function fits). A question stops at "None", or when Qwen's answer is not
   above chance. Each answer: reasoning first, then the answer read from its
   probabilities right after that reasoning (see models.py).
3. Llama checks every candidate it is given:
     check: same_question   Llama answers the same question; it passes a
                            function it also rates above chance.
     check: yes_no          Llama is asked "should the leader of <function>
                            be a <role>?"; it passes when Yes is above 0.5.
   A function Llama rejects for one role is offered to the other roles with
   Llama's yes/no question (roles still short of their minimum first).
4. Seats. Minimums are floors: first every role is filled to its minimum from
   its candidates in Qwen's order; then the remaining passed candidates join
   as extra members, round-robin across the questions, up to `max_members`.
   One title, one role: a function can supply more than one title (CIO, then
   VP of IT) but only one per role.
5. A minimum nothing passes stays unfilled and is reported. Nothing is forced in.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
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


BRIEF = ("The buying-group role \"{role}\", as its source defines it: {text}\n\n"
         "Task: Using that definition, the campaign and the published job descriptions above, write 2 to 3 "
         "sentences on what the {role} does in a buying group for this particular offering: what stake or "
         "responsibility makes someone the {role} here, and which kinds of leaders usually hold it for an "
         "offering like this.")
YES_NO_QUESTION = ("Question: At a target company buying this offering, should the leader of the {function} "
                   "function be a {role} in the buying group?\nOptions:\n- Yes\n- No\n"
                   "First explain your thinking in 2 to 4 sentences. Then give your answer on its own line as "
                   "\"Answer: Yes\" or \"Answer: No\".")


def question_text(role: str, options) -> str:
    lines = [f"- {o}" if o != NONE else f"- None (no listed function's leader would be the {role})"
             for o in options]
    return QUESTION.format(role=role) + "\nOptions:\n" + "\n".join(lines) + "\n" + INSTRUCTION
BRIEF_TOKENS = 160
PARALLEL = int(__import__("os").getenv("SIEVE_PARALLEL", "1"))   # questions sent to Ollama at once (match OLLAMA_NUM_PARALLEL)
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
            reasoning: str = "", answer_token: str = "", seconds: float | None = None, kind: str = "") -> None:
        with self._lock:
            self.data[key] = {"model": model, "campaign": campaign, "role": role,
                              "options": sorted(options), "probs": probs,
                              "reasoning": reasoning, "answer_token": answer_token}
            if seconds is not None:
                self.data[key]["seconds"] = seconds
            if kind:
                self.data[key]["kind"] = kind

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=0, sort_keys=True))
            tmp.replace(self.path)


NONE = "None"                  # "no other function's leader would be the <role>"
YES_NO = ("Yes", "No")


def _h(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:n]


@dataclass(frozen=True)
class Ask:
    model: str            # "judge" | "checker"
    campaign: str
    role: str
    options: tuple[str, ...]
    brief: str = ""       # this model's campaign-specific description of the role
    kind: str = "pick"    # "pick" | "brief" | "yesno"
    function: str = ""    # yesno: the function asked about
    role_text: str = ""   # brief: the role's sourced definition

    @property
    def label(self) -> str:
        if self.kind == "brief":
            return f"describing the {self.role} for this campaign"
        if self.kind == "yesno":
            return f"is {self.function} a {self.role}?"
        return self.role

    def store_key(self, model_name: str) -> str:
        h = _h(self.campaign, 12)
        if self.kind == "brief":
            return f"{model_name}|{h}|brief|{self.role}|{_h(self.role_text)}"
        if self.kind == "yesno":
            return f"{model_name}|{h}|yesno|{self.role}|{self.function}|b:{_h(self.brief)}"
        key = answer_key(model_name, self.campaign, self.role, self.options)
        return key + (f"|b:{_h(self.brief)}" if self.brief else "")

    def prompt_text(self) -> str:
        head = campaign_block(self.campaign)
        if self.kind == "brief":
            return head + BRIEF.format(role=self.role, text=self.role_text)
        brief = f"This campaign's {self.role}, as you described it: {self.brief}\n\n" if self.brief else ""
        if self.kind == "yesno":
            return head + brief + YES_NO_QUESTION.format(role=self.role, function=self.function)
        return head + brief + question_text(self.role, self.options)


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
    briefs: dict[str, dict[str, str]] = field(default_factory=dict)    # model -> role -> brief
    candidates: dict[str, list[dict]] = field(default_factory=dict)   # question -> Qwen's answers in order
    settings: dict = field(default_factory=dict)

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
             judge_name: str = "", checker_name: str = "", minimums: dict | None = None,
             check: str | None = None, candidates_per_question: int | None = None,
             max_members: int | None = None) -> Committee:
    """The committee from the models' answers (see the module docstring).
    answers(ask) returns {"probs", "reasoning"} (or bare probs), the brief's text, or None
    when the question has not been answered yet; unanswered questions are listed in
    committee.missing in the order the models need them."""
    rules = dict(rules or load_rules())
    if minimums is not None:                     # a list generated for this campaign replaces the default
        rules["minimums"] = {r: int(n) for r, n in minimums.items()}
    check = check or rules.get("check", "same_question")
    per_q = int(candidates_per_question or rules.get("candidates_per_question", 5))
    cap = int(max_members or rules.get("max_members", 10))
    funcs = functions(reference)
    all_functions = sorted(funcs)
    minimums = rules["minimums"]
    order = [r for r in rules["seat_order"] if r in minimums] + [r for r in minimums if r not in rules["seat_order"]]
    feeds = {r: [r] for r in minimums}
    for extra, v in rules.get("also_asked", {}).items():
        if v.get("fills") in feeds:
            feeds[v["fills"]].append(extra)
    role_text = {r: " ".join(v["text"].split()) for r, v in {**rules["roles"], **rules.get("also_asked", {})}.items()}
    questions = [(role, asked) for role in order for asked in feeds[role]]
    role_of = {asked: role for role, asked in questions}

    c = Committee(campaign=campaign, judge=judge_name, checker=checker_name if use_checker else "none",
                  settings={"check": check if use_checker else "none", "candidates_per_question": per_q,
                            "max_members": cap})
    models = ("judge", "checker") if use_checker else ("judge",)

    def ask(a: Ask):
        got = answers(a)
        if got is None:
            c.missing.append(a)
            return None
        if a.kind == "brief":
            return got if isinstance(got, str) else got.get("reasoning", "")
        if isinstance(got, dict) and "probs" in got:
            return got["probs"], got.get("reasoning", "")
        return got, ""

    # 1. each model's own campaign-specific brief of every role it is asked about
    def briefs_of(model: str) -> bool:
        out = c.briefs.setdefault(model, {})
        for asked in [q for _, q in questions]:
            got = ask(Ask(model, campaign, asked, (), kind="brief", role_text=role_text.get(asked, asked)))
            if got is not None:
                out[asked] = got
        return not c.missing

    if not briefs_of("judge"):
        return c

    # 2. Qwen's candidates per question: asked again with its earlier answers removed
    for _, asked in questions:
        chain, proposed = [], []
        for k in range(per_q):
            opts = tuple(f for f in all_functions if f not in proposed) + (NONE,)
            if len(opts) < 2:
                break
            got = ask(Ask("judge", campaign, asked, opts, brief=c.briefs["judge"][asked]))
            if got is None:
                break
            probs, why = got
            top = max(probs, key=lambda o: (probs[o], o == NONE))
            chance = 1.0 / len(opts)
            chain.append({"k": k, "function": top, "judge_p": round(probs[top], 4), "chance": round(chance, 4),
                          "options": opts, "judge_reasoning": why,
                          "stop": top == NONE or probs[top] <= chance})
            if chain[-1]["stop"]:
                break
            proposed.append(top)
        c.candidates[asked] = chain
    if c.missing:
        return c
    if use_checker and not briefs_of("checker"):
        return c

    # 3 and 4. checks and seats
    used: set[str] = set()
    seated: dict[str, set[str]] = {r: set() for r in minimums}
    offered: set[tuple[str, str]] = set()

    def count(role: str) -> int:
        return sum(1 for m in c.members if m["role"] == role)

    def free_title(f: str) -> TitleDef | None:
        return next((t for t in funcs[f] if t.title not in used), None)

    def checked(asked: str, f: str, opts: tuple[str, ...], mode: str):
        """(passed, checker_p, chance, reasoning) or None while unanswered."""
        if not use_checker:
            return True, None, None, ""
        brief = c.briefs["checker"].get(asked, "")
        if mode == "yes_no":
            got = ask(Ask("checker", campaign, asked, YES_NO, brief=brief, kind="yesno", function=f))
            if got is None:
                return None
            probs, why = got
            return probs["Yes"] > 0.5, round(probs["Yes"], 4), 0.5, why
        got = ask(Ask("checker", campaign, asked, opts, brief=c.briefs["checker"][asked]))
        if got is None:
            return None
        probs, why = got
        return probs[f] > 1.0 / len(opts), round(probs[f], 4), round(1.0 / len(opts), 4), why

    def seat(f: str, role: str, asked: str, rec: dict) -> None:
        t = free_title(f)
        used.add(t.title)
        seated[role].add(f)
        c.members.append({"title": t.title, "function": f, "level": t.level, "role": role, "asked_as": asked,
                          "extra": count(role) >= minimums[role], **rec})

    def offer(f: str, from_role: str, from_asked: str) -> bool | None:
        """A function Llama rejected for one role, offered to the others (yes/no).
        True when seated, False when not, None while unanswered."""
        rest = [r for r in order if r != from_role]
        for role in sorted(rest, key=lambda r: count(r) >= minimums[r]):     # roles short of their minimum first
            if len(c.members) >= cap or f in seated[role] or not free_title(f) or (f, role) in offered:
                continue
            got = checked(role, f, (), "yes_no")
            if got is None:
                return None
            ok, cp, chance, why = got
            rec = {"round": None, "judge_p": None, "checker_p": cp, "chance": chance,
                   "offered_from": from_asked, "judge_reasoning": "", "checker_reasoning": why}
            offered.add((f, role))
            if ok:
                seat(f, role, role, rec)
                return True
            c.rejected.append({"role": role, "asked_as": role, "function": f, **rec})
        return False

    def consider(asked: str, cand: dict) -> bool:
        """False while an answer is missing."""
        role, f = role_of[asked], cand["function"]
        if cand["stop"] or f in seated[role] or not free_title(f) or len(c.members) >= cap:
            return True
        got = checked(asked, f, cand["options"], check)
        if got is None:
            return False
        ok, cp, chance, why = got
        rec = {"round": cand["k"], "judge_p": cand["judge_p"], "checker_p": cp,
               "chance": chance if check == "yes_no" else cand["chance"],
               "judge_reasoning": cand["judge_reasoning"], "checker_reasoning": why}
        if ok:
            seat(f, role, asked, rec)
            return True
        c.rejected.append({"role": role, "asked_as": asked, "function": f, **rec})
        return offer(f, role, asked) is not None

    done: dict[str, int] = {asked: 0 for _, asked in questions}       # candidates considered per question
    # minimums first, in seat order
    for role in order:
        for k in range(per_q):
            for asked in feeds[role]:
                if count(role) >= minimums[role] or len(c.members) >= cap:
                    break
                chain = c.candidates[asked]
                if k < len(chain):
                    if not consider(asked, chain[k]):
                        return c
                    done[asked] = k + 1
    # then the rest, round-robin across the questions, up to the cap
    for k in range(per_q):
        for _, asked in questions:
            chain = c.candidates[asked]
            if k >= done[asked] and k < len(chain) and len(c.members) < cap:
                if not consider(asked, chain[k]):
                    return c
    for role in order:
        short = minimums[role] - count(role)
        if short > 0:
            c.unfilled[role] = short
    for chain in c.candidates.values():
        for cand in chain:
            cand.pop("options", None)
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
        self.stage = ""                     # set by the caller, for the metrics
        self.stats = {"swaps": 0, "models": {}, "stages": {}}

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
            if self.loaded and self.loaded != which:
                self.stats["swaps"] += 1
            self.loaded = which
            model = self.models[which]
            batch.sort(key=lambda a: a.campaign)            # similar prompts back to back: prompt cache
            lock = threading.Lock()

            def one(a, which=which, model=model):
                what = getattr(a, "label", a.role)
                label = f"{'Qwen (judge)' if which == 'judge' else 'Llama (checker)'}: {what}"
                if self.progress:
                    self.progress(self.answered, label)
                t0 = time.time()
                if getattr(a, "kind", "") == "brief":
                    got = {"probs": {}, "reasoning": model.write(SYSTEM, self.user_text(a), BRIEF_TOKENS),
                           "answer_token": ""}
                else:
                    got = model.ask(SYSTEM, self.user_text(a), a.options)
                with lock:
                    self.store.put(self.key(a), self.name(which), a.campaign, a.role, a.options, got["probs"],
                                   got["reasoning"], got["answer_token"], seconds=round(time.time() - t0, 1),
                                   kind=getattr(a, "kind", "pick"))
                    self.store.save()                            # resumable after a restart
                    self.answered += 1
                    self._count(which, a, time.time() - t0)

            if PARALLEL > 1 and len(batch) > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(PARALLEL) as pool:
                    list(pool.map(one, batch))
            else:
                for a in batch:
                    one(a)
            self.log(f"  {which}: {len(batch)} questions answered ({model.seconds:.0f}s so far)")

    def _count(self, which: str, a, seconds: float) -> None:
        kind = getattr(a, "kind", None) or ("pick" if hasattr(a, "store_key") else "role")
        for bucket in (self.stats["models"].setdefault(self.name(which), {}),
                       self.stats["stages"].setdefault(self.stage or "-", {}).setdefault(self.name(which), {})):
            bucket["calls"] = bucket.get("calls", 0) + 1
            bucket["seconds"] = round(bucket.get("seconds", 0.0) + seconds, 1)
            kinds = bucket.setdefault("kinds", {})
            kinds[kind] = kinds.get(kind, 0) + 1

    def close(self) -> None:
        pass


def decide_many(campaigns: dict[str, str], store: AnswerStore, use_checker: bool = True,
                run_models: bool = True, log=print, runner: "ModelRunner | None" = None,
                progress=None, minimums: dict | None = None, rules: dict | None = None,
                **settings) -> dict[str, Committee]:
    """Decide several committees together, batching each model's questions.
    campaigns maps a name to its campaign text. Without run_models, only stored answers are used."""
    from .models import CHECKER, JUDGE
    runner = runner or (ModelRunner(store, log, progress, rules=rules) if run_models else None)
    ctx = ctx_of(rules)

    def lookup(a: Ask):
        return store.get(ctx + a.store_key((JUDGE if a.model == "judge" else CHECKER).name))

    while True:
        out = {k: assemble(t, lookup, rules=rules, use_checker=use_checker, judge_name=JUDGE.name,
                           checker_name=CHECKER.name, minimums=minimums, **settings)
               for k, t in campaigns.items()}
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
