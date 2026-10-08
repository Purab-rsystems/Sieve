"""Measure the buying committee against the 10-campaign reference set.

    python benchmarks/committee/run_benchmark.py            # ask the models (answers saved)
    python benchmarks/committee/run_benchmark.py --offline  # recorded answers only

Per campaign in reference.yaml:
  precision  share of committee titles whose function the research names
             (finance and procurement count as fine everywhere)
  recall     share of the research's committee functions the committee holds
  F1         their harmonic mean
  unfilled   minimum seats nothing passed (reported, never forced)
Baselines, graded the same way:
  judge only  the same assembly with every judge pick passed (no checker)
  generic     the committee decided for an empty campaign ("Offering being
              sold: N/A"), applied to every campaign: what habit alone scores
For the Simplifi campaign: how many of the team's 8 picks the committee holds.

answers.json holds every model answer the run reads (with each model's written
reasoning), so the numbers reproduce without the models. The models run in
Ollama (`ollama pull qwen2.5:7b`, `ollama pull llama3.1:8b`).
answers_v1_llamacpp.json / results_v1_llamacpp.json are the earlier method
(no written reasoning, llama.cpp), kept for comparison.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sieve.committee.campaign import EMPTY_CAMPAIGN, campaign_text   # noqa: E402
from sieve.committee.decide import AnswerStore, ModelRunner, decide_many   # noqa: E402
from sieve.committee.qualify import qualify   # noqa: E402
from sieve.committee.reference import by_title, load_rules   # noqa: E402

REF = yaml.safe_load((HERE / "reference.yaml").read_text())
FINE = set(REF["also_fine_everywhere"])


def campaign_of(c: dict) -> str:
    if c.get("campaign_file"):
        return campaign_text(ROOT / c["campaign_file"])
    return campaign_text({"offering": {"offering": c["offering"], "verticals": c.get("verticals", [])}})


def grade(c: dict, committee) -> dict:
    fn = {t: d.function for t, d in by_title().items()}
    chosen = committee.titles()
    slots = [set(s) for s in c["slots"]]
    funcs = {fn[t] for t in chosen}
    met = sum(1 for s in slots if s & funcs)
    ok = set().union(*slots) | FINE
    right = sum(1 for t in chosen if fn[t] in ok)
    recall = met / len(slots)
    precision = right / len(chosen) if chosen else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    out = {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
           "size": len(chosen), "unfilled": dict(committee.unfilled),
           "qualified": qualify(committee.members, load_rules()["minimums"])["qualified"],
           "members": [f"{m['role']}: {m['title']}" + (" (extra)" if m["extra"] else "") for m in committee.members],
           "wrong": [t for t in chosen if fn[t] not in ok],
           "missed_slots": [sorted(s) for s in slots if not (s & funcs)]}
    if c.get("team_picks"):
        held = [t for t in c["team_picks"] if t in chosen]
        out["team_picks_held"] = f"{len(held)} of {len(c['team_picks'])}"
        out["team_picks_list"] = held
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use recorded answers only")
    ap.add_argument("--answers", default=str(HERE / "answers.json"))
    ap.add_argument("--out", default=str(HERE / "results.json"))
    a = ap.parse_args()

    t0 = time.time()
    store = AnswerStore(a.answers)
    texts = {c["id"]: campaign_of(c) for c in REF["campaigns"]}
    texts["_empty"] = EMPTY_CAMPAIGN
    runner = None if a.offline else ModelRunner(store, log=lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True))
    try:
        both = decide_many(texts, store, use_checker=True, run_models=not a.offline, runner=runner)
        judge_only = decide_many(texts, store, use_checker=False, run_models=not a.offline, runner=runner)
    finally:
        if runner:
            runner.close()

    incomplete = [k for k, c in {**both, **{k + " (judge only)": v for k, v in judge_only.items()}}.items()
                  if not c.complete]
    if incomplete:
        sys.exit(f"answers missing for {incomplete}; run without --offline")

    res = {"campaigns": {}, "generic_committee": both["_empty"].to_dict()}
    for c in REF["campaigns"]:
        cid = c["id"]
        res["campaigns"][cid] = {
            "split": c["split"], "campaign_text": texts[cid],
            "judge_and_checker": grade(c, both[cid]),
            "judge_only": grade(c, judge_only[cid]),
            "generic": grade(c, both["_empty"]),
            "committee": both[cid].to_dict(),
        }
    for split in ("dev", "test", "all"):
        ids = [c["id"] for c in REF["campaigns"] if split == "all" or c["split"] == split]
        res[f"{split}_mean"] = {m: {k: round(statistics.mean(res["campaigns"][i][m][k] for i in ids), 3)
                                    for k in ("precision", "recall", "f1")}
                                for m in ("judge_and_checker", "judge_only", "generic")}
    res["campaigns_with_unfilled_minimum"] = [cid for cid, r in res["campaigns"].items()
                                              if r["judge_and_checker"]["unfilled"]]
    res["committees_qualified_against_default_list"] = sum(r["judge_and_checker"]["qualified"]
                                                           for r in res["campaigns"].values())
    Path(a.out).write_text(json.dumps(res, indent=1))

    print(f"\n{'campaign':22s} {'split':5s} {'J+C P':>6s} {'R':>5s} {'F1':>5s} | {'judge-only F1':>13s} | {'generic F1':>10s} | unfilled")
    for cid, r in res["campaigns"].items():
        q = r["judge_and_checker"]
        print(f"{cid:22s} {r['split']:5s} {q['precision']:6.2f} {q['recall']:5.2f} {q['f1']:5.2f} | "
              f"{r['judge_only']['f1']:13.2f} | {r['generic']['f1']:10.2f} | {q['unfilled'] or '-'}")
    for split in ("dev", "test", "all"):
        m = res[f"{split}_mean"]
        print(f"{'mean ' + split:28s} {m['judge_and_checker']['precision']:6.2f} {m['judge_and_checker']['recall']:5.2f} "
              f"{m['judge_and_checker']['f1']:5.2f} | {m['judge_only']['f1']:13.2f} | {m['generic']['f1']:10.2f}")
    lm = res["campaigns"]["legacy_modernization"]["judge_and_checker"]
    print(f"\nSimplifi team picks held: {lm.get('team_picks_held')} {lm.get('team_picks_list')}")
    print(f"Committees that qualify against the default list: {res['committees_qualified_against_default_list']} of 10")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
