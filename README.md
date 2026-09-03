# Rand McNally Bankers' Directory OCR Revision

This is the single authoritative runbook for recovering the legacy evidence, ranking pages for review, and running the bounded 100-page OCR trial. Recovery, evidence preparation, and calibration are complete. The paid trial is **paused after a failed 10-page provider ramp**; its remaining 90 pages have not been sent.

## Non-negotiable boundaries

- `/home/sergio/Dropbox/Projects/Historical-Documents/Rand McNally` is immutable legacy reference material.
- `/home/sergio/data/rand-mcnally-v1` is the immutable recovered V1 tree. Its resolved path may appear as `/mnt/data/rand-mcnally-v1` in receipts.
- Project code and compact artifacts belong only in `/home/sergio/Dropbox/Projects/Historical-Documents/Rand-McNally-v2`.
- Bulk V2 PDFs, renders, caches, reservations, and extraction exports belong only in `/home/sergio/data/rand-mcnally-v2`.
- Every write-capable project command resolves symlinks and accepts destinations only inside V2 or its external root; neither mutable root may overlap an immutable tree.
- `.git` is a file pointing to `/home/sergio/git/rand-mcnally-v2/.git`; never create a Dropbox-hosted `.git` directory.
- Yachay, Locro, and Banknorm are shared read-only dependencies. Do not patch or reinstall them here.
- Never copy or reference a service-account JSON. Use user Application Default Credentials (ADC) only.
- Statistical flags are review leads. They never authorize a correction, rerun, retry, or promotion by themselves.

## Verified state

The finalized recovery receipt is `output/2-inventory/v1-recovery-final.json`; it records the recovery tree before the two owner-authorized source copies described below. On 2026-09-02, `1887-1-hathi.pdf` and `1903-1-hathi.pdf` were copied from the immutable legacy `sources/` directory into the recovered V1 `downloads/` directory after their SHA-256 hashes and page counts matched the receipt. A targeted manifest check now finds every configured edition present; the full recovery audit was not rerun.

| Stage | Verified result |
| --- | --- |
| Legacy metadata | 129 rows: 84 configured editions, 44 blank placeholders, and one known-invalid `1803-1` row |
| Source recovery | All 84 configured editions mapped across 112,097 physical PDF pages: 81 recovered V1 PDF/multipart sources, 3 reviewed raw-scan crosswalks, and 0 legacy fallbacks |
| V1 source copies | 84 available and 0 missing after the two owner-authorized, byte-identical legacy-source copies |
| Recovered cache | 106,099 readable JSON files, 15 explicit error markers, and 0 invalid JSON files; 105,742 artifacts mapped and 372 retained as unmapped forensic evidence |
| Recovered images | 48,431 files; useful when hash-matched, but not required for a page that can be rendered from its exact PDF |
| Page universe | 106,948 unique pages: 85,994 eligible and 20,954 ineligible, including 20,632 legacy advertisement pages |
| Review evidence | 39,793 page-localized or supporting signals across 24,787 ranked candidate pages; 30 source-level exclusions; 2,645 unlocalized gap rows intentionally not emitted |
| Five-percent guard | Hard ceiling 5,347 pages; 2 completed smoke pages plus 10 reserved provider-error pages are counted; 5,335 remain now and 5,245 will remain after the untouched 90-page cohort |

The 150-page calibration sample is fixed at 50 documented pages, 50 top candidates, and 50 matched controls. `manual/page_review_calibration.tsv` contains 150 evidence-bound decisions: 92 `confirmed_problem`, 58 `not_problem`, and no `uncertain` rows.

| Stratum | Confirmed | Not problem | Observed rate |
| --- | ---: | ---: | ---: |
| Documented | 35 | 15 | 70% |
| Candidate | 50 | 0 | 100% |
| Control | 7 | 43 | 14% |

The candidate gate passes: its 95% Wilson lower bound is `0.9286499658`, above the configured `0.50` minimum. The signed ranking originally selected 100 new pages; 10 are now attempted provider errors and 90 remain untouched. Calibration sample SHA-256 is `f835d4c89fc72a4fe74502d0dd3451ea8b96cb1c92c6a47bf0545247d3afb529`; calibration-label SHA-256 is `0cf24db2c83151b7b35c8a972946d01ca92013a9a5ee1ccf21dbc91e7dae0469`; selected-queue SHA-256 is `c9744c9baf58e9fb2db1ce05c77761b56ededa9e51b7388b1ea8b5a63728484a`.

