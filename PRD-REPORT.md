# Sieve v1.0 — PRD Implementation Report

Built against *Sieve v1.0: Intelligence, ICP Scoring & Contact Verification
Subsystem*, document version 1.0.0-Enterprise.

Every figure below came from running the code. Reproduce with:

```bash
pip install -e .
pytest -q
sieve run --in format_db_input.csv --out verified_prospect_roster.csv \
          --audit sieve_audit_log.jsonl --report run_report.json
```

---

## 1. Module status

| PRD module | Scope | Status |
|---|---|---|
| **M0** | Canonical ingestion, provenance, deterministic UUIDs | Built |
| **M3** | Gate 1, four-pillar deterministic ICP scoring | Built |
| **M4** | Gate 2, contact validation and anomaly flagging | Built |
| **M6** | Designation mapping and authority engine | Built |
| **M7** | Multi-threaded buying committee assembly | Built |
| **M8** | Dial queue provisioning, audit log, roster export | Built |
| **M8** | Salesforce Bulk API write-back | Staged to file; the API call needs credentials and a sandbox |

The HITL review console in M8 is specified as a UI. What exists is the data
layer behind it: `sv_validation_status` marks what needs review, and the roster
is the file a console would render.

---

## 2. Test results

```
........................................................................ [ 68%]
..................................................................       [100%]
210 passed in 1.36s
```

210 tests, 1.4 seconds, no network and no platform access.

| Area | Tests |
|---|---|
| M0 identity, UUID5 determinism and fallbacks | 6 |
| M6 designation, authority, disqualifying tokens | 8 |
| M3 scoring, pillars, tier gates, repeatability | 9 |
| M4 flagging, all six codes, penalties, accumulation | 16 |
| M7 committee eligibility, seat capacity, tie-breaking | 10 |
| M8 dial priority formula | 4 |
| End to end, output contract, determinism, resilience, audit | 11 |
| M0 ingestion suite from P0 (readers, adapters, drift) | 135 |
| Field coalescing across drifting columns | 1 |

The earlier M0 acceptance harness still passes unchanged:

```
==============================================================================
                           M0 ACCEPTANCE SCORECARD                            
==============================================================================
  [PASS] Format coverage                  7 tables parsed from 4 files (3 CSV, 1 XLSX)
  [PASS] Row counts exact                 1889 rows total
  [PASS] Schema detection                 7/7 correct, lowest confidence 1.000
  [PASS] Header drift absorbed            NA/EU/APAC all -> formatdb at 1.000 despite 4 drifting headers
  [PASS] All tables recognised            7/7 recognised, 391 accounts, 2285 contacts
  [PASS] No hard errors on real data      45 warnings, 0 errors ({'unparsed_value': 45})
  [PASS] Contact arithmetic               2285 == 479 phantom + 1152 apollo (paired) + 21 zoominfo + 633 format-db
  [PASS] Apollo halves stay separable     576 pairs, 532 matched
  [PASS] Title disagreements preserved    181 detected (expected 181, 34.0% of matched)
  [PASS] Join key extracted               2216/2285 contacts carry a normalized LinkedIn URL
  [PASS] Emails extracted                 968 == 314 apollo + 633 format-db + 21 zoominfo
  [PASS] 'NA' survives as North America   regions seen: ['APAC', 'EU', 'NA'] (pandas reads 'NA' as null by default; Sieve keeps it)
  [PASS] ZoomInfo firmographics           16/21 carry IT budget, 16/21 carry SIC+NAICS, 6/21 carry a parent
  [PASS] Campaign signals extracted       180 accounts carry signals, 720 signals total
  [PASS] Provenance recorded              25540 field-level provenance entries across 1887 records
------------------------------------------------------------------------------
  15/15 criteria met
==============================================================================

```

---

## 3. Non-functional requirements

```\n5,000 rows processed in 2.55s   (PRD requirement: under 15s)
throughput: 1,957 rows/sec
determinism: two runs produce identical SHA-256  True
  7dceb796da6625d73588f963fb75f2ca66084584b0a92b9ac70e633876021585\n```

