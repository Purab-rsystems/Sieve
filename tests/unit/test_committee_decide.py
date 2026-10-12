"""Committee decided by judge + checker: assembly rules, with scripted answers.

The models are not loaded here; `answers` plays both of them. The live test
(test_committee_live.py) runs the real models.
"""

from __future__ import annotations

import pandas as pd
import pytest

from sieve.committee.campaign import EMPTY_CAMPAIGN, campaign_text
from sieve.committee.decide import AnswerStore, Ask, answer_key, assemble, shared_text
from sieve.committee.match import match_contacts
from sieve.committee.reference import (by_title, functions, holds, load_reference,
                                       load_rules)

FUNCS = sorted(functions())


def scripted(judge: dict, checker: dict | None = None, yes: dict | None = None):
    """judge/checker: {role: {function: weight}}, renormalised over the options
    actually offered ("None" gets weight only when the table gives it some).
    Without a checker table the checker agrees with the judge; a role with no
    weights answers uniformly (exactly chance: never a pick, never a pass).
    yes: {role: {function, ...}} the checker says Yes to on the yes/no question
    (default: the functions its own table weights for that role)."""
    def dist(table, a: Ask):
        w = {f: table.get(a.role, {}).get(f, 0.0) for f in a.options}
        if not any(w.values()):
            return {f: 1.0 / len(a.options) for f in a.options}
        tot = sum(w.values())
        return {f: v / tot for f, v in w.items()}

    def answers(a: Ask):
        table = judge if a.model == "judge" or checker is None else checker
        if a.kind == "brief":
            return f"{a.model}'s brief of {a.role}"
        if a.kind == "yesno":
            ok = a.function in (yes or {}).get(a.role, set()) if yes is not None \
                else table.get(a.role, {}).get(a.function, 0) > 0
            return {"Yes": 1.0 if ok else 0.0, "No": 0.0 if ok else 1.0}
        return dist(table, a)
    return answers


RULES = load_rules()


def roles_of(c):
    out = {}
    for m in c.members:
        out.setdefault(m["role"], []).append(m["title"])
    return out


def test_minimums_filled_and_one_title_one_role():
    judge = {"Economic Buyer": {"Technology": 0.9},
             "Champion": {"Technology": 0.9},
             "Technical Evaluator": {"Architecture": 0.9},
             "Influencer": {"Product": 0.5, "Platform": 0.5},
             "User": {"Operations": 0.9}}
    c = assemble("camp", scripted(judge))
    r = roles_of(c)
    assert r["Economic Buyer"] == ["Chief Technology Officer"]
    assert r["Champion"] == ["VP of Engineering"]           # same function, next title: never the CTO twice
    assert r["Technical Evaluator"] == ["Chief Architect"]
    assert len(r["Influencer"]) >= 3
    titles = c.titles()
    assert len(titles) == len(set(titles))
    assert not c.unfilled


def test_checker_must_pass_every_pick():
    judge = {"Economic Buyer": {"Finance": 0.6, "Executive": 0.4},
             "Champion": {"IT": 1.0}, "Technical Evaluator": {"IT": 1.0},
             "Influencer": {"Data": 0.4, "Security": 0.3, "Marketing": 0.3}}
    checker = {"Economic Buyer": {"Executive": 1.0},                 # rejects Finance
               "Champion": {"IT": 1.0}, "Technical Evaluator": {"IT": 1.0},
               "Influencer": {"Data": 0.5, "Security": 0.5}}          # rejects Marketing
    c = assemble("camp", scripted(judge, checker))
    assert roles_of(c)["Economic Buyer"] == ["Chief Executive Officer"]
    assert {"role": "Economic Buyer", "function": "Finance"}.items() <= c.rejected[0].items()
    assert all(m["function"] != "Marketing" for m in c.members)
    for m in c.members:
        assert m["checker_p"] > m["chance"]