The earlier two-page smoke test—1881-1 physical pages 84 and 143—completed and replayed from cache. It is documented in `manual/smoke_test_review.md` and is separate from the 100-page trial.

### One-page Gemini 3.8 Flash benchmark

On 2026-09-02, one fresh Standard PayGo request each for `gemini-3.7-flash` and `gemini-3.8-flash` ran concurrently against the existing clean Kansas render `1881-1-hathi.pdf#page=143`, with medium thinking, 64,000 maximum output tokens, ultra-high media resolution, no temperature, no cache reuse, and no retries. Exactly two provider calls were started and both succeeded. The raw responses and request receipt are immutable under `/home/sergio/data/rand-mcnally-v2/model-benchmarks/20260902T184326.468332Z/`; the comparison is `output/model-benchmarks/20260902T184326.468332Z/report.md`; and the reference plus grouped adjudication are under `manual/model-benchmarks/gemini-3.8-vs-3.7-page-143/`.

Both models found all 40 banks and correctly associated all 77 correspondents. Reasonable state inference from an unambiguous city is now treated as a policy-neutral normalization choice rather than an extraction error. On that basis, the adjudicated critical/substantive/minor counts are `0/0/13` for 3.8 and `0/1/10` for 3.7; the sole substantive 3.7 error is an omitted printed `Actg.` officer qualifier. Performance was mixed on literal transcription: 3.7 exactly matched 530 of 544 populated fields and 36 of 40 raw correspondent strings, versus 528 and 31 for 3.8. The 76 inferred state values remain reported as unexpected non-null fields under the literal prompt contract, but they do not enter the quality ordering. Both extracted all 26 populated amount fields exactly.

Gemini 3.8 used 10,948 visible output tokens and 4,568 thought tokens, versus 11,105 and 3,735 for 3.7. Its visible output was 1.41% smaller, hidden reasoning was 22.30% larger, billable generated tokens were 4.56% larger, and total tokens were 3.40% larger. At identical Standard rates, the calls cost $0.0619665 and $0.0594315 respectively, so 3.8 cost 4.27% more on this page. The extra consumption was concentrated entirely in hidden thoughts and is consistent with a larger effective reasoning budget, but does not prove an internal budget change. Under the corrected policy-neutral treatment of inferred states, this page shows a mixed trade-off—one fewer substantive error for a 4.27% higher cost, but more minor literal-transcription differences. It is not corpus evidence; the project default remains `gemini-3.7-flash` and Flex.

The benchmark used Yachay commit `d3a407ad725623860cbd79ad406d05eecea8f415`, whose separate repository commit is `add support for gemini 3.8 flash`. The benchmark did not modify the signed trial queue, canonical extraction caches, paid-page ledger, production gate, manual rerun ledger, or any current-extraction pointer.

### Degraded-scan follow-up benchmark

At the owner's request, a second two-call benchmark used the first uniform random draw from physical pages 100–2900 of the degraded 1922 Hathi scan: `1922-2-hathi.pdf#page=1654` (printed page 1357). The source page is rotated, faded blue, and watermarked. It was rendered once at 400 dpi, rotated 90 degrees clockwise without cropping or enhancement, and encoded as a lossless WebP. One fresh Standard request per model ran concurrently with the same medium-thinking, 64,000-output-token, ultra-high-media-resolution, no-temperature, no-cache, no-retry settings. Exactly two provider calls were started and both succeeded.

On this harder page, 3.8 was both more accurate and cheaper. The critical/substantive/minor counts were `1/30/4` for 3.8 and `3/50/5` for 3.7. Both found all 22 bank rows and semantically recovered 60 of 62 correspondents. Both omitted the two Franklin Clearing House correspondents and misread transit `60-768` as `60-763`; the main 3.8 gains were preserving all 19 printed two-digit establishment years and extracting all 112 populated amount fields exactly, versus 110 for 3.7. Both models struggled with the faint membership-symbol column. Policy-neutral changes—such as 3.7's semantically correct expansion of 60 abbreviated correspondent-bank names—are excluded from the error ordering but remain visible in the exact-transcription metrics.

