"""Picking people at Tier A companies, and generating the committee list:
the rules, with scripted answers (the live tests run the real models)."""

from __future__ import annotations

import pandas as pd

from sieve.committee.required import COUNTS, generate_list
from sieve.committee.targets import LETTERS, NOBODY, pick_for_account, seats_of

COMMITTEE = {"members": [
    {"title": "Chief Information Officer", "function": "IT", "role": "Technical Evaluator", "extra": False},
    {"title": "Chief Executive Officer", "function": "Executive", "role": "Economic Buyer", "extra": False},
    {"title": "Chief Architect", "function": "Architecture", "role": "Champion", "extra": False},
    {"title": "Chief Technology Officer", "function": "Technology", "role": "Influencer", "extra": False}]}
COMPANY = {"account_id": "a1", "company": "Acme", "website": "acme.com", "score": 90, "tier": "Tier A"}


def contacts(*titles):
    return [{"contact_id": f"c{i}", "name": f"Person {i}", "title": t} for i, t in enumerate(titles)]


def letter_of(prompt, title):
    for line in prompt.splitlines():
        if line[:2] in {f"{l})" for l in LETTERS} and line.endswith("- " + title):
            return line[0]
    return "Z"


def scripted(judge_wants, checker_rejects=()):
    """judge_wants: seat title -> contact title it picks (or None = nobody)."""
    def answers(a):
        seat = next(t for t in judge_wants if f"committee's {t} (" in a.prompt)
        want = judge_wants[seat]
        letter = letter_of(a.prompt, want) if want else "Z"
        if a.model == "checker" and (seat, want) in checker_rejects:
            letter = "Z"
        return {"probs": {o: (1.0 if o == letter else 0.0) for o in a.options},
                "reasoning": f"{a.model}: {seat} -> {want}"}
    return answers


def test_seats_follow_the_committee_seat_order():
    assert [s["role"] for s in seats_of(COMMITTEE)] == ["Economic Buyer", "Champion", "Technical Evaluator", "Influencer"]


def test_each_seat_gets_the_judges_pick_with_both_reasonings():
    cs = contacts("CEO", "Chief Architect", "CIO", "CTO", "Intern")
    t = pick_for_account(COMPANY, cs, COMMITTEE, "camp\n", scripted(
        {"Chief Executive Officer": "CEO", "Chief Architect": "Chief Architect",
         "Chief Information Officer": "CIO", "Chief Technology Officer": "CTO"}))
    assert {p["role"]: p["title"] for p in t.picks} == {"Economic Buyer": "CEO", "Champion": "Chief Architect",
                                                        "Technical Evaluator": "CIO", "Influencer": "CTO"}
    assert all(p["judge_reasoning"] and p["checker_reasoning"] for p in t.picks)
    assert not t.empty and not t.rejected


def test_nobody_fits_is_an_answer_and_nothing_is_forced():
    cs = contacts("CEO", "Office Manager")
    t = pick_for_account(COMPANY, cs, COMMITTEE, "camp\n", scripted(
        {"Chief Executive Officer": "CEO", "Chief Architect": None, "Chief Information Officer": None,
         "Chief Technology Officer": None}))
    assert [p["title"] for p in t.picks] == ["CEO"]
    assert {e["seat_title"] for e in t.empty} == {"Chief Architect", "Chief Information Officer", "Chief Technology Officer"}
    assert all(e["judge_reasoning"] for e in t.empty)


def test_one_person_one_seat_and_clashes_go_to_the_earlier_seat():
    cs = contacts("CEO", "CTO and Chief Architect", "VP Engineering")
    answers = scripted({"Chief Executive Officer": "CEO", "Chief Architect": "CTO and Chief Architect",
                        "Chief Information Officer": None, "Chief Technology Officer": "CTO and Chief Architect"})
    t = pick_for_account(COMPANY, cs, COMMITTEE, "camp\n", answers)
    ids = [p["contact_id"] for p in t.picks]
    assert len(ids) == len(set(ids))
    assert {p["role"]: p["title"] for p in t.picks}["Champion"] == "CTO and Chief Architect"   # Champion before Influencer


def test_checker_rejection_takes_the_person_off_and_asks_again():
    cs = contacts("CEO", "President", "CIO")
    answers = scripted({"Chief Executive Officer": "CEO", "Chief Architect": None,
                        "Chief Information Officer": "CIO", "Chief Technology Officer": None},
                       checker_rejects={("Chief Executive Officer", "CEO")})
    t = pick_for_account(COMPANY, cs, COMMITTEE, "camp\n", answers)
    assert any(r["title"] == "CEO" and r["role"] == "Economic Buyer" for r in t.rejected)
    assert all(p["title"] != "CEO" for p in t.picks if p["role"] == "Economic Buyer")


def test_more_than_25_contacts_are_all_considered():
    cs = contacts(*[f"Analyst {i}" for i in range(27)], "CEO")          # the CEO is number 28
    t = pick_for_account(COMPANY, cs, COMMITTEE, "camp\n", scripted(
        {"Chief Executive Officer": "CEO", "Chief Architect": None, "Chief Information Officer": None,
         "Chief Technology Officer": None}))
    assert [p["title"] for p in t.picks] == ["CEO"]


def test_missing_answers_are_listed_not_guessed():
    t = pick_for_account(COMPANY, contacts("CEO"), COMMITTEE, "camp\n", lambda a: None)
    assert t.missing and not t.picks


def test_generated_list_needs_both_models_and_falls_back_to_default():
    def answers(a):
        want = {"Champion": "2", "Economic Buyer": "1", "Technical Evaluator": "2", "Influencer": "4"}[a.role]
        judge_pick = want if want in a.options else a.options[-1]
        want = judge_pick
        if a.model == "checker" and a.role == "Influencer":                     # never agrees with the judge
            want = next(o for o in a.options if o != judge_pick)
        return {"probs": {o: (1.0 if o == want else 0.0) for o in a.options}, "reasoning": f"{a.model} {a.role}"}
    out, missing = generate_list("camp", answers)
    assert not missing
    assert {r: d["count"] for r, d in out.items()} == {"Champion": 2, "Economic Buyer": 1,
                                                        "Technical Evaluator": 2, "Influencer": 3}
    assert out["Influencer"]["source"].startswith("default") and out["Influencer"]["rejected"]
    assert out["Champion"]["source"] == "generated" and out["Champion"]["judge_reasoning"]
    assert set(COUNTS) == {"0", "1", "2", "3", "4", "5", "6"}
