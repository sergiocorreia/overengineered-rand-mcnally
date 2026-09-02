# Data Extraction Guide for Historical Documents

This guide describes how to turn scanned historical sources into auditable structured records. It governs source acquisition, physical-page selection, schema and prompt design, gold transcription, model calibration, caching, review, and the handoff to standardization. [The data quality guide](DATA_QUALITY_GUIDE.md) begins once observations exist and governs anomaly detection, evidence review, correction, and release.

The central rule is: **build extraction as a versioned evidence contract, not as a one-off OCR loop.** A usable row must remain traceable to what was visible on a particular physical page under a particular prompt, schema, model, render, and software revision.

## 1. Define the research object before the extraction object

Write down the intended analytical object before searching pages or designing fields:

- the unit of observation;
- the entity identifier and whether it is printed, constructed, or standardized later;
- the canonical time variable and frequency for a panel;
- whether one source can print several periods or repeated publication vintages;
- eligible publications, editions, dates, geographies, and table families;
- the meaning of blank, explicit zero, dash, textual `None`, unreadable, not applicable, and excluded;
- the final coauthor-facing `.dta` and `.tsv` files; and
- the claims that the data must be able to support.

Do not let a convenient page layout silently define the unit. One table row may contain several analytical observations, and one observation may be repeated in later publications. Preserve those distinctions from the beginning.

For a panel, define both the intended analytical time spine and the source-support spine. For a cross-section, define the sampling or coverage frame and meaningful groups. These definitions later determine whether a missing row is an extraction failure, a source gap, an ineligible observation, or a real absence.

## 2. Store sources, caches, and decisions separately

The standard project boundaries are:

```text
sources/   Small source metadata and, only for small corpora, immutable PDFs
manual/    Durable human selections, transcriptions, corrections, and decisions
data/      Flat coauthor-ready DTA/TSV products
temp/      Regenerable intermediate files
output/    Review queues, receipts, figures, QC reports, and releases
```

If all PDFs are expected to total at most 100 MB, they may live in `sources/pdfs/`. Otherwise they live under `/home/sergio/data/<slug>/pdfs/`. Rendered images, Locro output, selector caches, model caches, and bulk extraction runs always remain under `/home/sergio/data/<slug>/` rather than Dropbox.

Original sources, rendered evidence, OCR, model responses, and immutable run receipts are never edited in place. A reviewer changes a separate file under `manual/`; a deterministic rebuild combines the immutable evidence with current reviewed decisions.

## 3. Acquire against a source manifest

Use one ordered `sources/source_manifest.tsv` for downloaded and manually acquired material. A source row should include at least:

- stable `source_id`;
- provider and collection/title identifier;
- title and edition information;
- optional canonical `source_date` as `YYYY-MM-DD`, left blank when one exact source-level date is not supported;
- source URL or a documented manual acquisition method;
- exact relative filename;
- expected SHA-256 hash when known;
- expected page-count or size constraints when known; and
- notes on rights, completeness, duplicates, or provider quirks.

The acquisition layer should be generic. Provider-specific catalog parsing belongs in a small adapter, such as the FRASER adapter. It must not contain page-selection or extraction-schema logic.

Required acquisition behavior:

- bounded listing and downloading are the default; `--all` is explicit;
- exact destination paths are checked for containment beneath the configured PDF root;
- URLs and filenames are validated; collisions fail rather than overwrite;
- completed source files are immutable and validated before reuse;
- downloads publish atomically through a same-directory `.part` file;
- interrupted downloads or metadata listing can be resumed;
- metadata is append-only and each run snapshots the input manifest and settings;
- PDF readability, hash, and physical page count are checked; and
- manually acquired PDFs pass through the same inventory and provenance contract.

Do not trust a provider basename to be globally unique. Do not select one of several scans merely because it downloads first; preserve provider and copy identity until quality and completeness can be compared.

## 4. Inspect the form family before designing fields

View complete pages at full resolution. Sample across:

