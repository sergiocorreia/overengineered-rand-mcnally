# {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

This README is the authoritative runbook for the project. Keep its command order, expected outputs, status, principal datasets, known gaps, and QC state current. Do not create a separate `RUNBOOK.md`.

Before source-to-record work, read `guides/DATA_EXTRACTION_GUIDE.md`. Before anomaly review or repair, read `guides/DATA_QUALITY_GUIDE.md`. Implementation follows the Python and Stata guides in the same directory and the repository-wide `AGENTS.md` safeguards.

## Project contract

| Item | Value |
| --- | --- |
| Project slug | `{{PROJECT_SLUG}}` |
| Dataset shape | `{{DATASET_SHAPE}}` |
| Unit of observation | **TODO: define before extraction** |
| Analytical key | **TODO: define before standardization** |
| Canonical time variable | **TODO: define, or write “not applicable” for a cross-section** |
| Source provider(s) | **TODO: list provider and collection identifiers** |
| Source PDF directory | `{{PDF_DIRECTORY}}` |
| External data root | `{{EXTERNAL_DATA_ROOT}}` |
| Principal Stata dataset | `data/{{PROJECT_SLUG}}.dta` |
| Principal tabular dataset | `data/{{PROJECT_SLUG}}.tsv` |

The source-specific extraction contract is not complete until every **TODO** in this README, `project.toml`, `code/2-inventory/selection_rules.py`, and `code/3-extraction/definitions/` is resolved from inspected source evidence.

## Current status

Update this table after every material run or review campaign. A check mark means the named acceptance condition passed, not merely that a command ran.

| Stage | Status | Last verified | Acceptance condition |
| --- | --- | --- | --- |
| 0. Project definition | ☐ Not ready | — | Unit, keys, source scope, missing-value semantics, and storage are documented |
| 1. Sources | ☐ Not started | — | Manifested source files are immutable, readable, hashed, and complete for declared scope |
| 2. Page inventory | ☐ Not started | — | Every source page is inventoried; all eligible pages have reviewed final classifications |
| 3. Extraction | ☐ Not started | — | Gold and concurrent trial pass; all selected pages have success or reviewed disposition |
| 4. Standardization | ☐ Not started | — | Flat contract asserted; raw values and provenance preserved; identities reviewed |
| 5. Reconciliation | ☐ Not started | — | Keys are unique; repeated vintages are resolved or excluded without averaging |
| 6. Exploration | ☐ Not started | — | Coverage, support, distributions, aggregates, and source composition reviewed |
| 7. Quality control | ☐ Not started | — | No open blocking cases; release manifest matches current artifacts |

**Current release status:** not release-ready.

**Last known good build:** none.

**Current extraction contract signature:** none.

**Current extraction pointer:** none.

## Principal datasets and artifacts

Keep `data/` flat. Add only coauthor-ready datasets and compact audit companions to this table.

| Artifact | Unit / purpose | Key | Status |
| --- | --- | --- | --- |
| `data/{{PROJECT_SLUG}}.dta` | Principal Stata dataset | **TODO** | Not built |
| `data/{{PROJECT_SLUG}}.tsv` | Principal language-neutral dataset | **TODO** | Not built |
| `data/pages.tsv` | One row per physical source page | `page_id` | Not built |
| `data/{{PROJECT_SLUG}}_coverage.tsv` | Explicit analytical and source support | **TODO** | Not built |
| `data/{{PROJECT_SLUG}}_audit.tsv` | Compact row-to-source lineage companion | **TODO** | Not built |

Bulk extraction runs, rendered pages, OCR, and model caches live under `{{EXTERNAL_DATA_ROOT}}`; they are not coauthor-facing datasets.

### Required audit contracts

