# Sieve v1.0

**Quality and intelligence engine, downstream of LeadDrip.**

Implements the PRD: modules M0, M3, M4, M6, M7 and M8.

---

## In a nutshell

You give it a `Format-DB.csv`. It gives you the same file back with analytical
columns appended, plus an audit log. Every added column starts with `sv_`, so
nothing collides with your schema or with Salesforce. No column you passed in
is ever modified, reordered or dropped.

| Command | Give it | Get back |
|---|---|---|
| `sieve run` | Format-DB file from LeadDrip | `verified_prospect_roster.csv` with all 12 PRD columns, plus `sieve_audit_log.jsonl` and a JSON summary |
| `sieve score` | Any contact file | `sv_icp_score`, `sv_target_tier`, `sv_score_breakdown` |
| `sieve validate` | Any contact file | `sv_flags`, `sv_confidence`, `sv_validation_status` |
| `sieve designate` | Any file with a title column | `sv_canonical_title`, `sv_seniority`, `sv_is_authoritative` |
| `sieve console` | A roster | `review_console.html` for rep sign-off (M8 step i) |
| `sieve sync` | A roster plus `approval.json` | Salesforce Bulk API upsert payloads (M8 step iii) |
| `sieve queue` | A roster | `compass_max_dial_queue.csv`, priority ordered (M8 step iv) |
| `sieve normalize` | Raw Phantom / Zoom / Apollo dumps | Canonical accounts and contacts |
| `sieve inspect` | Any file | Which adapter each sheet scores against |

```bash
sieve run --in format_db_input.csv \
          --out verified_prospect_roster.csv \
          --audit sieve_audit_log.jsonl \
          --report run_report.json
```

```python
from sieve import run
result = run(df)              # DataFrame in, DataFrame out
result.summary                # counts by tier, flag, committee seat
```

---

## What each stage does

**M0 identity.** Deterministic UUID5 IDs. `sv_account_id` hashes the
normalized domain, `sv_contact_id` hashes the email inside that account
namespace. Same input, same ID, every machine, every run.

**M3 scoring (Gate 1).** Four pillars, each 0-100, each weighted 25%: industry
fit, legacy stack signals, platform scale, modernization triggers. Tier A at
80+, Tier B at 60-79, Tier C below. No model calls, so it is repeatable and
free. The rubric is `config/icp_adtech_modernization.yaml`; a new campaign
means a new file.

**M6 designation.** Normalizes titles onto a taxonomy seeded from 552 real
observed titles. `VP Tech & Architecture` and `Head of Engineering` both
resolve to `VP of Engineering`. Derives seniority, authority and which
committee seat the role naturally fills.

**M4 validation (Gate 2).** Six flags at four severities, each subtracting
from a 1.00 confidence: TITLE_MISMATCH, ROLE_DEPARTED, GENERIC_EMAIL,
DOMAIN_ANOMALY, SENIORITY_AMBIGUITY, DISPOSABLE_PHONE. Nothing is dropped.

**M7 committee.** For each Tier A account, fills Economic Buyer, Champion and
up to three Influencer seats. Selection sorts by seniority, then confidence,
then contact ID, so two runs produce the same committee.

**M8 export.** Four pieces. Dial priority as `(icp x 0.50) +
(seniority x 0.30) + (phone verified x 0.20)`. A JSONL audit log carrying every
scoring decision, flag and committee promotion. A self-contained HTML review
console where a rep works the flagged rows and clicks Approve Roster. And
Salesforce Bulk API upsert payloads for Account and Contact, keyed on the
external IDs.

The sign-off is a real gate: `sieve sync` refuses to build a payload without an
approval file, and anything the rep drops in the console never reaches the
payload. `--force` stages an unapproved dry run. Sending the payload needs
credentials and a sandbox; everything up to the HTTP call is here.

Stage order in `run` is M0, M3, M6, M4, M7, M8. M6 comes before M4 because
SENIORITY_AMBIGUITY needs the seniority mapping.

---

## Measured

| Requirement | Target | Measured |
|---|---|---|
| Throughput | 5,000 rows under 15s | 2.55s, ~1,957 rows/sec |
| Reproducibility | Bitwise identical | Identical SHA-256 across runs |
| Audit coverage | Every decision logged | 1,530 lines for 633 rows |

244 unit tests in 2 seconds, plus two acceptance harnesses that run the whole
pipeline over the real 633-contact campaign file:

```bash
pytest -q                                          # 244 unit tests
python evals/prd_acceptance.py --data-dir sample-input   # 28 criteria, M0-M8
python evals/m0_acceptance.py  --data-dir sample-input   # 15 criteria, ingestion
python evals/io_demo.py        --data-dir sample-input   # input/output walkthrough
```

