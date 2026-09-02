# Reference Projects and Design Lineage

This template condenses patterns that were proved in several historical-data projects. These projects are evidence and implementation references, not libraries. Adapt the smallest relevant component, preserve its tests and safeguards, and replace every source-specific rule. Do not import a project directory or copy empirical thresholds wholesale.

## Methodological baseline

- [Historical-document-extraction skill](</home/sergio/.codex/skills/historical-document-extraction/SKILL.md>) — inspection before schema design, raw-plus-normalized contracts, representative gold transcription, immutable contract caches, bounded calibration, and provenance-rich exports.
- [Historical-extraction quality gates](</home/sergio/.codex/skills/historical-document-extraction/references/quality-gates.md>) — risk coverage, per-page checks, and production-readiness requirements.
- [Template extraction guide](guides/DATA_EXTRACTION_GUIDE.md) — the full source-to-extraction workflow.
- [Template data-quality guide](guides/DATA_QUALITY_GUIDE.md) — detection, evidence review, repair, counterfactual comparison, and release gates.

## Reference map

| Project | Reuse or adapt | Do not copy unchanged |
| --- | --- | --- |
| Bank debits | FRASER adapter; source/page manifests; extraction contract and cache signatures; gold validation; flat-record review; raw-place preservation; Banknorm integration; repeated-vintage reconciliation; stable QC cases; counterfactual and release manifests | G.6 title IDs, statement numbers, city/date parsing, weekly time spine, place overrides, reconciliation rankings, amount thresholds, request counts, or empirical release results |
| Building permits | Generic source-manifest acquisition; multiple-provider inventories; atomic and restartable downloads; explicit form regimes; bounded calibration; keyed record corrections; cross-sectional/grouped review artifacts | BLS/JSTOR/Google Books provider IDs, city-table ranges, permit fields, source-specific cost anomalies, form dates, place mappings, or project QA thresholds |
| The Chronicle | Production-tested FRASER catalogue and downloader; text/OCR page selection; keyboard-first page review; complete-selection export; cache-only extraction; Banknorm standardization; deterministic repeated-vintage reconciliation; bounded dense-page segmentation with overlapping headers | Chronicle title ID, clearing-table headings, weekly continuation heuristics, page-coordinate plans, city clearing schema, source rankings, repair ledgers, or any corpus-specific request ceiling |
| ST6386 | Classification provenance and override precedence; path-based page identity; extraction-manifest ordering; nested and flat audit exports; normalized-plus-raw form fields; hand-transcribed gold/reference validation; bank/place standardization | Form classifiers, archival box logic, ST6386/X-4401/X-4402 fields, check-mark semantics, sample pages, or NARA-specific storage conventions |
| Rand McNally | Evidence that long-lived publications require explicit form regimes; prompt/schema consistency checks; separation of banks, correspondents, and nonbanks; direct page/image audits; Stata import and mapping outputs | The monolithic extraction script, year-by-year duplicated prompts/schemas as an architecture, mutable post-hoc fix scripts, ad hoc files, implicit retries, mixed current/backup code, or historical outputs without current contract hashes and review receipts |

## Bank debits

Root: [`/home/sergio/Dropbox/Projects/Historical-Documents/bank-debits`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits>)

Particularly useful components:

- [`code/1-download/fraser.py`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/1-download/fraser.py>) and [`download_pdfs.py`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/1-download/download_pdfs.py>) for a project-local FRASER adapter with bounded operation.
- [`code/2-inventory/build_manifest.py`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/2-inventory/build_manifest.py>) for physical-page manifests and explicit review precedence.
- [`code/3-extraction/contract.py`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/3-extraction/contract.py>) and [`extract_city_values.py`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/3-extraction/extract_city_values.py>) for contract hashing, immediate page caches, guarded cohorts, and deterministic exports.
- [`manual/gold/`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/manual/gold>) and extraction tests for independent image transcription and critical expectations.
- [`code/4-standardization/standardize_bank_debits.do`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/4-standardization/standardize_bank_debits.do>) and [`code/5-reconciliation/reconcile_bank_debits.do`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/5-reconciliation/reconcile_bank_debits.do>) for observation preservation, Banknorm, deterministic source selection, and explicit conflicts.
- [`code/7-quality-control/`](</home/sergio/Dropbox/Projects/Historical-Documents/bank-debits/code/7-quality-control>) for structural gates, anomaly queues, durable decisions, release accounting, and hash manifests.