Gemini 3.8 used 9,788 visible output tokens and 6,581 thought tokens, versus 10,388 and 7,401 for 3.7. That is 600 fewer visible tokens, 820 fewer thought tokens, 1,420 fewer billable generated tokens, and 1,420 fewer total tokens. At identical Standard rates, 3.8 cost `$0.0659145` versus `$0.0712395`, a 7.47% saving. This call does not support the hypothesis that 3.8's consumption increase primarily reflects more hidden thinking: both its visible and hidden counts were lower. A single page cannot establish an internal budget change or a corpus-wide cost ratio.

The immutable raw responses, receipt, exact input image, prompt, schema, and sealed pre-call reference are under `/home/sergio/data/rand-mcnally-v2/model-benchmarks/20260902T192156.120141Z/`. The full comparison is `output/model-benchmarks/20260902T192156.120141Z/report.md`; the adjudicated comparison JSON and verification script are beside it. The pre-call and corrected references plus manual adjudication are under `manual/model-benchmarks/gemini-3.8-vs-3.7-1922-2-page-1654/`. The pre-call reference remains immutable; the corrected reference records four full-resolution adjudication corrections and binds them to the pre-call and response hashes. Rand McNally's configured production model remains `gemini-3.7-flash` with Flex. This follow-up likewise did not modify the signed trial queue, canonical extraction caches, paid-page ledger, production gate, manual rerun ledger, or current-extraction pointer.

#### Gemini 3 Flash Preview augmentation

A separately receipted augmentation added one fresh `gemini-3-flash-preview` Standard request with `think_level="low"`, reusing the exact 400 dpi image, prompt, schema, and manual reference. The existing 3.8-medium response was reused without another provider request. This is therefore a model-plus-effort comparison, not an effort-controlled model comparison. The recovered V1 cache for this page actually used `minimal` thinking, Flex, temperature 0.2, a 32,000-token cap, and a different render, so it is retained only as provenance and was not scored.

Gemini 3.8 remained the page-specific quality winner: its critical/substantive/minor counts were `1/30/4`, versus `2/37/4` for Preview. Preview's extra critical error was a wrong amount (`698,350` transcribed as `608,350`); it also expanded all 19 two-digit years, missed 11 Federal Reserve symbols, omitted two cashier-column names, and semantically recovered 58 of 62 correspondents versus 60 for 3.8. Preview's 59 correct inferred correspondent states and its source-visible supplemental location codes are policy-neutral and do not enter the quality ordering.

Preview reported 10,754 visible output tokens and zero thought tokens, for 10,754 billable generated tokens and 16,795 total tokens. Gemini 3.8 used 9,788 visible plus 6,581 thought tokens, for 16,369 billable generated and 22,410 total tokens. At their model-specific 2026-09-02 Standard rates—Preview `$0.50/M` input and `$3.00/M` generated; 3.8 `$0.75/M` and `$3.75/M`—the calls cost `$0.0352825` and `$0.0659145`. Thus 3.8 cost 86.82% more while avoiding one critical and seven substantive errors, or `$0.003829` of added cost per critical-or-substantive error avoided on this page. The raw Preview result and receipt are under `/home/sergio/data/rand-mcnally-v2/model-benchmarks/20260902T194625.093226Z/`; the report is `output/model-benchmarks/20260902T192156.120141Z/report-preview-vs-3.8.md` and the machine-readable comparison is beside it.

## Runtime and authentication

| Setting | Value |
| --- | --- |
| Python | 3.12 or newer |
| Interface | Vertex AI through Yachay |
| Project / ADC quota project | `rand-mcnally-489320` |
| Location | `global` |
| Model | `gemini-3.7-flash` |
| Service | Flex |
| Thinking | medium |
| Media resolution | `ultra_high` |
| Page image | Per-page WebP render through Yachay |
| Maximum output | 64,000 tokens |
| Temperature | unset (`None`) |
| Retries | disabled |

The runner passes only the project ID and location to Yachay. On a new workstation, configure user ADC with:

```bash
unset GOOGLE_APPLICATION_CREDENTIALS
gcloud auth application-default login
gcloud auth application-default set-quota-project rand-mcnally-489320
```

Every Yachay entry point uses the same metadata-only preflight: it requires an `authorized_user` ADC file with that quota project and rejects `GOOGLE_APPLICATION_CREDENTIALS`. Dry runs and complete zero-request replays never construct a provider client.

