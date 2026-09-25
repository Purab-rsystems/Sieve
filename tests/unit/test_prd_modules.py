"""Tests for Sieve v1.0, covering PRD modules M0, M3, M4, M6, M7 and M8.

Every assertion traces to a clause in the PRD. Where a number appears, it is
the number the PRD specifies.
"""

from __future__ import annotations

import json
import uuid

import pandas as pd
import pytest

from sieve import (
    account_id, assemble, contact_id, designate, identify, load_rubric,
    load_taxonomy, run, score, validate,
)
from sieve.committee.assembly import (
    CommitteeCandidate, SEAT_CAPACITY, assemble_committee,
)
from sieve.export.dial_queue import DialInputs, dial_priority
from sieve.roster import ROSTER_COLUMNS
from sieve.validation.engine import (
    SEVERITY_PENALTY, ContactUnderTest, EmailPatternIndex, Severity,
    validate_contact,
)


# ==========================================================================
# M0 — deterministic identity
# ==========================================================================

class TestDeterministicIdentity:
    def test_account_id_is_a_valid_uuid5(self):
        result = account_id("www.adform.com", "Adform")
        parsed = uuid.UUID(result.value)
        assert parsed.version == 5

    def test_same_domain_always_yields_the_same_id(self):
        first = account_id("https://WWW.Adform.com/products/", "Adform")
        second = account_id("adform.com", "Adform Inc.")
        assert first.value == second.value
        assert first.basis == "domain"

    def test_name_fallback_when_no_domain_exists(self):
        first = account_id(None, "33Across")
        second = account_id(None, "33Across Inc.")
        assert first.value == second.value
        assert first.basis == "name"

    def test_contact_id_is_namespaced_by_its_account(self):
        alpha = account_id("alpha.com", "Alpha")
        beta = account_id("beta.com", "Beta")
        assert (
            contact_id(alpha.value, "jane@x.com").value
            != contact_id(beta.value, "jane@x.com").value
        )

    def test_contact_id_falls_back_through_linkedin_then_name(self):
        parent = account_id("alpha.com", "Alpha")
        assert contact_id(parent.value, None,
                          "linkedin.com/in/jane").basis == "linkedin"
        assert contact_id(parent.value, None, None, "Jane Doe").basis == "name"

    def test_ids_survive_a_round_trip_through_a_frame(self):
        frame = pd.DataFrame({
            "Company": ["Adform", "Adform"],
            "Website": ["www.adform.com", "adform.com"],
            "Email": ["a@adform.com", "b@adform.com"],
        })
        out = identify(frame)
        assert out["sv_account_id"].nunique() == 1
        assert out["sv_contact_id"].nunique() == 2


# ==========================================================================
# M6 — designation and authority
# ==========================================================================

