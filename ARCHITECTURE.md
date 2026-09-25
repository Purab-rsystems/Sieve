# Sieve — Architecture and Scope

**A companion layer to LeadDrip that owns the work happening before a query is formed and after enrichment returns.**

Version 0.1, draft for review. The name is a placeholder and anyone can change it.

---

## 0. In a nutshell

Sieve is a Python library with a command-line wrapper. You hand it a file, and it hands the same file back with additional columns appended. Every column it adds carries the prefix `sv_`, which keeps its output separate from yours so the two can never collide. Each command works independently, so you can call one of them, several of them, or none of them, and your pipeline continues to run either way.

| Command | What you give it | What you get back |
|---|---|---|
| `sieve titles expand` | A role string such as `"CTO"` | A JSON list of canonical and variant titles, together with LinkedIn and ZoomInfo filter IDs where they are known, ready to drop into your query builder |
| `sieve titles normalize` | Any CSV or XLSX containing a title column | The same file plus `sv_canonical_role`, `sv_role_tier`, and `sv_role_confidence` |
| `sieve resolve` | Any contact or account file | The same file plus `sv_account_id`, `sv_contact_id`, `sv_duplicate_of`, and `sv_parent_account_id` |
| `sieve score` | An account file containing company, website, and signal text | The same file plus `sv_icp_score`, `sv_intent_tier`, and `sv_score_reasons` |
| `sieve validate` | Your enriched output, after Apollo has run | The same file plus `sv_flags`, `sv_flag_detail`, and `sv_confidence` |
| `sieve lookup` | Candidate contacts, before enrichment runs | The same file plus `sv_known`, along with any email or phone already on record, so you can skip paying to enrich people you already hold |
| `sieve normalize` | Any raw dump from PhantomBuster, ZoomInfo, Apollo, or Format-DB | One canonical schema, which is what lets the six commands above accept input from any source |

You can call it from the shell:

```bash
sieve validate --in enriched.csv --out enriched_flagged.csv --report report.json
```

Or from Python, where it takes a DataFrame and returns a DataFrame:

```python
from sieve import validate
df = validate(df)
```

Both forms run the same code. Every function is free of side effects, and a file gets written only when you pass `--out`.

Your pipeline keeps ownership of everything Sieve stays away from: the ZoomInfo and Apollo API calls, the query builder, the chat interface, the Format-DB schema, and DNC suppression. Sieve reads files and produces columns.

---

## 1. Why this exists

Sanjay Sir's database creation flow runs in eight steps. Campaign details produce an ICP and a TAL. The roles that need targeting get identified. Designations and titles get derived from internal data and external search. Those designations get validated. ZoomInfo, Apollo, and the internal database get queried. The contacts get cleaned. Everything gets validated against LinkedIn and other sources, with suspect records flagged for review. An enriched list emerges at the end.

LeadDrip v1 implements steps five, six, and eight. It treats the first four as constants written into its own specification, and step seven is missing from it entirely. Sieve covers steps one, three, four, and seven, and it adds the internal database leg of step five.

The division works cleanly because the two systems meet only at file boundaries. Sieve produces assets and gates, and LeadDrip consumes them and runs the pipeline.

```
                 ┌──────────────────────────────────────────┐
                 │            SIEVE (this doc)              │
                 │                                          │
  campaign brief │  ICP scoring ──┐                         │
  ──────────────▶│  role taxonomy ├──▶ query-ready assets   │
                 │  account graph ─┘                        │
                 └────────────┬─────────────────────────────┘
                              │  roles.json, scored_accounts.csv
                              ▼
                 ┌──────────────────────────────────────────┐
                 │           LEADDRIP (Spoorthy)            │
                 │  chat → query builder → ZoomInfo/Apollo  │
                 │  → Format-DB → dedup → clean output      │
                 └────────────┬─────────────────────────────┘
                              │  enriched contacts
                              ▼
                 ┌──────────────────────────────────────────┐
                 │            SIEVE (this doc)              │
                 │  validation & flagging → reviewable file │
                 └──────────────────────────────────────────┘
```

---

## 2. Scope

Six modules make up the system, and each one maps onto a step in Sanjay Sir's flow.