The worked findings in [the quality guide](guides/DATA_QUALITY_GUIDE.md#appendix-a-bank-debits-case-study-not-template-defaults) explain what this project learned. Its numerical thresholds and results are a case study, not template defaults.

## Building permits

Root: [`/home/sergio/Dropbox/Projects/Historical-Documents/building-permits`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits>)

Particularly useful components:

- [`code/1-download/sources.py`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits/code/1-download/sources.py>) and [`download_sources.py`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits/code/1-download/download_sources.py>) for a provider-neutral source inventory and bounded downloader.
- The download tests for filename safety, exact destinations, source metadata, collisions, resumption, and provider variants.
- [`code/2-inventory/`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits/code/2-inventory>) for locating table families across multiple source forms and providers.
- [`code/3-extraction/FORM_REGIMES.md`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits/code/3-extraction/FORM_REGIMES.md>) and sibling regime notes for recording layout changes before schema/prompt design.
- [`manual/gold/`](</home/sergio/Dropbox/Projects/Historical-Documents/building-permits/manual/gold>) and the keyed correction/QA ledgers for risk-based calibration and evidence-preserving review.

This is the best generic acquisition reference because it separates provider metadata, acquisition mechanics, source inventories, and table-family logic.

## The Commercial and Financial Chronicle

Root: [`/home/sergio/Dropbox/Projects/the-chronicle/digitization`](</home/sergio/Dropbox/Projects/the-chronicle/digitization>)

Particularly useful components:

- [`code/1-pdf-download/fraser.py`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/1-pdf-download/fraser.py>), [`fraser_download.py`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/1-pdf-download/fraser_download.py>), and their tests for catalog pagination, item metadata, validation, collision detection, atomic `.part` publication, restartability, and offline indexing.
- [`code/2-page-selection/`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/2-page-selection>) for embedded-text/OCR selection, targeted rescue, a fast keyboard reviewer, and `--require-complete` export.
- [`code/3-data-extraction/`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/3-data-extraction>) for bounded extraction, immediate caches, status, review overlays, cache-only canonical reconstruction, and dense-page repair using visually planned repeated-header bands.
- [`code/4-standardization/standardize_clearings.do`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/4-standardization/standardize_clearings.do>) and [`code/5-reconciliation/reconcile_clearings.do`](</home/sergio/Dropbox/Projects/the-chronicle/digitization/code/5-reconciliation/reconcile_clearings.do>) for raw-place preservation, candidate retention, Banknorm, and deterministic non-averaging reconciliation.

The Chronicle contains advanced repair machinery. Reuse its invariants—bounded requests, immutable plans, agreeing overlaps, full-block review, and no automatic promotion—without copying its calibrated crop coordinates or repair campaign.

## ST6386

Root: [`/home/sergio/Dropbox/Projects/Historical-Documents/st6386`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386>)

Particularly useful components:

- [`code/1-classification/classification_methods.md`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386/code/1-classification/classification_methods.md>) for classification provenance and explicit manual override semantics.
- [`code/2-extraction/internal/extraction_manifest.py`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386/code/2-extraction/internal/extraction_manifest.py>) for stable path-based identities, precedence, grouping, and source ordering.
- [`code/2-extraction/internal/extraction_results.py`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386/code/2-extraction/internal/extraction_results.py>) for nested audit records and flat analytical outputs.
- [`code/2-extraction/validation/validate_reference.py`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386/code/2-extraction/validation/validate_reference.py>) for a bounded, manually checked pre-production reference.
- [`code/3-compilation/4-standardize-names.do`](</home/sergio/Dropbox/Projects/Historical-Documents/st6386/code/3-compilation/4-standardize-names.do>) for Banknorm use and identifier conventions.

ST6386 is the strongest reference for form-like records with checkboxes, annotations, partial dates, and multiple document statuses.

## Rand McNally

Root: [`/home/sergio/Dropbox/Projects/Historical-Documents/Rand McNally`](</home/sergio/Dropbox/Projects/Historical-Documents/Rand McNally>)

Rand McNally predates the current safeguards and is primarily a lessons-learned reference. Its publication spans many layout regimes, and its notes document provider-quality shifts, fields discovered after earlier runs, prompt omissions, zero/year confusion, and separate bank/correspondent/nonbank outputs. Those facts justify explicit form-regime inspection, raw fields, contract versioning, representative gold pages, and rerunnable standardization.

Do not reproduce these older patterns:

- one large script that mixes discovery, rendering, provider calls, retries, flattening, and publication;
- near-duplicate prompt/schema files without a small documented regime registry;
- editing or replacing outputs as a correction mechanism;
- implicit `retry_errors=True` or unbounded reruns;
- backups and diagnostic scratch files mixed with current code;
- schema gaps repaired only by later cleaning scripts; or
- outputs that cannot be tied to a prompt/schema/model/render/source signature.

When a Rand McNally behavior conflicts with this template's extraction guide or quality gates, the current template rule wins.

## Shared libraries

- **Yachay** is the extraction interface. Reuse its installed API; record the exact revision in run metadata.
- **Locro** supplies OCR where embedded PDF text is absent or inadequate. Prefer targeted OCR before full-corpus OCR and keep its high-volume outputs external.
- **Banknorm** standardizes bank and city identities in Python/Stata. Preserve raw values and unmatched candidates; never silently patch the shared package from a research project.

Changes to any shared library require separate approval and regression testing in that library's repository. A revision change is material provenance and may invalidate downstream comparability even when the extraction prompt and schema do not change.