class TestDesignationEngine:
    @staticmethod
    @pytest.fixture(scope="class")
    def taxonomy():
        return load_taxonomy()

    @pytest.mark.parametrize("title", [
        "VP Tech & Architecture", "Head of Engineering", "VP Platform",
        "Vice President Of Engineering", "SVP, Engineering",
    ])
    def test_prd_examples_normalize_to_vp_of_engineering(self, taxonomy, title):
        """The PRD names these three explicitly under module M6."""
        result = taxonomy.resolve(title)
        assert result.canonical in {"VP of Engineering", "VP of Platform Engineering"}
        assert result.seniority == "VP"

    def test_c_suite_titles_carry_authority(self, taxonomy):
        for title in ("CEO", "CFO", "CTO", "Chief Operating Officer"):
            assert taxonomy.resolve(title).is_authoritative

    def test_practitioners_carry_no_authority(self, taxonomy):
        for title in ("Data Engineer", "Solutions Architect", "Data Analyst"):
            result = taxonomy.resolve(title)
            assert result.is_authoritative is False
            assert result.committee_role is None

    @pytest.mark.parametrize("title", [
        "Data Engineer Intern", "Former CTO", "Retired Chief Executive Officer",
    ])
    def test_disqualifying_tokens_block_a_match(self, taxonomy, title):
        result = taxonomy.resolve(title)
        assert result.canonical is None
        assert result.match_basis == "disqualified"

    def test_seniority_ambiguity_is_detected(self, taxonomy):
        """PRD M4 SENIORITY_AMBIGUITY: 'Specialist', 'Consultant'."""
        assert taxonomy.resolve("Senior Consultant").is_ambiguous
        assert taxonomy.resolve("Marketing Specialist").is_ambiguous
        assert taxonomy.resolve("CTO").is_ambiguous is False

    def test_committee_mapping_matches_the_prd_seats(self, taxonomy):
        assert taxonomy.resolve("CEO").committee_role == "Economic Buyer"
        assert taxonomy.resolve("CFO").committee_role == "Economic Buyer"
        assert taxonomy.resolve("CTO").committee_role == "Champion"
        assert taxonomy.resolve("Chief Architect").committee_role == "Champion"
        assert taxonomy.resolve("VP of Product").committee_role == "Influencer"

    def test_designate_appends_the_expected_columns(self):
        frame = pd.DataFrame({"Title": ["CTO", "Data Engineer Intern"]})
        out = designate(frame)
        # pandas stores the missing value as NaN in an object column.
        assert out["sv_canonical_title"].iloc[0] == "Chief Technology Officer"
        assert pd.isna(out["sv_canonical_title"].iloc[1])
        assert out["sv_seniority"].tolist() == ["C-Suite", "Other"]
        assert out["sv_is_authoritative"].tolist() == [True, False]


# ==========================================================================
# M3 — deterministic ICP scoring
# ==========================================================================

class TestIcpScoring:
    @staticmethod
    @pytest.fixture(scope="class")
    def rubric():
        return load_rubric()

    def test_four_pillars_each_weighted_a_quarter(self, rubric):
        assert set(rubric.weights.values()) == {0.25}
        assert len(rubric.weights) == 4

    def test_a_perfect_account_reaches_tier_a(self, rubric):
        result = rubric.score(
            industry="Demand-Side Platform",
            legacy_signal="ASP.NET monolith, 15 year codebase",
            employees=5000,
            modernization_signal="post-acquisition platform consolidation",
        )
        assert result.score == 100
        assert result.tier == "Tier A"

    def test_an_unrelated_account_lands_in_tier_c(self, rubric):
        result = rubric.score(
            industry="Construction", legacy_signal="cloud-native serverless",
            employees=20,
        )
        assert result.tier == "Tier C"
        assert result.score < 60

    def test_tier_boundaries_match_the_prd(self, rubric):
        """Tier A at 80 and above, Tier B from 60 to 79, Tier C below 60."""
        assert rubric._classify(80)[0] == "Tier A"
        assert rubric._classify(79)[0] == "Tier B"
        assert rubric._classify(60)[0] == "Tier B"
        assert rubric._classify(59)[0] == "Tier C"

    def test_modern_stacks_score_low_on_pillar_two(self, rubric):
        """A cloud-native stack has no modernization pain to sell into."""
        modern = rubric.score(industry="DSP", legacy_signal="React, Node, serverless")
        legacy = rubric.score(industry="DSP", legacy_signal="VB.NET thick client")
        modern_points = next(p.points for p in modern.pillars if p.pillar == "legacy_stack")
        legacy_points = next(p.points for p in legacy.pillars if p.pillar == "legacy_stack")
        assert modern_points == 20
        assert legacy_points == 100

    def test_every_score_carries_its_evidence(self, rubric):
        result = rubric.score(industry="Supply-Side Platform (SSP)", employees=2000)
        assert len(result.pillars) == 4
        industry = next(p for p in result.pillars if p.pillar == "industry_fit")
        assert industry.evidence is not None
        assert industry.points == 100

    def test_missing_data_falls_through_without_raising(self, rubric):
        result = rubric.score()
        assert 0 <= result.score <= 100
        assert result.tier == "Tier C"

    def test_score_is_repeatable(self, rubric):
        kwargs = dict(industry="Ad Exchange", legacy_signal="Java monolith",
                      employees=1500, modernization_signal="cookieless")
        assert rubric.score(**kwargs).score == rubric.score(**kwargs).score

    def test_score_appends_a_parseable_breakdown(self):
        frame = pd.DataFrame({
            "Industry": ["Programmatic Advertising"],
            "Number of Employees": [3000],
        })
        out = score(frame)
        breakdown = json.loads(out["sv_score_breakdown"].iloc[0])
        assert {p["pillar"] for p in breakdown} == {
            "industry_fit", "legacy_stack", "platform_scale",
            "modernization_triggers",
        }