| Artifact | Required identity and content |
| --- | --- |
| `sources/source_manifest.tsv` | Ordered source ID, provider/provider ID, title, optional canonical `source_date` (`YYYY-MM-DD` or blank), item/download URL or manual method, immutable filename, expected hash/page constraints, and notes |
| `data/source_inventory.tsv` | Exact manifest fields, including `source_date`, plus status, relative PDF path, size, physical page count, actual SHA-256, and check time |
| `data/pages.tsv` | `source_date`, source/page hashes, physical page, stable `page_id`, automatic/source/page decisions, final classification, and provenance |
| `manual/page_overrides.tsv` | Stable page ID, expected source hash, reviewed classification, and evidence note |
| `manual/gold/` | Risk-labeled selected pages, independent complete transcriptions, critical expectations, and contract-bound production gate |
| Extraction `nested.jsonl` / `flat.tsv` | Stable page/record IDs, source/render hashes, contract signature, explicit statuses/errors, values, provenance, and usage |
| `manual/record_corrections.tsv` | Record/field, expected old value, replacement, evidence page, source/contract hashes, reason, and review date |
| QC queue / `manual/quality_decisions.tsv` | Stable case ID, check, affected key/page, severity, evidence hash, disposition, evidence, and reason |
| Final data | `data/{{PROJECT_SLUG}}.dta`, `data/{{PROJECT_SLUG}}.tsv`, and documented flat audit/coverage companions |

## Known gaps and unresolved decisions

Replace this starter list with concrete cases and stable IDs as the project develops.

- **Source scope:** TODO — state dates, publications, editions, providers, and known missing holdings.
- **Form or table regimes:** TODO — record every layout change found during full-page inspection.
- **Selection evidence:** TODO — define positive, negative, and uncertain page evidence.
- **Extraction semantics:** TODO — define blank, explicit zero, dash, textual none, unreadable, partial date, and visible correction.
- **Identity policy:** TODO — define valid entities and how Banknorm/unmatched names are handled.
- **Repeated vintages:** TODO — define candidate eligibility and deterministic ranking, or state that vintages do not repeat.
- **QC bounds:** TODO — justify structural bounds and statistical review rules from domain evidence.
- **Open blocking cases:** none recorded yet; this means QC has not run, not that the data are clean.

## Storage and evidence policy

- Source PDFs: `{{PDF_DIRECTORY}}`.
- Acquisition receipts and snapshots: `{{EXTERNAL_DATA_ROOT}}/acquisition-runs/`.
- Rendered page images: `{{EXTERNAL_DATA_ROOT}}/rendered-pages/`.
- Page-review images: `{{EXTERNAL_DATA_ROOT}}/page-review-images/`.
- Locro/OCR evidence and selector cache: `{{EXTERNAL_DATA_ROOT}}/ocr/` and `{{EXTERNAL_DATA_ROOT}}/page-selection-cache/`.
- Model caches: `{{EXTERNAL_DATA_ROOT}}/data-extraction/cache/`.
- Immutable extraction run directories and bulk exports: `{{EXTERNAL_DATA_ROOT}}/data-extraction/exports/`.
- Immutable alternate-extraction candidates: `{{EXTERNAL_DATA_ROOT}}/data-extraction/alternate-exports/`.
- Banknorm cache: `{{EXTERNAL_DATA_ROOT}}/banknorm-cache/`.
- Project source metadata: `sources/`.
- Durable human decisions: `manual/`.
- Regenerable intermediate files: `temp/`.
- Figures, tables, QC queues/reports, and release artifacts: `output/`.
- Coauthor-ready datasets only: flat files under `data/`.

Never edit source PDFs, model cache records, generated queues, or prior run directories. Human corrections live in `manual/` and must name stable IDs, expected prior values, evidence, source/contract hashes, reason, and review date. Review queues are generated and read-only.

## Prerequisites

Run every command from the project root unless the step says otherwise.

```bash
uv lock
uv sync --locked
uv run ruff check .
uv run pytest
```

Python dependencies are declared in `pyproject.toml`. Yachay, Locro, and Banknorm are shared read-only dependencies; do not patch or install them in editable mode from this project. Stata 19 is available at `/usr/local/stata19/stata-mp`.