| Module | Responsibility | Sanjay Sir's step |
|---|---|---|
| M0 | Ingestion, normalization, the canonical model, and provenance tracking | Foundation for everything else |
| M1 | Role and title taxonomy, covering both normalization and expansion | 3 and 4 |
| M2 | Account and contact resolution, including deduplication, parent and subsidiary relationships, and acquisitions | Foundation for 1 and 6 |
| M3 | ICP scoring and intent classification | 1 |
| M4 | Contact validation and flagging | 7 |
| M5 | Internal contact lookup | The internal leg of 5 |

Several responsibilities sit deliberately outside these boundaries. Sieve reads files and leaves every paid platform API to LeadDrip. Query construction and execution belong to the query builder. The conversational interface belongs to LeadDrip's frontend. Format-DB emission stays with LeadDrip, since Sieve only appends columns while LeadDrip owns the schema. DNC suppression waits until the list arrives, and the resolution layer in M2 is what it will build on when that happens. Content generation, campaign execution, and HubSpot integration remain parked at project level.

Three further limits reflect judgment calls worth stating up front. Version one runs entirely on files with no database server, and a store can slot in behind the same interface later. Nothing here trains a model from scratch, because 180 labelled rows cannot support it. Live LinkedIn verification stays a manual step until there is a legitimate access path.

---

## 3. Design rules

The entire value of this component rests on Spoorthy being able to drop it into her pipeline and trust it, so the following eight rules hold everywhere in the codebase.

**Output is additive.** Sieve preserves every input column exactly as it received it, in the original order, and appends its own columns at the end.

**Everything is namespaced.** Each added column and each configuration key begins with `sv_`, which keeps collisions impossible.

**Functions are pure.** Every public function takes data and configuration and returns data. Global state, hidden file writes, and network calls stay out of the core.

**Behaviour is deterministic by default.** The same input always produces the same output. Anything inherently variable, such as an LLM call, sits behind an interface that has a deterministic fallback, which lets tests and CI run fully offline.

**Every result explains itself.** Each score and each flag carries a reason string, so a campaign manager can ask why a record was flagged and find the answer by reading the output file.

**Configuration drives behaviour.** Campaigns, taxonomies, thresholds, and scoring weights live in YAML files, so adding a campaign means adding a file.

**Schema failures are loud and data failures are soft.** An unrecognised source schema raises an exception. A malformed row gets flagged and carried through, so the person reviewing the output can see it and decide.

**Provenance travels with every derived field.** Each value records which source reported it, which file it came from, and when it was captured.

---

## 4. Layered architecture

The system stacks into five layers, and each layer depends only on the one beneath it.

```
┌────────────────────────────────────────────────────────────────┐
│ L5  Interface        CLI  ·  Python API  ·  JSON report        │
├────────────────────────────────────────────────────────────────┤
│ L4  Capabilities     taxonomy · resolve · score · validate     │
│                                          · lookup              │
├────────────────────────────────────────────────────────────────┤
│ L3  Canonical model  Account · Contact · Signal · Provenance   │
├────────────────────────────────────────────────────────────────┤
│ L2  Adapters         schema mapping: Phantom, Zoom, Apollo,    │
│                      Format-DB, CampaignStatus                 │
├────────────────────────────────────────────────────────────────┤
│ L1  Readers          format handling: CSV, XLSX (multi-sheet), │
│                      TSV, JSON                                 │
└────────────────────────────────────────────────────────────────┘
```

### Readers and adapters stay separate

A file's format and its source schema vary independently of one another. `ZoomDump.csv` arrives as CSV today and could arrive as XLSX tomorrow, while the ZoomInfo schema stays identical in both cases. The modernization campaign file demonstrates the reverse situation, since a single XLSX workbook holds four sheets carrying three different schemas between them.

Merging these two concerns into one class is the most reliable way to make this kind of codebase rot, so they stay apart. A reader answers the question of how to get rows out of a set of bytes, and it exposes `read(path) -> Iterator[RawTable]`. An adapter answers the question of what those columns mean, and it exposes `to_canonical(RawTable) -> CanonicalBatch`. A registry composes the two at runtime.

```python
class Reader(Protocol):
    extensions: ClassVar[tuple[str, ...]]
    def read(self, path: Path) -> Iterator[RawTable]: ...

class SourceAdapter(Protocol):
    source_id: ClassVar[str]
    def detect(self, table: RawTable) -> float:      # 0.0–1.0 confidence
        ...
    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        ...
```

Supporting a new source therefore costs one adapter file and one YAML mapping, and supporting a new file type costs one reader.

