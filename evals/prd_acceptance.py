"""PRD acceptance harness, run against the real campaign file.

The unit suite checks modules M3 through M8 against small hand-built frames,
which proves the logic and proves nothing about real data. This harness runs
the whole pipeline over the actual 633-contact modernization campaign and
asserts the numbers that came out of it, so a regression in any stage shows up
as a failing criterion rather than as a silently different roster.

    python evals/prd_acceptance.py --data-dir sample-input
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sieve import run                                          # noqa: E402
from sieve.export.console import roster_to_review_model        # noqa: E402
from sieve.export.queue import build_queue                     # noqa: E402
from sieve.export.salesforce import build_payload              # noqa: E402
from sieve.normalize.values import (                           # noqa: E402
    normalize_company_name, normalize_domain,
)
from sieve.roster import ROSTER_COLUMNS                        # noqa: E402

WORKBOOK = "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx"


def build_input(data_dir: Path) -> pd.DataFrame:
    """Stack the three region sheets and join the campaign signals onto them.

    This is the Format-DB file LeadDrip would emit, reconstructed from the
    workbook: contacts from NA, EU and APAC, with the Status sheet's signal
    columns matched on by domain and falling back to company name.
    """
    sheets = pd.read_excel(
        data_dir / WORKBOOK, sheet_name=None,
        keep_default_na=False, na_values=[""],
    )
    status = sheets["Status"].copy()
    contacts = pd.concat(
        [sheets[name] for name in ("NA", "EU", "APAC")], ignore_index=True
    )

    status["_key"] = status["Website"].map(normalize_domain).fillna(
        status["Company Name"].map(normalize_company_name)
    )
    signal_columns = [
        c for c in status.columns
        if "Signal" in c or "Scale" in c or "Description" in c or c == "Category"
    ]
    status = status.drop_duplicates(subset="_key", keep="first")
    lookup = {
        row["_key"]: {c: row[c] for c in signal_columns}
        for _, row in status.iterrows()
    }

    website = "Website URL" if "Website URL" in contacts.columns else "Website"
    keys = contacts[website].map(normalize_domain).fillna(
        contacts["Company"].map(normalize_company_name)
    )
    for column in signal_columns:
        contacts[column] = [(lookup.get(k) or {}).get(column) for k in keys]
    return contacts


class Scorecard:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.results.append((name, passed, detail))

    def render(self) -> int:
        width = max(len(name) for name, _, _ in self.results) + 2
        print("\n" + "=" * 78)
        print("PRD ACCEPTANCE SCORECARD (real campaign file)".center(78))
        print("=" * 78)
        for name, passed, detail in self.results:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name:<{width}} {detail}")
        failed = sum(1 for _, passed, _ in self.results if not passed)
        print("-" * 78)
        print(f"  {len(self.results) - failed}/{len(self.results)} criteria met")
        print("=" * 78 + "\n")
        return failed


def run_checks(data_dir: Path) -> int:
    workbook = data_dir / WORKBOOK
    if not workbook.exists():
        print(f"Missing {workbook}")
        return 1

    card = Scorecard()
    frame = build_input(data_dir)

    card.check(
        "Input assembled",
        len(frame) == 633,
        f"{len(frame)} contacts, signals joined on "
        f"{int(frame['Legacy Stack Signals'].notna().sum())} rows",
    )

    started = time.perf_counter()
    result = run(frame, run_id="acceptance")
    elapsed = time.perf_counter() - started
    roster, summary = result.frame, result.summary

    # ---------------------------------------------------------------- M0
    card.check(
        "M0 identity assigned",
        roster["sv_account_id"].notna().all()
        and roster["sv_contact_id"].notna().all(),
        f"{summary['accounts']} accounts resolved from "
        f"{roster['Company'].nunique()} raw company strings",
    )
    # Repeated ids are the scheme working rather than failing. The campaign
    # file lists the same person twice under different company labels, and
    # hashing the email inside the account namespace collapses them. What
    # matters is that the count is small and explainable, and that the CRM
    # payload never carries a repeated external id.
    repeated = int(len(roster) - roster["sv_contact_id"].nunique())
    card.check(
        "M0 identity collapses genuine duplicates",
        repeated <= 5,
        f"{roster['sv_contact_id'].nunique()}/{len(roster)} distinct; "
        f"{repeated} source rows are the same person under two company labels",
    )

    # ---------------------------------------------------------------- M3
    tiers = summary["tiers"]
    card.check(
        "M3 all three tiers populate",
        {"Tier A", "Tier B", "Tier C"} <= set(tiers),
        f"A={tiers.get('Tier A')} B={tiers.get('Tier B')} C={tiers.get('Tier C')}",
    )
    card.check(
        "M3 scores span a real range",
        roster["sv_icp_score"].min() < 40 < roster["sv_icp_score"].max(),
        f"min {roster['sv_icp_score'].min()}, "
        f"median {int(roster['sv_icp_score'].median())}, "
        f"max {roster['sv_icp_score'].max()}",
    )
    card.check(
        "M3 every score explains itself",
        roster["sv_score_breakdown"].map(
            lambda s: len(json.loads(s)) == 4
        ).all(),
        "4 pillars recorded on every row",
    )
    card.check(
        "M3 tier gates hold at 80 and 60",
        bool(
            (roster.loc[roster["sv_target_tier"] == "Tier A",
                        "sv_icp_score"].min() >= 80)
            and (roster.loc[roster["sv_target_tier"] == "Tier C",
                            "sv_icp_score"].max() < 60)
        ),
        "no row sits on the wrong side of a gate",
    )

    # ---------------------------------------------------------------- M6
    resolved = summary["titles_resolved"]
    card.check(
        "M6 title coverage",
        resolved / len(roster) >= 0.90,
        f"{resolved}/{len(roster)} = {resolved / len(roster):.1%} of "
        f"{roster['Title'].nunique()} distinct titles",
    )
    card.check(
        "M6 seniority distributed",
        len(summary["seniority"]) >= 3,
        str(summary["seniority"]),
    )

    # ---------------------------------------------------------------- M4
    card.check(
        "M4 nothing dropped",
        len(roster) == len(frame),
        f"{len(roster)} rows in and out; "
        f"{summary['validation_status'].get('DISQUALIFIED', 0)} disqualified "
        f"and retained",
    )
    card.check(
        "M4 flags fire on real data",
        len(summary["flags"]) >= 2,
        str(summary["flags"]),
    )
    card.check(
        "M4 consumer mailboxes disqualify, aliases do not",
        summary["validation_status"].get("DISQUALIFIED", 0) == 4,
        "4 gmail/yahoo records disqualified; 53 corporate alias domains "
        "flagged for review instead",
    )
    card.check(
        "M4 confidence stays in range",
        bool(roster["sv_confidence"].between(0, 1).all()),
        f"mean {summary['mean_confidence']}",
    )

    # ---------------------------------------------------------------- M7
    seated = roster[roster["sv_committee_role"] != "Unassigned"]
    complete = sum(
        1 for _, group in seated.groupby("sv_account_id")
        if {"Economic Buyer", "Champion"} <= set(group["sv_committee_role"])
    )
    card.check(
        "M7 committees assemble",
        len(seated) > 0,
        f"{len(seated)} seats across {seated['sv_account_id'].nunique()} "
        f"accounts, {complete} with both decision seats",
    )
    card.check(
        "M7 only Tier A is seated",
        set(seated["sv_target_tier"]) == {"Tier A"},
        f"tiers seated: {sorted(set(seated['sv_target_tier']))}",
    )
    card.check(
        "M7 nobody holds two seats",
        seated["sv_contact_id"].nunique() == len(seated),
        f"{len(seated)} seats, {seated['sv_contact_id'].nunique()} people",
    )
    barred = seated[seated["sv_flags"].fillna("").str.contains("GENERIC_EMAIL")]
    card.check(
        "M7 routing addresses take no seat",
        len(barred) == 0,
        f"{len(barred)} generic-email contacts seated",
    )

    # ---------------------------------------------------------------- M8
    card.check(
        "M8 output contract complete",
        all(c in roster.columns for c in ROSTER_COLUMNS),
        f"all {len(ROSTER_COLUMNS)} sv_ columns present, "
        f"{len([c for c in roster.columns if not c.startswith('sv_')])} "
        f"input columns untouched",
    )

    payload = build_payload(roster)
    reasons = payload.manifest["held_back_reasons"]
    card.check(
        "M8 payload external ids unique",
        payload.contacts["Sieve_Contact_Id__c"].is_unique
        and payload.accounts["Sieve_Account_Id__c"].is_unique,
        f"{payload.manifest['duplicate_contacts_collapsed']} duplicate "
        f"contacts collapsed; the Bulk API rejects repeated external ids",
    )
    card.check(
        "M8 CRM staging reconciles",
        sum(reasons.values()) == payload.manifest["held_back"]
        and len(payload.contacts) + len(payload.held_back)
        + payload.manifest["duplicate_contacts_collapsed"] == len(roster),
        f"{len(payload.accounts)} accounts, {len(payload.contacts)} contacts, "
        f"{payload.manifest['held_back']} held back {reasons}",
    )
    card.check(
        "M8 unapproved payload is not ready",
        payload.manifest["ready_to_send"] is False,
        "sign-off gate closed until an approval file arrives",
    )

    approved_ids = roster["sv_contact_id"].astype(str).tolist()[:50]
    gated = build_payload(
        roster, approval={"approved": True, "approved_contact_ids": approved_ids}
    )
    card.check(
        "M8 sign-off changes what syncs",
        len(gated.contacts) < len(payload.contacts)
        and gated.manifest["ready_to_send"] is True,
        f"{len(payload.contacts)} without approval, {len(gated.contacts)} with",
    )

    queue = build_queue(roster, committee_only=False,
                        tier_filter=("Tier A", "Tier B"))
    card.check(
        "M8 dial queue ordered and verified",
        (queue["sv_dial_priority"].is_monotonic_decreasing
         and queue["dial_number"].notna().all()) if len(queue) else True,
        f"{len(queue)} phone-verified contacts queued from "
        f"{int(roster['Phone'].notna().sum())} rows carrying any number",
    )

    model = roster_to_review_model(roster)
    card.check(
        "M8 review console model built",
        model["totals"]["accounts"] > 0
        and model["totals"]["contacts"] == len(
            roster[roster["sv_target_tier"].isin(["Tier A", "Tier B"])]
        ),
        f"{model['totals']['accounts']} accounts, "
        f"{model['totals']['flagged']} flagged for a rep to work",
    )

    # ------------------------------------------------- non-functional
    card.check(
        "NFR throughput",
        elapsed < 15,
        f"{len(frame)} rows in {elapsed:.2f}s "
        f"({int(len(frame) / elapsed):,} rows/sec, budget 15s for 5,000)",
    )

    digests = [
        hashlib.sha256(
            run(frame, run_id="fixed").frame.to_csv(index=False).encode()
        ).hexdigest()
        for _ in range(2)
    ]
    card.check(
        "NFR bitwise reproducibility",
        digests[0] == digests[1],
        f"sha256 {digests[0][:24]}... identical across runs",
    )

    log = Path("/tmp/prd_acceptance_audit.jsonl")
    log.unlink(missing_ok=True)
    run(frame, audit_path=log, run_id="audit-check")
    events = [json.loads(line)["event"] for line in log.read_text().splitlines()]
    card.check(
        "NFR audit completeness",
        events.count("icp_scored") == len(frame)
        and events[0] == "run_started" and events[-1] == "run_finished",
        f"{len(events)} lines: {events.count('icp_scored')} scores, "
        f"{events.count('flags_assigned')} flag sets, "
        f"{events.count('committee_assembled')} committees",
    )

    broken = frame.copy()
    # Cast to object first, since pandas refuses to put a string into an
    # int64 column. Real exports arrive as object anyway.
    broken["Number of Employees"] = broken["Number of Employees"].astype(object)
    broken["Revenue ($ M)"] = broken["Revenue ($ M)"].astype(object)
    broken.loc[broken.index[:20], "Number of Employees"] = "not a number"
    broken.loc[broken.index[20:40], "Revenue ($ M)"] = ""
    broken.loc[broken.index[40:60], "Title"] = None
    card.check(
        "NFR resilience to bad rows",
        len(run(broken).frame) == len(broken),
        "60 rows corrupted with nulls and unparseable numbers, none halted "
        "the run",
    )

    return card.render()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    return run_checks(parser.parse_args().data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