- earliest, middle, and latest dates;
- known publication or form revisions;
- offices, states, volumes, boxes, providers, or scan batches;
- printed, typed, and handwritten entries;
- clean, faded, extremely faded, skewed, clipped, or damaged pages;
- handwritten annotations, stamps, corrections, and marginal facts;
- pages with explicit zero, blanks, dashes, cents, partial dates, and unusual marks;
- dense multi-block tables, continuation pages, and repeated headers; and
- non-target, blank, cover, administrative, index, and uncertain pages.

Distinguish printed labels from entered values. List filing stamps, routing marks, declassification marks, record-entry dates, page furniture, subtotals, and other administrative material that must not become data.

Write a short form-regime note whenever labels, columns, meaning, precision, page layout, or target population changes. Prefer one clear regime registry and small differences over duplicated year-specific pipelines. A different visible concept may require a different schema; a cosmetic layout change may require only a selection or prompt rule.

Do not finalize the schema from cropped examples alone. Crops can hide remarks, corrected labels, continuation cues, footnotes, a second table block, or evidence that the page is not the intended document.

## 5. Inventory every physical page with stable identity

Inventory the complete physical PDF page sequence. The stable page identity is:

```text
relative/source/path.pdf#page=N
```

Here `N` is the physical PDF page number used by the renderer and extraction runner. Preserve printed page labels separately; they may be absent, duplicated, or inconsistent. Identity must include the relative path, not only the basename.

`data/pages.tsv` should preserve:

- `source_id`, optional source-level `source_date`, relative path, source hash, and page hash;
- physical page number and stable `page_id`;
- embedded-text availability and relevant text evidence;
- OCR method/version/cache identity when OCR was needed;
- automatic classification and rule/evidence provenance;
- source-level manual decision;
- page-level manual decision;
- final classification and the precedence that produced it; and
- current/stale/missing status.

Preserve source-manifest order and physical page order in manifests and all later exports. Model concurrency must not reorder research evidence.

## 6. Use text and OCR proportionally

Use the least expensive evidence source that reliably supports selection:

1. search existing embedded PDF text;
2. run targeted Locro OCR on pages or regions with plausible evidence;
3. run full-document Locro only when embedded text and bounded OCR cannot establish the page family.

OCR is selection evidence, not authoritative transcription. Keep its raw output, coordinates when available, tool revision, settings, source/page hash, and cache location. A changed source image or OCR contract invalidates the derived evidence.

Keep source-specific scoring in `code/2-inventory/selection_rules.py`. The reusable inventory layer should handle stable identity, OCR dispatch, ordering, caching, precedence, manifests, and review without knowing that one publication uses a particular heading.

Automatic selection should prefer interpretable signals: visible headings, table labels, distinctive row/column language, date patterns, and continuation phrases. Numerical density alone is weak evidence; many historical pages contain unrelated tables.

## 7. Review page selection and fail closed

Every unresolved source and page defaults to `unreviewed`. Apply decisions in this order:

1. automatic suggestion provides evidence only;
2. a manual source-level decision overrides automatic evidence for that source;
3. a manual page-level decision overrides the source-level decision for that page.

Never use absence from an overrides file as implicit approval. Store page decisions in `manual/page_overrides.tsv` with stable page ID, expected source hash, classification, note, and review metadata.

A page reviewer should be localhost-only and keyboard-first. It should support:

- previous/next page and document;
- accept, flag/uncertain, and exclude;
- zoom and full-resolution inspection;
- go-to-page and unresolved/flagged filters;
- source context and automatic evidence;
- progress counts;
- disabled shortcuts while typing;
- unsaved-change warnings; and
- same-directory atomic saves.

Export the extraction manifest only with a `--require-complete`-style gate. Refuse export when:

- any in-scope source or page remains unresolved;
- a source/PDF/page is missing or its hash changed;
- an override is stale or references an unknown page;
- a selected page cannot be rendered;
- a positive or negative page fixture disagrees with final selection; or
- selected page order or identity is inconsistent.

An interim diagnostic export must be clearly labeled incomplete and cannot authorize production extraction.

## 8. Design a strict, auditable schema

The default model response should be flat and observation-oriented. The extraction runner adds known provenance; the model should not waste tokens repeating path, page, contract, or render data already known exactly.

