"""Sieve web app: upload files, then committee, TAL scores, roster, approve.

    sieve ui            # opens http://127.0.0.1:8765

Every step calls the same code as the CLI:
  upload     app.ingest (stack contact sheets, join research sheets), then
             roster.run with the uploaded campaign file as the TAL rubric
  committee  committee.decide in the background (Qwen judges, Llama checks,
             through Ollama), then committee.match on the roster
  approve    the review console's approval file, then export.salesforce
             (as `sieve sync`) and export.queue (as `sieve queue`)

Runs live in SIEVE_HOME (default ~/.sieve/runs/<id>/). Model answers are
kept in SIEVE_HOME/answers.json, so a campaign decided once is instant again.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from ..committee.campaign import campaign_text
from ..committee.decide import AnswerStore, ModelRunner, decide_list, decide_many, decide_targets
from ..committee.qualify import qualify
from ..committee.rolelist import ListError, fill_descriptions, parse_list, rules_for, template_csv
from ..committee.targets import to_dict as targets_to_dict
from ..committee.match import match_contacts
from ..committee.reference import load_rules
from ..roster import run as run_roster
from ..scoring.icp import load_rubric
from .ingest import ingest

STATIC = Path(__file__).resolve().parent / "static"
REVIEW_TIERS = ("Tier A", "Tier B")


def _home() -> Path:
    if os.getenv("SIEVE_HOME"):
        return Path(os.environ["SIEVE_HOME"])
    try:
        return Path.home() / ".sieve"
    except RuntimeError:
        return Path(".sieve").resolve()


def runs_dir() -> Path:
    d = _home() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def answers_path() -> Path:
    return _home() / "answers.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data) -> None:
    """Write whole or not at all: the page reads these files while the job writes them."""
    with _LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)


def _read_json(path: Path):
    with _LOCK:
        return json.loads(path.read_text()) if path.exists() else None


def _read(path: Path) -> pd.DataFrame:
    """Read a roster exactly as the CLI does (keeps "NA" as text)."""
    return pd.read_csv(path, keep_default_na=False, na_values=[""], low_memory=False)


class Run:
    """One upload and everything Sieve produced from it."""

    def __init__(self, rid: str) -> None:
        self.id = rid
        self.dir = runs_dir() / rid
        if not self.dir.exists():
            raise HTTPException(404, "run not found")

    @property
    def meta_path(self) -> Path:
        return self.dir / "run.json"

    def meta(self) -> dict:
        # Reads and writes take turns: Windows will not replace a file that is open.
        with _LOCK:
            return json.loads(self.meta_path.read_text())

    def update(self, **kw) -> dict:
        with _LOCK:
            m = json.loads(self.meta_path.read_text())
            m.update(kw)
            tmp = self.meta_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, indent=1))
            tmp.replace(self.meta_path)
            return m

    def roster(self) -> pd.DataFrame:
        return _read(self.dir / "roster.csv")

    def committee(self) -> dict | None:
        return _read_json(self.dir / "committee.json")

    def targets(self) -> dict | None:
        return _read_json(self.dir / "targets.json")

    def gen_list(self) -> dict | None:
        return _read_json(self.dir / "list.json")

    def dropped(self) -> set[str]:
        return set(_read_json(self.dir / "dropped.json") or [])


_LOCK = threading.RLock()
_JOBS: dict[str, threading.Thread] = {}


def run_rules(meta: dict) -> dict:
    """The roles and descriptions this run uses: an uploaded list, or the default."""
    return rules_for(meta.get("role_list"))


def active_list(meta: dict) -> dict:
    """The counts the committee must meet: the run's list as given, numbers set for
    the run, or numbers the AI generates (Generate committee)."""
    lst = meta.get("list") or {"mode": "default"}
    base = run_rules(meta)["minimums"]
    if lst.get("mode") == "custom":
        return {"mode": "custom", "minimums": {r: int(n) for r, n in lst["minimums"].items()}}
    if lst.get("mode") == "generated":
        return {"mode": "generated"}
    return {"mode": "uploaded" if meta.get("role_list") else "default", "minimums": dict(base)}


def _committee_job(rid: str) -> None:
    """The list (mandatory), then the committee, which must meet it, then the
    people to target at each Tier A company. Every model answer is saved as it
    comes. A committee that does not meet the list is "not qualified": no
    targets are picked and the roster stays closed."""
    run = Run(rid)
    try:
        m = run.meta()
        text = m["campaign_text"]
        store = AnswerStore(answers_path())
        run.update(committee_status="running", committee_error=None, committee_started=_now())

        timings: list[dict] = []
        runner = None

        def metrics():
            return {"timings": [{k: v for k, v in t.items() if k != "t0"} for t in timings],
                    "models": runner.stats if runner else {}}

        def progress_for(stage):
            def progress(n, label):
                run.update(committee_stage=stage, committee_progress={"answered": n, "now": label},
                           metrics=metrics())
            return progress

        def begin(stage):
            now = time.time()
            if timings and timings[-1]["seconds"] is None:
                timings[-1]["seconds"] = round(now - timings[-1]["t0"], 1)
            if stage:
                timings.append({"stage": stage, "started": _now(), "t0": now, "seconds": None})
            if runner is not None:
                runner.stage = stage
                runner.progress = progress_for(stage)
            run.update(committee_stage=stage or "done", metrics=metrics())

        def decide(minimums, stage="committee"):
            begin(stage)
            c = decide_many({"c": text}, store, log=lambda *_: None, progress=progress_for(stage),
                            minimums=minimums, rules=rules, runner=runner)["c"].to_dict()
            c["rules"] = minimums
            c["seat_order"] = [r for r in rules["seat_order"] if r in minimums]
            c["role_definitions"] = {r: rules["roles"][r] for r in minimums if r in rules["roles"]}
            c["qualification"] = qualify(c["members"], minimums)
            return c

        if any(not r.get("description") for r in (m.get("role_list") or [])):
            begin("descriptions")
            run.update(committee_progress={"answered": 0, "now": "Qwen (judge): describing roles"})
            from ..committee.models import JUDGE, OllamaModel, check_ollama
            from ..committee.decide import SYSTEM as _SYS
            qwen = None

            def write(prompt):
                nonlocal qwen
                key = "desc|" + JUDGE.name + "|" + __import__("hashlib").sha1(prompt.encode()).hexdigest()[:16]
                got = store.get(key)
                if got is None:
                    if qwen is None:
                        problems = check_ollama([JUDGE])
                        if problems:
                            raise RuntimeError("; ".join(problems))
                        qwen = OllamaModel(JUDGE)
                    text = qwen.write(_SYS, prompt)
                    store.put(key, JUDGE.name, prompt, "description", (), {}, text, "")
                    store.save()
                    return text
                return got["reasoning"]
            filled = fill_descriptions(m["role_list"], write)
            m = run.update(role_list=filled)
        rules = run_rules(m)
        runner = ModelRunner(store, log=lambda *_: None, rules=rules)
        lst = active_list(m)
        attempts = []
        if lst["mode"] == "generated":
            # Generate committee: the AI writes the list, then a committee must meet it.
            # If the first try does not qualify, one retry: the failed numbers are taken
            # off those roles' options and the list is asked again for them.
            begin("list")
            gl = decide_list(text, store, log=lambda *_: None, progress=progress_for("list"), rules=rules,
                             runner=runner)
            minimums = {r: d["count"] for r, d in gl.items()}
            c = decide(minimums)
            attempts.append({"list": minimums, "qualified": c["qualification"]["qualified"],
                             "failed": c["qualification"]["failed"]})
            if not c["qualification"]["qualified"]:
                failed_roles = [ln["role"] for ln in c["qualification"]["lines"] if not ln["met"]]
                begin("list (retry)")
                retry = decide_list(text, store, log=lambda *_: None, progress=progress_for("list (retry)"),
                                    exclude={r: {str(minimums[r])} for r in failed_roles}, roles=failed_roles,
                                    rules=rules, runner=runner)
                for r, d in retry.items():
                    d["retry"] = True
                    gl[r] = d
                minimums = {r: d["count"] for r, d in gl.items()}
                c = decide(minimums, stage="committee (retry)")
                attempts.append({"list": minimums, "qualified": c["qualification"]["qualified"],
                                 "failed": c["qualification"]["failed"]})
            _write_json(run.dir / "list.json", gl)
            c["list_source"] = "generated by the AI for this campaign"
        else:
            c = decide(lst["minimums"])
            c["list_source"] = {"default": "default", "uploaded": "uploaded list"}.get(lst["mode"], "set for this run")
        c["attempts"] = attempts
        _write_json(run.dir / "committee.json", c)

        if c["qualification"]["qualified"]:
            begin("targets")
            picked = decide_targets(run.roster(), c, text, store, log=lambda *_: None, progress=progress_for("targets"),
                                    rules=rules, runner=runner)
            out = {}
            for k, v in picked.items():
                d = targets_to_dict(v)
                d["qualification"] = qualify(d["picks"], c["rules"], who="name")
                out[k] = d
            _write_json(run.dir / "targets.json", out)
        begin(None)
        run.update(committee_status="done", committee_stage="done", committee_finished=_now(),
                   committee_qualified=c["qualification"]["qualified"],
                   committee_progress={"answered": None, "now": ""})
    except Exception as e:  # noqa: BLE001
        try:
            begin(None)
        except Exception:  # noqa: BLE001
            pass
        run.update(committee_status="error", committee_error=str(e),
                   committee_trace=traceback.format_exc()[-2000:])


def start_committee(rid: str) -> None:
    t = _JOBS.get(rid)
    if t and t.is_alive():
        return
    t = threading.Thread(target=_committee_job, args=(rid,), daemon=True)
    _JOBS[rid] = t
    t.start()


def _jsonable(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


def resume_unfinished() -> list[str]:
    """Restart committees left unfinished when the app last stopped (answers
    already given are saved, so they pick up where they were)."""
    resumed = []
    for d in runs_dir().iterdir():
        p = d / "run.json"
        if p.exists() and json.loads(p.read_text()).get("committee_status") in ("queued", "running"):
            start_committee(d.name)
            resumed.append(d.name)
    return resumed


def create_app(resume: bool = True) -> FastAPI:
    app = FastAPI(title="Sieve")
    if resume:
        resume_unfinished()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health():
        from ..committee.models import OLLAMA_URL, check_ollama
        return {"ollama_url": OLLAMA_URL, "problems": check_ollama()}

    @app.get("/api/runs")
    def list_runs():
        out = []
        for d in sorted(runs_dir().iterdir(), reverse=True):
            p = d / "run.json"
            if p.exists():
                m = json.loads(p.read_text())
                out.append({k: m.get(k) for k in ("id", "created", "campaign_name", "contacts",
                                                  "accounts", "committee_status", "approved")})
        return out

    @app.post("/api/runs")
    async def create_run(campaign: UploadFile = File(...), contacts: list[UploadFile] = File(...),
                         name: str = Form(""), committee_list: UploadFile | None = File(None)):
        rid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        d = runs_dir() / rid
        up = d / "uploads"
        up.mkdir(parents=True)
        try:
            cpath = up / Path(campaign.filename or "campaign.yaml").name
            cpath.write_bytes(await campaign.read())
            try:
                cdata = yaml.safe_load(cpath.read_text(encoding="utf-8"))
                rubric = load_rubric(cpath)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"Campaign file could not be read as a Sieve campaign (TAL rubric) "
                                         f"YAML: {e}") from e
            role_list = None
            if committee_list is not None and committee_list.filename:
                try:
                    role_list = parse_list(await committee_list.read(), committee_list.filename)
                except ListError as e:
                    raise HTTPException(400, str(e)) from e
            paths = []
            for f in contacts:
                p = up / Path(f.filename or "contacts.csv").name
                p.write_bytes(await f.read())
                paths.append(p)
            try:
                ing = ingest(paths)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            ing.frame.to_csv(d / "input.csv", index=False)
            result = run_roster(ing.frame, rubric=rubric, audit_path=d / "sieve_audit_log.jsonl", run_id=rid)
            result.to_csv(d / "roster.csv")
            meta = {"id": rid, "created": _now(),
                    "campaign_name": name or cdata.get("description") or cdata.get("campaign") or cpath.stem,
                    "campaign_file": cpath.name, "campaign_text": campaign_text(cdata),
                    "contact_files": [p.name for p in paths], "ingest": ing.summary(),
                    "contacts": result.summary["rows"], "accounts": result.summary["accounts"],
                    "tiers": result.summary["tiers"], "committee_status": "queued", "approved": None,
                    "role_list": role_list}
            (d / "run.json").write_text(json.dumps(meta, indent=1))
        except HTTPException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(500, f"Sieve could not process these files: {e}") from e
        start_committee(rid)
        return {"id": rid}

    @app.get("/api/runs/{rid}")
    def get_run(rid: str):
        m = Run(rid).meta()
        m.pop("committee_trace", None)
        return m

    @app.delete("/api/runs/{rid}")
    def delete_run(rid: str):
        shutil.rmtree(Run(rid).dir, ignore_errors=True)
        return {"deleted": rid}

    @app.post("/api/runs/{rid}/committee/retry")
    def retry_committee(rid: str):
        Run(rid).update(committee_status="queued", committee_error=None)
        start_committee(rid)
        return {"ok": True}

    @app.get("/api/runs/{rid}/committee")
    def get_committee(rid: str):
        run = Run(rid)
        m = run.meta()
        c = run.committee()
        coverage = None
        if c:
            roster = run.roster()
            _, acc = match_contacts(roster, c, account_col="sv_account_id")
            names = roster.drop_duplicates("sv_account_id").set_index("sv_account_id")
            acc["company"] = [names.at[a, "Company"] if "Company" in names.columns else a for a in acc["account"]]
            acc["tier"] = [names.at[a, "sv_target_tier"] for a in acc["account"]]
            coverage = {"accounts": int(len(acc)), "complete": int(acc["minimums_met"].sum()),
                        "missing_counts": acc["missing"].str.split("; ").explode().replace("", None)
                        .dropna().value_counts().to_dict()}
        return {"status": m.get("committee_status"), "stage": m.get("committee_stage"),
                "error": m.get("committee_error"),
                "progress": m.get("committee_progress"), "campaign_text": m.get("campaign_text"),
                "list": active_list(m), "default_list": load_rules()["minimums"],
                "base_list": run_rules(m)["minimums"], "uploaded": bool(m.get("role_list")),
                "roles": {r: {"text": " ".join(v["text"].split()), "source": v.get("source", "")}
                          for r, v in run_rules(m)["roles"].items() if r in run_rules(m)["minimums"]},
                "generated_list": run.gen_list(), "committee": c, "coverage": coverage}

    @app.get("/api/runs/{rid}/metrics")
    def run_metrics(rid: str):
        """Time per stage and per model, model calls and swaps, and what the
        committee and Tier A steps did."""
        run = Run(rid)
        m = run.meta()
        mx = m.get("metrics") or {}
        models = []
        for name, v in (mx.get("models") or {}).get("models", {}).items():
            models.append({"model": name, "calls": v["calls"], "seconds": v["seconds"],
                           "per_call": round(v["seconds"] / v["calls"], 1) if v["calls"] else None,
                           "kinds": v.get("kinds", {})})
        stages = []
        for t in mx.get("timings", []):
            per = (mx.get("models") or {}).get("stages", {}).get(t["stage"], {})
            stages.append({**t, "models": {k: {"calls": v["calls"], "seconds": v["seconds"]} for k, v in per.items()}})
        c = run.committee()
        com = None
        if c:
            cands = [x for chain in (c.get("candidates") or {}).values() for x in chain]
            offered = [r for r in c.get("rejected", []) if r.get("offered_from")] + \
                      [x for x in c.get("members", []) if x.get("offered_from")]
            com = {"members": len(c["members"]), "extras": sum(1 for x in c["members"] if x.get("extra")),
                   "qwen_candidates": sum(1 for x in cands if not x.get("stop")),
                   "qwen_said_none": sum(1 for x in cands if x.get("function") == "None"),
                   "llama_rejections": sum(1 for r in c.get("rejected", []) if not r.get("offered_from")),
                   "offered_to_other_roles": len(offered),
                   "seated_from_offers": sum(1 for x in c["members"] if x.get("offered_from")),
                   "unfilled": c.get("unfilled", {}),
                   "qualified": (c.get("qualification") or {}).get("qualified"),
                   "settings": c.get("settings", {})}
        t = _read_json(run.dir / "targets.json") if (run.dir / "targets.json").exists() else None
        tgt = None
        if t:
            tgt = {"companies": len(t), "picked": sum(len(v["picks"]) for v in t.values()),
                   "empty_seats": sum(len(v["empty"]) for v in t.values()),
                   "llama_rejections": sum(len(v["rejected"]) for v in t.values()),
                   "companies_qualified": sum(1 for v in t.values() if (v.get("qualification") or {}).get("qualified"))}
        total = None
        if m.get("committee_started") and m.get("committee_finished"):
            total = round((datetime.fromisoformat(m["committee_finished"]) -
                           datetime.fromisoformat(m["committee_started"])).total_seconds(), 1)
        return {"status": m.get("committee_status"), "started": m.get("committee_started"),
                "finished": m.get("committee_finished"), "total_seconds": total,
                "stages": stages, "models": models, "swaps": (mx.get("models") or {}).get("swaps", 0),
                "committee": com, "targets": tgt}

    @app.get("/api/metrics/benchmark")
    def benchmark_metrics():
        """The committee accuracy on the 10-campaign reference set: before and after
        the recall fixes, per check type and candidates per question, with
        leave-one-out."""
        path = Path(__file__).resolve().parents[3] / "benchmarks" / "committee" / "results.json"
        if not path.exists():
            return {"available": False}
        r = json.loads(path.read_text())
        if "methods" not in r:                     # results from before the recall fixes
            return {"available": True, "before": r.get("all_mean", {}).get("judge_and_checker"), "methods": {}}
        return {"available": True, "before": r.get("before"), "methods": r["methods"],
                "leave_one_out": r.get("leave_one_out"), "settings": r.get("settings"), "seconds": r.get("seconds")}

    @app.post("/api/runs/{rid}/list")
    def set_list(rid: str, body: dict):
        """The list the committee must meet. mode "default"; "custom" with minimums
        {role: count}; or "generated" (Generate committee: the AI writes the list).
        The committee and the Tier A picks are then decided again."""
        mode = body.get("mode")
        run = Run(rid)
        meta = run.meta()
        if mode == "default" and body.get("builtin"):
            meta["role_list"] = None
            run.update(role_list=None)
        roles = list(run_rules(meta)["minimums"])
        if mode == "custom":
            try:
                mins = {r: int(body["minimums"][r]) for r in roles}
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, f"minimums must give a whole number for each of: {', '.join(roles)}")
            if any(n < 0 or n > 10 for n in mins.values()):
                raise HTTPException(400, "each count must be between 0 and 10")
            lst = {"mode": "custom", "minimums": mins}
        elif mode in ("default", "generated"):
            lst = {"mode": mode}
        else:
            raise HTTPException(400, 'mode must be "default", "custom" or "generated"')
        if run.meta().get("committee_status") in ("running", "queued") and _JOBS.get(rid) and _JOBS[rid].is_alive():
            raise HTTPException(409, "The committee is still being decided; wait for it to finish.")
        for f in ("committee.json", "targets.json", "dropped.json", "list.json"):
            (run.dir / f).unlink(missing_ok=True)
        run.update(list=lst, committee_status="queued", committee_error=None, approved=None,
                   committee_qualified=None)
        start_committee(rid)
        return {"ok": True}

    @app.post("/api/runs/{rid}/rolelist")
    async def upload_list(rid: str, file: UploadFile = File(...)):
        """Upload a committee list (Role, Count, Description) for this run; the committee
        and the Tier A picks are decided again with it."""
        run = Run(rid)
        if _JOBS.get(rid) and _JOBS[rid].is_alive():
            raise HTTPException(409, "The committee is still being decided; wait for it to finish.")
        try:
            rows = parse_list(await file.read(), file.filename or "")
        except ListError as e:
            raise HTTPException(400, str(e)) from e
        for f in ("committee.json", "targets.json", "dropped.json", "list.json"):
            (run.dir / f).unlink(missing_ok=True)
        run.update(role_list=rows, list={"mode": "default"}, committee_status="queued", committee_error=None,
                   approved=None, committee_qualified=None)
        start_committee(rid)
        return {"roles": rows}

    @app.get("/api/list-template.csv", response_class=PlainTextResponse)
    def list_template():
        return PlainTextResponse(template_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=committee_list_template.csv"})

    @app.get("/api/runs/{rid}/scores")
    def get_scores(rid: str):
        run = Run(rid)
        roster = run.roster()
        cfg = yaml.safe_load((run.dir / "uploads" / run.meta()["campaign_file"]).read_text(encoding="utf-8"))
        labels = {k: v.get("label", k) for k, v in (cfg.get("pillars") or {}).items()}
        weights = cfg.get("weights") or {}
        accounts = []
        for aid, g in roster.groupby("sv_account_id", sort=False):
            first = g.iloc[0]
            breakdown = json.loads(first.get("sv_score_breakdown") or "[]")
            for b in breakdown:
                b["label"] = labels.get(b["pillar"], b["pillar"].replace("_", " ").title())
            accounts.append({"account_id": aid, "company": _jsonable(first.get("Company")),
                             "website": _jsonable(first.get("Website URL") or first.get("Website")),
                             "score": int(float(first.get("sv_icp_score") or 0)),
                             "tier": first.get("sv_target_tier"), "contacts": int(len(g)),
                             "breakdown": breakdown})
        accounts.sort(key=lambda a: (-a["score"], a["company"] or ""))
        tiers = {t["name"]: {"min": t["min_score"], "disposition": t.get("disposition", "")}
                 for t in (cfg.get("tiers") or [])}
        return {"accounts": accounts, "tiers": tiers, "weights": weights}

    @app.get("/api/runs/{rid}/roster")
    def get_roster(rid: str):
        run = Run(rid)
        m = run.meta()
        t = run.targets()
        if m.get("committee_status") == "done" and m.get("committee_qualified") is False:
            raise HTTPException(409, "The committee does not qualify: it does not meet the list. Change the list "
                                     "or generate the committee on the Committee page; the roster opens once a "
                                     "committee qualifies.")
        if m.get("committee_status") != "done" or t is None:
            raise HTTPException(409, "The roster is built after the committee is decided and the Tier A "
                                     "targets are picked. See the Committee page for progress.")
        roster = run.roster().set_index("sv_contact_id", drop=False)
        dropped = run.dropped()
        accounts = []
        for aid, a in t.items():
            def person(x):
                r = roster.loc[x["contact_id"]] if x.get("contact_id") in roster.index else {}
                flags = str(_jsonable(r.get("sv_flags")) or "") if len(r) else ""
                return {**x, "email": _jsonable(r.get("Email")) if len(r) else None,
                        "flags": [f for f in flags.split(",") if f.strip()],
                        "confidence": float(r.get("sv_confidence") or 0) if len(r) else 0.0,
                        "keep": x.get("contact_id") not in dropped}
            g = roster[roster["sv_account_id"].astype(str) == aid]
            first = g.iloc[0]
            accounts.append({"account_id": aid, "company": a["company"], "tier": first.get("sv_target_tier"),
                             "score": int(float(first.get("sv_icp_score") or 0)), "contacts": int(len(g)),
                             "picks": [person(x) for x in a["picks"]], "empty": a["empty"],
                             "rejected": a["rejected"], "qualification": a.get("qualification")})
        accounts.sort(key=lambda a: (-a["score"], a["company"] or ""))
        picked = [p for a in accounts for p in a["picks"]]
        return {"accounts": accounts, "targets": len(picked), "dropped": sum(1 for p in picked if not p["keep"]),
                "list_source": (run.committee() or {}).get("list_source", "default")}

    @app.post("/api/runs/{rid}/keep")
    def set_keep(rid: str, body: dict):
        run = Run(rid)
        with _LOCK:
            dropped = run.dropped()
            for cid in body.get("contact_ids") or [body.get("contact_id")]:
                if body.get("keep"):
                    dropped.discard(cid)
                else:
                    dropped.add(cid)
            _write_json(run.dir / "dropped.json", sorted(dropped))
        return {"dropped": len(dropped)}

    @app.post("/api/runs/{rid}/approve")
    def approve(rid: str):
        from ..export.queue import build_queue, write_queue
        from ..export.salesforce import build_payload
        run = Run(rid)
        t = run.targets()
        if run.meta().get("committee_qualified") is False:
            raise HTTPException(409, "Nothing to approve: the committee does not qualify against the list.")
        if run.meta().get("committee_status") != "done" or t is None:
            raise HTTPException(409, "Nothing to approve yet: the Tier A targets are still being picked.")
        roster = run.roster()
        picks = [{**p, "company": a["company"], "account_id": aid} for aid, a in t.items() for p in a["picks"]]
        dropped = run.dropped() & {p["contact_id"] for p in picks}
        approved_ids = [p["contact_id"] for p in picks if p["contact_id"] not in dropped]
        approval = {"approved": True, "approved_at": _now(), "approved_contact_ids": approved_ids,
                    "dropped_contact_ids": sorted(dropped),
                    "totals": {"approved": len(approved_ids), "dropped": len(dropped)}}
        exp = run.dir / "exports"
        if exp.exists():
            shutil.rmtree(exp)
        exp.mkdir()
        (exp / "approval.json").write_text(json.dumps(approval, indent=2))
        payload = build_payload(roster, approval=approval, approved=True, tier_filter=REVIEW_TIERS)
        payload.write(exp / "salesforce")
        approved_rows = roster[roster["sv_contact_id"].astype(str).isin(approved_ids)]
        write_queue(build_queue(approved_rows, tier_filter=("Tier A",), committee_only=False), exp / "dial_queue")
        cols = ["company", "role", "seat_title", "function", "name", "title", "contact_id", "judge_p", "checker_p",
                "judge_reasoning", "checker_reasoning"]
        tdf = pd.DataFrame([{k: p.get(k) for k in cols} | {"approved": p["contact_id"] in approved_ids,
                             "company_committee_qualified": t[p["account_id"]]["qualification"]["qualified"],
                             "company_committee_missing": "; ".join(t[p["account_id"]]["qualification"]["failed"])}
                            for p in picks])
        tdf = tdf.merge(roster[["sv_contact_id", "Email"]].astype({"sv_contact_id": str}),
                        left_on="contact_id", right_on="sv_contact_id", how="left").drop(columns="sv_contact_id")
        tdf.to_csv(exp / "tier_a_targets.csv", index=False)
        shutil.copy(run.dir / "roster.csv", exp / "verified_prospect_roster.csv")
        (exp / "committee.json").write_text(json.dumps(run.committee(), indent=1))
        (exp / "tier_a_targets.json").write_text(json.dumps(t, indent=1))
        c = run.committee()
        (exp / "qualification.json").write_text(json.dumps({
            "list_source": c.get("list_source"), "list": c.get("rules"), "committee": c.get("qualification"),
            "attempts": c.get("attempts"),
            "companies": {a["company"]: a["qualification"] for a in t.values()}}, indent=1))
        run.update(approved=approval["approved_at"], approval_totals=approval["totals"],
                   salesforce_manifest=payload.manifest)
        return {"approval": approval["totals"], "manifest": payload.manifest, "files": _files(exp)}

    @app.get("/api/runs/{rid}/exports")
    def exports(rid: str):
        exp = Run(rid).dir / "exports"
        return {"files": _files(exp) if exp.exists() else []}

    @app.get("/api/runs/{rid}/files/{path:path}")
    def download(rid: str, path: str):
        exp = (Run(rid).dir / "exports").resolve()
        target = (exp / path).resolve()
        if exp not in target.parents or not target.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(target, filename=target.name)

    @app.exception_handler(HTTPException)
    async def http_error(_req, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def _files(exp: Path) -> list[dict]:
    return [{"path": str(p.relative_to(exp)).replace("\\", "/"), "bytes": p.stat().st_size}
            for p in sorted(exp.rglob("*")) if p.is_file()]


def main(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn
    if open_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