The `detect` method earns its place because the three region sheets in the modernization workbook carry headers that drift between them. The same field appears as `Mobile ` in one sheet and `Mobile Phone` in another, as `Website URL` in one and `Website` in another, as `Linkedin Profile` and `LinkedIn Profile`, as `Phone-2` and `Phone 2`. Detection performs fuzzy header matching against each adapter's declared column set, so this kind of drift resolves itself at runtime. When confidence falls below the floor, the adapter raises and asks a human to look at it.

### Provenance

Each canonical field records where it came from.

```python
@dataclass(frozen=True)
class Provenance:
    source_id: str          # "apollo" | "phantombuster" | "zoominfo" | ...
    file: str
    row: int
    captured_at: datetime | None
    field: str              # original column name
```

M4 depends entirely on this. When Apollo reports one title and LinkedIn reports another for the same person, the disagreement becomes actionable only once you know which source said which. Provenance lives in a sidecar structure alongside the data, which keeps the output file clean.

---

## 5. The canonical model

Every field below comes from one of the real files rather than from imagination.

An **Account** carries the following. Its `account_id` is assigned by M2. Its `name` and `name_raw` come from all sources, and its normalized `domain` derives from whichever of `Website`, `Website URL`, or `regularCompanyUrl` a given source provides. Location fields (`hq_country`, `hq_city`, `region`) appear everywhere, and `industry` with `sub_industry` come from Format-DB, Apollo, and ZoomInfo. ZoomInfo alone supplies `sic_codes` and `naics_codes`, which serve as the canonical join keys across sources whose industry strings are free text. Firmographics cover `revenue_usd`, `revenue_band`, `employees`, `employee_band`, `it_budget_usd`, `employee_growth_1y`, and `employee_growth_2y`, with the budget and growth fields coming from ZoomInfo. Structural fields include `ownership_type`, `business_model`, `parent_account_id`, `parent_relationship`, `subsidiary_domains`, `is_acquired`, `is_defunct`, and `certified_at`. A list of `signals` comes from the campaign status sheet.

A **Contact** carries a `contact_id` and an `account_id` assigned by M2, the name fields, and `title_raw` exactly as the source reported it. M1 assigns `canonical_role`, `role_tier`, `seniority`, and `function` alongside it. The normalized `linkedin_url` acts as the primary join key across every file. Contact details cover `email`, `email_pattern_match`, `phone`, `mobile`, and `direct_dials`, and location covers `city`, `state`, and `country`. PhantomBuster contributes `tenure_role_months`, `tenure_company_months`, `bio`, and `role_description`. M4 attaches a list of `flags`.

A **Signal** carries a `kind` drawn from a controlled vocabulary such as `legacy_stack`, `modernization_intent`, `hiring`, or `cloud_migration`. It holds the `text` as recorded, a `source_url` where one exists, an `observed_at` timestamp that supports the six-month freshness rule, and a `confidence` assigned by M3.

One split in this model matters more than the rest. `title_raw` persists unchanged forever while `canonical_role` sits beside it as a derived value, which means every downstream disagreement stays traceable back to what a source actually said.

---

## 6. The modules

### M0 — Ingestion and normalization

**Objective.** Any of the five known source schemas, arriving in any supported format, becomes the canonical model with every field preserved.

Version one ships five adapters. The `phantombuster` adapter handles 34 columns, `zoominfo_enrich` handles 67, `apollo_enrich` handles 34, `formatdb` handles 32 across three header variants, and `campaign_status` handles 15.

```
sieve normalize --in <file> [--source auto] --out-accounts A.csv --out-contacts C.csv
```

The Apollo adapter deserves a design note, because that file is structurally unusual. Each row holds the PhantomBuster input columns and the Apollo output columns side by side, describing the same person twice. The adapter therefore emits two contact records per row, distinguished by their provenance, and that choice is precisely what preserves the disagreement M4 later detects.

### M1 — Role and title taxonomy

**Objective one, normalization.** Map a free-text title onto a canonical role, resolving at least 90% of observed titles at a confidence of 0.8 or higher.

**Objective two, expansion.** Given a role, return the full variant set a query needs: the canonical name, spelling variants, seniority siblings, and platform filter IDs wherever they are known.