Validate the existing Stata environment with the offline smoke check:

```bash
/usr/local/stata19/stata-mp -b do code/test_common.do
```

Normal `common.do`, `build.do`, and stage-test commands fail closed when a required Stata package is missing or outdated. They never install packages, access the network, or mutate the personal Stata ado setup. Only when the smoke check reports a dependency problem, review `code/requirements.txt` and deliberately run the explicit bootstrap below; it may access the network and write to the personal Stata PLUS directory:

```bash
/usr/local/stata19/stata-mp -b do code/bootstrap_stata.do
```

Bootstrap is environment setup, not an ordinary rebuild command. Rerun `code/test_common.do` offline after bootstrap and record any shared-environment change in this README.

No command in this README is standing authorization for a network request, model call, retry, or full-corpus run. Full operations require approval for the exact displayed scope, settings, service, and expected cost immediately before launch.

## Complete workflow

### Step 0 — Define and inspect the research object

Before writing a schema:

1. Fill the project contract and known-gaps sections above.
2. Complete the relevant values in `project.toml`.
3. Inspect complete source pages at full resolution across dates, forms, offices, typography, handwriting, fading, corrections, annotations, tables, blank pages, and non-target material.
4. Document layout regimes and decide what printed administrative material must be ignored.
5. Define the analytical unit, key, time spine (for a panel), and missing-value meanings.

Run the offline configuration and contract tests without acquiring data:

```bash
uv run pytest
```

Expected result: configuration and directory checks pass. Do not continue to model work while any schema-critical TODO remains.

### Step 1 — Acquire or inventory source PDFs

First edit `sources/source_manifest.tsv`. Keep one ordered row per intended source with provider, title, optional canonical `source_date`, URL or manual acquisition method, immutable filename, expected checksum/page constraints, and notes. `source_date` must be blank or an exact `YYYY-MM-DD`; never infer a date from a title, filename, trend, or catalog decade. Leave it blank for a volume spanning dates unless one source-level date is genuinely supported.

For a generic provider, preview a bounded acquisition:

```bash
uv run code/1-download/download_sources.py --limit 5 --dry-run
```

Then acquire only that bounded cohort:

```bash
uv run code/1-download/download_sources.py --limit 5
```

For FRASER sources, configure the title/collection in `project.toml`, then collect a bounded metadata cohort into the canonical manifest:

```bash
uv run code/1-download/download_fraser.py \
  --title-slug REPLACE_WITH_FRASER_TITLE_SLUG \
  --metadata-jsonl {{EXTERNAL_DATA_ROOT}}/fraser-metadata.jsonl \
  --output-manifest sources/source_manifest.tsv \
  --limit 5
```

This bounded command makes at most five uncached item-metadata calls, preserves append-only metadata externally, and rebuilds the ordered manifest from cached records. Inspect the manifest before downloading PDFs. Use `--all` only after reviewing the catalog size and authorizing every remaining metadata call.

For manually procured files, place immutable PDFs in `{{PDF_DIRECTORY}}`, declare them with `acquisition_method=manual` in the same source manifest, and validate each explicit source:

```bash
uv run code/1-download/download_sources.py --source-id REPLACE_WITH_SOURCE_ID
```

Expected outputs:

- immutable PDFs at `{{PDF_DIRECTORY}}`;
- the ordered audited inventory `data/source_inventory.tsv`;
- append-only acquisition events and per-run snapshots beneath `{{EXTERNAL_DATA_ROOT}}/acquisition-runs/`;
- validated source IDs, canonical source dates, URLs/methods, filenames, SHA-256 hashes, page counts, and storage paths.

The full acquisition form is guarded:

```bash
# Run only after contemporaneous authorization for the displayed source count.
uv run code/1-download/download_sources.py --all
```