def test_nothing_forced_in_when_checker_rejects_everything():
    judge = {r: {"IT": 1.0} for r in ("Economic Buyer", "Champion", "Technical Evaluator", "Influencer")}
    checker = {r: {"Legal": 1.0} for r in ("Economic Buyer", "Champion", "Technical Evaluator", "Influencer",
                                            "User", "Ratifier")}
    c = assemble("camp", scripted(judge, checker))
    assert all(m["function"] != "IT" for m in c.members)
    assert c.unfilled                                     # reported, not filled by force


def test_follow_up_after_rejection_removes_rejected_option():
    asked = []
    base = scripted({"Economic Buyer": {"Finance": 0.7, "Executive": 0.3}},
                    {"Economic Buyer": {"Executive": 1.0}})

    def answers(a):
        asked.append(a)
        return base(a)
    c = assemble("camp", answers)
    eb_judge = [a for a in asked if a.model == "judge" and a.role == "Economic Buyer" and a.kind == "pick"]
    assert "Finance" in eb_judge[0].options and "Finance" not in eb_judge[1].options
    assert roles_of(c)["Economic Buyer"] == ["Chief Executive Officer"]


def test_judge_only_mode_never_asks_the_checker():
    asked = []
    base = scripted({"Economic Buyer": {"Executive": 1.0}})

    def answers(a):
        asked.append(a)
        return base(a)
    assemble("camp", answers, use_checker=False)
    assert all(a.model == "judge" for a in asked)


def test_missing_answers_are_listed_not_guessed():
    c = assemble("camp", lambda a: None)
    assert not c.complete and c.members == []
    assert c.missing and c.missing[0].model == "judge"


def test_answer_store_round_trip(tmp_path):
    s = AnswerStore(tmp_path / "a.json")
    k = answer_key("m", "camp", "Champion", ("IT", "Data"))
    assert k == answer_key("m", "camp", "Champion", ("Data", "IT"))      # order-free key
    s.put(k, "m", "camp", "Champion", ("IT", "Data"), {"IT": 0.7, "Data": 0.3})
    s.save()
    assert AnswerStore(tmp_path / "a.json").get(k)["probs"] == {"IT": 0.7, "Data": 0.3}


def test_reasoning_travels_to_members_and_rejections():
    def answers(a):
        if a.kind == "brief":
            return f"{a.model} brief"
        if a.kind == "yesno":
            return {"probs": {"Yes": 0.0, "No": 1.0}, "reasoning": "no"}
        probs = {f: (1.0 if f == ("Finance" if a.model == "judge" or a.role != "Economic Buyer" else "Executive")
                     else 0.0) for f in a.options}
        if a.role == "Economic Buyer" and a.model == "judge" and "Finance" not in a.options:
            probs = {f: (1.0 if f == "Executive" else 0.0) for f in a.options}
        return {"probs": probs, "reasoning": f"{a.model} thinks about {a.role} among {len(a.options)}"}
    c = assemble("camp", answers)
    eb = [m for m in c.members if m["role"] == "Economic Buyer"][0]
    assert eb["function"] == "Executive"                          # Finance rejected, follow-up picked Executive
    assert eb["judge_reasoning"].startswith("judge thinks about Economic Buyer")
    assert eb["checker_reasoning"].startswith("checker thinks about Economic Buyer")
    rej = [r for r in c.rejected if r["function"] == "Finance" and r["role"] == "Economic Buyer"][0]
    assert rej["checker_reasoning"] and rej["judge_reasoning"]


def test_option_probs_from_first_tokens():
    from sieve.committee.models import option_probs
    import math
    top = [{"token": " Finance", "logprob": math.log(0.6)}, {"token": " IT", "logprob": math.log(0.2)},
           {"token": " P", "logprob": math.log(0.1)}, {"token": " The", "logprob": math.log(0.1)}]
    p = option_probs(top, ["Finance", "IT", "Product", "People", "Data"])
    assert abs(sum(p.values()) - 1) < 1e-9
    assert p["Finance"] > p["IT"] > p["Product"] == p["People"] > p["Data"] == 0


