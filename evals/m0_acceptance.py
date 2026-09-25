"""M0 acceptance harness.

Runs the criteria from section 7 of the architecture document against the full
source files rather than the trimmed fixtures, and prints a scorecard.

Unit tests gate merges. This produces a report, which is why it lives outside
the test suite.

    python evals/m0_acceptance.py --data-dir /path/to/files
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sieve import normalize                                    # noqa: E402
from sieve.io.adapters.base import default_registry            # noqa: E402
from sieve.pipeline import load_tables, pair_by_source_row     # noqa: E402

FILES = {
    "phantombuster": "result__91_.csv",
    "apollo": "Apollo_Dump.csv",
    "zoominfo": "ZoomDump.csv",
    "workbook": "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx",
}

EXPECTED_ROWS = {
    "result__91_.csv": 479,
    "Apollo_Dump.csv": 576,
    "ZoomDump.csv": 21,
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::NA": 424,
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::EU": 148,
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::APAC": 61,
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::Status": 180,
}

EXPECTED_ADAPTERS = {
    "result__91_.csv": "phantombuster",
    "Apollo_Dump.csv": "apollo_enrich",
    "ZoomDump.csv": "zoominfo_enrich",
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::NA": "formatdb",
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::EU": "formatdb",
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::APAC": "formatdb",
    "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx::Status": "campaign_status",
}


class Scorecard:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.results.append((name, passed, detail))

    def render(self) -> int:
        width = max(len(name) for name, _, _ in self.results) + 2
        print("\n" + "=" * 78)
        print("M0 ACCEPTANCE SCORECARD".center(78))
        print("=" * 78)
        for name, passed, detail in self.results:
            mark = "PASS" if passed else "FAIL"
            print(f"  [{mark}] {name:<{width}} {detail}")
        failed = sum(1 for _, passed, _ in self.results if not passed)
        print("-" * 78)
        print(f"  {len(self.results) - failed}/{len(self.results)} criteria met")
        print("=" * 78 + "\n")
        return failed


def run(data_dir: Path) -> int:
    paths = {key: data_dir / name for key, name in FILES.items()}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        print("Missing source files:\n  " + "\n  ".join(missing))
        return 1

    card = Scorecard()
    registry = default_registry()

    # --- format coverage --------------------------------------------------
    tables = []
    for path in paths.values():
        tables.extend(load_tables(path))
    card.check(
        "Format coverage",
        len(tables) == 7,
        f"{len(tables)} tables parsed from {len(paths)} files "
        f"({len([p for p in paths.values() if p.suffix == '.csv'])} CSV, "
        f"{len([p for p in paths.values() if p.suffix == '.xlsx'])} XLSX)",
    )

    # --- row counts -------------------------------------------------------
    actual_rows = {t.name: len(t) for t in tables}
    mismatches = {
        k: (v, actual_rows.get(k))
        for k, v in EXPECTED_ROWS.items() if actual_rows.get(k) != v
    }
    card.check(
        "Row counts exact",
        not mismatches,
        f"{sum(actual_rows.values())} rows total" if not mismatches
        else f"mismatches: {mismatches}",
    )

    # --- schema detection -------------------------------------------------
    detected, confidences = {}, {}
    for table in tables:
        adapter, confidence = registry.detect(table)
        detected[table.name] = adapter.source_id
        confidences[table.name] = confidence
    wrong = {k: (v, detected.get(k)) for k, v in EXPECTED_ADAPTERS.items()
             if detected.get(k) != v}
    lowest = min(confidences.values())
    card.check(
        "Schema detection",
        not wrong and lowest >= 0.9,
        f"7/7 correct, lowest confidence {lowest:.3f}" if not wrong
        else f"wrong: {wrong}",
    )

    # --- header drift -----------------------------------------------------
    region_scores = {
        t.sheet: confidences[t.name] for t in tables
        if t.sheet in {"NA", "EU", "APAC"}
    }
    region_adapters = {
        t.sheet: detected[t.name] for t in tables
        if t.sheet in {"NA", "EU", "APAC"}
    }
    card.check(
        "Header drift absorbed",
        set(region_adapters.values()) == {"formatdb"}
        and len(set(region_scores.values())) == 1,
        f"NA/EU/APAC all -> formatdb at {list(region_scores.values())[0]:.3f} "
        f"despite 4 drifting headers",
    )

    # --- normalization ----------------------------------------------------
    result = normalize(list(paths.values()))
    report = result.report()
    card.check(
        "All tables recognised",
        report["totals"]["tables_recognised"] == 7,
        f"{report['totals']['tables_recognised']}/7 recognised, "
        f"{report['totals']['accounts']} accounts, "
        f"{report['totals']['contacts']} contacts",
    )

    # --- soft failure -----------------------------------------------------
    card.check(
        "No hard errors on real data",
        report["issues"]["errors"] == 0,
        f"{report['issues']['total']} warnings, 0 errors "
        f"({report['issues']['by_code']})",
    )

    # --- contact arithmetic -----------------------------------------------
    expected_contacts = 479 + (576 * 2) + 21 + 424 + 148 + 61
    card.check(
        "Contact arithmetic",
        len(result.contacts) == expected_contacts,
        f"{len(result.contacts)} == 479 phantom + 1152 apollo (paired) "
        f"+ 21 zoominfo + 633 format-db",
    )

    # --- apollo pairing ---------------------------------------------------
    apollo = normalize(paths["apollo"])
    groups = pair_by_source_row(apollo.contacts)
    matched, disagreements = 0, 0
    for members in groups.values():
        statuses = {c.match_status for c in members}
        if "Matched" not in statuses:
            continue
        matched += 1
        titles = {(c.title_raw or "").strip().lower() for c in members}
        if len(titles) > 1:
            disagreements += 1
    card.check(
        "Apollo halves stay separable",
        len(groups) == 576 and matched == 532,
        f"{len(groups)} pairs, {matched} matched",
    )
    card.check(
        "Title disagreements preserved",
        disagreements == 181,
        f"{disagreements} detected (expected 181, "
        f"{disagreements / matched:.1%} of matched)",
    )

    # --- field extraction -------------------------------------------------
    linkedin = sum(1 for c in result.contacts if c.linkedin_url)
    emails = sum(1 for c in result.contacts if c.email)
    card.check(
        "Join key extracted",
        linkedin >= 2200,
        f"{linkedin}/{len(result.contacts)} contacts carry a normalized "
        f"LinkedIn URL",
    )
    card.check(
        "Emails extracted",
        emails == 968,
        f"{emails} == 314 apollo + 633 format-db + 21 zoominfo",
    )

    # --- the NA bug -------------------------------------------------------
    regions = {a.region for a in result.accounts if a.region}
    card.check(
        "'NA' survives as North America",
        "NA" in regions,
        f"regions seen: {sorted(regions)} (pandas reads 'NA' as null by "
        f"default; Sieve keeps it)",
    )

    # --- firmographics ----------------------------------------------------
    zoom = normalize(paths["zoominfo"])
    with_budget = [a for a in zoom.accounts if a.it_budget_usd]
    with_codes = [a for a in zoom.accounts if a.sic_codes and a.naics_codes]
    with_parent = [a for a in zoom.accounts if a.parent_name]
    card.check(
        "ZoomInfo firmographics",
        len(with_budget) >= 15 and len(with_codes) >= 15,
        f"{len(with_budget)}/21 carry IT budget, {len(with_codes)}/21 carry "
        f"SIC+NAICS, {len(with_parent)}/21 carry a parent",
    )

    # --- signals ----------------------------------------------------------
    status_tables = [t for t in tables if t.sheet == "Status"]
    signals = [a for a in result.accounts if a.signals]
    card.check(
        "Campaign signals extracted",
        len(signals) >= 180,
        f"{len(signals)} accounts carry signals, "
        f"{sum(len(a.signals) for a in signals)} signals total",
    )

    # --- provenance -------------------------------------------------------
    entries = sum(len(v) for v in result.batch.provenance.values())
    card.check(
        "Provenance recorded",
        entries > 10_000,
        f"{entries} field-level provenance entries across "
        f"{len(result.batch.provenance)} records",
    )

    return card.render()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    return run(args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