Rerun an interrupted bounded command; atomic `.part` publication and existing-file validation make resumption safe. Never delete a validated source to force a redownload without preserving and documenting the prior evidence.

### Step 2 — Inventory, select, and review physical pages

Build the physical-page manifest. Page identity is always `relative/source/path.pdf#page=N`:

```bash
uv run code/2-inventory/build_manifest.py --limit 3
```

Expected bounded output: `temp/pages.sample.tsv`, with source and page hashes, physical page number, stable `page_id`, embedded-text status, automatic suggestion, manual decision, final classification, and provenance. Every unresolved page defaults to `unreviewed`.

Configure source-specific evidence only in `code/2-inventory/selection_rules.py`, then run a bounded candidate search:

```bash
uv run code/2-inventory/find_candidates.py --limit 3
```

The selector searches embedded PDF text first, then targeted Locro OCR. Use a full-document Locro pass only when source inspection shows it is necessary and the exact scope is approved. OCR evidence and caches stay under `{{EXTERNAL_DATA_ROOT}}`.

Once the bounded sample behaves correctly, inventory and score the complete local source corpus:

```bash
uv run code/2-inventory/build_manifest.py --all
uv run code/2-inventory/find_candidates.py --all
```

Review candidate, negative-gold, and unresolved pages:

```bash
uv run code/2-inventory/review_pages.py
```

The localhost reviewer uses `[review].port_pages` from `project.toml` by default; pass `--port PORT` for a one-run override. It saves atomically to `manual/page_overrides.tsv`, and its keyboard shortcuts are displayed in the sidebar. Review at full resolution when classification is uncertain; nearby and continuation pages remain part of the evidence.

Build the extraction manifest only after review:

```bash
uv run code/2-inventory/export_selected_pages.py
```

Expected outputs:

- current `data/pages.tsv`;
- durable `manual/page_overrides.tsv`;
- the contract-bound extraction manifest `data/selected_pages.tsv`.

The export command is the fail-closed completeness gate: it must fail if a source/page is unresolved, stale, missing, hash-mismatched, or inconsistent with the positive/negative page fixtures. Do not bypass this gate to obtain a convenient extraction count.

### Step 3 — Define, calibrate, extract, and review records

The extraction definition lives in:

```text
code/3-extraction/definitions/schema.py
code/3-extraction/definitions/prompt.md
```

Keep it flat and observation-oriented unless visible evidence requires nesting. Pair normalized identity, date, and amount fields with raw transcriptions. Preserve explicit zero, blank, dash, printed `None`, unreadable, and not-applicable states through an explicit status field; also preserve partial precision, corrections, uncertainty, remarks, and document status. Known page provenance is supplied by the runner rather than requested from the model.

Manually transcribe a risk-based gold set from the rendered images into `manual/gold/`. Cover every known risk dimension, including difficult and negative pages. The gold set is independent evidence, not copied or retrofitted from model output.

Production readiness is recorded in `manual/gold/production_gate.json`. It binds the current contract/evidence signatures, exact rendered-corpus/preflight signatures, approved request ceiling, and four booleans: `gold_passed`, `trial_passed`, `cache_reuse_passed`, and `cost_reviewed`. `validate_gold.py` supplies the gold/evidence signatures and resets downstream approvals whenever the contract, selected corpus, fixtures, gold, or pricing changes. Record the other three fields as true only after the matching trial, repeated/cache-only checks, and immediate cost/scope review actually occurred for those exact signatures and the intended service. Enter an ISO-date and current non-placeholder rates in `[pricing]` before any provider call.

Preview the calibration cohort without provider calls:

```bash
uv run code/3-extraction/extract_records.py --calibration --dry-run
```

Run the bounded calibration only after reviewing its exact page and request count:

```bash
uv run code/3-extraction/extract_records.py --calibration --workers 2 --flex
uv run code/3-extraction/extract_records.py --calibration --status
uv run code/3-extraction/validate_gold.py
```