# ---------------------------------------------------------------- inputs the models see
def test_campaign_text_from_sieve_campaign_file_has_no_contacts():
    t = campaign_text("config/icp_adtech_modernization.yaml")
    assert t.startswith("Offering being sold: AdTech Platform Modernization")
    assert "ASP.NET" in t and "DSP/SSP" in t
    assert "@" not in t
    assert campaign_text({"offering": {"offering": "X", "verticals": ["A"]}}) == \
        "Offering being sold: X\nTarget companies: A"
    assert EMPTY_CAMPAIGN == "Offering being sold: N/A"


def test_shared_text_holds_every_title_and_role_definition():
    s = shared_text()
    for t in load_reference():
        assert f"- {t.title}:" in s
    for r in list(RULES["roles"]) + list(RULES["also_asked"]):
        assert f"- {r}:" in s


def test_rules_are_rays_minimums():
    assert RULES["minimums"] == {"Champion": 1, "Economic Buyer": 1, "Technical Evaluator": 1, "Influencer": 3}


# ---------------------------------------------------------------- reference sheet
def test_reference_sheet_sources():
    ref = load_reference()
    assert len(ref) == len({t.title for t in ref})
    for t in ref:
        if t.source.startswith(("AvaHR", "Workable")):
            assert t.url.startswith("https://") and t.summary and t.duties and not t.approximate
        elif t.source.startswith("O*NET"):
            assert t.approximate and t.summary
        else:
            assert t.source.startswith("none") and not t.summary


@pytest.mark.parametrize("raw,expected", [
    ("Chief Information Security Officer", ["Chief Information Security Officer"]),
    ("Chief Information Officer", ["Chief Information Officer"]),
    ("Assistant Vice President, IT", ["VP of IT"]),
    ("Executive Assistant to the CEO", []),
    ("Sr. Executive Assistant | CEO & COO", []),
    ("SVP, Engineering", ["VP of Engineering"]),
    ("VP Platform Engineering", ["VP of Platform Engineering"]),
    ("President & CEO", ["Chief Executive Officer", "President"]),
    ("Credit Controller", []),
    ("Product Marketing Director", ["Marketing Director"]),
    ("", []),
])
def test_holds(raw, expected):
    assert holds(raw) == expected


def test_match_contacts_reports_missing_seats():
    committee = {"members": [
        {"title": "Chief Technology Officer", "role": "Economic Buyer"},
        {"title": "VP of Engineering", "role": "Champion"},
        {"title": "Chief Architect", "role": "Technical Evaluator"},
        {"title": "Chief Product Officer", "role": "Influencer"},
        {"title": "VP of Product", "role": "Influencer"},
        {"title": "Chief Information Officer", "role": "Influencer"}]}
    df = pd.DataFrame({"Company": ["a", "a", "a", "b"],
                       "Title": ["CTO", "SVP Engineering", "Head of Sales", "Chief Architect"]})
    rows, accounts = match_contacts(df, committee)
    assert list(rows["sv_decided_role"]) == ["Economic Buyer", "Champion", "", "Technical Evaluator"]
    a = accounts.set_index("account")
    assert a.loc["a", "seats_Economic Buyer"] == 1 and not a.loc["a", "minimums_met"]
    assert "Technical Evaluator x1" in a.loc["a", "missing"] and "Influencer x3" in a.loc["a", "missing"]


# ---------------------------------------------------------------- candidates, floors, offers, briefs
def test_qwen_asked_again_without_earlier_answers_until_none():
    asked = []
    judge = {"Champion": {"Revenue": 0.5, "Marketing": 0.3, "None": 0.2}}
    base = scripted(judge)

    def answers(a):
        asked.append(a)
        return base(a)
    c = assemble("camp", answers)
    champ = [a for a in asked if a.model == "judge" and a.role == "Champion" and a.kind == "pick"]
    assert [("Revenue" in a.options, "Marketing" in a.options) for a in champ] == \
        [(True, True), (False, True), (False, False)]                 # third answer: None, stop
    assert [x["function"] for x in c.candidates["Champion"]] == ["Revenue", "Marketing", "None"]
    assert {m["function"] for m in c.members if m["role"] == "Champion"} == {"Revenue", "Marketing"}


