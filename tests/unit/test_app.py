"""The web app, end to end through its HTTP API: upload the real campaign
workbook and campaign file, then committee, TAL scores, roster, keep/drop,
approve and download. The models are replaced by scripted answers here; the
live tests run the real ones.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from sieve.app import server
from sieve.committee import decide as decide_mod

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "config" / "icp_adtech_modernization.yaml"
WORKBOOK = ROOT / "sample-input" / "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx"

PICKS = {"Economic Buyer": "Finance", "Champion": "Technology", "Technical Evaluator": "Architecture",
         "Influencer": "Product", "User": "Platform", "Ratifier": "Procurement"}
SECOND = {"Champion": "IT"}


def fake_decide_many(campaigns, store, use_checker=True, run_models=True, log=print, runner=None, progress=None,
                     minimums=None, rules=None, **settings):
    def answers(a):
        want = PICKS.get(a.role, "Security")
        if a.kind == "brief":
            return f"{a.model}'s brief of the {a.role} for this campaign."
        if a.kind == "yesno":
            yes = a.function == want
            return {"probs": {"Yes": float(yes), "No": float(not yes)}, "reasoning": f"{a.model}: {yes}"}
        if want not in a.options:            # Qwen's next answer, asked again without its first one
            want = SECOND.get(a.role, "None")
            want = want if want in a.options else ("None" if "None" in a.options else a.options[0])
        return {"probs": {f: (1.0 if f == want else 0.0) for f in a.options},
                "reasoning": f"{a.model} reasoning for {a.role}: {want} fits this campaign."}
    if progress:
        progress(0, "Qwen (judge): Economic Buyer")
    return {k: decide_mod.assemble(t, answers, rules=rules, judge_name="qwen", checker_name="llama", minimums=minimums,
                                    **settings)
            for k, t in campaigns.items()}


def fake_decide_targets(roster, committee, campaign, store, run_models=True, log=print, progress=None,
                        runner=None, use_checker=True, rules=None):
    from sieve.committee.reference import holds
    from sieve.committee.targets import pick_targets

    def answers(a):
        seat = next(m["title"] for m in committee["members"] if f"committee's {m['title']} (" in a.prompt)
        pick = "Z"
        for line in a.prompt.splitlines():
            if len(line) > 3 and line[1] == ")" and line[0] != "Z" and seat in holds(line.split(" - ", 1)[-1]):
                pick = line[0]
                break
        return {"probs": {o: (1.0 if o == pick else 0.0) for o in a.options},
                "reasoning": f"{a.model}: the {seat} seat -> {pick}"}
    return pick_targets(roster, committee, campaign, answers, use_checker=use_checker)


def fake_decide_list(campaign, store, run_models=True, log=print, progress=None, runner=None, use_checker=True,
                     exclude=None, roles=None, rules=None):
    from sieve.committee.required import generate_list
    counts = {"Champion": "2", "Economic Buyer": "1", "Technical Evaluator": "1", "Influencer": "2"}
    out, _ = generate_list(campaign, lambda a: {"probs": {o: (1.0 if o == counts[a.role] else 0.0) for o in a.options},
                                                "reasoning": f"{a.model}: {a.role} needs {counts[a.role]}"})
    return out


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIEVE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "decide_many", fake_decide_many)
    monkeypatch.setattr(server, "decide_targets", fake_decide_targets)
    monkeypatch.setattr(server, "decide_list", fake_decide_list)
    return TestClient(server.create_app(resume=False))


def upload(client, files=None, campaign=CAMPAIGN):
    files = files or [WORKBOOK]
    multipart = [("campaign", (campaign.name, campaign.read_bytes(), "application/x-yaml"))]
    multipart += [("contacts", (f.name, f.read_bytes(), "application/octet-stream")) for f in files]
    return client.post("/api/runs", files=multipart, data={"name": "Simplifi test"})


def wait_committee(client, rid):
    for _ in range(600):                      # up to 60 s: generous when the machine is busy
        r = client.get(f"/api/runs/{rid}/committee").json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.1)
    raise AssertionError("committee job did not finish")


def test_page_is_served(client):
    html = client.get("/").text
    assert "Start a run" in html and "Approve" in html and "TAL" in html


def test_full_flow(client):
    r = upload(client)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    meta = client.get(f"/api/runs/{rid}").json()
    assert meta["contacts"] == 633 and meta["accounts"] == 98          # as `sieve run` on the reference input
    assert meta["ingest"]["research_tables"] and len(meta["ingest"]["contact_tables"]) == 3
    assert meta["tiers"] == {"Tier C": 387, "Tier B": 168, "Tier A": 78}       # same as Sieve's reference input
    assert "AdTech Platform Modernization" in meta["campaign_text"]
    assert [x["id"] for x in client.get("/api/runs").json()] == [rid]

    c = wait_committee(client, rid)
    assert c["status"] == "done", c
    members = c["committee"]["members"]
    assert {m["role"] for m in members} >= {"Economic Buyer", "Champion", "Technical Evaluator", "Influencer"}
    assert all(m["judge_reasoning"] and m["checker_reasoning"] for m in members)
    assert c["coverage"]["accounts"] == meta["accounts"]

    s = client.get(f"/api/runs/{rid}/scores").json()
    top = s["accounts"][0]
    assert top["score"] == max(a["score"] for a in s["accounts"])
    assert len(top["breakdown"]) == 4
    assert {"label", "band", "points", "weight", "contribution", "evidence"} <= set(top["breakdown"][0])
    assert top["breakdown"][0]["label"] == "Industry & Vertical Fit"
    assert any(b["evidence"] for b in top["breakdown"])
    assert set(s["tiers"]) == {"Tier A", "Tier B", "Tier C"}

    ro = client.get(f"/api/runs/{rid}/roster").json()
    accounts = ro["accounts"]
    assert accounts and {a["tier"] for a in accounts} == {"Tier A"}                 # AI picks only at Tier A
    picks = [p for a in accounts for p in a["picks"]]
    assert picks and ro["targets"] == len(picks)
    assert all(p["judge_reasoning"] and p["checker_reasoning"] for p in picks)
    assert all(e["judge_reasoning"] for a in accounts for e in a["empty"])
    from sieve.committee.reference import holds
    assert all(p["seat_title"] in holds(p["title"]) for p in picks)                # scripted judge: title holds seat
    for a in accounts:
        ids = [p["contact_id"] for p in a["picks"]]
        assert len(ids) == len(set(ids))                                           # one person, one seat
    assert not any(f.lower() == "nan" for p in picks for f in p["flags"])
    victim = picks[0]["contact_id"]
    assert client.post(f"/api/runs/{rid}/keep", json={"contact_id": victim, "keep": False}).json()["dropped"] == 1
    assert client.get(f"/api/runs/{rid}/roster").json()["dropped"] == 1

    ap = client.post(f"/api/runs/{rid}/approve").json()
    assert ap["approval"] == {"approved": len(picks) - 1, "dropped": 1}
    paths = {f["path"] for f in ap["files"]}
    assert {"approval.json", "verified_prospect_roster.csv", "committee.json", "tier_a_targets.csv",
            "tier_a_targets.json"} <= paths
    assert any(p.startswith("salesforce/") for p in paths) and any(p.startswith("dial_queue/") for p in paths)
    approval = json.loads(client.get(f"/api/runs/{rid}/files/approval.json").content)
    assert victim in approval["dropped_contact_ids"] and victim not in approval["approved_contact_ids"]
    assert set(approval["approved_contact_ids"]) == {p["contact_id"] for p in picks} - {victim}
    sf = next(p for p in paths if p.startswith("salesforce/") and p.endswith("contact_upsert.csv"))
    sfc = pd.read_csv(io.BytesIO(client.get(f"/api/runs/{rid}/files/{sf}").content))
    assert len(sfc) <= len(picks) - 1                                              # only approved targets reach Salesforce
    tcsv = pd.read_csv(io.BytesIO(client.get(f"/api/runs/{rid}/files/tier_a_targets.csv").content))
    assert len(tcsv) == len(picks) and tcsv["judge_reasoning"].notna().all()
    assert client.get(f"/api/runs/{rid}/files/../run.json").status_code == 404
    assert client.get(f"/api/runs/{rid}").json()["approved"]

    assert client.delete(f"/api/runs/{rid}").status_code == 200
    assert client.get("/api/runs").json() == []


def test_several_contact_files_and_csv(client, tmp_path):
    sheets = pd.read_excel(WORKBOOK, sheet_name=None, dtype=str, keep_default_na=False, na_values=[""])
    na = tmp_path / "na.csv"
    sheets["NA"].to_csv(na, index=False)
    eu = tmp_path / "eu_and_status.xlsx"
    with pd.ExcelWriter(eu) as w:
        sheets["EU"].to_excel(w, sheet_name="EU", index=False)
        sheets["Status"].to_excel(w, sheet_name="Status", index=False)
    r = upload(client, [na, eu])
    assert r.status_code == 200, r.text
    meta = client.get(f"/api/runs/{r.json()['id']}").json()
    assert meta["contacts"] == len(sheets["NA"]) + len(sheets["EU"])
    assert meta["ingest"]["research_matched"] > 0


def test_bad_uploads_are_refused_clearly(client, tmp_path):
    bad_campaign = tmp_path / "x.yaml"
    bad_campaign.write_text("just: text\n")
    r = upload(client, campaign=bad_campaign)
    assert r.status_code == 400 and "campaign" in r.json()["error"].lower()
    no_titles = tmp_path / "nothing.csv"
    no_titles.write_text("a,b\n1,2\n")
    r = upload(client, [no_titles])
    assert r.status_code == 400 and "Title" in r.json()["error"]
    assert client.get("/api/runs").json() == []            # nothing half-made is left behind


def test_committee_error_is_reported_and_retryable(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Ollama is not running at http://127.0.0.1:11434: start the Ollama app.")
    monkeypatch.setattr(server, "decide_many", boom)
    rid = upload(client).json()["id"]
    c = wait_committee(client, rid)
    assert c["status"] == "error" and "Ollama is not running" in c["error"]
    assert client.get(f"/api/runs/{rid}/scores").status_code == 200       # the rest still works
    monkeypatch.setattr(server, "decide_many", fake_decide_many)
    client.post(f"/api/runs/{rid}/committee/retry")
    assert wait_committee(client, rid)["status"] == "done"


def test_unfinished_committee_resumes_on_start(client):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "start_committee", lambda rid: None)               # upload, but no job runs
        rid = upload(client).json()["id"]
    assert client.get(f"/api/runs/{rid}").json()["committee_status"] == "queued"
    assert server.resume_unfinished() == [rid]
    assert wait_committee(client, rid)["status"] == "done"


def test_roster_is_locked_until_the_committee_and_targets_are_done(client):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "start_committee", lambda rid: None)
        rid = upload(client).json()["id"]
    r = client.get(f"/api/runs/{rid}/roster")
    assert r.status_code == 409 and "after the committee" in r.json()["error"]
    assert client.post(f"/api/runs/{rid}/approve").status_code == 409
    assert client.get(f"/api/runs/{rid}/scores").status_code == 200              # TAL scores need no AI


def test_default_list_is_mandatory_and_cited(client):
    rid = upload(client).json()["id"]
    c = wait_committee(client, rid)
    assert c["list"] == {"mode": "default", "minimums": c["default_list"]}
    assert c["default_list"] == {"Champion": 1, "Economic Buyer": 1, "Technical Evaluator": 1, "Influencer": 3}
    q = c["committee"]["qualification"]
    assert q["qualified"] and not q["failed"]
    for line in q["lines"]:
        assert line["met"] and line["have"] >= line["need"] and len(line["proof"]) == line["have"]
        assert all(p["judge_quote"] and p["checker_quote"] and p["judge_p"] is not None for p in line["proof"])
    ro = client.get(f"/api/runs/{rid}/roster").json()
    assert all("qualified" in a["qualification"] for a in ro["accounts"])
    assert {a["tier"] for a in ro["accounts"]} == {"Tier A"}                     # tiers are left as they are


def test_a_committee_that_misses_the_list_does_not_qualify_and_blocks_the_roster(client):
    rid = upload(client).json()["id"]
    wait_committee(client, rid)
    # 8 Economic Buyers: the scripted models can only ever seat a few, so the list is not met
    mins = {"Champion": 1, "Economic Buyer": 8, "Technical Evaluator": 1, "Influencer": 3}
    assert client.post(f"/api/runs/{rid}/list", json={"mode": "custom", "minimums": mins}).status_code == 200
    c = wait_committee(client, rid)
    assert c["list"] == {"mode": "custom", "minimums": mins} and c["committee"]["rules"] == mins
    q = c["committee"]["qualification"]
    assert not q["qualified"] and any(f.startswith("Economic Buyer:") for f in q["failed"])
    r = client.get(f"/api/runs/{rid}/roster")
    assert r.status_code == 409 and "does not qualify" in r.json()["error"]
    assert client.post(f"/api/runs/{rid}/approve").status_code == 409
    assert client.get(f"/api/runs/{rid}/scores").status_code == 200              # tiers and scores untouched
    # back to the default list: qualifies again, roster opens
    client.post(f"/api/runs/{rid}/list", json={"mode": "default"})
    c = wait_committee(client, rid)
    assert c["committee"]["qualification"]["qualified"] and c["committee"]["rules"] == c["default_list"]
    assert client.get(f"/api/runs/{rid}/roster").status_code == 200


def test_custom_list_is_abided_by(client):
    rid = upload(client).json()["id"]
    wait_committee(client, rid)
    mins = {"Champion": 2, "Economic Buyer": 1, "Technical Evaluator": 1, "Influencer": 2}
    client.post(f"/api/runs/{rid}/list", json={"mode": "custom", "minimums": mins})
    c = wait_committee(client, rid)
    assert c["committee"]["rules"] == mins and c["committee"]["list_source"] == "set for this run"
    assert sum(1 for m in c["committee"]["members"] if m["role"] == "Champion") >= 2
    assert c["committee"]["qualification"]["qualified"]
    for bad in ({"mode": "custom", "minimums": {"Champion": "x"}}, {"mode": "custom", "minimums": {**mins, "Champion": -1}},
                {"mode": "x"}):
        assert client.post(f"/api/runs/{rid}/list", json=bad).status_code == 400


def test_generate_committee_writes_the_list_and_retries_once(client, monkeypatch):
    calls = []

    def generated(campaign, store, run_models=True, log=print, progress=None, runner=None, use_checker=True,
                  exclude=None, roles=None, rules=None):
        from sieve.committee.required import generate_list
        calls.append({"exclude": exclude, "roles": roles})
        # first try asks for 9 Economic Buyers (cannot be met); the retry, with 9 taken off, says 1
        counts = {"Champion": "1", "Economic Buyer": "6" if not exclude else "1", "Technical Evaluator": "1",
                  "Influencer": "2"}
        out, _ = generate_list(campaign, lambda a: {"probs": {o: (1.0 if o == counts[a.role] else 0.0) for o in a.options},
                                                    "reasoning": f"{a.model}: {a.role} needs {counts[a.role]}"},
                               exclude=exclude, roles=roles)
        return out
    monkeypatch.setattr(server, "decide_list", generated)
    rid = upload(client).json()["id"]
    wait_committee(client, rid)
    client.post(f"/api/runs/{rid}/list", json={"mode": "generated"})
    c = wait_committee(client, rid)
    assert len(calls) == 2
    assert calls[1] == {"exclude": {"Economic Buyer": {"6"}}, "roles": ["Economic Buyer"]}
    att = c["committee"]["attempts"]
    assert len(att) == 2 and not att[0]["qualified"] and att[1]["qualified"]
    assert c["committee"]["rules"]["Economic Buyer"] == 1 and c["generated_list"]["Economic Buyer"]["retry"]
    assert c["committee"]["list_source"] == "generated by the AI for this campaign"
    assert c["committee"]["qualification"]["qualified"]
    assert client.get(f"/api/runs/{rid}/roster").status_code == 200
    ap = client.post(f"/api/runs/{rid}/approve").json()
    assert "qualification.json" in {f["path"] for f in ap["files"]}
    q = json.loads(client.get(f"/api/runs/{rid}/files/qualification.json").content)
    assert q["committee"]["qualified"] and len(q["attempts"]) == 2 and q["companies"]


LIST_CSV = (b"Role,Count,Description\n"
            b"Economic Buyer,1,Signs the budget for the modernization.\n"
            b"Champion,1,\n"
            b"Security Reviewer,1,Checks the platform against security policy.\n")


def test_uploaded_list_with_a_missing_description_filled_by_qwen(client, monkeypatch):
    from sieve.committee import models
    written = []
    monkeypatch.setattr(models, "check_ollama", lambda *a, **k: [])
    monkeypatch.setattr(models.OllamaModel, "write",
                        lambda self, system, user, max_tokens=160: written.append(user) or "Advocates for the purchase inside the company.")
    multipart = [("campaign", (CAMPAIGN.name, CAMPAIGN.read_bytes(), "application/x-yaml")),
                 ("contacts", (WORKBOOK.name, WORKBOOK.read_bytes(), "application/octet-stream")),
                 ("committee_list", ("list.csv", LIST_CSV, "text/csv"))]
    rid = client.post("/api/runs", files=multipart).json()["id"]
    c = wait_committee(client, rid)
    assert c["status"] == "done", c
    assert len(written) == 1 and "Champion" in written[0]                          # only the missing one
    assert c["uploaded"] and c["list"]["mode"] == "uploaded"
    assert c["base_list"] == {"Economic Buyer": 1, "Champion": 1, "Security Reviewer": 1}
    assert c["roles"]["Champion"]["source"].startswith("written by Qwen")
    assert c["roles"]["Economic Buyer"] == {"text": "Signs the budget for the modernization.", "source": "uploaded"}
    com = c["committee"]
    assert com["rules"] == c["base_list"] and com["list_source"] == "uploaded list"
    assert {m["role"] for m in com["members"]} == {"Economic Buyer", "Champion", "Security Reviewer"}
    assert com["qualification"]["qualified"]
    assert [ln["role"] for ln in com["qualification"]["lines"]] == ["Economic Buyer", "Champion", "Security Reviewer"]
    ro = client.get(f"/api/runs/{rid}/roster").json()
    assert {p["role"] for a in ro["accounts"] for p in a["picks"]} <= {"Economic Buyer", "Champion", "Security Reviewer"}
    # numbers can still be changed for the uploaded roles
    client.post(f"/api/runs/{rid}/list", json={"mode": "custom", "minimums": {"Economic Buyer": 1, "Champion": 2,
                                                                              "Security Reviewer": 1}})
    c = wait_committee(client, rid)
    assert c["committee"]["rules"]["Champion"] == 2 and len(written) == 1           # descriptions are kept
    # back to the built-in default list
    client.post(f"/api/runs/{rid}/list", json={"mode": "default", "builtin": True})
    c = wait_committee(client, rid)
    assert not c["uploaded"] and c["committee"]["rules"] == c["default_list"]


def test_list_upload_later_and_bad_lists(client):
    rid = upload(client).json()["id"]
    wait_committee(client, rid)
    r = client.post(f"/api/runs/{rid}/rolelist", files=[("file", ("l.csv", b"Role,Count\nChampion,x\n", "text/csv"))])
    assert r.status_code == 400 and "whole number" in r.json()["error"]
    r = client.post(f"/api/runs/{rid}/rolelist", files=[("file", ("l.csv", b"Name\nChampion\n", "text/csv"))])
    assert r.status_code == 400 and "Count column" in r.json()["error"]
    ok = b"Role,Count,Description\nEconomic Buyer,1,Pays.\nChampion,1,Pushes for it.\n"
    assert client.post(f"/api/runs/{rid}/rolelist", files=[("file", ("l.csv", ok, "text/csv"))]).status_code == 200
    c = wait_committee(client, rid)
    assert c["committee"]["rules"] == {"Economic Buyer": 1, "Champion": 1}
    t = client.get("/api/list-template.csv")
    assert t.status_code == 200 and t.text.startswith("Role,Count,Description")


def test_metrics_page_data(client):
    rid = upload(client).json()["id"]
    wait_committee(client, rid)
    r = client.get(f"/api/runs/{rid}/metrics").json()
    assert r["status"] == "done" and r["total_seconds"] is not None
    assert [s["stage"] for s in r["stages"]] == ["committee", "targets"]
    assert all(s["seconds"] is not None for s in r["stages"])
    c = r["committee"]
    assert c["members"] >= 6 and c["qualified"] and c["qwen_candidates"] >= 6
    assert c["settings"] == {"check": "same_question", "candidates_per_question": 5, "max_members": 10}
    assert r["targets"]["companies"] >= 1
    b = client.get("/api/metrics/benchmark").json()
    assert b["available"] and b["before"]["f1"] == 0.77
    page = client.get("/").text
    assert "tabMetrics" in page and "Accuracy metrics" in page and "ndcg" not in page.lower()