Review every critical discrepancy against the image. Fix a prompt/schema error and create a new contract namespace; fix a gold error only when direct image inspection proves the transcription wrong. Repeat pages to prove cache reuse.

Then test a small concurrent cohort through the intended production service:

```bash
uv run code/3-extraction/extract_records.py --trial --workers 4 --flex
uv run code/3-extraction/extract_records.py --trial --cache-only
uv run code/3-extraction/validate_gold.py
```

After inspecting the trial receipt and proving repeated/cache-only reuse, record `trial_passed` and `cache_reuse_passed` in `manual/gold/production_gate.json` for the current signature. Record `cost_reviewed=true` and a timezone-aware `cost_reviewed_at` only after the final full-run scope and price estimate are reviewed immediately before launch. The live command rejects a cost review older than `pricing.review_max_age_hours`.

Expected bounded outputs are immutable run directories beneath `{{EXTERNAL_DATA_ROOT}}/data-extraction/exports/`, each containing:

- `nested.jsonl` with complete audit envelopes and model records;
- `flat.tsv` with flat observations in manifest order;
- `tokens.tsv`, `errors.tsv`, and `review_queue.tsv`;
- a manifest snapshot and `run.json` with contract, model/service, render, source, image, library-revision, timing, and usage metadata;
- immediate per-page cache files beneath the contract namespace.

Review extracted records without editing caches:

```bash
uv run code/3-extraction/review_records.py
```

The reviewer stores append-preserved human files under `manual/` with expected model-response, source, render, and contract hashes. It supports row add/remove/reorder/duplicate, schema validation, side-by-side scans, zoom, keyboard navigation, save-and-next, and raw JSON for exceptional nested contracts. Accepted changes feed standardization with an exact before/after ledger; flagged or excluded reviews block the build.

Only after all production gates pass, obtain contemporaneous authorization for the exact page count, request ceiling, model, reasoning, service, and cost shown by the dry run. The owner launches the guarded full command; agents stop after verifying and reporting it. Full extraction requires Flex:

```bash
uv run code/3-extraction/extract_records.py --all --flex --max-requests REPLACE_WITH_APPROVED_COUNT --dry-run

# Remove --dry-run only after approval of the exact preview above.
uv run code/3-extraction/extract_records.py --all --flex --max-requests REPLACE_WITH_APPROVED_COUNT
```

The no-model dry run verifies the current page-selection gate, sources, exact rendered bytes, cache state, pending requests, model/reasoning/service, dated pricing, and projected incremental cost. Copy its `render_signature` and `preflight_signature` into the matching fields of `production_gate.json`, copy its exact `max_requests` into `approved_max_requests`, and record the review time in `cost_reviewed_at`; set `cost_reviewed=true` only after reviewing that output. Any changed evidence, expired cost review, or changed preflight blocks the live command and requires a fresh preview.

Flex is the full-run default. Standard service is restricted to configured bounded cohorts and an explicit hard request ceiling. A complete zero-error full run—or a complete cache-only reconstruction—may update the current-extraction pointer; bounded or partial runs may not.

The pointer is `data/extraction_current.json`; it records the immutable run directory, flat export, contract signature, and run-receipt hash rather than copying bulk results into Dropbox.

Reconstruct outputs later without model calls:

```bash
uv run code/3-extraction/extract_records.py --all --cache-only --flex
```

Failed pages remain explicit failures. Use `--retry-errors` only after the causes and bounded retry count are reviewed and approved.

### Step 4 — Standardize source observations

Edit `code/4-standardization/standardize.do` for the project's exact flat extraction contract. Preserve every raw value, source observation, source/page/record ID, hash, extraction status, and contract signature.

The stage first verifies every immutable run hash and applies accepted `manual/record-reviews/` decisions with their expected model hashes. It then applies corrections only from `manual/record_corrections.tsv`. Every correction must match the expected old value and source/contract hashes or the build fails. Use Banknorm through its installed interface for relevant U.S. bank/city names; preserve raw names and export unmatched or ambiguous cases.