def test_minimums_are_floors_extras_join_up_to_the_cap():
    judge = {"Economic Buyer": {"Finance": 0.5, "Executive": 0.4, "None": 0.1},
             "Champion": {"Revenue": 0.6, "Marketing": 0.4},
             "Technical Evaluator": {"IT": 1.0},
             "Influencer": {"Customer": 0.4, "Data": 0.3, "Security": 0.3},
             "Ratifier": {"Procurement": 1.0}}
    c = assemble("camp", scripted(judge))
    fs = [m["function"] for m in c.members]
    assert "Procurement" in fs                                   # never dropped once Influencer has 3
    assert len(c.members) <= 10
    assert sum(not m["extra"] for m in c.members) == 6
    c5 = assemble("camp", scripted(judge), max_members=7)
    assert len(c5.members) == 7 and not c5.unfilled


def test_cap_never_blocks_the_minimums():
    judge = {r: {f: 1.0 for f in ("Finance", "Executive", "Revenue", "Marketing", "IT")}
             for r in ("Economic Buyer", "Champion", "Technical Evaluator", "Influencer", "User", "Ratifier")}
    c = assemble("camp", scripted(judge), max_members=6)
    assert not c.unfilled and len(c.members) == 6


def test_rejected_function_is_offered_to_other_roles():
    judge = {"Champion": {"Revenue": 1.0}, "Economic Buyer": {"Finance": 1.0},
             "Technical Evaluator": {"IT": 1.0}}
    checker = {"Champion": {"IT": 1.0}, "Economic Buyer": {"Finance": 1.0}, "Technical Evaluator": {"IT": 1.0}}
    c = assemble("camp", scripted(judge, checker, yes={"Influencer": {"Revenue"}}))
    rej = [r for r in c.rejected if r["function"] == "Revenue" and r["role"] == "Champion"]
    assert rej
    inf = [m for m in c.members if m["function"] == "Revenue"]
    assert inf and inf[0]["role"] == "Influencer" and inf[0]["offered_from"] == "Champion"


def test_rejection_does_not_stop_the_question():
    judge = {"Champion": {"IT": 0.6, "Revenue": 0.4}}
    checker = {"Champion": {"Revenue": 1.0}}
    c = assemble("camp", scripted(judge, checker, yes={}))
    assert [m["function"] for m in c.members if m["role"] == "Champion"] == ["Revenue"]


def test_yes_no_check():
    judge = {"Economic Buyer": {"Finance": 1.0}, "Champion": {"Revenue": 0.6, "IT": 0.4}}
    c = assemble("camp", scripted(judge, yes={"Economic Buyer": {"Finance"}, "Champion": {"Revenue"}}),
                 check="yes_no")
    champ = [m for m in c.members if m["role"] == "Champion"]
    assert [m["function"] for m in champ] == ["Revenue"] and champ[0]["chance"] == 0.5
    assert any(r["function"] == "IT" and r["role"] == "Champion" for r in c.rejected)


def test_each_model_reads_its_own_brief():
    asked = []
    base = scripted({"Champion": {"Revenue": 1.0}})

    def answers(a):
        asked.append(a)
        return base(a)
    assemble("camp", answers)
    briefs = [a for a in asked if a.kind == "brief"]
    assert {a.model for a in briefs} == {"judge", "checker"}
    assert all(a.role_text for a in briefs)                     # written from the sourced definition
    for a in asked:
        if a.kind != "brief" and a.brief:
            assert a.brief == f"{a.model}'s brief of {a.role}"   # never the other model's words
    assert "Champions act as the key advocates" in briefs[0].prompt_text() or \
        any("Champions act as the key advocates" in b.prompt_text() for b in briefs)


def test_judge_finishes_before_checker_is_asked():
    seen = []

    def answers(a):
        seen.append(a.model)
        return None
    c = assemble("camp", answers)
    assert c.missing and {a.model for a in c.missing} == {"judge"}
