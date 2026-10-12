"""Integration: campaign file -> committee (CLI, answer store) -> contact match,
and the benchmark grader. Models are not loaded: the answer store is seeded with
scripted answers, exactly as `--offline` reads recorded ones.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from sieve.committee.campaign import campaign_text
from sieve.committee.decide import (AnswerStore, Ask, answer_key, assemble, decide_committee,
                                    decide_many)
from sieve.committee.match import match_contacts
from sieve.committee.models import CHECKER, JUDGE
from sieve.committee.reference import by_title

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "config" / "icp_adtech_modernization.yaml"
WORKBOOK = ROOT / "sample-input" / "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx"

JUDGE_TABLE = {"Economic Buyer": {"Finance": 0.9, "Executive": 0.1},
               "Champion": {"Technology": 0.6, "IT": 0.4},
               "Technical Evaluator": {"Architecture": 0.7, "Technology": 0.3},
               "Influencer": {"Product": 0.5, "Platform": 0.5},
               "User": {"Operations": 1.0}, "Ratifier": {"Procurement": 1.0}}
CHECKER_TABLE = {**JUDGE_TABLE, "Champion": {"Technology": 1.0}}       # rejects IT as Champion


def dist(table, a: Ask):
    w = {f: table.get(a.role, {}).get(f, 0.0) for f in a.options}
    if not any(w.values()):
        return {f: 1.0 / len(a.options) for f in a.options}
    s = sum(w.values())
    return {f: v / s for f, v in w.items()}


def seed_store(path: Path, campaigns: list[str]) -> AnswerStore:
    """Fill a store with every answer the assembly asks for, as the models would."""
    store = AnswerStore(path)

    def name(a):
        return (JUDGE if a.model == "judge" else CHECKER).name

    configs = [dict(use_checker=False, candidates_per_question=n) for n in (3, 4, 5)] + \
        [dict(use_checker=True, check=chk, candidates_per_question=n)
         for chk in ("same_question", "yes_no") for n in (3, 4, 5)]
    for cfg in configs:
        for text in campaigns:
            while True:
                c = assemble(text, lambda a: store.get(a.store_key(name(a))), **cfg)
                if c.complete:
                    break
                for a in c.missing:
                    table = JUDGE_TABLE if a.model == "judge" else CHECKER_TABLE
                    if a.kind == "brief":
                        probs, why = {}, f"{a.model}'s brief of {a.role}"
                    elif a.kind == "yesno":
                        yes = table.get(a.role, {}).get(a.function, 0) > 0
                        probs, why = {"Yes": float(yes), "No": float(not yes)}, ""
                    else:
                        probs, why = dist(table, a), ""
                    store.put(a.store_key(name(a)), name(a), a.campaign, a.role, a.options, probs, why)
    store.save()
    return store


@pytest.fixture()
def store_path(tmp_path):
    p = tmp_path / "answers.json"
    seed_store(p, [campaign_text(CAMPAIGN)])
    return p


def test_decide_from_campaign_file_offline(store_path):
    c = decide_committee(CAMPAIGN, store_path=store_path, run_models=False, log=lambda *_: None)
    assert c.complete
    roles = {m["title"]: m["role"] for m in c.members}
    assert roles["Chief Financial Officer"] == "Economic Buyer"
    assert roles["Chief Technology Officer"] == "Champion"
    assert roles["Chief Architect"] == "Technical Evaluator"
    assert "Chief Information Officer" not in roles or roles["Chief Information Officer"] != "Champion"
    # IT, Qwen's second Champion answer, was turned down by the checker, then offered to the
    # other roles (yes/no) and turned down there too
    assert any(r["function"] == "IT" and r["role"] == "Champion" and r["round"] == 1 for r in c.rejected)
    assert {r["role"] for r in c.rejected if r["function"] == "IT" and r.get("offered_from") == "Champion"} == \
        {"Economic Buyer", "Technical Evaluator", "Influencer"}
    assert all(m["function"] != "IT" for m in c.members)
    assert sum(1 for m in c.members if m["role"] == "Influencer") >= 3
    assert not c.unfilled
    assert len(c.titles()) == len(set(c.titles()))


def test_offline_with_no_answers_reports_missing(tmp_path):
    c = decide_committee(CAMPAIGN, store_path=tmp_path / "empty.json", run_models=False, log=lambda *_: None)
    assert not c.complete and not c.members


def test_decide_many_matches_single_decisions(tmp_path):
    texts = {"a": campaign_text(CAMPAIGN),
             "b": campaign_text({"offering": {"offering": "Cloud HR and payroll software"}})}
    p = tmp_path / "s.json"
    seed_store(p, list(texts.values()))
    many = decide_many(texts, AnswerStore(p), run_models=False)
    for k, t in texts.items():
        one = decide_many({k: t}, AnswerStore(p), run_models=False)[k]
        assert many[k].members == one.members


def test_judge_only_differs_only_where_checker_rejected(store_path):
    store = AnswerStore(store_path)
    text = campaign_text(CAMPAIGN)
    both = decide_many({"x": text}, store, run_models=False)["x"]
    judge_only = decide_many({"x": text}, store, use_checker=False, run_models=False)["x"]
    assert judge_only.checker == "none" and not judge_only.rejected
    jo = {m["title"]: m["role"] for m in judge_only.members}
    assert jo["Chief Technology Officer"] == "Champion"          # first IT pick would also pass; Technology ranks first


# The CLI runs with the real environment (Windows needs USERPROFILE etc.), plus src on the path.
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}


def test_cli_decide_then_match(store_path, tmp_path):
    out = tmp_path / "committee.json"
    env_py = [sys.executable, "-m", "sieve.cli"]
    r = subprocess.run(env_py + ["committee", "decide", "--campaign", str(CAMPAIGN), "--out", str(out),
                                 "--answers", str(store_path), "--offline"],
                       cwd=ROOT, capture_output=True, text=True,
                       env=ENV)
    assert r.returncode == 0, r.stderr + r.stdout
    committee = json.loads(out.read_text())
    assert committee["seat_counts"]["Economic Buyer"] >= 1
    r = subprocess.run(env_py + ["committee", "match", "--committee", str(out), "--in", str(WORKBOOK),
                                 "--out", str(tmp_path / "rows.csv"), "--accounts-out", str(tmp_path / "acc.csv")],
                       cwd=ROOT, capture_output=True, text=True,
                       env=ENV)
    assert r.returncode == 0, r.stderr + r.stdout
    rows = pd.read_csv(tmp_path / "rows.csv", keep_default_na=False)
    acc = pd.read_csv(tmp_path / "acc.csv", keep_default_na=False)
    assert len(rows) == 424 and {"sv_decided_title", "sv_decided_role"} <= set(rows.columns)
    assert len(acc) == rows["Website URL"].nunique()
    # every seated contact's title really holds its seat's title
    from sieve.committee.reference import holds
    seated = rows[rows["sv_decided_title"] != ""]
    assert len(seated) > 0
    assert all(t in holds(raw) for raw, t in zip(seated["Title"], seated["sv_decided_title"]))


def test_match_real_workbook_all_regions(store_path):
    c = decide_committee(CAMPAIGN, store_path=store_path, run_models=False, log=lambda *_: None)
    frames = [pd.read_excel(WORKBOOK, sheet_name=s, keep_default_na=False)[["Title", "Company"]]
              for s in ("NA", "EU", "APAC")]
    rows, acc = match_contacts(pd.concat(frames, ignore_index=True), c.to_dict(), account_col="Company")
    assert len(rows) == 633
    seat_cols = [col for col in acc.columns if col.startswith("seats_")]
    assert (acc[seat_cols].sum(axis=1) <= acc["contacts"]).all()
    assert acc["minimums_met"].dtype == bool


# ---------------------------------------------------------------- benchmark grader
def test_benchmark_grader():
    sys.path.insert(0, str(ROOT / "benchmarks" / "committee"))
    import run_benchmark as rb

    class C:
        def __init__(self, titles):
            self.members = [{"title": t, "role": "Influencer", "extra": False} for t in titles]
            self.unfilled = {}

        def titles(self):
            return [m["title"] for m in self.members]

    ref = {c["id"]: c for c in rb.REF["campaigns"]}
    g = rb.grade(ref["data_analytics"], C(["Chief Information Officer", "Chief Data Officer",
                                            "Chief Financial Officer", "Chief Marketing Officer"]))
    assert g["recall"] == 1.0 and g["precision"] == 0.75 and g["wrong"] == ["Chief Marketing Officer"]
    g = rb.grade(ref["legacy_modernization"], C(["Chief Technology Officer", "Chief Architect"]))
    assert g["team_picks_held"] == "2 of 8"
    assert set(by_title()) >= set(ref["legacy_modernization"]["team_picks"])
    assert len(rb.REF["campaigns"]) == 10
    texts = {c["id"]: rb.campaign_of(c) for c in rb.REF["campaigns"]}
    assert len(set(texts.values())) == 10


def test_benchmark_end_to_end_offline(tmp_path, monkeypatch, capsys):
    """The full accuracy script on a seeded store: every campaign decided twice
    (judge + checker, judge only), graded, means and table written."""
    sys.path.insert(0, str(ROOT / "benchmarks" / "committee"))
    import run_benchmark as rb
    from sieve.committee.campaign import EMPTY_CAMPAIGN

    texts = [rb.campaign_of(c) for c in rb.REF["campaigns"]] + [EMPTY_CAMPAIGN]
    answers = tmp_path / "answers.json"
    seed_store(answers, texts)
    out = tmp_path / "results.json"
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--offline", "--answers", str(answers), "--out", str(out)])
    rb.main()
    res = json.loads(out.read_text())
    assert len(res["campaigns"]) == 10
    assert set(res["methods"]) == {f"{c}/{n}" for c in ("judge_only", "same_question", "yes_no") for n in (3, 4, 5)}
    for m in res["methods"].values():
        for split in ("dev", "test", "all"):
            assert 0.0 <= m[split]["f1"] <= 1.0
    for chk in ("judge_only", "same_question", "yes_no"):
        assert set(res["leave_one_out"][chk]["chosen_per_held_out_campaign"].values()) <= {3, 4, 5}
    lm = res["campaigns"]["legacy_modernization"]["same_question/5"]
    assert lm["team_picks_held"].endswith("of 8")
    assert res["before"]["all"]["f1"] == 0.77                 # the recorded method before the fixes
    out = capsys.readouterr().out
    assert "before (6 seats)" in out and "leave-one-out" in out


def test_saved_answers_need_no_ollama(store_path, monkeypatch):
    """A committee whose answers are all saved is decided even with Ollama down."""
    from sieve.committee import models
    monkeypatch.setattr(models, "OLLAMA_URL", "http://127.0.0.1:9")                 # nothing listens here
    monkeypatch.setattr(models, "check_ollama", lambda *a, **k: ["Ollama is not running"])
    c = decide_many({"x": campaign_text(CAMPAIGN)}, AnswerStore(store_path), run_models=True)["x"]
    assert c.complete and c.members