## Rebuild the evidence and signed queue offline

Run all commands from the V2 project root. None of this section contacts Gemini.

First run the bounded offline tests:

```bash
uv run --project . pytest \
  code/2-inventory/test_recovery_audit.py \
  code/2-inventory/test_prepare_rerun_evidence.py \
  code/2-inventory/test_rerun_priority.py \
  code/2-inventory/test_stage_queue_sources.py \
  tests/test_rand_mcnally_runner.py
```

Only repeat the recovery audit if the immutable recovery tree changes. Take the first snapshot, wait at least 60 seconds with the copy idle, then finalize against it:

```bash
uv run --project . code/2-inventory/audit_v1_recovery.py \
  --output output/2-inventory/v1-recovery-compatibility.json \
  --page-mapping-output output/2-inventory/v1-recovery-page-mapping.tsv

uv run --project . code/2-inventory/audit_v1_recovery.py \
  --output output/2-inventory/v1-recovery-final.json \
  --previous-snapshot output/2-inventory/v1-recovery-compatibility.json \
  --minimum-quiet-seconds 60 \
  --page-mapping-output output/2-inventory/v1-recovery-page-mapping.tsv \
  --finalize
```

Rebuild the compact legacy summaries and prepared evidence only when their immutable inputs or reviewed rules change:

```bash
/usr/local/stata19/stata-mp -b do code/2-inventory/export_legacy_review_inputs.do

uv run --project . code/2-inventory/prepare_rerun_evidence.py
uv run --project . code/2-inventory/prepare_rerun_evidence.py --write
```

Preview the ranking first. The frozen receipt was created with 150 labels, a passing gate, 100 selected pages, 2 prior manual-ledger pages, and a 5,245-page post-selection remainder. The live runner now also counts the external 10-page reservation.

```bash
uv run --project . code/2-inventory/rerun_priority.py \
  --pages data/rerun_priority_pages.tsv \
  --signals data/rerun_priority_signals.tsv \
  --calibration-labels manual/page_review_calibration.tsv

uv run --project . code/2-inventory/rerun_priority.py \
  --pages data/rerun_priority_pages.tsv \
  --signals data/rerun_priority_signals.tsv \
  --calibration-labels manual/page_review_calibration.tsv \
  --write
```

The publish step atomically writes `output/rerun-ranking/signal_details.tsv`, `page_priority.tsv`, `calibration_sample.tsv`, `selected_pages.tsv`, and `ranking_receipt.json`. Never edit these generated files by hand. Any changed input invalidates the signed queue.

## Stage the exact queue

Preview source staging, inspect the receipt, then copy only the hash-bound PDFs named by the signed queue:

```bash
uv run --project . code/2-inventory/stage_queue_sources.py \
  --queue output/rerun-ranking/selected_pages.tsv

uv run --project . code/2-inventory/stage_queue_sources.py \
  --queue output/rerun-ranking/selected_pages.tsv \
  --copy
```

Staged PDFs go beneath `/home/sergio/data/rand-mcnally-v2/pdfs/`. Each invocation writes an immutable `output/stage-queue-sources-<signature>.json` receipt. Existing matching PDFs are reused; conflicting files are never overwritten.

## Bounded 100-page trial — paused after provider failure

The signed queue dry-ran successfully with 100 pending requests and zero provider calls. The first ten live calls ran on 2026-09-02 as `20260902T143416.409782Z`; all ten returned the same `503 UNAVAILABLE` after one attempt, so the 90-page cohort was not released. Google's [Vertex AI error guide](https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/api-errors) defines 503 as a temporarily unavailable service. The failure export is `/home/sergio/data/rand-mcnally-v2/data-extraction/exports/targeted/20260902T143416.409782Z`.

The ten failures replayed as immutable error-cache hits in `20260902T144602.979668Z`, with zero provider calls and byte-identical payload files. This trial does not retry or replace them; any future reattempt requires a new explicit owner decision. The external reservation counts them against the ceiling. Keep `manual/rerun_pages.tsv` unchanged until this signed queue finishes; append all 100 final statuses together afterward, because changing that bound input invalidates the queue receipt.

### 1. Ten-page ramp

The original ramp dry run created deterministic renders and made zero provider calls. This command is retained as historical provenance; running it now reports ten cached errors and no pending requests:

```bash
uv run --project . code/1-extract-data.py \
  --queue output/rerun-ranking/selected_pages.tsv \
  --limit 10 \
  --workers 10 \
  --max-requests 10 \
  --dry-run
```

The corresponding live command was:

```bash
uv run --project . code/1-extract-data.py \
  --queue output/rerun-ranking/selected_pages.tsv \
  --limit 10 \
  --workers 10 \
  --max-requests 10
```

It stopped as designed. Do not run this command again for this trial: all ten pages now have immutable error caches and one reservation record.

### 2. Remaining ninety pages

The complete signed queue can still be previewed safely. This was rechecked after the failure: it reports 10 cached errors, 90 pending requests, 12 prior counted pages, 5,245 pages remaining after the cohort, and zero provider calls:

```bash
uv run --project . code/1-extract-data.py \
  --queue output/rerun-ranking/selected_pages.tsv \
  --limit 100 \
  --workers 50 \
  --max-requests 90 \
  --dry-run
```

There is deliberately no live continuation command in this runbook. The runner refuses to submit the 90 pages unless all first-ten ramp pages are successful result-cache hits. The present ramp has ten error-cache hits, so continuing, retrying, or choosing a replacement ramp requires a new explicit owner decision and a reviewed update to the trial evidence. Do not delete or edit the error caches or reservation to bypass that gate.

### 3. Zero-request replay

After the remaining 90 pages finish, reconstruct the full queue from its immutable caches:

```bash
uv run --project . code/1-extract-data.py \
  --queue output/rerun-ranking/selected_pages.tsv \
  --limit 100 \
  --workers 50 \
  --max-requests 0
```

If every remaining page succeeds, the replay must report 90 result-cache hits, 10 error-cache hits, and zero provider calls. Further explicit provider errors reduce the result-hit count by the same amount. The replay exits nonzero while any error remains. Its `banks.tsv`, `correspondents.tsv`, `tokens.tsv`, `errors.tsv`, `pages.jsonl`, and `contract.json` must match the completed full-queue run byte for byte; only `run.json` may differ in run metadata and cache accounting.

## Trial outputs and promotion rule

The runner writes only under `/home/sergio/data/rand-mcnally-v2`:

- renders: `rendered-pages/targeted/<render-signature>/<source-hash>/<document>/page-<physical-page>.webp`;
- page caches: `data-extraction/cache/targeted/<contract-signature>/<source-hash>/<document>/page-<six-digit-physical-page>/<render-hash>.json`;
- provider errors: immutable `.error-<run-id>.json` siblings;
- paid-page reservations: `data-extraction/paid-page-ledger/reservation-*.json`;
- immutable exports: `data-extraction/exports/targeted/<run-id>/`.

Each export contains `banks.tsv`, `correspondents.tsv`, `tokens.tsv`, `errors.tsv`, `pages.jsonl`, `contract.json`, and `run.json`.

Reservations are deliberately fail-closed. If a live process is interrupted after reserving a cohort, do not rerun it automatically: some submitted pages may lack a cache. Preserve the reservation and caches, determine which requests reached Vertex, and require an explicit owner decision before any recovery attempt.

**Nothing is promoted automatically.** A successful trial or replay does not modify the legacy datasets, `data/`, a canonical-current pointer, calibration decisions, or the signed ranking queue. Rerun output remains advisory until a human checks the full scans and approves a separate evidence-bound correction or replacement. Failed or paid pages continue to count against the five-percent ceiling.

## Known source limitations

- `1915-1-google.pdf#page=653` is documented as cropped and incomplete.
- `1901-1-hathi.pdf#page=314`, `1901-1-hathi.pdf#page=674`, and `1898-2-google.pdf#page=47` are severely clipped; a same-PDF rerun may confirm the problem without recovering the missing content.
- `1911-1-google.pdf#page=238` preserves a directory row only in a clipped edge strip.
- `1911-1-google.pdf#page=852` visibly names both Grand Forks and Fargo; a single-town field cannot represent that evidence without an explicit reviewed policy.

Route clipped pages to alternate-source, render, or adjacent-page review rather than interpreting sparse OCR as a true empty page. The recovered V1 cache is diagnostic evidence only and is never reused as a compatible V2 model cache.