For each normalized identity, date, or amount, preserve a raw transcription. Typical pairs are:

```text
entity_raw       entity_normalized
date_raw         date_normalized
amount_raw       amount_normalized
```

Normalization must not exceed the precision shown. A readable month/year must not become a fabricated day. A damaged value fragment should remain raw with normalized null and uncertainty, not be completed from another page or a total.

Preserve explicitly:

- blank separately from explicit zero;
- partial dates at visible precision;
- dollars/cents and signs as printed;
- original and replacement values for visible corrections;
- raw placement and details for marks in adjacent rows or columns;
- remarks without paraphrase;
- supplemental facts outside standard boxes;
- uncertain fields and substantive unmapped text; and
- a document-status field so a residual classification error yields a non-target/uncertain result rather than fabricated observations.

When a correction changes a printed label or concept, leave the superseded standard field null, store the new concept as a supplemental fact, and preserve the label correction. Never infer a total, status, person, bank, city, date, cause, or unit from outside knowledge.

Use Pydantic descriptions for field semantics and strict validation. Keep interpretation and visual-reading rules in the prompt. Avoid deeply nested abstractions when a flat observation list plus page-level status is sufficient.

## 9. Write the prompt as transcription policy

The prompt should identify the visible target, define entered data, and list what must be ignored. Give explicit rules for:

- blank, zero, dash, textual none, and unreadable;
- partial dates and precision;
- currency, cents, parentheses, and signs;
- corrections, overwriting, and crossed-out text;
- faint characters and uncertainty;
- marks in adjacent rows or columns;
- footnotes, remarks, marginal facts, and form variants;
- multi-block tables and continuations;
- non-target or uncertain pages; and
- the prohibition on inference from totals, neighboring records, trends, or outside knowledge.

Tell the model to preserve uncertainty instead of guessing. Do not give examples containing plausible values that could be copied into the current record. Keep the prompt concise enough that the visible page remains the dominant evidence.

Test prompt/schema consistency: every prompt field should exist in the schema, every important schema concept should be explained, and nullable fields should have a clear evidence rule.

## 10. Construct independent risk-based gold data

Transcribe the gold fixture manually from images before judging model output. Do not derive it from OCR, model output, cleaned data, or a later source table.

Choose a compact sample that covers every known risk, not only clean pages. Coverage should include, when present:

- clean, faded, and extremely faded pages;
- typed and handwritten values;
- cents, explicit zero, dashes, textual none, and true blanks;
- full and partial dates;
- crossed-out and overwritten values;
- marks in each adjacent column, both columns, and unusual mark text;
- supplemental/marginal facts and remarks;
- later form revisions and alternate providers; and
- a residual non-target or uncertain page when selection risk is meaningful.

Store stable page IDs, risk labels, the complete reviewed record, and critical expectations. Include both positive and negative page fixtures for selection when appropriate.

Review each critical discrepancy against the full-resolution image. Do not weaken a correct fixture to make the model pass. If the fixture is wrong, document the image evidence for changing it. If the extraction contract is wrong, change the prompt/schema, create a new namespace, and rerun only the bounded affected sample.

## 11. Sign every material extraction input

The extraction contract signature and per-page cache identity must cover all material inputs:

- prompt bytes;
- generated JSON schema and schema source bytes;
- model, reasoning level, and service mode;
- output-token, media, image-detail, and render settings;
- pipeline version;
- Yachay revision/tree hash;
- source PDF hash;
- actual rendered-image hash; and
- ordered page identities for a multi-page request.

Record Locro and Banknorm revisions in the stages where they affect results. A changed material input creates a new cache namespace. Do not silently migrate or relabel an older response as compatible.

Worker count and output directory do not change what the model sees and therefore do not invalidate a cache. Record them in run metadata, but keep them out of the semantic contract signature.

For render identity, hash the exact bytes sent to the model, not merely the nominal DPI setting. This catches clipping, rotation, resampling, and renderer changes.

## 12. Keep the runner small and its cohorts explicit

The canonical extraction entry point should require exactly one cohort selector:

- `--calibration` for the manually transcribed risk set;
- `--trial` for a small concurrent production-service check;
- `--limit N` for a bounded manifest prefix;
- `--year YEAR` for a bounded documented slice;
- repeatable `--page-id PAGE_ID`;
- `--queue-tsv PATH` for an immutable reviewed queue; or
- `--all` for a guarded corpus operation.

Operational options include `--workers`, `--flex`/`--standard`, `--dry-run`, `--status`, `--cache-only`, `--retry-errors`, and `--max-requests`.

Required guard behavior:

- Flex is the default service.
- Standard is limited to bounded cohorts and a configured hard ceiling.
- Large and full runs require Flex unless a separately approved bounded repair explicitly uses Standard.
- `--dry-run` prints ordered pages, cached/uncached/error counts, estimated requests, service/model/reasoning, expected cost when available, and output/cache locations without provider calls.
- `--max-requests 0` is a useful proof of the exact uncached request count.
- Cached errors are not retried unless `--retry-errors` is explicit.
- A queue is bound to its path, bytes/hash, selected page identities, and contract.
- A bounded run never overwrites a shared canonical export or pointer.

The runner should coordinate operations, not absorb schema definitions, page selection, review storage, QC rules, or project-specific normalization.

## 13. Cache immediately and export deterministically

Write each page result as soon as the request finishes. An interruption should lose at most in-flight requests. Use atomic publication and do not treat a failed response as a successful page with zero records.

Preserve canonical manifest order in exports even when requests finish concurrently. Each immutable run directory should contain:

- complete nested JSONL;
- flat TSV;
- explicit page and record statuses;
- errors and review queue;
- token usage and request timing;
- manifest and manual-selection snapshots;
- contract and per-page content hashes;
- model, reasoning, service, and render settings;
- pipeline and shared-library revisions;
- cache locations; and
- a `run.json` receipt with requested, cached, successful, failed, skipped, and exported counts.

Provide cache-only reconstruction that applies current reviewed page classifications without making model calls. Its output must be deterministic. A complete zero-error full extraction or complete cache-only reconstruction may update the current-extraction pointer; targeted, bounded, incomplete, or error-containing runs may not.

## 14. Calibrate in gates, not one leap

Use this order:

1. Unit-test identities, manifest precedence/order, contract signatures, schema validation, normalization/flattening, cache behavior, and cache-only export.
2. Run one clear and one difficult page through the ordinary service.
3. Run the complete gold sample and compare every critical field directly with the image.
4. Run a very small concurrent cohort through the intended production model, reasoning, region, and service.
5. Repeat pages to prove durable cache reuse and interruption-safe resumption.
6. Reconstruct the cohort cache-only and prove deterministic bytes/order where promised.
7. Stop before production unless every gate passes and the owner explicitly authorizes the exact previewed run.

Production readiness requires:

- unit tests pass;
- every critical gold expectation passes after image review;
- intended model, reasoning, region, and service were tested live;
- concurrent and repeat calls demonstrate cache safety;
- contract changes create new namespaces;
- cache-only export follows current reviewed classifications;
- outputs contain nested/flat data, provenance, statuses, usage, hashes, and revisions;
- page selection is complete;
- the full command has an explicit guard; and
- expected page count, uncached requests, and likely cost were reviewed immediately before launch.

Record these gates in `manual/gold/production_gate.json`. It contains the current contract/evidence signatures, exact rendered-corpus and preflight signatures, approved request ceiling, and four booleans: `gold_passed`, `trial_passed`, `cache_reuse_passed`, and `cost_reviewed`. `validate_gold.py` supplies the gold/evidence check and invalidates downstream approvals when material evidence changes. Record trial, cache-reuse, and cost review as passed only after actual evidence exists for the same signatures and intended production service. Record a timezone-aware `cost_reviewed_at`; the live full-run guard enforces the configured maximum review age. Provider calls require ISO-dated, non-placeholder pricing. The final no-model preflight supplies the exact render/preflight signatures and request ceiling for contemporaneous cost authorization; an older namespace or assumed result is not evidence.

`--all` must refuse a missing, stale, false, or signature-mismatched production gate and must require Flex. The owner launches the guarded production command. An earlier approval or a successful older contract is not permission for a new production run.

