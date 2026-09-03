# Instructions for Codex and Other Coding Agents

This repository is a template for auditable historical-document extraction. In an initialized project, the project `README.md` is the single authoritative human runbook. Keep it accurate whenever commands, paths, data contracts, or project status change. Do not create `RUNBOOK.md` or another competing run-order document.

## Required reading and inspection

Before changing an initialized project:

1. Read `README.md` and `project.toml` completely.
2. Read the relevant stage code and its tests.
3. Read `guides/DATA_EXTRACTION_GUIDE.md`, `guides/DATA_QUALITY_GUIDE.md`, and the applicable language style guide.
4. Read `REFERENCES.md` before adapting code from a prior project; reuse invariants and tests, not source-specific fields, thresholds, IDs, or empirical decisions.
5. Use the `historical-document-extraction` skill for schema, prompt, gold-sample, cache, calibration, production, or audit work. Read its quality gates when reviewing discrepancies or production readiness.
6. Inspect the actual source material before proposing a schema or prompt. View complete pages at full resolution and sample all visible regimes, dates, scan qualities, handwriting/typing, annotations, corrections, blanks, and non-target pages.
7. Read existing manifests, overrides, manual ledgers, gold fixtures, extraction contracts, and prior run metadata. Preserve their identities and history.

Do not ask the owner for facts that are discoverable from the repository or sources. Do ask when a material research choice cannot be resolved from evidence.

## Non-negotiable evidence rules

- Never invent, impute, smooth, winsorize, or infer a source value merely because it looks plausible.
- Use source-faithful field types appropriate to each project. Do not require duplicate raw-value fields unless the project's own contract calls for them.
- Distinguish blank, explicit zero, dash, textual `None`, unreadable, excluded, and not applicable.
- Preserve visible corrections, crossed-out values, uncertainty, remarks, and substantive text outside standard boxes.
- Keep original PDFs, rendered images, OCR, model caches, and run receipts immutable.
- Store human decisions in `manual/`, separately from generated queues and model output. Never edit a cache or source manifest to encode a correction.
- Require stable IDs, expected prior values, and relevant hashes on correction overlays. Reject stale decisions rather than guessing how to carry them forward.
- A statistical anomaly is a review lead, never authorization to change a value.
- Never weaken a correct manually transcribed gold fixture to make a model pass.

## Safe execution

Development commands must be bounded. Use `--calibration`, `--trial`, `--limit`, explicit `--page-id`, or a reviewed queue. Use `--dry-run`, `--status`, `--cache-only`, and `--max-requests` when available.

Never run any of the following without contemporaneous, explicit authorization for that exact run:

- a command with `--all` that downloads, OCRs, extracts, retries, or transmits the full corpus;
- an unbounded provider or model request;
- a Standard-service extraction beyond the configured bounded request ceiling;
- a command whose displayed page count, request count, cost, model, reasoning setting, or service differs materially from what the owner authorized.

An old approval, a README example, and the existence of `--all` do not count as authorization. The owner launches a full `--all` model run; an agent must stop after verifying and reporting the guarded command unless the project policy is changed explicitly. Full extraction uses Flex.

The machine-readable gate is `manual/gold/production_gate.json`. It binds contract/evidence, exact render/preflight signatures, the approved request ceiling, and the booleans `gold_passed`, `trial_passed`, `cache_reuse_passed`, and `cost_reviewed`. `validate_gold.py` supplies the gold/evidence check. Set the trial, cache-reuse, and cost-review fields only after the corresponding actual evidence exists for the same signatures and intended production service. Record the cost review in timezone-aware `cost_reviewed_at`; a live full run rejects a review older than `pricing.review_max_age_hours`. Provider calls also require an ISO-dated, non-placeholder pricing table. The final no-model preflight is evidence only for its displayed corpus, cache state, settings, request ceiling, and cost; copy its exact signatures only after review. `--all` must refuse a missing, stale, false, expired, or signature-mismatched gate.

Do not retry cached errors unless specifically requested. A failed page must remain an explicit failed status; it is not an empty successful page. Bounded extraction must not update the canonical current-extraction pointer.

Ordinary Stata setup, build, and test commands must remain offline and must not install or update user ado-files. `code/common.do` validates `code/requirements.txt` and fails closed. Treat `code/bootstrap_stata.do` as a separate, explicit network/environment operation; review its requirements and obtain appropriate authorization before running it, then report any shared Stata environment change.

