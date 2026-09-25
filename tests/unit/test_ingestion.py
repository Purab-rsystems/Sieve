"""Ingestion tests, run against trimmed copies of the real source files.

The fixtures preserve every structural quirk that matters: the header drift
across the three region sheets, the paired input and output halves in the
Apollo export, and the four-sheet workbook carrying three schemas.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sieve import normalize
from sieve.io.adapters.base import (
    ColumnResolver, NoAdapterMatched, canonical_header, default_registry,
)
from sieve.io.readers.base import UnsupportedFormat
from sieve.io.readers.base import default_registry as reader_registry
from sieve.pipeline import load_tables

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

PHANTOM = FIXTURES / "phantombuster_sample.csv"
APOLLO = FIXTURES / "apollo_sample.csv"
ZOOMINFO = FIXTURES / "zoominfo_sample.csv"
WORKBOOK = FIXTURES / "campaign_workbook_sample.xlsx"


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------

class TestReaders:
    def test_csv_yields_one_table(self):
        tables = load_tables(PHANTOM)
        assert len(tables) == 1
        assert len(tables[0].columns) == 34

    def test_workbook_yields_one_table_per_sheet(self):
        tables = load_tables(WORKBOOK)
        assert [t.sheet for t in tables] == ["NA", "EU", "APAC", "Status"]

    def test_row_counts_are_exact(self):
        counts = {t.sheet: len(t) for t in load_tables(WORKBOOK)}
        assert counts == {"NA": 25, "EU": 25, "APAC": 25, "Status": 25}

    def test_large_cells_do_not_break_csv_reading(self):
        # PhantomBuster bios run to several kilobytes.
        table = load_tables(PHANTOM)[0]
        assert any(len(str(row.get("summary") or "")) > 200 for row in table.rows)

    def test_unknown_extension_raises(self, tmp_path):
        target = tmp_path / "data.parquet"
        target.write_bytes(b"x")
        with pytest.raises(UnsupportedFormat, match="No reader handles"):
            list(reader_registry().read(target))


# --------------------------------------------------------------------------
# header resolution
# --------------------------------------------------------------------------

class TestColumnResolver:
    def test_canonical_header_normalizes_noise(self):
        assert canonical_header("  Mobile ") == "mobile"
        assert canonical_header("State/Province") == "state/province"
        assert canonical_header("Revenue ($ M)") == "revenue ( m)"

    def test_exact_match_wins(self):
        resolver = ColumnResolver(["Mobile Phone", "Phone 2"])
        assert resolver.resolve(["Mobile Phone"]) == "Mobile Phone"

    def test_case_drift_resolves(self):
        resolver = ColumnResolver(["Linkedin Profile"])
        assert resolver.resolve(["LinkedIn Profile"]) == "Linkedin Profile"

    def test_separator_drift_resolves(self):
        resolver = ColumnResolver(["Phone-2"])
        assert resolver.resolve(["Phone 2"]) == "Phone-2"

    def test_unrelated_header_stays_unmatched(self):
        resolver = ColumnResolver(["Revenue ($ M)", "Country"])
        assert resolver.resolve(["ZoomInfo Company ID"]) is None


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

class TestDetection:
    @pytest.mark.parametrize("path,expected", [
        (PHANTOM, "phantombuster"),
        (APOLLO, "apollo_enrich"),
        (ZOOMINFO, "zoominfo_enrich"),
    ])
    def test_csv_schemas_are_identified(self, path, expected):
        registry = default_registry()
        adapter, confidence = registry.detect(load_tables(path)[0])
        assert adapter.source_id == expected
        assert confidence >= 0.9

    def test_every_workbook_sheet_is_identified(self):
        registry = default_registry()
        chosen = {}
        for table in load_tables(WORKBOOK):
            adapter, confidence = registry.detect(table)
            chosen[table.sheet] = (adapter.source_id, confidence)
        assert chosen["NA"][0] == "formatdb"
        assert chosen["EU"][0] == "formatdb"
        assert chosen["APAC"][0] == "formatdb"
        assert chosen["Status"][0] == "campaign_status"
        assert all(confidence >= 0.9 for _, confidence in chosen.values())

    def test_header_drift_does_not_lower_confidence(self):
        """All three region sheets must score identically despite drift."""
        registry = default_registry()
        scores = {
            t.sheet: registry.detect(t)[1]
            for t in load_tables(WORKBOOK) if t.sheet in {"NA", "EU", "APAC"}
        }
        assert len(set(scores.values())) == 1

    def test_unrecognised_schema_raises_with_guidance(self, tmp_path):
        target = tmp_path / "mystery.csv"
        target.write_text("alpha,beta,gamma\n1,2,3\n")
        with pytest.raises(NoAdapterMatched, match="detection floor"):
            default_registry().detect(load_tables(target)[0])


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------

class TestNormalize:
    def test_phantom_contacts_map_one_to_one(self):
        result = normalize(PHANTOM)
        assert len(result.contacts) == 25
        assert all(c.linkedin_url for c in result.contacts)

    def test_phantom_tenure_is_parsed(self):
        result = normalize(PHANTOM)
        tenures = [c.tenure_role_months for c in result.contacts if c.tenure_role_months]
        assert tenures and max(tenures) > 12

    def test_apollo_emits_two_contacts_per_row(self):
        """The paired halves must stay separate, or M4 loses its signal."""
        result = normalize(APOLLO)
        assert len(result.contacts) == 50
        statuses = {c.match_status for c in result.contacts}
        assert "input" in statuses

    def test_apollo_title_disagreement_survives_ingestion(self):
        result = normalize(APOLLO)
        by_url: dict[str, set[str]] = {}
        for contact in result.contacts:
            if contact.linkedin_url and contact.title_raw:
                by_url.setdefault(contact.linkedin_url, set()).add(contact.title_raw)
        assert any(len(titles) > 1 for titles in by_url.values())

    def test_zoominfo_money_converts_from_thousands(self):
        result = normalize(ZOOMINFO)
        revenues = [a.revenue_usd for a in result.accounts if a.revenue_usd]
        assert revenues and min(revenues) > 1_000_000

    def test_zoominfo_hierarchy_is_captured(self):
        result = normalize(ZOOMINFO)
        assert any(a.parent_name for a in result.accounts)

    def test_workbook_normalizes_every_sheet(self):
        result = normalize(WORKBOOK)
        assert all(o.source_id is not None for o in result.outcomes)
        assert len(result.contacts) == 75          # NA + EU + APAC
        assert len(result.outcomes) == 4

    def test_region_na_is_preserved_not_read_as_null(self):
        result = normalize(WORKBOOK)
        regions = {a.region for a in result.accounts if a.region}
        assert "NA" in regions

    def test_campaign_signals_are_extracted(self):
        result = normalize(WORKBOOK)
        with_signals = [a for a in result.accounts if a.signals]
        assert with_signals
        kinds = {s.kind.value for a in with_signals for s in a.signals}
        assert "legacy_stack" in kinds and "modernization_intent" in kinds

    def test_malformed_row_is_carried_with_a_warning(self, tmp_path):
        """A bad value produces a warning and keeps the row."""
        target = tmp_path / "zoom.csv"
        source = ZOOMINFO.read_text().splitlines()
        header, first = source[0], source[1]
        target.write_text("\n".join([header, first]) + "\n")
        result = normalize(target)
        assert len(result.accounts) == 1
        assert result.report()["issues"]["errors"] == 0

    def test_forcing_a_source_skips_detection(self):
        result = normalize(PHANTOM, source="phantombuster")
        assert result.outcomes[0].source_id == "phantombuster"

    def test_strict_mode_raises_on_unrecognised_table(self, tmp_path):
        target = tmp_path / "mystery.csv"
        target.write_text("alpha,beta\n1,2\n")
        with pytest.raises(NoAdapterMatched):
            normalize(target, strict=True)

    def test_lenient_mode_records_and_continues(self, tmp_path):
        target = tmp_path / "mystery.csv"
        target.write_text("alpha,beta\n1,2\n")
        result = normalize([target, PHANTOM])
        assert result.outcomes[0].source_id is None
        assert result.outcomes[0].error
        assert result.outcomes[1].source_id == "phantombuster"

    def test_multiple_files_combine_into_one_result(self):
        result = normalize([PHANTOM, APOLLO, ZOOMINFO, WORKBOOK])
        assert len(result.outcomes) == 7
        assert all(o.source_id for o in result.outcomes)
        report = result.report()
        assert report["totals"]["tables_recognised"] == 7


class TestProvenance:
    def test_every_contact_row_records_its_origin(self):
        result = normalize(APOLLO)
        refs = result.batch.provenance
        assert any(ref.endswith(":input") for ref in refs)
        assert any(ref.endswith(":apollo") for ref in refs)

    def test_provenance_entries_name_their_source_and_column(self):
        result = normalize(ZOOMINFO)
        entries = [e for group in result.batch.provenance.values() for e in group]
        assert entries
        sample = entries[0]
        assert sample.source_id == "zoominfo_enrich"
        assert sample.field and sample.row >= 1


class TestSourceRowPairing:
    """The Apollo halves must remain pairable, since M4 depends on it."""

    def test_each_apollo_row_yields_a_pairable_group_of_two(self):
        from sieve.pipeline import pair_by_source_row

        result = normalize(APOLLO)
        groups = pair_by_source_row(result.contacts)
        assert len(groups) == 25
        assert all(len(members) == 2 for members in groups.values())

    def test_pairs_carry_one_input_and_one_apollo_half(self):
        from sieve.pipeline import pair_by_source_row

        result = normalize(APOLLO)
        for members in pair_by_source_row(result.contacts).values():
            statuses = [c.match_status for c in members]
            assert statuses.count("input") == 1

    def test_title_disagreement_is_detectable_within_a_pair(self):
        from sieve.pipeline import pair_by_source_row

        result = normalize(APOLLO)
        disagreements = 0
        for members in pair_by_source_row(result.contacts).values():
            titles = {
                (c.title_raw or "").strip().lower()
                for c in members if c.match_status in {"input", "Matched"}
            }
            if len(titles) > 1:
                disagreements += 1
        # The 25-row fixture carries ten, confirmed against the raw file.
        assert disagreements == 10

    def test_case_only_header_collision_resolves_to_the_right_column(self):
        """`title` and `Title` differ only in case and must stay distinct."""
        from sieve.io.adapters.base import ColumnResolver

        resolver = ColumnResolver(["title", "Result", "Title"])
        assert resolver.resolve(["title"]) == "title"
        assert resolver.resolve(["Title"]) == "Title"
        assert "title" in resolver.ambiguous()