# ==========================================================================
# M4 — validation and flagging
# ==========================================================================

class TestFlagging:
    @staticmethod
    @pytest.fixture(scope="class")
    def taxonomy():
        return load_taxonomy()

    def test_penalties_match_the_prd(self):
        assert SEVERITY_PENALTY == {
            "Critical": 0.40, "High": 0.25, "Medium": 0.15, "Low": 0.05,
        }

    def test_a_clean_record_scores_full_confidence(self, taxonomy):
        result = validate_contact(ContactUnderTest(
            email="adam.assinder@uobgroup.com", company_domain="uobgroup.com",
            title="CTO", designation=taxonomy.resolve("CTO"),
        ))
        assert result.confidence == 1.00
        assert result.status == "VERIFIED"
        assert result.flags == []

    def test_title_mismatch_fires_at_medium(self):
        result = validate_contact(ContactUnderTest(
            title="First Vice President, Data & Analytics Programmes",
            alternate_title="Data & Analytics Programme Lead",
            alternate_source="Apollo",
        ))
        assert result.codes == "TITLE_MISMATCH"
        assert result.confidence == 0.85
        assert result.status == "FLAGGED_REVIEW"

    @pytest.mark.parametrize("email", [
        "info@adform.com", "sales@adform.com", "support@adform.com",
        "contact@adform.com", "admin@adform.com",
    ])
    def test_generic_email_fires_at_high(self, email):
        result = validate_contact(ContactUnderTest(
            email=email, company_domain="adform.com"))
        assert "GENERIC_EMAIL" in result.codes
        assert result.confidence == 0.75

    def test_a_personal_name_is_not_a_generic_address(self):
        result = validate_contact(ContactUnderTest(
            email="vaidas.dzedulionis@adform.com", company_domain="adform.com"))
        assert "GENERIC_EMAIL" not in result.codes

    def test_consumer_mailbox_disqualifies(self):
        """A gmail address on a corporate record is a data-entry error."""
        result = validate_contact(ContactUnderTest(
            email="jonahrrosenberg@gmail.com", company_domain="basis.com"))
        assert "DOMAIN_ANOMALY" in result.codes
        assert result.status == "DISQUALIFIED"
        assert result.confidence == 0.60

    def test_a_corporate_alias_domain_flags_without_disqualifying(self):
        """Outbrain acquired Teads, so teads.com mail is fine, and reviewable.

        Treating these as Critical would disqualify 9% of the campaign file
        over acquisitions and rebrands.
        """
        result = validate_contact(ContactUnderTest(
            email="someone@teads.com", company_domain="outbrain.com"))
        assert "DOMAIN_ANOMALY" in result.codes
        assert result.status == "FLAGGED_REVIEW"
        assert result.confidence == 0.85

    def test_a_regional_domain_does_not_flag(self):
        result = validate_contact(ContactUnderTest(
            email="jan@adform.de", company_domain="adform.com"))
        assert result.codes == ""

    def test_seniority_ambiguity_fires_at_low(self, taxonomy):
        result = validate_contact(ContactUnderTest(
            title="Senior Consultant",
            designation=taxonomy.resolve("Senior Consultant"),
        ))
        assert "SENIORITY_AMBIGUITY" in result.codes
        assert result.confidence == 0.95

    def test_switchboard_numbers_flag_at_medium(self):
        for number in ("6535350000", "6566666666"):
            result = validate_contact(ContactUnderTest(phone=number))
            assert "DISPOSABLE_PHONE" in result.codes

    def test_a_direct_dial_does_not_flag(self):
        result = validate_contact(ContactUnderTest(phone="6466062174"))
        assert result.codes == ""

    def test_penalties_accumulate(self):
        result = validate_contact(ContactUnderTest(
            email="info@gmail.com", company_domain="adform.com",
        ))
        # GENERIC_EMAIL (0.25) plus DOMAIN_ANOMALY Critical (0.40)
        assert result.confidence == 0.35

    def test_confidence_never_goes_negative(self):
        result = validate_contact(ContactUnderTest(
            email="info@gmail.com", company_domain="x.com",
            title="Specialist", alternate_title="Consultant",
            phone="6500000000", tenure_role_months=1,
        ))
        assert 0.0 <= result.confidence <= 1.0

    def test_nothing_is_ever_dropped(self):
        """PRD: suspect records are flagged, never silently removed."""
        frame = pd.DataFrame({
            "Email": ["info@x.com", "real.person@x.com", "bad@gmail.com"],
            "Website": ["x.com"] * 3,
            "Title": ["CTO", "CEO", "Consultant"],
        })
        out = validate(frame)
        assert len(out) == len(frame)
        assert list(out["Email"]) == list(frame["Email"])