## Selection and review

- Build page identities from the stable relative source path and physical page number: `relative/path.pdf#page=N`.
- Default unresolved pages to `unreviewed`. Manual source-level decisions override automatic evidence; page-level decisions override source-level decisions.
- Fail closed when a source/page is unresolved, missing, stale, hash-mismatched, or inconsistent with positive/negative gold fixtures.
- Use embedded PDF text first, then targeted Locro OCR, then full-document Locro only when justified.
- Keep source-specific scoring in `code/2-inventory/selection_rules.py`; do not entangle it with generic inventory or review infrastructure.
- Read [`guides/MANUAL_REVIEW_WEBAPP_GUIDE.md`](guides/MANUAL_REVIEW_WEBAPP_GUIDE.md) before building or materially changing a reviewer. Local review apps bind to `127.0.0.1`. Preserve keyboard shortcuts, progress, unsaved-change warnings, schema validation, atomic saves, and durable review receipts.

## Extraction contracts and caching

Keep the extraction definition in `code/3-extraction/definitions/schema.py` and `prompt.md`. Prefer a flat, observation-oriented response unless the visible document genuinely requires nesting. Provenance already known to the runner stays outside the model response.

The contract signature must include every material input: prompt bytes, JSON schema, schema source, model, reasoning, service, token/media/render settings, pipeline version, Yachay revision, source hash, and rendered-image hash. Worker count and output destination are not contract inputs. A material change creates a new namespace; never migrate incompatible responses into the old namespace.

Write each page cache immediately and export in canonical manifest order. Preserve nested JSONL, flat TSV, statuses, errors, token usage, timestamps, hashes, settings, and library revisions. Cache-only reconstruction must make no provider calls and must apply the current reviewed classifications.

## Storage and repository boundaries

- `data/` contains only flat, coauthor-ready `.dta`/`.tsv` products and compact audit companions. Do not create stage subdirectories there.
- `manual/` contains durable human decisions and is never regenerated.
- `temp/` contains regenerable intermediates.
- `output/` contains figures, tables, QC reports, receipts, and release artifacts.
- `sources/` contains source metadata and only small source PDFs when `project.toml` selects project storage.
- PDFs larger than the project-storage policy, rendered pages, Locro output, high-volume caches, and bulk extraction results belong under `/home/sergio/data/<slug>/`.
- Never put API keys or credentials in committed files.
- The Dropbox worktree uses a `.git` file pointing to `/home/sergio/git/<slug>/.git`; never create a `.git` directory here.

## Shared-library caution

Yachay, Locro, and Banknorm are shared libraries used by unrelated projects. Treat their installed revisions as read-only dependencies unless the owner explicitly authorizes a library change.

If a problem appears to belong in a shared library:

1. reproduce it with the smallest project-local test;
2. rule out a project adapter or configuration error;
3. describe the cross-project risk and obtain approval;
4. change and test the shared library in its own repository, never through an ad hoc local patch; and
5. record the new revision in extraction or standardization provenance.

Do not use editable local dependency sources in an initialized research project. Do not silently upgrade or reinstall a shared library while rebuilding old results.

## Standardization, reconciliation, and QC

- Assert the flat extraction input contract before transforming it.
- Preserve every source observation and its complete provenance.
- Use Banknorm through its installed Python or Stata interface; preserve raw names and export unmatched or ambiguous identities for review.
- Reconcile repeated vintages deterministically. Never average disagreements. Preserve every candidate and export conflicts.
- Separate blocking structural failures from advisory statistical flags.
- Generate stable QC case IDs and keep the generated queue read-only. Human dispositions are `corrected`, `resolved`, `excluded`, `source_verified`, `expected_gap`, or `open`.
- An open blocking case fails release. Any alternate extraction is advisory until a human approves an evidence-bound overlay.
- Compare every repair with a cache-only baseline and reject downstream changes outside the authorized lineage.

## Documentation and completion

Update `README.md` in the same change whenever an entry point, command, expected artifact, principal dataset, known gap, or QC status changes. Keep project-specific empirical results and thresholds out of the reusable guides.

Before handing work back, report:

- what changed and what was verified;
- what commands or provider calls ran, and which did not;
- exact output and external-cache locations;
- unresolved discrepancies or blocking cases;
- whether any manual state or shared dependency changed; and
- the guarded command for the next authorized stage, without running a full operation on the user's behalf.
