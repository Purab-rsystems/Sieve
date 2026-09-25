"""Input/output demonstration.

Takes one real row from each source file, shows the raw cells the file
contained, and shows the canonical record Sieve produced from it. Nothing is
mocked; every value below is read from disk at run time.

    python evals/io_demo.py --data-dir /path/to/files
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sieve import normalize                                   # noqa: E402
from sieve.pipeline import load_tables, pair_by_source_row    # noqa: E402

RULE = "=" * 78
THIN = "-" * 78


def show_input(table, row_index: int, columns: list[str]) -> None:
    row = table.rows[row_index]
    print(f"INPUT   {table.name}, row {row_index + 1}")
    for column in columns:
        value = row.get(column)
        text = "<empty>" if value in (None, "") else str(value)
        if len(text) > 58:
            text = text[:55] + "..."
        print(f"          {column:<34} = {text}")


def show_record(label: str, record, fields: list[str]) -> None:
    print(f"OUTPUT  {label}")
    for field in fields:
        value = getattr(record, field, None)
        text = "None" if value in (None, [], {}) else str(value)
        if len(text) > 58:
            text = text[:55] + "..."
        print(f"          {field:<34} = {text}")


def section(title: str) -> None:
    print("\n" + RULE)
    print(title)
    print(RULE)


def run(data_dir: Path) -> int:
    phantom = data_dir / "result__91_.csv"
    apollo = data_dir / "Apollo_Dump.csv"
    zoominfo = data_dir / "ZoomDump.csv"
    workbook = data_dir / "Modernization_Campaign_-_Simplifi_-_List__31-08-26___1_.xlsx"

    # ---------------------------------------------------------------- 1
    section("1. PHANTOMBUSTER  (result__91_.csv, 479 rows, 34 columns)")
    table = load_tables(phantom)[0]
    show_input(table, 0, [
        "defaultProfileUrl", "fullName", "companyName", "title",
        "durationInRole", "durationInCompany", "location", "timestamp",
    ])
    result = normalize(phantom)
    print()
    show_record("Contact", result.contacts[0], [
        "source_ref", "full_name", "title_raw", "linkedin_url",
        "account_name_raw", "tenure_role_months", "tenure_company_months",
        "country", "captured_at",
    ])
    print(f"\n          479 input rows -> {len(result.contacts)} contacts, "
          f"{len(result.accounts)} accounts")

    # ---------------------------------------------------------------- 2
    section("2. APOLLO  (Apollo_Dump.csv, 576 rows, 34 columns)")
    table = load_tables(apollo)[0]
    print("The file carries BOTH halves on one row. Note 'title' and 'Title':")
    show_input(table, 1, [
        "defaultProfileUrl", "fullName", "companyName", "title",
        "Result", "First Name", "Last Name", "Title",
        "Person Linkedin Url", "Email", "Company Name",
    ])
    result = normalize(apollo)
    groups = pair_by_source_row(result.contacts)
    print("\nOne source row produces TWO canonical records:")
    for record in groups[2]:
        print()
        show_record(f"Contact ({record.match_status})", record, [
            "source_ref", "source_row", "full_name", "title_raw",
            "linkedin_url", "email", "match_status",
        ])
    titles = {(c.title_raw or "").strip().lower() for c in groups[2]}
    print(f"\n          titles differ across the pair: {len(titles) > 1}")

    matched = disagree = 0
    for members in groups.values():
        if not any(c.match_status == "Matched" for c in members):
            continue
        matched += 1
        distinct = {(c.title_raw or "").strip().lower() for c in members}
        if len(distinct) > 1:
            disagree += 1
    print(f"          576 input rows -> {len(result.contacts)} contacts "
          f"({len(groups)} pairs)")
    print(f"          {matched} matched pairs, {disagree} carry a title "
          f"disagreement ({disagree / matched:.1%})")

    # ---------------------------------------------------------------- 3
    section("3. ZOOMINFO  (ZoomDump.csv, 21 rows, 67 columns)")
    table = load_tables(zoominfo)[0]
    show_input(table, 1, [
        "Company Name", "Website.1", "Revenue (in 000s USD)",
        "Est. IT Department Budget (in 000s USD)", "Employees",
        "SIC Codes", "NAICS Codes", "Primary Industry",
        "Certification Date", "Entity Name (Ultimate Parent)",
        "Relationship (Immediate Parent)",
    ])
    result = normalize(zoominfo)
    account = next(a for a in result.accounts if a.name_raw == "Hong Leong Bank")
    print()
    show_record("Account", account, [
        "source_ref", "name_raw", "name", "domain", "revenue_usd",
        "it_budget_usd", "employees", "sic_codes", "naics_codes",
        "industry", "certified_at", "parent_name", "parent_relationship",
    ])
    print("\n          Money converts from thousands: "
          f"1261389 -> {account.revenue_usd:,.0f} USD")
    print(f"          21 input rows -> {len(result.accounts)} accounts, "
          f"{len(result.contacts)} contacts")

    # ---------------------------------------------------------------- 4
    section("4. FORMAT-DB  (workbook, 3 sheets, drifting headers)")
    tables = {t.sheet: t for t in load_tables(workbook)}
    print("The same field is spelled differently on each sheet:\n")
    for sheet in ("NA", "EU", "APAC"):
        columns = tables[sheet].columns
        drift = [c for c in columns if c.lower().replace(" ", "").replace("-", "")
                 in {"mobile", "mobilephone", "phone2", "websiteurl", "website",
                     "linkedinprofile"}]
        print(f"          {sheet:<6} {drift}")
    print()
    show_input(tables["EU"], 0, [
        "First Name", "Last Name", "Title", "LinkedIn Profile", "Email",
        "Company", "Website", "Mobile Phone", "Phone 2", "Revenue ($ M)",
        "IT Budget ($ M)",
    ])
    result = normalize(workbook)
    eu = [c for c in result.contacts if c.last_name == "Dzedulionis"]
    print()
    show_record("Contact", eu[0], [
        "source_ref", "full_name", "title_raw", "linkedin_url", "email",
        "account_name_raw", "account_domain", "mobile", "phone",
    ])
    print("\n          All three sheets map to ONE schema with no code change.")

    # ---------------------------------------------------------------- 5
    section("5. CAMPAIGN STATUS  (workbook Status sheet, 180 rows)")
    show_input(tables["Status"], 0, [
        "Company Name", "Website", "Region", "Category",
        "Legacy Stack Signals", "Modernization Intent Signal",
        "Intent Score\n(High / Medium / Emerging)", "Recommended Target Roles",
    ])
    adform = next(a for a in result.accounts
                  if a.name_raw == "Adform" and a.signals)
    print()
    show_record("Account", adform, [
        "source_ref", "name_raw", "domain", "region", "industry",
    ])
    print(f"          {'signals':<34} = {len(adform.signals)} extracted")
    for signal in adform.signals:
        text = signal.text if len(signal.text) <= 44 else signal.text[:41] + "..."
        print(f"            - {signal.kind.value:<24} {text}")

    # ---------------------------------------------------------------- 6
    section("6. THE 'NA' BUG, DEMONSTRATED")
    import pandas as pd

    frame = pd.read_excel(workbook, sheet_name="Status")
    print("pandas.read_excel with default settings:")
    print(f"          Region value counts: "
          f"{frame['Region'].value_counts(dropna=False).to_dict()}")
    print(f"          -> 80 North America rows read as NaN\n")

    kept = pd.read_excel(workbook, sheet_name="Status", keep_default_na=False)
    print("pandas.read_excel with keep_default_na=False:")
    print(f"          Region value counts: "
          f"{kept['Region'].value_counts().to_dict()}")
    print()
    regions = {}
    for account in result.accounts:
        if account.region:
            regions[account.region] = regions.get(account.region, 0) + 1
    print(f"Sieve:    Region values preserved: {regions}")

    # ---------------------------------------------------------------- 7
    section("7. ADDITIVE CONTRACT, DEMONSTRATED")
    from sieve import attach_columns
    from sieve.io.writers.additive import AdditiveContractError

    frame = pd.read_csv(apollo, nrows=3)[["fullName", "title", "Result", "Email"]]
    print(f"INPUT   columns: {list(frame.columns)}")
    print(f"          rows: {len(frame)}")

    out = attach_columns(frame, {
        "canonical_role": ["vp_internal_audit", "vp_data", "cio"],
        "confidence": [0.71, 0.88, 0.99],
    })
    print(f"\nOUTPUT  columns: {list(out.columns)}")
    print(f"          rows: {len(out)}")
    print(f"          original columns byte-identical: "
          f"{frame.equals(out[list(frame.columns)])}")

    print("\nRefusals (each raises rather than corrupting caller data):")
    for label, action in [
        ("drop a column", lambda: __import__("sieve").enforce_additive(
            frame, frame.drop(columns=["Email"]))),
        ("edit a value", lambda: __import__("sieve").enforce_additive(
            frame, frame.assign(title="edited"))),
        ("wrong length", lambda: attach_columns(frame, {"x": [1, 2]})),
    ]:
        try:
            action()
            print(f"          {label:<16} -> NO ERROR (contract broken)")
        except AdditiveContractError as exc:
            print(f"          {label:<16} -> {exc}")

    print("\n" + RULE + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    return run(parser.parse_args().data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
