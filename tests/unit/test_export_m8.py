"""Tests for PRD module M8: review console, CRM sync and dial queue."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from sieve import run
from sieve.export.console import build_console, roster_to_review_model
from sieve.export.queue import build_queue, write_queue
from sieve.export.salesforce import build_payload, load_approval


@pytest.fixture
def roster() -> pd.DataFrame:
    frame = pd.DataFrame({
        "First Name": ["Ben", "Joshua", "Kirstie", "Routing", "Outsider"],
        "Last Name": ["John", "Kinsell", "Brown", "Address", "Smallco"],
        "Title": ["Chief Technology Officer", "Chief Financial Officer",
                  "VP of Product", "CEO", "CEO"],
        "Company": ["Digital Turbine"] * 4 + ["Tiny Builders"],
        "Website URL": ["digitalturbine.com"] * 4 + ["tinybuilders.com"],
        "Email": ["ben.john@digitalturbine.com", "j.kinsell@digitalturbine.com",
                  "k.brown@digitalturbine.com", "info@digitalturbine.com",
                  "o.smallco@tinybuilders.com"],
        "Phone": ["6466062174", "2125551234", None, "2120000000", "3105551111"],
        "Direct Dial 1": ["6466062174", None, None, None, None],
        "Mobile": [None, "9175551212", None, None, None],
        "Industry": ["Demand-Side Platform"] * 4 + ["Construction"],
        "Number of Employees": [1200] * 4 + [15],
        "Legacy Stack Signals": ["ASP.NET monolith, 12 year codebase"] * 4 + [""],
        "Modernization Intent Signal": ["post-acquisition consolidation"] * 4 + [""],
    })
    return run(frame).frame


class TestSalesforceStaging:
    def test_account_payload_deduplicates_by_account(self, roster):
        payload = build_payload(roster)
        assert len(payload.accounts) == payload.accounts[
            "Sieve_Account_Id__c"].nunique()

    def test_external_ids_drive_the_upsert(self, roster):
        payload = build_payload(roster)
        assert payload.manifest["operation"] == "upsert"
        assert payload.manifest["account_external_id"] == "Sieve_Account_Id__c"
        assert payload.manifest["contact_external_id"] == "Sieve_Contact_Id__c"

    def test_analytical_columns_reach_salesforce(self, roster):
        payload = build_payload(roster)
        for field in ("Sieve_ICP_Score__c", "Sieve_Target_Tier__c",
                      "Sieve_Score_Breakdown__c"):
            assert field in payload.accounts.columns
        for field in ("Sieve_Committee_Role__c", "Sieve_Confidence__c",
                      "Sieve_Flags__c", "Sieve_Direct_Dial__c"):
            assert field in payload.contacts.columns

    def test_out_of_tier_accounts_are_held_back(self, roster):
        payload = build_payload(roster, tier_filter=("Tier A",))
        companies = set(payload.accounts["Name"])
        assert "Tiny Builders" not in companies
        assert payload.manifest["held_back"] > 0

    def test_revenue_scales_from_millions(self):
        frame = pd.DataFrame({
            "Company": ["Alpha"], "Website URL": ["alpha.com"],
            "Title": ["CEO"], "Email": ["a@alpha.com"],
            "Industry": ["Ad Exchange"], "Number of Employees": [2000],
            "Revenue ($ M)": [250],
        })
        payload = build_payload(run(frame).frame, tier_filter=("Tier A", "Tier B",
                                                              "Tier C"))
        assert payload.accounts["AnnualRevenue"].iloc[0] == 250_000_000

    def test_reasons_sum_to_the_held_back_total(self, roster):
        payload = build_payload(roster, tier_filter=("Tier A",))
        reasons = payload.manifest["held_back_reasons"]
        assert sum(reasons.values()) == payload.manifest["held_back"]

    def test_an_unapproved_payload_is_not_marked_ready(self, roster):
        assert build_payload(roster).manifest["ready_to_send"] is False

    def test_approval_marks_it_ready_and_drops_what_the_rep_dropped(self, roster):
        keep = roster["sv_contact_id"].astype(str).tolist()
        dropped = keep.pop()
        payload = build_payload(
            roster,
            approval={"approved": True, "approved_contact_ids": keep},
            tier_filter=("Tier A", "Tier B", "Tier C"),
        )
        assert payload.manifest["ready_to_send"] is True
        assert dropped not in set(payload.contacts["Sieve_Contact_Id__c"])

    def test_load_approval_rejects_an_unapproved_file(self, tmp_path):
        path = tmp_path / "approval.json"
        path.write_text(json.dumps({"approved": False}))
        with pytest.raises(ValueError, match="not an approval"):
            load_approval(path)

    def test_load_approval_requires_the_id_list(self, tmp_path):
        path = tmp_path / "approval.json"
        path.write_text(json.dumps({"approved": True}))
        with pytest.raises(ValueError, match="approved_contact_ids"):
            load_approval(path)

    def test_a_roster_without_ids_is_refused(self):
        with pytest.raises(ValueError, match="missing"):
            build_payload(pd.DataFrame({"Company": ["Alpha"]}))

    def test_files_are_written_where_asked(self, roster, tmp_path):
        paths = build_payload(roster).write(tmp_path / "sf")
        assert all(path.exists() for path in paths.values())
        assert json.loads(paths["manifest"].read_text())["operation"] == "upsert"


class TestDialQueue:
    def test_only_phone_verified_contacts_are_queued(self, roster):
        queue = build_queue(roster, committee_only=False,
                            tier_filter=("Tier A", "Tier B", "Tier C"))
        assert queue["dial_number"].notna().all()
        assert (queue["dial_number"].str.len() >= 6).all()

    def test_a_direct_dial_is_preferred_over_a_switchboard(self, roster):
        queue = build_queue(roster, committee_only=False,
                            tier_filter=("Tier A", "Tier B", "Tier C"))
        ben = queue[queue["full_name"] == "Ben John"]
        assert ben["number_type"].iloc[0] == "direct"

    def test_the_queue_is_ordered_by_priority(self, roster):
        queue = build_queue(roster, committee_only=False,
                            tier_filter=("Tier A", "Tier B", "Tier C"))
        assert queue["sv_dial_priority"].is_monotonic_decreasing
        assert queue["queue_position"].tolist() == list(range(1, len(queue) + 1))

    def test_disqualified_records_never_queue(self):
        frame = pd.DataFrame({
            "Company": ["Alpha"], "Website URL": ["alpha.com"],
            "Title": ["CEO"], "Email": ["someone@gmail.com"],
            "Phone": ["6466062174"], "Industry": ["Ad Exchange"],
            "Number of Employees": [2000],
        })
        queue = build_queue(run(frame).frame, committee_only=False,
                            tier_filter=("Tier A", "Tier B", "Tier C"))
        assert queue.empty

    def test_committee_only_is_the_default(self, roster):
        default = build_queue(roster)
        widened = build_queue(roster, committee_only=False)
        assert len(default) <= len(widened)

    def test_queue_order_repeats(self, roster):
        first = build_queue(roster, committee_only=False)
        second = build_queue(roster.sample(frac=1, random_state=7),
                             committee_only=False)
        assert first["sv_contact_id"].tolist() == second["sv_contact_id"].tolist()

    def test_an_empty_queue_keeps_its_columns(self):
        frame = pd.DataFrame({
            "Company": ["Alpha"], "Website URL": ["alpha.com"],
            "Title": ["CEO"], "Email": ["a@alpha.com"],
            "Industry": ["Construction"], "Number of Employees": [3],
        })
        queue = build_queue(run(frame).frame)
        assert queue.empty
        assert "queue_position" in queue.columns

    def test_write_queue_produces_both_files(self, roster, tmp_path):
        paths = write_queue(build_queue(roster), tmp_path / "q")
        assert paths["queue"].exists()
        assert "by_committee_role" in json.loads(paths["summary"].read_text())

    def test_a_roster_without_priority_is_refused(self):
        with pytest.raises(ValueError, match="sv_dial_priority"):
            build_queue(pd.DataFrame({"Company": ["Alpha"]}))


class TestReviewConsole:
    def test_the_model_groups_contacts_under_their_account(self, roster):
        model = roster_to_review_model(roster, tier_filter=("Tier A", "Tier B",
                                                           "Tier C"))
        companies = {a["company"] for a in model["accounts"]}
        assert "Digital Turbine" in companies
        turbine = next(a for a in model["accounts"]
                       if a["company"] == "Digital Turbine")
        assert len(turbine["members"]) == 4

    def test_committee_completeness_is_reported(self, roster):
        model = roster_to_review_model(roster)
        turbine = next(a for a in model["accounts"]
                       if a["company"] == "Digital Turbine")
        assert turbine["complete"] is True

    def test_flagged_contacts_are_counted(self, roster):
        model = roster_to_review_model(roster, tier_filter=("Tier A", "Tier B",
                                                           "Tier C"))
        assert model["totals"]["flagged"] >= 1

    def test_accounts_sort_by_score(self, roster):
        model = roster_to_review_model(roster, tier_filter=("Tier A", "Tier B",
                                                           "Tier C"))
        scores = [a["icp_score"] for a in model["accounts"]]
        assert scores == sorted(scores, reverse=True)

    def test_the_page_is_self_contained(self, roster):
        page = build_console(roster_to_review_model(roster))
        assert "<script" in page and "</html>" in page
        assert "http://" not in page and "https://" not in page
        assert "localStorage" not in page

    def test_the_model_is_embedded_as_parseable_json(self, roster):
        page = build_console(roster_to_review_model(roster))
        raw = page.split("const DATA = ", 1)[1].split(";\n", 1)[0]
        model = json.loads(raw.replace("<\\/", "</"))
        assert "accounts" in model and "totals" in model

    def test_a_closing_script_tag_in_the_data_cannot_break_the_page(self):
        frame = pd.DataFrame({
            "Company": ["</script><b>oops</b>"], "Website URL": ["x.com"],
            "Title": ["CEO"], "Email": ["a@x.com"],
            "Industry": ["Ad Exchange"], "Number of Employees": [2000],
        })
        page = build_console(roster_to_review_model(run(frame).frame))
        head, _, tail = page.partition("const DATA = ")
        assert "</script>" not in tail.split(";\n", 1)[0]


class TestApprovalGate:
    def test_the_gate_changes_what_reaches_the_crm(self, roster):
        everything = build_payload(
            roster, tier_filter=("Tier A", "Tier B", "Tier C"))
        keep = roster["sv_contact_id"].astype(str).tolist()[:2]
        approved = build_payload(
            roster,
            approval={"approved": True, "approved_contact_ids": keep},
            tier_filter=("Tier A", "Tier B", "Tier C"),
        )
        assert len(approved.contacts) < len(everything.contacts)
        assert len(approved.contacts) == 2

    def test_held_back_records_are_kept_for_inspection(self, roster):
        payload = build_payload(roster, tier_filter=("Tier A",))
        assert len(payload.held_back) + len(payload.contacts) == len(roster)


class TestDuplicateExternalIds:
    """The Bulk API rejects a batch carrying the same external id twice.

    Duplicates arrive legitimately: the real campaign file lists the same
    person under 'Criteo' and 'Criteo (EU HQ)', and the UUID5 scheme correctly
    collapses both onto one contact id.
    """

    @staticmethod
    def _same_person_twice() -> pd.DataFrame:
        frame = pd.DataFrame({
            "First Name": ["Todd", "Todd"],
            "Last Name": ["Parsons", "Parsons"],
            "Title": ["Chief Product Officer"] * 2,
            "Company": ["Criteo", "Criteo (EU HQ)"],
            "Website URL": ["criteo.com", "criteo.com"],
            "Email": ["t.parsons@criteo.com"] * 2,
            "Industry": ["Demand-Side Platform"] * 2,
            "Number of Employees": [3000] * 2,
            "Legacy Stack Signals": ["ASP.NET monolith, 14 year codebase"] * 2,
        })
        return run(frame).frame

    def test_the_same_person_collapses_onto_one_id(self):
        roster = self._same_person_twice()
        assert len(roster) == 2
        assert roster["sv_contact_id"].nunique() == 1
        assert roster["sv_account_id"].nunique() == 1

    def test_the_payload_carries_each_external_id_once(self):
        payload = build_payload(
            self._same_person_twice(),
            tier_filter=("Tier A", "Tier B", "Tier C"),
        )
        assert payload.contacts["Sieve_Contact_Id__c"].is_unique
        assert payload.accounts["Sieve_Account_Id__c"].is_unique
        assert len(payload.contacts) == 1

    def test_the_manifest_reports_what_it_collapsed(self):
        payload = build_payload(
            self._same_person_twice(),
            tier_filter=("Tier A", "Tier B", "Tier C"),
        )
        assert payload.manifest["duplicate_contacts_collapsed"] == 1

    def test_counts_still_reconcile_against_the_roster(self):
        roster = self._same_person_twice()
        payload = build_payload(roster, tier_filter=("Tier A", "Tier B", "Tier C"))
        total = (len(payload.contacts) + len(payload.held_back)
                 + payload.manifest["duplicate_contacts_collapsed"])
        assert total == len(roster)