class TestEmailPatternIndex:
    def test_learns_the_dominant_shape(self):
        index = EmailPatternIndex([
            "a.b@x.com", "c.d@x.com", "e.f@x.com", "gh@x.com",
        ])
        assert index.expected("x.com") == "word.word"
        assert index.sample_size("x.com") == 4

    def test_stays_quiet_below_three_samples(self):
        index = EmailPatternIndex(["a.b@y.com", "c.d@y.com"])
        assert index.expected("y.com") is None

    def test_shape_normalizes_digits(self):
        assert EmailPatternIndex.shape("j.smith2") == "word.word"
        assert EmailPatternIndex.shape("jsmith") == "word"


# ==========================================================================
# M7 — committee assembly
# ==========================================================================

def _candidate(contact_id_value: str, seat: str | None, rank: int,
               **overrides) -> CommitteeCandidate:
    defaults = dict(
        contact_id=contact_id_value, account_id="acct-1", full_name="Someone",
        canonical_title="Title", seniority="VP", seniority_rank=rank,
        natural_seat=seat, is_authoritative=True, confidence=1.0,
    )
    defaults.update(overrides)
    return CommitteeCandidate(**defaults)


class TestCommitteeAssembly:
    def test_each_prd_seat_gets_filled(self):
        committee = assemble_committee("acct-1", [
            _candidate("c1", "Economic Buyer", 100),
            _candidate("c2", "Champion", 100),
            _candidate("c3", "Influencer", 80),
        ])
        assert committee.filled_seats == {"Economic Buyer", "Champion", "Influencer"}
        assert committee.is_complete

    def test_seniority_wins_the_seat(self):
        committee = assemble_committee("acct-1", [
            _candidate("junior", "Champion", 60),
            _candidate("senior", "Champion", 100),
        ])
        assert committee.seats[0].contact_id == "senior"

    def test_confidence_breaks_a_seniority_tie(self):
        committee = assemble_committee("acct-1", [
            _candidate("low", "Champion", 100, confidence=0.7),
            _candidate("high", "Champion", 100, confidence=1.0),
        ])
        assert committee.seats[0].contact_id == "high"

    def test_contact_id_breaks_a_full_tie_so_runs_repeat(self):
        pool = [
            _candidate("zzz", "Champion", 100),
            _candidate("aaa", "Champion", 100),
        ]
        first = assemble_committee("acct-1", pool)
        second = assemble_committee("acct-1", list(reversed(pool)))
        assert first.seats[0].contact_id == second.seats[0].contact_id == "aaa"

    def test_generic_email_is_barred_from_every_seat(self):
        committee = assemble_committee("acct-1", [
            _candidate("c1", "Economic Buyer", 100,
                       flag_codes=frozenset({"GENERIC_EMAIL"})),
        ])
        assert committee.seats == []

    def test_ambiguous_seniority_is_barred_from_decision_seats(self):
        """The PRD system action for SENIORITY_AMBIGUITY, enforced."""
        barred = assemble_committee("acct-1", [
            _candidate("c1", "Champion", 40,
                       flag_codes=frozenset({"SENIORITY_AMBIGUITY"})),
        ])
        assert barred.seats == []

        allowed = assemble_committee("acct-1", [
            _candidate("c2", "Influencer", 40,
                       flag_codes=frozenset({"SENIORITY_AMBIGUITY"})),
        ])
        assert allowed.filled_seats == {"Influencer"}

    def test_disqualified_records_take_no_seat(self):
        committee = assemble_committee("acct-1", [
            _candidate("c1", "Champion", 100, validation_status="DISQUALIFIED"),
        ])
        assert committee.seats == []

    def test_influencer_seats_are_multi_threaded(self):
        committee = assemble_committee("acct-1", [
            _candidate(f"c{i}", "Influencer", 80) for i in range(6)
        ])
        assert len(committee.seats) == SEAT_CAPACITY["Influencer"]

    def test_nobody_holds_two_seats(self):
        committee = assemble_committee("acct-1", [
            _candidate("c1", "Economic Buyer", 100),
            _candidate("c2", "Champion", 100),
        ])
        seated = [seat.contact_id for seat in committee.seats]
        assert len(seated) == len(set(seated))

    def test_assemble_requires_ids(self):
        with pytest.raises(ValueError, match="sv_account_id"):
            assemble(pd.DataFrame({"Title": ["CTO"]}))