Matching proceeds through three stages, cheapest first. The first stage attempts exact and alias matching against the taxonomy YAML, which costs nothing, behaves deterministically, and handles the bulk of real traffic. The second stage normalizes aggressively, casefolding, stripping punctuation, and expanding `VP` to `Vice President` and `Sr` to `Senior`, then applies token-set fuzzy matching, which collapses the `VP of Engineering`, `Vice President of Engineering`, and `VP, Software Engineering` family onto one role. The third stage compares an embedding of the title against embeddings of each role description and thresholds the similarity, which catches phrasings the lexicon has never seen. It runs on a local sentence-transformer model and calls no API. An optional fourth stage sends the residue to an LLM behind the standard interface, and it stays switched off by default.

Titles falling below the confidence floor land in an `unmatched.csv` for human review, and that review is how the taxonomy grows over time.

Seniority, function, and role form three independent axes, following the structure Savinay drew in SalesFlow. Seniority runs Chief, VP, Director, Manager, Junior. Function covers Engineering, IT, HR, Marketing, and Finance. Role covers specialisations such as AI, ML, and Data. A single title resolves on all three axes separately.

The taxonomy seeds itself from real data. The seven decision-maker tiers in the LeadDrip specification provide the campaign-level structure. The `Recommended Target Roles` column across 180 accounts provides the human answer to what a campaign needs. The 387 observed titles in the modernization contacts and the 169 in the PhantomBuster export provide the variant space. The nine-value `CURRENT_TITLE` enumeration captured in the real Sales Navigator query shows how a practitioner writes these out by hand today.

```
sieve titles normalize --in contacts.csv --title-col Title --out out.csv
sieve titles expand --role "CTO" --campaign modernization --format leaddrip
```

```json
{
  "input": "CTO",
  "canonical": "chief_technology_officer",
  "tier": 1,
  "expansions": [
    {"text": "CTO", "linkedin_title_id": null, "seniority": "chief"},
    {"text": "Chief Technology Officer", "linkedin_title_id": null, "seniority": "chief"},
    {"text": "VP of Engineering", "linkedin_title_id": null, "seniority": "vp"}
  ],
  "siblings": ["chief_information_officer", "vp_engineering", "chief_architect"],
  "exclusions": [{"text": "Retired", "linkedin_title_id": 286}]
}
```

The `exclusions` block carries forward the retired-title exclusion visible in the captured query. Filtering departed people at query time costs nothing, whereas discovering them after enrichment means the credits have already been spent.

### M2 — Account and contact resolution

**Objective.** Assign one stable identifier per real-world entity across every source, and expose the parent and subsidiary graph.

Account matching runs in precedence order. It first attempts the normalized domain, stripping the scheme, the `www.` prefix, and any trailing slash, casefolding the result, and respecting the public suffix list. It then tries platform identifiers, using the ZoomInfo Company ID or the Apollo organization identity wherever either is present. It then normalizes the legal name, stripping `Inc`, `Ltd`, `plc`, `GmbH`, `Berhad`, `Pte`, and punctuation. It finally attempts fuzzy name matching within a single country, above a threshold.

Contact matching follows the same pattern. The normalized LinkedIn URL comes first, stripping the scheme, the `www.` prefix, the `/sales/` path segment, and any trailing slash, and it stands as the strongest key available across every file you have. Exact work email comes second. Normalized name combined with a resolved `account_id` comes third.

The hierarchy draws on both platforms. ZoomInfo supplies `Company ID (Ultimate Parent)`, `Entity Name (Immediate Parent)`, and `Relationship`, while Apollo supplies `Ultimate Parent Company Name` and `Suborganization Domains`. A future domain-level DNC check will depend on exactly this graph, since a suppression rule matching only a parent's primary domain lets every subsidiary domain walk straight through.

Acquisitions and rebrands are first-class entities in the model. The campaign status sheet already records several of them as free text: Xandr moving into Microsoft, AdTheorent into Cadent, playgroundxyz redirecting to gumgum, Outbrain and Teads merging, and MediaDonuts rebranding under Aleph. Each of these becomes an explicit `merged_into` edge in the account graph.

```
sieve resolve --in contacts.csv --out out.csv --graph accounts.json
```

The command appends `sv_account_id`, `sv_contact_id`, `sv_duplicate_of`, `sv_parent_account_id`, `sv_match_key`, and `sv_match_confidence`.

### M3 — ICP scoring and intent classification

**Objective.** Given an account and its signals, produce an ICP fit score and an intent tier, each accompanied by its reasons.

Two separable pieces make this up.