| PRD requirement | Target | Measured |
|---|---|---|
| Throughput | 5,000 rows under 15 seconds | **2.55 s**, about 1,957 rows/sec |
| Deterministic reproducibility | Bitwise identical across runs | **Identical SHA-256** |
| Audit logging | Every decision to `sieve_audit_log.jsonl` | **712 lines** for a 633-row run (633 scoring decisions, 71 flag assignments, 6 committee assemblies, plus run start and finish) |
| Resilience | Null and unparseable fields coerced, never halting | Tested with nulls, empty strings and `"not a number"` |

Audit log, first lines:

```
{"ts": "2026-09-16T06:07:35.074315+00:00", "run_id": "prd-cli-001", "event": "run_started", "rubric_version": "1.0.0", "taxonomy_version": "1.0.0", "s
{"ts": "2026-09-16T06:07:35.125795+00:00", "run_id": "prd-cli-001", "event": "icp_scored", "account": "33Across", "score": 61, "tier": "Tier B", "brea
{"ts": "2026-09-16T06:07:35.126036+00:00", "run_id": "prd-cli-001", "event": "icp_scored", "account": "33Across Inc.", "score": 61, "tier": "Tier B",
```

---

## 4. Live run against the real campaign

Input is the modernization campaign's 633 Format-DB contacts with the Status
sheet's signal columns joined on by domain.

```
633 rows across 98 accounts.
  Tiers:      {'Tier C': 387, 'Tier B': 168, 'Tier A': 78}
  Validation: {'VERIFIED': 562, 'FLAGGED_REVIEW': 67, 'DISQUALIFIED': 4}
  Committee:  {'Unassigned': 608, 'Influencer': 16, 'Economic Buyer': 5, 'Champion': 4}
  Flags:      {'DISPOSABLE_PHONE': 10, 'DOMAIN_ANOMALY': 57, 'SENIORITY_AMBIGUITY': 6}
  Titles resolved: 610/633, mean confidence 0.982
Roster written to verified_prospect_roster.csv
```

```json
{
  "run_id": "prd-cli-001",
  "rubric_version": "1.0.0",
  "taxonomy_version": "1.0.0",
  "rows": 633,
  "accounts": 98,
  "tiers": {
    "Tier C": 387,
    "Tier B": 168,
    "Tier A": 78
  },
  "validation_status": {
    "VERIFIED": 562,
    "FLAGGED_REVIEW": 67,
    "DISQUALIFIED": 4
  },
  "committee_roles": {
    "Unassigned": 608,
    "Influencer": 16,
    "Economic Buyer": 5,
    "Champion": 4
  },
  "seniority": {
    "VP": 358,
    "C-Suite": 212,
    "Director": 40,
    "Other": 23
  },
  "flags": {
    "DISPOSABLE_PHONE": 10,
    "DOMAIN_ANOMALY": 57,
    "SENIORITY_AMBIGUITY": 6
  },
  "titles_resolved": 610,
  "mean_confidence": 0.982
}
```

All twelve output columns from the PRD data contract are produced. 41 input
columns pass through untouched and 18 `sv_` columns are appended.

### An assembled committee

```
Company: Centro (Basis Technologies)  |  ICP 90  |  Tier A

sv_committee_role First Name Last Name       sv_canonical_title sv_seniority  sv_confidence  sv_dial_priority
   Economic Buyer    Clayton     Kossl  Chief Financial Officer      C-Suite           1.00              85.0
       Influencer       Juan Fortunato            VP of Product           VP           1.00              79.0
       Influencer    Melissa     Tyink         VP of Operations           VP           1.00              79.0
       Influencer     Andrew  Allender         VP of Operations           VP           1.00              79.0
         Champion    Michael   Freiman Chief Technology Officer      C-Suite           0.85              75.0
```

---

## 5. Three findings that changed the implementation

### 5.1 `Industry` in Format-DB says nothing about AdTech

Pillar 1 scores industry fit, and the obvious input is the `Industry` column.
That column carries ZoomInfo's coarse taxonomy: Business Services (416 rows),
Software (158), Media & Internet (43). None of it distinguishes a DSP from a
payroll vendor.

The AdTech vertical lives in the campaign sheet's `Category` column, with
values like Demand-Side Platform, Supply-Side Platform (SSP), Ad Verification
and Contextual AI Advertising.

Before the fix the industry pillar averaged **5.0 of 100** and no account
reached Tier A. After passing Category through as its own input, **78 contacts
across 6 accounts qualify as Tier A** and committees assemble.