Run the bounded stage and its checks:

```bash
/usr/local/stata19/stata-mp -b do code/4-standardization/standardize.do
/usr/local/stata19/stata-mp -b do code/4-standardization/test_standardize.do
```

Expected outputs: all standardized source observations in `temp/4-standardization/`, plus review TSVs in `output/4-standardization/`. No unresolved identity is silently dropped from the audit universe.

### Step 5 — Reconcile repeated vintages

Edit `code/5-reconciliation/reconcile.do` to state candidate eligibility and deterministic ranking. Preserve all candidates, never average disagreements, and export ties or ambiguous choices for review. For a project without repeated vintages, this stage validates keys and passes all observations through unchanged.

```bash
/usr/local/stata19/stata-mp -b do code/5-reconciliation/reconcile.do
/usr/local/stata19/stata-mp -b do code/5-reconciliation/test_reconcile.do
```

Expected outputs: one candidate selection or reviewed exclusion per analytical key in `temp/5-reconciliation/`, complete candidate support and conflict files in `output/5-reconciliation/`, and no unresolved tie entering the principal data.

### Step 6 — Explore and validate the analytical object

Edit `code/6-exploration/explore.do` to describe coverage, source support, identity composition, distributions, missingness, heaping, aggregates, maps, and meaningful source regimes. For panels, show the explicit time spine, gaps, entries/exits, coverage changes, and source-vintage shares. For cross-sections, show group support, robust within-group distributions, duplicates, accounting relationships, and source/page clustering.

```bash
/usr/local/stata19/stata-mp -b do code/6-exploration/explore.do
/usr/local/stata19/stata-mp -b do code/6-exploration/test_explore.do
```

Expected outputs: review tables and figures beneath `output/6-exploration/`. Exploration does not mutate raw observations or authorize corrections.

After standardization, reconciliation, exploration, and the project's Stata domain-QC hooks have been configured and tested independently, the ordinary deterministic Stata rebuild command is:

```bash
/usr/local/stata19/stata-mp -b do code/build.do
```

This writes the principal flat files `data/{{PROJECT_SLUG}}.dta` and `data/{{PROJECT_SLUG}}.tsv`, their documented audit/coverage companions, exploration artifacts, and Stata domain-QC results. It fails when a Stata contract or release assertion fails; the configurable Python case queue and hash release manifest still follow in Step 7.

### Step 7 — Detect, adjudicate, repair, and release

Add project-specific source/accounting assertions to the Stata hook, then run it and its contract test:

```bash
/usr/local/stata19/stata-mp -b do code/7-quality-control/quality_control.do
/usr/local/stata19/stata-mp -b do code/7-quality-control/test_quality_control.do
```

Run deterministic Python QC from the current principal dataset and provenance:

```bash
uv run code/7-quality-control/run_quality_control.py
```

Expected outputs beneath `output/7-quality-control/` include a stable read-only case queue, structural check results, advisory statistical flags, coverage/support files, page clusters, a summary, and release accounting. Human dispositions belong in `manual/quality_decisions.tsv`: `corrected`, `resolved`, `excluded`, `source_verified`, `expected_gap`, or `open`.

The QC command deliberately returns a failing status while any blocking case remains, after writing the review artifacts. Treat that as a release gate, not as permission to suppress the check or edit the generated queue.

Blocking checks cover key integrity, unresolved/failed pages, stale overlays, provenance, configured bounds, unresolved identity/reconciliation cases, and open blocking decisions. Panel checks add time-spine/coverage/gap/change/reversal/shift/entry/exit and repeated-vintage diagnostics. Cross-sectional checks add grouped robust outliers, duplicates, accounting inconsistencies, heaping, and bounds. Statistical flags never change data automatically.

When direct evidence indicates that a small page set merits an alternate extraction, first plan with zero transmission:

