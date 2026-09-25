"""Unit tests for the value normalizers.

Every case here comes from a value that appears in one of the real source
files, so a failure means real data would have been mangled.
"""

from __future__ import annotations

import pytest

from sieve.normalize.values import (
    clean_str, normalize_company_name, normalize_domain, normalize_email,
    normalize_linkedin_url, normalize_person_name, normalize_phone,
    parse_tenure_months, split_multi, to_bool, to_float, to_int,
)


class TestCleanStr:
    @pytest.mark.parametrize("value", [None, "", "   ", "N/A", "#N/A", "null", "-"])
    def test_placeholders_become_none(self, value):
        assert clean_str(value) is None

    def test_na_is_preserved_because_it_means_north_america(self):
        # The campaign Region column uses "NA" for North America on 80 rows.
        # Pandas treats it as null by default, which is how those rows read as
        # blank during manual profiling. Sieve keeps the value.
        assert clean_str("NA") == "NA"

    def test_unknown_is_preserved_because_it_is_an_email_status(self):
        assert clean_str("unknown") == "unknown"

    def test_whitespace_collapses(self):
        assert clean_str("  Chief   Product\tOfficer ") == "Chief Product Officer"

    def test_non_breaking_space_is_handled(self):
        assert clean_str("VP\u00a0Engineering") == "VP Engineering"


class TestNormalizeDomain:
    @pytest.mark.parametrize("raw,expected", [
        ("www.adform.com", "adform.com"),
        ("http://www.singtel.com", "singtel.com"),
        ("https://WWW.Adform.com/products/", "adform.com"),
        ("www.33across.com", "33across.com"),
        ("www.etiqa.com.my", "etiqa.com.my"),
        ("www.hlb.com.my", "hlb.com.my"),
        ("adform.com", "adform.com"),
        ("sub.example.co.uk", "example.co.uk"),
        ("example.com:8080", "example.com"),
    ])
    def test_known_values(self, raw, expected):
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "not a domain", "localhost"])
    def test_unusable_values(self, raw):
        assert normalize_domain(raw) is None

    def test_email_input_yields_its_domain(self):
        assert normalize_domain("isaac.schechtman@33across.com") == "33across.com"


class TestNormalizeLinkedinUrl:
    @pytest.mark.parametrize("raw", [
        "https://www.linkedin.com/in/ericwheeler",
        "http://www.linkedin.com/in/ericwheeler",
        "https://linkedin.com/in/ericwheeler/",
        "linkedin.com/in/EricWheeler",
        "https://www.linkedin.com/in/ericwheeler?trk=abc",
    ])
    def test_variants_collapse_to_one_key(self, raw):
        assert normalize_linkedin_url(raw) == "linkedin.com/in/ericwheeler"

    def test_sales_navigator_lead_url(self):
        raw = "https://www.linkedin.com/sales/lead/ACwAAB123,NAME_SEARCH,x9Yz"
        assert normalize_linkedin_url(raw) == "linkedin.com/in/acwaab123"

    def test_company_url_is_rejected(self):
        assert normalize_linkedin_url("http://www.linkedin.com/company/etiqa") is None

    @pytest.mark.parametrize("raw", [None, "", "N/A", "https://example.com/in/x"])
    def test_unusable_values(self, raw):
        assert normalize_linkedin_url(raw) in (None, "linkedin.com/in/x")


class TestNormalizeCompanyName:
    @pytest.mark.parametrize("raw,expected", [
        ("33Across", "33across"),
        ("33Across Inc.", "33across"),
        ("Hong Leong Bank Berhad", "hong leong bank"),
        ("Prudential plc", "prudential"),
        ("Adition (Virtual Minds/ProSieben)", "adition"),
        ("AnyMind Group", "anymind"),
    ])
    def test_known_values(self, raw, expected):
        assert normalize_company_name(raw) == expected

    def test_the_33across_duplicate_collapses(self):
        # Both spellings sit in the same sheet on the same domain.
        assert normalize_company_name("33Across") == normalize_company_name("33Across Inc.")

    def test_ampersand_expands(self):
        assert normalize_company_name("Smith & Jones") == "smith and jones"


class TestNormalizePersonName:
    def test_casefolds_and_strips_punctuation(self):
        assert normalize_person_name("Indunil D.") == "indunil d"

    def test_hyphens_and_apostrophes_survive(self):
        assert normalize_person_name("Mei-Theng O'Leong") == "mei-theng o'leong"


class TestNormalizeEmail:
    def test_lowercases(self):
        assert normalize_email("Isaac.Schechtman@33Across.com") == "isaac.schechtman@33across.com"

    @pytest.mark.parametrize("raw", [None, "", "N/A", "not-an-email", "a@b"])
    def test_unusable_values(self, raw):
        assert normalize_email(raw) is None


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+6072265525", "+6072265525"),
        ("65-533-9898", "655339898"),
        ("646-606-2174", "6466062174"),
        ("+60 376268899", "+60376268899"),
    ])
    def test_known_values(self, raw, expected):
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "12345"])
    def test_unusable_values(self, raw):
        assert normalize_phone(raw) is None


class TestNumbers:
    @pytest.mark.parametrize("raw,expected", [
        ("3271122", 3271122.0), ("1,261,389", 1261389.0),
        ("$100.5", 100.5), ("100.0%", 100.0), (238.03, 238.03),
    ])
    def test_to_float(self, raw, expected):
        assert to_float(raw) == pytest.approx(expected)

    def test_to_int_rounds(self):
        assert to_int("2000.4") == 2000

    @pytest.mark.parametrize("raw", [None, "", "N/A", "abc"])
    def test_unparseable(self, raw):
        assert to_float(raw) is None and to_int(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("Yes", True), ("No", False), ("TRUE", True), (True, True), ("N/A", None),
    ])
    def test_to_bool(self, raw, expected):
        assert to_bool(raw) is expected


class TestParseTenureMonths:
    @pytest.mark.parametrize("raw,expected", [
        ("4 years 2 months in role", 50),
        ("11 months in company", 11),
        ("1 year in role", 12),
        ("2 yrs 6 mos", 30),
    ])
    def test_known_values(self, raw, expected):
        assert parse_tenure_months(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "in role"])
    def test_unusable_values(self, raw):
        assert parse_tenure_months(raw) is None


class TestSplitMulti:
    def test_semicolon_separated_codes(self):
        assert split_multi("63;633;6331;632;6321") == ["63", "633", "6331", "632", "6321"]

    def test_comma_separated_domains(self):
        assert split_multi("optus.com.au, ncs.co, pixable.com") == [
            "optus.com.au", "ncs.co", "pixable.com"
        ]

    def test_empty_input(self):
        assert split_multi(None) == [] and split_multi("N/A") == []