The first is **ICP fit**, which runs rules over firmographics and technographics defined in the campaign YAML. It considers revenue band, geography, industry via SIC and NAICS codes, employee count, IT budget, and growth rate. Geography follows the groupings Savinay recorded in SalesFlow, where APAC means six named countries, namely Singapore, Indonesia, Malaysia, the Philippines, Thailand, and Vietnam, rather than the region as a whole. The whole thing behaves deterministically, weights every factor explicitly, and explains its own output, and it is executable against your data today.

The second is the **intent tier**, which classifies signal text into High, Medium, or Emerging. It combines keyword families covering legacy stack indicators, M&A activity, platform consolidation, and compliance pressure, with optional embedding similarity against labelled exemplars.

One limitation deserves stating honestly. A human selected all 180 accounts in the campaign file, which means the dataset contains positives only and no negative class exists within it. That rules out training a discriminative ICP classifier for the time being. Two things remain genuinely supported. Similarity-based lookalike modelling works from positives alone, and it is exactly what Sanjay Sir describes as like-modelling. Intent tier prediction is a real labelled two-class problem sitting inside those same 180 rows.

TAL membership checking gets built as an interface now and wired up once the master list arrives.

```
sieve score --in accounts.csv --campaign config/scoring/icp_modernization.yaml --out out.csv
```

The command appends `sv_icp_score` on a scale of 0 to 100, `sv_icp_band`, `sv_intent_tier`, and `sv_score_reasons`, which holds a JSON list of objects carrying a factor, its contribution, and its supporting evidence.

### M4 — Contact validation and flagging

**Objective.** Surface records that look wrong, each with a reason attached, so that a human can review them. Sanjay Sir's step seven asks for suspect records to be flagged, and a flag preserves the record along with the information a reviewer needs in order to judge it.

Eight rules ship in version one, and each one can be toggled and scored independently.

| Rule | How it fires | Evidence available today |
|---|---|---|
| `title_disagreement` | Two sources report different titles for the same person | 181 real cases inside the Apollo file |
| `email_pattern_anomaly` | An email deviates from the dominant pattern inferred for its domain | 633 contacts spread across 124 companies |
| `role_irrelevant` | The canonical role falls outside the campaign's target roles | `Recommended Target Roles`, present per account |
| `tenure_stale` | Tenure suggests the person may have moved since capture | 427 of 479 PhantomBuster rows |
| `generic_mailbox` | The local part reads `info`, `sales`, `contact`, or similar | Savinay stated this requirement explicitly |
| `account_defunct` | The account is defunct, acquired, or carries a stale certification date | ZoomInfo status fields |
| `incomplete` | A required Format-DB field is absent | Present in every file |
| `duplicate` | The record resolves to an existing `contact_id` | Output of M2 |

The flags aggregate into a single `sv_confidence` value, and their weights live in YAML.

```
sieve validate --in enriched.csv --out out.csv --report report.json
```

The command appends `sv_flags` as comma-separated codes, `sv_flag_detail` as JSON, and `sv_confidence` on a scale of 0 to 1. The report summarises per-rule counts across the whole file.

### M5 — Internal contact lookup

**Objective.** Answer the question of whether you already hold this person, before a paid credit gets spent on them.

The module builds an index across every canonical contact you already have, which currently means 633 from the modernization campaign and 479 from the Singapore run. It keys on the normalized LinkedIn URL first, then email, then name combined with account.

```
sieve lookup --in candidates.csv --index data/index/ --out out.csv
```

The command appends `sv_known` as a boolean, along with `sv_known_contact_id`, `sv_known_email`, and `sv_known_captured_at`. Splitting the file on `sv_known` produces the subset that genuinely needs enrichment.

It also writes a savings figure into the report, and that figure is the number that makes the business case for this whole component when Savinay asks what it bought.

---

## 7. What it gets tested against

Everything in this section runs offline against files that already sit in the repository, and none of it requires platform access.

### M0, ingestion

| Test | Asset used | Passes when |
|---|---|---|
| Format coverage | Four CSVs and two XLSX files, one of which holds four sheets | Every file parses cleanly |
| Schema detection | Five distinct schemas | The correct adapter is selected at confidence 0.9 or above |
| Header drift | The NA, EU, and APAC sheets, carrying four drifting header names between them | All three map onto one canonical schema with no code change |
| Field preservation | A round trip from canonical form back to flat | Every non-null input cell is recoverable |
| Row counts | PhantomBuster 479, Apollo 576, ZoomInfo 21, Format-DB 633, Status 180 | The counts match exactly |