## 15. Review records without destroying evidence

The extracted-record reviewer should show the full scan beside the validated record and support:

- next/previous page and record;
- zoom and pan;
- row add, remove, duplicate, and reorder;
- schema-aware field editing and validation;
- raw JSON fallback for genuinely nested exceptional contracts;
- unresolved/error/flagged filters;
- save, save-and-next, and unchanged-but-reviewed status;
- keyboard shortcuts disabled while typing;
- progress and unsaved-change warnings; and
- atomic saves.

Human review files stay separate from model caches and carry stable record/page IDs, expected source and contract hashes, the reviewed values, reviewer/date where tracked, and a note for consequential changes. Saving an unchanged page is meaningful evidence that the page was checked.

If later QC raises a new concern, requeue the stable record without discarding earlier manual values or review history.

## 16. Handoff to standardization and reconciliation

Extraction exports source observations, not a silently cleaned analytical dataset. The flat export must let standardization assert its exact input contract and preserve:

- raw and normalized model fields;
- source/page/record IDs and order;
- PDF and rendered-image hashes;
- contract signature and run identity;
- extraction and review status;
- document/table regime;
- all uncertainty, remarks, and supplemental facts; and
- error and exclusion status rather than missing rows.

Standardization may apply deterministic parsing, Banknorm, and stale-checked manual overlays. It must retain all source observations and export unmatched or ambiguous identities for review.

Reconciliation may choose among repeated publication vintages under a documented deterministic rule. It must preserve every candidate and never average disagreements. A cross-section or source without repeated vintages still passes through a key/contract validation stage.

## 17. Use bounded alternate extraction only after evidence review

When QC isolates a small set of pages that ordinary extraction may have mishandled, preserve the original cache and plan a separate alternate namespace. Suitable alternatives include:

- a high-reasoning Standard full-page review for an explicit small set;
- a higher-resolution rerender when the original render is demonstrably inadequate; or
- overlapping vertical bands with the printed header repeated when a dense table exceeded output limits or stopped early.

The plan is dry-run by default and enforces page and request ceilings. Crop bounds must be visually calibrated per page. Overlaps must agree on repeated rows; conflicts or omissions block merging. Review the complete reconstructed page or table block against the source.

An alternate answer is advisory. It never replaces an original model cache or enters the data automatically. A human-approved evidence ledger and stale-checked correction overlay are required, followed by a baseline-versus-repaired comparison under [the data quality guide](DATA_QUALITY_GUIDE.md).

## 18. Common failure patterns

Avoid these recurring mistakes:

- designing the schema from one clean crop;
- placing known provenance in the model response instead of runner metadata;
- deeply nesting a table that naturally emits flat observations;
- conflating blank and zero or raw and normalized values;
- using a basename as page identity;
- treating a model success as proof that a dense page was complete;
- selecting pages from numerical density alone;
- allowing unresolved pages into extraction;
- caching by model name while ignoring prompt/schema/render bytes;
- buffering a full run in memory before writing results;
- letting concurrent completion reorder outputs;
- making bounded runs update canonical files;
- silently retrying errors;
- editing a cache through a review app;
- rerunning a corpus to repair a handful of pages;
- changing a shared Yachay, Locro, or Banknorm installation from the project; or
- interpreting a more plausible or smoother series as proof of a better transcription.

## 19. Extraction handoff checklist

Before handing extraction to standardization or another researcher, report:

- the selected-page manifest path, hash, row count, and completeness status;
- the prompt/schema/model/reasoning/service/render contract signature;
- the Yachay revision and exact external cache/run locations;
- what bounded and production commands actually ran, and what did not;
- gold risk coverage and every remaining discrepancy;
- cached success, explicit failure, reviewed exclusion, and pending counts;
- whether cache reuse and cache-only reconstruction were demonstrated;
- nested and flat export paths and their keys/order;
- manual review paths and stale-check behavior;
- estimated versus actual request/token usage; and
- the next guarded command without launching it unless the owner has just authorized that exact operation.

Extraction is complete only when every selected page has a usable, traceable result or an explicit reviewed disposition. “The script finished” is not a quality gate.
