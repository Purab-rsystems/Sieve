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


METRICS = ("precision", "recall", "f1", "size")
MIN_PRECISION = 0.85


def mean_of(rows: list[dict]) -> dict:
    return {m: round(statistics.mean(r[m] for r in rows), 3) for m in METRICS}


def leave_one_out(per_campaign: dict[str, dict[int, dict]]) -> dict:
    """Choose candidates_per_question on 9 campaigns (best F1 with precision >= 0.85,
    else best precision), grade the 10th with it; rotate through all 10."""
    ids = list(per_campaign)
    picks, held = {}, []
    for out in ids:
        rest = [i for i in ids if i != out]
        score = {}
        for n in per_campaign[out]:
            m = mean_of([per_campaign[i][n] for i in rest])
            score[n] = (m["precision"] >= MIN_PRECISION, m["f1"] if m["precision"] >= MIN_PRECISION else m["precision"])
        n = max(score, key=lambda x: (score[x], -x))
        picks[out] = n
        held.append(per_campaign[out][n])
    return {"chosen_per_held_out_campaign": picks, "held_out_mean": mean_of(held)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use recorded answers only")
    ap.add_argument("--answers", default=str(HERE / "answers.json"))
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--before", default=str(HERE / "results_before.json"),
                    help="results of the method before the recall fixes, for the side-by-side table")
    ap.add_argument("--checks", default="same_question,yes_no")
    ap.add_argument("--candidates", default="5,4,3")
    ap.add_argument("--campaigns", default="", help="comma-separated campaign ids (default: all 10)")
    a = ap.parse_args()

    t0 = time.time()
    store = AnswerStore(a.answers)
    if a.campaigns:
        keep = set(a.campaigns.split(","))
        REF["campaigns"] = [c for c in REF["campaigns"] if c["id"] in keep]
    texts = {c["id"]: campaign_of(c) for c in REF["campaigns"]}
    if not a.campaigns:
        texts["_empty"] = EMPTY_CAMPAIGN
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)   # noqa: E731
    runner = None if a.offline else ModelRunner(store, log=log)
    checks = a.checks.split(",")
    sizes = [int(x) for x in a.candidates.split(",")]
    runs = {}
    try:
        for n in sizes:                                   # judge-only first: Qwen's answers, no checker
            runs[("judge_only", n)] = decide_many(texts, store, use_checker=False, run_models=not a.offline,
                                                  runner=runner, candidates_per_question=n)
        for chk in checks:
            for n in sizes:
                log(f"check={chk} candidates_per_question={n}")
                runs[(chk, n)] = decide_many(texts, store, use_checker=True, run_models=not a.offline,
                                             runner=runner, check=chk, candidates_per_question=n)
    finally:
        if runner:
            runner.close()
    incomplete = [f"{k} {cid}" for k, cs in runs.items() for cid, c in cs.items() if not c.complete]
    if incomplete:
        sys.exit(f"answers missing for {incomplete[:5]}...; run without --offline")

    res = {"settings": {"checks": checks, "candidates_per_question": sizes, "max_members": 10,
                        "min_precision": MIN_PRECISION},
           "campaigns": {}, "methods": {}}
    for (chk, n), cs in runs.items():
        name = f"{chk}/{n}"
        rows = {c["id"]: grade(c, cs[c["id"]]) for c in REF["campaigns"]}
        generic = {c["id"]: grade(c, cs["_empty"] if "_empty" in cs else cs[c["id"]]) for c in REF["campaigns"]}
        res["methods"][name] = {
            split: mean_of([rows[c["id"]] for c in REF["campaigns"] if split == "all" or c["split"] == split])
            for split in ("all", "dev", "test")}
        res["methods"][name]["generic_all"] = mean_of(list(generic.values()))
        res["methods"][name]["qualified"] = sum(r["qualified"] for r in rows.values())
        for cid, r in rows.items():
            res["campaigns"].setdefault(cid, {})[name] = {**r, "committee": cs[cid].to_dict()}
    res["leave_one_out"] = {chk: leave_one_out({cid: {n: res["campaigns"][cid][f"{chk}/{n}"] for n in sizes}
                                                for cid in res["campaigns"]})
                            for chk in ["judge_only"] + checks}
    before = json.loads(Path(a.before).read_text()) if Path(a.before).exists() else None
    if before:
        rows = []
        for c in REF["campaigns"]:
            b = before["campaigns"][c["id"]]
            rows.append(b["judge_and_checker"])
        res["before"] = {"all": mean_of(rows),
                         "dev": mean_of([r for r, c in zip(rows, REF["campaigns"]) if c["split"] == "dev"]),
                         "test": mean_of([r for r, c in zip(rows, REF["campaigns"]) if c["split"] == "test"])}
    res["seconds"] = round(time.time() - t0)
    Path(a.out).write_text(json.dumps(res, indent=1))

    print(f"\n{'method':24s} {'P':>5s} {'R':>5s} {'F1':>5s} {'size':>5s} | test P/R/F1        | qualified")
    if before:
        m, t = res["before"]["all"], res["before"]["test"]
        print(f"{'before (6 seats)':24s} {m['precision']:5.2f} {m['recall']:5.2f} {m['f1']:5.2f} "
              f"{m['size']:5.1f} | {t['precision']:.2f}/{t['recall']:.2f}/{t['f1']:.2f}")
    for name, m in res["methods"].items():
        al, t = m["all"], m["test"]
        flag = "" if al["precision"] >= MIN_PRECISION else "  <- precision below 0.85"
        print(f"{name:24s} {al['precision']:5.2f} {al['recall']:5.2f} {al['f1']:5.2f} "
              f"{al['size']:5.1f} | {t['precision']:.2f}/{t['recall']:.2f}/{t['f1']:.2f}     | {m['qualified']}/10{flag}")
    print("\nleave-one-out (candidates per question chosen on the other 9):")
    for chk, r in res["leave_one_out"].items():
        m = r["held_out_mean"]
        print(f"  {chk:14s} P {m['precision']:.2f}  R {m['recall']:.2f}  F1 {m['f1']:.2f}"
              f"  chosen {sorted(set(r['chosen_per_held_out_campaign'].values()))}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
