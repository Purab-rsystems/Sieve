"""Integration on the real model answers recorded by the benchmark run
(benchmarks/committee/answers.json). Every campaign whose answers are complete
is re-assembled offline and held to the committee rules. Campaigns still being
answered are skipped, so this runs while the accuracy run is in progress.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from sieve.committee.decide import AnswerStore, decide_many
from sieve.committee.reference import by_title, load_rules

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "benchmarks" / "committee" / "answers.json"
sys.path.insert(0, str(ROOT / "benchmarks" / "committee"))
import run_benchmark as rb  # noqa: E402

CAMPAIGNS = {c["id"]: rb.campaign_of(c) for c in rb.REF["campaigns"]}
RULES = load_rules()


@pytest.fixture(scope="module")
def recorded():
    if not ANSWERS.exists():
        pytest.skip("no recorded answers yet")
    store = AnswerStore(ANSWERS)
    both = decide_many(CAMPAIGNS, store, run_models=False)
    judge_only = decide_many(CAMPAIGNS, store, use_checker=False, run_models=False)
    return store, both, judge_only


def test_every_recorded_answer_is_a_distribution(recorded):
    store, _, _ = recorded
    assert store.data
    for row in store.data.values():
        p = row["probs"]
        assert set(p) == set(row["options"])
        assert math.isclose(sum(p.values()), 1.0, abs_tol=1e-3)
        assert all(0.0 <= v <= 1.0 for v in p.values())
        assert "@" not in row["campaign"]                       # no contact ever reached a prompt


@pytest.mark.parametrize("cid", list(CAMPAIGNS))
def test_committee_rules_hold_on_real_answers(recorded, cid):
    _, both, _ = recorded
    c = both[cid]
    if not c.complete:
        pytest.skip(f"{cid}: answers still being recorded")
    titles = c.titles()
    assert len(titles) == len(set(titles)), "one title, one role"
    ref = by_title()
    for m in c.members:
        assert m["title"] in ref and ref[m["title"]].function == m["function"]
        assert m["judge_p"] > m["chance"], "judge proposed it above chance"
        assert m["checker_p"] is not None and m["checker_p"] > m["chance"], "checker passed it"
    for role, need in RULES["minimums"].items():
        have = sum(1 for m in c.members if m["role"] == role)
        assert have >= need or c.unfilled.get(role) == need - have, f"{role}: short and not reported"
    per_role = {}
    for m in c.members:
        key = (m["role"], m["function"])
        assert key not in per_role, "a function supplies at most one title per role"
        per_role[key] = m["title"]
    for r in c.rejected:
        assert r["checker_p"] <= r["chance"]


@pytest.mark.parametrize("cid", list(CAMPAIGNS))
def test_judge_only_baseline_is_the_judge_unchecked(recorded, cid):
    """The baseline really is the judge alone: no checker answer is read and its
    first seat per role is the judge's own top pick."""
    _, _, judge_only = recorded
    c = judge_only[cid]
    if not c.complete:
        pytest.skip(f"{cid}: answers still being recorded")
    assert not c.rejected and c.checker == "none"
    assert all(m["checker_p"] is None for m in c.members)
    for role in RULES["seat_order"]:
        first = [m for m in c.members if m["role"] == role and m["round"] == 0 and not m["extra"]]
        if first and role != "Influencer":
            assert first[0]["judge_p"] == max(m["judge_p"] for m in first)