### M1, taxonomy

| Test | Asset used | Passes when |
|---|---|---|
| Coverage | 387 modernization titles plus 169 PhantomBuster titles | At least 90% resolve at confidence 0.8 or above |
| Variant clustering | The five spellings of `VP of Engineering`, and `CTO` against `Chief Technology Officer` | Each family collapses onto one canonical role |
| Expansion fidelity | The captured Sales Navigator query, which enumerates nine `CURRENT_TITLE` values for roughly six roles | `titles expand` reproduces at least eight of the nine human-written variants |
| Role recall | `Recommended Target Roles` across 180 accounts | A recommended role expands into a set containing the titles actually sourced for that account |
| Precision guard | A curated list of near-miss titles | `Data Engineer Intern` stays separate from `Data Engineer` at senior tier |

### M2, resolution

| Test | Asset used | Passes when |
|---|---|---|
| Trivial deduplication | `33Across` and `33Across Inc.`, sharing a domain within one sheet | Both receive one `account_id` |
| Legal suffix stripping | Berhad, plc, Pte, and Inc, appearing across all files | One `account_id` exists per real entity |
| Hierarchy | Singtel under Temasek with twelve subsidiary domains, Etiqa under Maybank, Hong Leong Bank under Hong Leong Financial Group | Parent edges exist and subsidiary domains attach correctly |
| Acquisitions | Six cases recorded as free text in the `Status` column | A `merged_into` edge exists for each |
| Cross-source contact join | 447 profiles present in both the PhantomBuster and Apollo files | At least 445 join on normalized LinkedIn URL |
| Account count sanity | 124 unique `Company` strings across the modernization contacts | The canonical count falls below 124, and every collapse can be explained |

### M3, scoring

| Test | Asset used | Passes when |
|---|---|---|
| Intent classification | 180 accounts labelled High or Medium | Stratified five-fold cross-validation, since the dataset is too small for a single holdout |
| Baseline to beat | The majority class is Medium at 103 of 180 | Accuracy exceeds 57.2%, with a target macro-F1 of 0.70 or better |
| Regional stability | EU at 21 High and 29 Medium, APAC identical, NA at 35 High and 45 Medium | Accuracy varies by ten points or less across the three regions |
| ICP rules | SalesFlow defaults, meaning $100M to $2B revenue, six named APAC countries, and an Oracle, Azure, or SAP stack | Known accounts land inside their expected bands |
| Explainability | Any scored row | Every score carries at least one reason with supporting evidence |

### M4, validation

| Test | Asset used | Passes when |
|---|---|---|
| `title_disagreement` | 181 confirmed disagreements among 532 matched rows | All 181 fire, and the 351 agreeing rows stay silent |
| Manual sub-review | A stratified sample of 50 drawn from the 181, hand-labelled as genuine role change or phrasing difference | Precision reaches 0.7 or better on genuine changes once the phrasing filter applies |
| `email_pattern_anomaly` | 633 contacts across 124 domains, every one carrying an email | A pattern is inferred for every domain holding three or more contacts, and the anomaly rate stays below 5% |
| `generic_mailbox` | Zero generic addresses present in the Apollo output | Zero false positives |
| `tenure_stale` | 427 PhantomBuster rows carrying tenure | Flags correlate with Apollo title disagreement above chance |
| `account_defunct` | ZoomInfo certification dates spanning 2024 to 2026 | Staleness computes correctly against the capture date |
| `incomplete` | 44 Apollo rows lacking a `Result`, and 262 matched rows lacking an email | Both classes fire, and they fire distinctly |

### M5, lookup

| Test | Asset used | Passes when |
|---|---|---|
| Recall | Index the 633 modernization contacts, then query the same set | Every record hits |
| Cross-source recall | Index the 479 PhantomBuster rows, then query the 576 Apollo rows | At least 447 hit, matching the known overlap |
| Precision | Query the 129 Apollo profiles absent from PhantomBuster | Zero hits |
| Savings report | The tests above | The report shows 447 avoidable enrichment calls out of 576 |

### Test infrastructure

```
tests/unit/         pure functions with no I/O, completing in under five seconds
tests/fixtures/     trimmed real files of 20 to 50 rows, committed to the repo
tests/golden/       expected outputs, diffed byte for byte
evals/              the accuracy harness for M1, M3, and M4, kept separate from
                    unit tests, reporting metrics rather than pass or fail
```