# ==========================================================================
# M8 — dial priority
# ==========================================================================

class TestDialPriority:
    def test_formula_matches_the_prd(self):
        """(icp x 0.50) + (seniority x 0.30) + (phone verified x 0.20)."""
        result = dial_priority(DialInputs(
            icp_score=100, seniority_rank=100,
            has_direct_dial=True, has_mobile=False, phone_flagged=False,
        ))
        assert result == 100.0

    def test_a_flagged_phone_scores_zero_on_that_term(self):
        with_phone = dial_priority(DialInputs(90, 100, True, False, False))
        flagged = dial_priority(DialInputs(90, 100, True, False, True))
        assert with_phone - flagged == pytest.approx(20.0)

    def test_mobile_counts_for_half_of_a_direct_dial(self):
        direct = dial_priority(DialInputs(0, 0, True, False, False))
        mobile = dial_priority(DialInputs(0, 0, False, True, False))
        assert direct == 20.0 and mobile == 10.0

    def test_priority_stays_inside_bounds(self):
        assert dial_priority(DialInputs(0, 0, False, False, True)) == 0.0
        assert dial_priority(DialInputs(100, 100, True, True, False)) <= 100.0


# ==========================================================================
# End to end
# ==========================================================================

@pytest.fixture
def campaign_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "First Name": ["Ben", "Joshua", "Kirstie", "Nobody", "Routing"],
        "Last Name": ["John", "Kinsell", "Brown", "Junior", "Address"],
        "Title": ["Chief Technology Officer", "Chief Financial Officer",
                  "VP of Product", "Data Engineer Intern", "CEO"],
        "Company": ["Digital Turbine"] * 5,
        "Website URL": ["digitalturbine.com"] * 5,
        "Email": ["ben.john@digitalturbine.com", "j.kinsell@digitalturbine.com",
                  "k.brown@digitalturbine.com", "n.junior@digitalturbine.com",
                  "info@digitalturbine.com"],
        "Industry": ["Demand-Side Platform"] * 5,
        "Number of Employees": [1200] * 5,
        "Legacy Stack Signals": ["ASP.NET monolith, 12 year codebase"] * 5,
        "Modernization Intent Signal": ["post-acquisition consolidation"] * 5,
    })


