"""Generate the committee list for a campaign: how many people each role needs.

The default list is rules.yaml's minimums (1 Champion, 1 Economic Buyer,
1 Technical Evaluator, 3 Influencers). When asked to, Qwen (judge) and Llama
(checker) replace it for one campaign:

* for each role, the judge reads the role definitions and the campaign, writes
  its reasoning, then answers how many people in that role a buying group for
  this offering needs (0 to 6);
* the checker answers the same question; the number stands only if the checker
  also rates it above chance. A rejected number is taken off the options and
  the judge is asked again (follow-up rounds from rules.yaml);
* a role the two never agree on keeps its default number, and says so.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from .reference import load_rules

COUNTS = ("0", "1", "2", "3", "4", "5", "6")


@dataclass(frozen=True)
class CountAsk:
    model: str
    campaign: str
    role: str
    options: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"how many {self.role} seats"

    def store_key(self, model_name: str) -> str:
        h = hashlib.sha1(self.campaign.encode()).hexdigest()[:12]
        return f"{model_name}|count|{h}|{self.role}|{','.join(self.options)}"

    def prompt_text(self) -> str:
        return (f"A seller is running this B2B campaign:\n{self.campaign}\n\n"
                f"Question: At a target company buying this offering, how many people in the {self.role} role "
                f"should the buying group include?\nOptions: {', '.join(self.options)}\n"
                f"First explain your thinking in 2 to 4 sentences. Then give your answer on its own line as "
                f"\"Answer: <number>\", using one of the options.")


def generate_list(campaign: str, answers: Callable[[CountAsk], dict | None],
                  use_checker: bool = True, exclude: dict[str, set[str]] | None = None,
                  roles: list[str] | None = None, rules: dict | None = None) -> tuple[dict | None, list[CountAsk]]:
    """({role: detail}, missing asks). detail has count, source and both models' reasoning.
    exclude: numbers taken off a role's options (a retry after a failed first try).
    roles: only these roles (default: every role on the default list)."""
    rules = rules or load_rules()
    rounds = 1 + int(rules.get("follow_up_rounds", 0))
    exclude = exclude or {}
    out, missing = {}, []
    for role, default in rules["minimums"].items():
        if roles is not None and role not in roles:
            continue
        options = [c for c in COUNTS if c not in exclude.get(role, set())]
        rejected = []
        detail = None
        for _ in range(rounds):
            if len(options) < 2:
                break
            a = CountAsk("judge", campaign, role, tuple(options))
            j = answers(a)
            if j is None:
                missing.append(a)
                break
            pick = max(j["probs"], key=j["probs"].get)
            chance = 1 / len(options)
            base = {"count": int(pick), "judge_p": round(j["probs"][pick], 4), "chance": round(chance, 4),
                    "judge_reasoning": j.get("reasoning", "")}
            if not use_checker:
                detail = {**base, "source": "generated", "checker_p": None, "checker_reasoning": ""}
                break
            c = answers(CountAsk("checker", campaign, role, tuple(options)))
            if c is None:
                missing.append(CountAsk("checker", campaign, role, tuple(options)))
                break
            cp = c["probs"][pick]
            base.update(checker_p=round(cp, 4), checker_reasoning=c.get("reasoning", ""))
            if cp > chance:
                detail = {**base, "source": "generated"}
                break
            rejected.append(base)
            options.remove(pick)
        if missing:
            continue
        if detail is None:
            detail = {"count": int(default), "source": "default: the models did not agree", "judge_p": None,
                      "checker_p": None, "chance": None, "judge_reasoning": "", "checker_reasoning": ""}
        detail["rejected"] = rejected
        detail["default"] = int(default)
        out[role] = detail
    return (None if missing else out), missing