Unit tests gate merges, and evals produce a scorecard. Keeping them apart matters, because a mixed suite makes ordinary accuracy work feel like breakage every time a metric moves.

---

## 8. Repository layout

```
sieve/
├── pyproject.toml
├── README.md                       # section 0 of this doc, verbatim
├── config/
│   ├── sources/                    # one YAML per adapter: column mappings
│   ├── taxonomy/
│   │   ├── roles.yaml              # canonical roles, tiers, aliases, filter IDs
│   │   ├── seniority.yaml
│   │   ├── functions.yaml
│   │   └── geography.yaml          # SalesFlow groupings, APAC as six countries
│   ├── scoring/
│   │   └── icp_modernization.yaml
│   └── validation/rules.yaml
├── src/sieve/
│   ├── model/                      # Account, Contact, Signal, Flag, Provenance
│   ├── io/
│   │   ├── readers/                # csv, excel, tsv, json
│   │   ├── adapters/               # phantombuster, zoominfo, apollo, formatdb,
│   │   │                           # campaign_status, registry
│   │   └── writers/additive.py     # where the additive-output guarantee lives
│   ├── taxonomy/                   # normalizer, expander, matchers/
│   ├── resolve/                    # domain, name, account, contact, hierarchy
│   ├── scoring/                    # features, icp, intent, explain
│   ├── validation/                 # engine, rules/
│   ├── lookup/                     # index, store
│   ├── llm/                        # interface plus a null implementation
│   └── cli.py
├── tests/
└── evals/
```

The dependency list stays deliberately short: pandas, openpyxl, pydantic, rapidfuzz, pyyaml, and typer. Sentence-transformers is optional and imports lazily, so the core installs and runs on a machine that lacks it.

---

## 9. Build order

The phases below are ordered by their value to the pipeline that already exists, which differs from the order architectural tidiness would suggest.

| Phase | Ships | Reasoning |
|---|---|---|
| **P0** | M0: readers, adapters, canonical model, provenance, CLI skeleton | Every other module depends on it, and it becomes testable immediately against all six files |
| **P1** | M1: taxonomy | This is the asset LeadDrip's query builder needs right now, and it has the smallest surface of any module |
| **P2** | M2: resolution | It unblocks lookup, validation, and eventually DNC |
| **P3** | M4: validation | It fills a complete gap at Sanjay Sir's step seven, and it has the strongest labelled test data of any module |
| **P4** | M5: lookup | It becomes cheap once M2 exists, and it produces the credit-savings figure |
| **P5** | M3: scoring | It carries the highest strategic value while sitting partly blocked on the ICP definition and master TAL, though the rules engine can ship ahead of the model |

M3 sits last in build order while sitting first in strategic order, and that tension is real. It is simultaneously the piece nobody else is building and the piece most dependent on an input you do not yet control. The rules half of it depends on nobody, so it can ship in P5 whatever happens with the TAL.

---

## 10. Open questions

**The master TAL** still has to come from Sanjay Sir. Until it does, M3's TAL gate exists as an interface with nothing behind it.

**The ICP weights** need one conversation rather than one document. The specific thing to settle is which dimensions gate a company out entirely and which ones merely adjust its score.

**The DNC key** has to be decided before M2's export shape gets fixed. The gap Savinay confirmed as unhandled, where an individually suppressed person has never interacted with R Systems, resolves only if the store keys on the person and keeps domain suppression in a separate table.

**Filter ID mapping** determines how valuable M1's expansion output is to the query builder. LinkedIn title IDs are visible inside captured queries, so the question is whether a lookup table can be harvested from them or whether free-text values will carry the load on their own.

**The column prefix** is Spoorthy's call, since she is the one consuming the output. `sv_` is a proposal.

---

## 11. Interface contract summary

The table below covers integration with LeadDrip. Every call is optional, and skipping any one of them degrades output quality while leaving the pipeline running.

| Stage in LeadDrip | Sieve call | Consequence of skipping it |
|---|---|---|
| Before the query is built | `titles expand` | Fewer title variants get queried, so recall drops |
| Before sourcing | `score` | Accounts arrive unprioritised by ICP fit |
| Before enrichment | `lookup` | Credits get spent on contacts already held |
| After sourcing | `resolve` | Duplicates and parent-subsidiary confusion persist |
| After sourcing | `titles normalize` | Titles remain free text |
| After enrichment | `validate` | Wrong records reach outreach with nothing marking them |