The unit suite covers logic against small built frames. The acceptance
harnesses are what catch data problems, and they earn their place: the PRD
harness found two source rows describing the same person under different
company labels, which would have produced a repeated external id in a
Salesforce batch and failed the Bulk API job.

---

## Three things found in the data

**`"NA"` means North America and pandas eats it.** The campaign Region column
carries `"NA"` on 80 rows. Pandas treats that string as null by default, so a
plain `read_excel` loses the whole North America segment. Sieve keeps it. If
you read that sheet yourself, pass `keep_default_na=False`.

**`Industry` says nothing about AdTech.** Format-DB's Industry column carries
ZoomInfo's coarse taxonomy (Business Services, Software). The vertical that
matters lives in the campaign sheet's `Category` column. Scoring on Industry
alone left the industry pillar averaging 5 of 100 and produced zero Tier A
accounts.

**Apollo carries both `title` and `Title`.** They differ only in case and hold
different values, the PhantomBuster input and Apollo's answer. Any
case-insensitive column lookup silently returns the same one for both.

---

## Run it (Windows, Python 3.14)

1. Install Ollama from https://ollama.com/download, then pull the two models:
   `ollama pull qwen2.5:7b` and `ollama pull llama3.1:8b`.
2. In the sieve folder:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
sieve ui
```

`sieve ui` opens Sieve in the browser. Upload the campaign file and the
contact files. Then, in order:

1. TAL scores (rule-based, ready at once): each check's band and the text that triggered it.
   Optional on upload: a committee list (CSV/Excel: Role, Count, Description;
   template at /api/list-template.csv). It replaces the default list for the
   run; a role without a description gets one written by Qwen (marked so).
2. Committee: decided from the campaign by Qwen (judge) and Llama (checker),
   with both models' reasoning on every pick and every rejection. It uses the
   default list (1 Economic Buyer, 1 Champion, 1 Technical Evaluator,
   3 Influencers); "Generate list for this campaign" has the models decide how
   many people each role needs instead (the default list stays as it is).
3. Roster: for every Tier A company and every committee seat, Qwen picks the
   contact to target, or says nobody fits, with its reasoning; Llama checks
   every pick. Locked until steps 2 and 3 are done.
4. Approve & export: the kept targets go to the approval file, the Salesforce
   upsert files, the dial queue and tier_a_targets.csv (with the reasoning).

## Buying committee: judge + checker

Decided from the campaign file before any contact is looked at. Qwen2.5-7B
(judge) proposes which function fills each role; Llama-3.1-8B (checker) must
pass every pick. Both run in Ollama. Each model writes its reasoning first and
its answer is read right after that reasoning, so the reasoning shown on every
pick and every rejection is what the answer came from. Minimums
(config/committee/rules.yaml): 1 Champion, 1 Economic Buyer, 1 Technical
Evaluator, 3 Influencers; more members are fine. One title, one role. A
minimum nothing passes is reported unfilled, never forced.

```bash
sieve committee decide --campaign config/icp_adtech_modernization.yaml --out committee.json
sieve committee match  --committee committee.json --in roster.xlsx \
                       --out matched.csv --accounts-out coverage.csv
python benchmarks/committee/run_benchmark.py --offline     # accuracy, recorded answers
```

Role reference sheet: config/committee/role_reference.json, 51 titles in 22
functions, descriptions copied word for word from AvaHR (26) / Workable (8),
the closest O*NET occupation marked approximate (10), or none (7). Rebuilt with
tools/role_reference/extract.py.

Accuracy on the 10-campaign reference set: see benchmarks/committee/results.json.
The earlier method (no written reasoning, llama.cpp) scored 0.82 F1
(results_v1_llamacpp.json).

---

## Layout

```
src/sieve/
  identity.py              M0  deterministic UUID5 IDs
  scoring/icp.py           M3  four-pillar rubric
  taxonomy/designation.py  M6  title normalization and authority
  validation/engine.py     M4  six flags, severities, confidence
  committee/assembly.py    M7  seat eligibility and selection
  export/                  M8  dial priority, audit log, review console,
                               Salesforce staging, dial queue
  roster.py                    the stages, frame in and frame out
  io/                          readers, adapters, additive writer
  pipeline.py                  raw dump normalization
config/
  icp_adtech_modernization.yaml
  titles.yaml
```

Dependencies: pandas, openpyxl, pydantic, rapidfuzz, pyyaml, typer.