class TestEndToEnd:
    def test_every_prd_output_column_is_produced(self, campaign_frame):
        result = run(campaign_frame)
        missing = [c for c in ROSTER_COLUMNS if c not in result.frame.columns]
        assert missing == []

    def test_input_columns_survive_untouched(self, campaign_frame):
        result = run(campaign_frame)
        for column in campaign_frame.columns:
            assert result.frame[column].equals(campaign_frame[column])
        assert len(result.frame) == len(campaign_frame)

    def test_the_account_qualifies_as_tier_a(self, campaign_frame):
        result = run(campaign_frame)
        assert set(result.frame["sv_target_tier"]) == {"Tier A"}

    def test_the_committee_assembles_correctly(self, campaign_frame):
        result = run(campaign_frame)
        roles = dict(zip(result.frame["Last Name"],
                         result.frame["sv_committee_role"]))
        assert roles["John"] == "Champion"              # CTO
        assert roles["Kinsell"] == "Economic Buyer"     # CFO
        assert roles["Brown"] == "Influencer"           # VP Product
        assert roles["Junior"] == "Unassigned"          # intern, disqualified
        assert roles["Address"] == "Unassigned"         # generic mailbox

    def test_the_routing_address_is_flagged_and_kept(self, campaign_frame):
        result = run(campaign_frame)
        row = result.frame[result.frame["Last Name"] == "Address"].iloc[0]
        assert "GENERIC_EMAIL" in row["sv_flags"]
        assert row["sv_validation_status"] == "FLAGGED_REVIEW"

    def test_two_runs_produce_identical_output(self, campaign_frame):
        """PRD non-functional requirement 2: bitwise reproducibility."""
        first = run(campaign_frame, run_id="fixed").frame.to_csv(index=False)
        second = run(campaign_frame, run_id="fixed").frame.to_csv(index=False)
        assert first == second

    def test_dial_priority_ranks_the_committee_above_the_rest(self, campaign_frame):
        result = run(campaign_frame)
        seated = result.frame[result.frame["sv_committee_role"] != "Unassigned"]
        intern = result.frame[result.frame["Last Name"] == "Junior"]
        assert seated["sv_dial_priority"].min() > intern["sv_dial_priority"].max()

    def test_null_numerics_are_coerced_without_halting(self):
        """PRD non-functional requirement 4: resilience."""
        frame = pd.DataFrame({
            "Company": ["Alpha", "Beta"],
            "Title": ["CTO", None],
            "Email": [None, "x@beta.com"],
            "Number of Employees": [None, "not a number"],
            "Revenue ($ M)": ["", None],
        })
        result = run(frame)
        assert len(result.frame) == 2
        assert result.frame["sv_icp_score"].notna().all()

    def test_a_missing_optional_column_does_not_raise(self):
        frame = pd.DataFrame({"Company": ["Alpha"], "Title": ["CTO"]})
        result = run(frame)
        assert len(result.frame) == 1

    def test_the_audit_log_records_every_decision(self, campaign_frame, tmp_path):
        """PRD non-functional requirement 3."""
        log = tmp_path / "sieve_audit_log.jsonl"
        run(campaign_frame, audit_path=log, run_id="audit-test")

        entries = [json.loads(line) for line in log.read_text().splitlines()]
        events = [e["event"] for e in entries]
        assert events[0] == "run_started"
        assert events[-1] == "run_finished"
        assert events.count("icp_scored") == len(campaign_frame)
        assert "committee_assembled" in events
        assert all(e["run_id"] == "audit-test" for e in entries)
        assert all("ts" in e for e in entries)

    def test_summary_reports_the_counts_a_reviewer_needs(self, campaign_frame):
        summary = run(campaign_frame).summary
        assert summary["rows"] == 5
        assert summary["accounts"] == 1
        assert summary["tiers"]["Tier A"] == 5
        assert "GENERIC_EMAIL" in summary["flags"]


class TestFieldCoalescing:
    def test_a_field_split_across_two_columns_is_coalesced(self):
        """Stacking sheets with drifting headers leaves both columns present,
        each filled for only some rows."""
        frame = pd.DataFrame({
            "Company": ["NA Co", "EU Co"],
            "Website URL": ["na-co.com", None],      # NA sheet spelling
            "Website": [None, "eu-co.com"],          # EU sheet spelling
            "Email": ["a@na-co.com", "b@eu-co.com"],
            "Title": ["CTO", "CTO"],
        })
        result = run(frame)
        # Both rows resolve their own domain, so neither flags a mismatch.
        assert list(result.frame["sv_flags"]) == ["", ""]
        assert result.frame["sv_account_id"].nunique() == 2