```bash
uv run code/7-quality-control/plan_alternate_extraction.py \
  --queue-tsv output/7-quality-control/review_queue.tsv \
  --case-id REPLACE_WITH_EXPLICIT_CASE_ID \
  --max-requests 5
```

The planner is dry-run by default: it prints the candidate-only namespace and exact request count but writes nothing and makes no model calls. Add `--write-plan --output output/7-quality-control/alternate_plan.json` only after inspecting the plan.

The actual alternate runner is also dry-run by default. Use it to inspect the exact page set, Standard/high settings, segmentation plan, and request ceiling without transmitting a page:

```bash
uv run code/7-quality-control/run_alternate_extraction.py \
  --queue-tsv output/7-quality-control/review_queue.tsv \
  --case-id REPLACE_WITH_EXPLICIT_CASE_ID \
  --max-requests 5
```

Only after contemporaneous authorization for that exact preview may the owner add `--execute`. Use `--cache-only` to reconstruct the candidate export with zero provider calls. For dense tables, add `--segmented --page-height-px HEIGHT` and, when needed, `--header-height-px HEIGHT`; the runner renders higher-resolution overlapping bands with repeated header context and refuses incomplete or inconsistent overlap evidence. Alternate caches stay beneath `{{EXTERNAL_DATA_ROOT}}/data-extraction/cache/alternate/`, while immutable candidate exports stay beneath `{{EXTERNAL_DATA_ROOT}}/data-extraction/alternate-exports/`. They never update the baseline current pointer or promote an answer. A human must approve an evidence-bound correction overlay before a rebuild.

After adjudication:

```bash
/usr/local/stata19/stata-mp -b do code/build.do
uv run code/7-quality-control/run_quality_control.py
```

Compare the repaired build with a frozen cache-only baseline. Export exact key/field/provenance differences and reject changes outside authorized ledger lineage.

Build a release manifest only when there are no open blocking cases:

```bash
uv run code/7-quality-control/build_release_manifest.py
uv run code/7-quality-control/build_release_manifest.py --verify
```

Expected release artifacts include deterministic quality summaries and flags, coverage/support, exact repair differences, release accounting, and a hash-based manifest beneath `output/7-quality-control/`. Update the status, principal-dataset table, known gaps, QC state, row/entity/period counts, and last-known-good build in this README in the same commit.

## Crash recovery and safe reruns

| Interrupted operation | Safe action |
| --- | --- |
| Bounded/source download | Rerun the identical command; validate existing immutable files and resume `.part` files |
| Page inventory/OCR | Rerun the identical bounded cohort; external caches are content addressed |
| Manual page or record review | Restart the local app; atomic decisions under `manual/` remain durable |
| Model extraction | Rerun the identical cohort; compatible page caches are reused immediately |
| Output reconstruction | Use `extract_records.py --all --cache-only`; it must make zero provider calls |
| Stata build | Rerun `code/build.do`; outputs are deterministic from immutable evidence and manual overlays |
| QC | Rerun QC; stable case IDs preserve prior decisions and newly stale decisions fail visibly |

Never recover by deleting manual decisions, changing a source file in place, editing a cache, lowering a gold expectation, or silently relaxing a blocking gate.

## Release and coauthor handoff

Before declaring a release ready, record here:

- principal file names, unit, key, and time semantics;
- rows, entities, periods, source pages, and source vintages;
- uniqueness, missingness, exclusion, and reconciliation guarantees;
- explicit confirmation of whether imputation or winsorization exists (normally neither);
- expected gaps, excluded conflicts, and retained source-verified extremes;
- every open advisory flag and confirmation that open blocking cases equal zero;
- provenance from principal rows to source observations, pages, rendered images, PDFs, and contract caches;
- the build command, test results, release-manifest hash, and last verified date.

The release criterion is not “no unusual values.” It is “no unexplained structural anomalies, no silent conflicts, and every consequential decision traceable to visible evidence.”