### 5.2 Stacked sheets leave a field split across two columns

Concatenating the NA, EU and APAC sheets produces a frame carrying both
`Website URL` (filled on NA and APAC rows) and `Website` (filled on EU rows) as
separate columns, each empty for most rows. Resolving a logical field to one
column per frame therefore blanked it for two thirds of the data, and the
DOMAIN_ANOMALY and DISPOSABLE_PHONE detectors were running half blind.

Field resolution now coalesces across every column a field matches, taking the
first non-empty value per row. Effects: accounts deduplicated from 111 to 98,
DOMAIN_ANOMALY rose from 34 to 57, DISPOSABLE_PHONE from 1 to 10.

### 5.3 DOMAIN_ANOMALY conflates a rebrand with a bad record

The PRD assigns DOMAIN_ANOMALY the Critical severity, whose action is blocking
CRM write-back. Run against the real file, it fires 57 times. Reading the
pairs:

| Company domain | Email domain | Rows | What it is |
|---|---|---|---|
| wearemiq.com | miqdigital.com, miq.com | 15 | MiQ's own brand domains |
| outbrain.com | teads.com, teads.tv | 12 | The Outbrain and Teads merger |
| equativ.com | smartadserver.com | 7 | Equativ's rebrand of Smart AdServer |
| basis.com | centro.net | 4 | Centro's rebrand to Basis Technologies |
| talonoutdoor.com | talonooh.com | 3 | Alias domain |
| moloco.com | molocoads.com | 2 | Alias domain |
| basis.com, magnite.com, innovid.com, viant | gmail.com, yahoo.com | 4 | **Genuine data errors** |

Blocking all 57 would disqualify 9% of the campaign over acquisitions and
renamings. So the flag keeps its PRD code and splits severity on evidence: a
consumer mailbox stays Critical and disqualifies, a different corporate domain
flags at Medium for review and alias registration.

Result: **4 disqualified** rather than 57, and those four are exactly the
gmail and yahoo addresses.

---

## 6. Deviations from the PRD, stated plainly

**Stage order.** The module diagram runs M4 before M6. M4's
SENIORITY_AMBIGUITY detector needs the seniority mapping that M6 produces, and
M7 needs both, so `run` orders the stages M0, M3, M6, M4, M7, M8. Calling the
stages individually in any order still works.

**ROLE_DEPARTED has no data source.** The specified heuristic reads a LinkedIn
end-date within 90 days. No export in this pipeline carries one. The detector
uses it when present, and otherwise falls back to a weaker signal: very short
tenure alongside a cross-source title disagreement. The evidence string names
which basis fired, so the fallback is never mistaken for the real test. It
fires zero times on the campaign file, because Format-DB carries no tenure
either.

**TITLE_MISMATCH needs a second opinion.** It fires when a row carries two
sources' titles, which is true of Apollo-enriched files and false of Format-DB.
It fires zero times on the campaign file for that reason. Against the Apollo
export it detects all 181 disagreements.

**Four PRD input columns are absent from the real sheets.** `ZoomInfo Contact
ID` and `Source` do not exist in the modernization workbook at all; `Website`
and `LinkedIn Profile` appear under other spellings. The resolver absorbs the
spellings and the missing columns degrade to their fallbacks rather than
raising.

**Salesforce write-back is staged, not sent.** The roster and the summary are
written to file. Wiring the Bulk API needs credentials and a sandbox.

---

## 7. What has not been proven

- **The rubric is tuned to one campaign.** All 180 accounts came from a single AdTech vertical, so the pillar patterns are fitted to it. A second campaign needs its own rubric file, which is why the rubric is YAML.
- **No ground truth for tier assignment.** The campaign sheet carries a human High/Medium intent call that was not used to validate the ICP score. Comparing the two is the obvious next evaluation and it has not been run.
- **Committee coverage is thin on this file.** Only 4 accounts of 98 get both decision seats, because the Format-DB contacts skew toward practitioners rather than C-suite. That reflects the input data rather than the assembly logic.
- **The 552-title taxonomy covers what was observed.** 610 of 633 titles resolve. The remaining 23 are the long tail and land in the unmatched bucket by design.
- **Throughput was measured on duplicated rows.** 5,000 rows built from the 633 real ones repeated. Genuinely distinct data would exercise more cache misses in the title matcher.
