# Rand McNally Bankers' Directory OCR Revision

This project restores the legacy Rand McNally extraction pipeline, proves it on one bounded two-page cohort, and then stops for review. The later goal is to rank suspect legacy pages and re-extract no more than five percent of the corpus. That selection policy is not implemented or authorized yet.

This README is the authoritative runbook for the current restoration phase.

The bounded checkpoint passed on 2026-09-02: exactly two paid page requests completed, the cache-only replay made zero provider calls, and the live/replay extraction artifacts were byte-identical. The visual and legacy-output comparison is recorded in `manual/smoke_test_review.md`.

The scaffold was imported from tracked `data-extraction-template` commit `d37e014a5acab52d74f405be55dc3baf4c63c241` before the project-specific restoration files were added.

## Hard boundaries

- The legacy project at `/home/sergio/Dropbox/Projects/Historical-Documents/Rand McNally` is immutable reference material. Do not write, rename, delete, format, or run output-producing commands there.
- All project changes belong in `/home/sergio/Dropbox/Projects/Historical-Documents/Rand-McNally-v2`.
- Its `.git` is a pointer file to `/home/sergio/git/rand-mcnally-v2/.git`, not a Dropbox-hosted Git directory.
- Large V2 inputs, renders, caches, and run exports belong under `/home/sergio/data/rand-mcnally-v2`.
- Legacy comparison files copied for this phase live at `/home/sergio/data/rand-mcnally-v2/legacy-reference`; comparisons never write back to the legacy project.
- Yachay, Locro, and Banknorm are shared, non-editable dependencies. Do not patch or reinstall them from this project.
- Never copy or commit a Google credential file. The runner uses user Application Default Credentials (ADC).
- No full-corpus command is documented or authorized in this phase.

## Inventory findings

The read-only migration inventory is `sources/legacy_migration_inventory.tsv`.

| Finding | Current evidence |
| --- | --- |
| Legacy range rows | 129 total: 84 configured, 44 blank placeholders, and one invalid stray `1803-1` row |
| Selected legacy sources | 30 available and 55 missing; missing sources remain acquisition work |
| Legacy output coverage | 80 editions with 106,948 page-level token records |
| Provisional five-percent guard | denominator 106,948; ceiling 5,347 pages (`floor(106948 × 0.05)`); two completed, provisional remainder 5,345 |
| Staged source | `1881-1-hathi.pdf`, 443 physical pages, SHA-256 `3d4a8c7aeae90b8116381811329ea6d3bc29d92c6ed13a273d63db70a5b0c7cd` |
| Legacy configured range | physical PDF pages 84–282, 199 pages, schema regime `1879` |
| Smoke pages | page 84 (clear) and page 143 (dense/faint; exercises the inspected JPX raster fallback) |

The staged PDF is `/home/sergio/data/rand-mcnally-v2/pdfs/1881-1-hathi.pdf`. Its manifest row is `sources/source_manifest.tsv`, and the explicit two-page authorization is `manual/rerun_pages.tsv`.

The external legacy-reference directory contains:

- `1881-1-banks.tsv`: 6,359 rows;
- `1881-1-correspondents.tsv`: 10,093 rows;
- `1881-1-tokens.tsv`: 199 rows.

The 106,948 denominator is provisional: it counts legacy token-ledger page rows, not a newly verified canonical physical-page universe. The 5,347 ceiling must not be raised, and it must be recomputed from the reviewed V2 manifest before any broader rerun is considered.

## Runtime contract

| Setting | Value |
| --- | --- |
| Python | 3.12 or newer |
| Provider | Vertex AI through Yachay |
| Google Cloud project and ADC quota project | `rand-mcnally-489320` |
| Location | `global` |
| Model | `gemini-3.7-flash` |
| Service | Flex |
| Thinking | medium |
| Media resolution | `ultra_high` |
| Maximum output | 64,000 tokens |
| Temperature | unset (`None`) |

These values come from `project.toml`. A material change requires a new cache namespace and a new bounded validation.

Python dependencies are declared in `pyproject.toml`. The shared sources under `/home/sergio/git/` are installed non-editably.

## Authentication

Use the same user-ADC convention as the recent Bank Debits and ST6386 projects:

```bash
unset GOOGLE_APPLICATION_CREDENTIALS
gcloud auth application-default login
gcloud auth application-default set-quota-project rand-mcnally-489320
```

The runner passes `project_id="rand-mcnally-489320"` and `location="global"` to Yachay and deliberately omits `credentials_file`. Its local preflight requires an `authorized_user` ADC file with the matching quota project. It checks metadata only: it never prints tokens or credential contents and does not refresh credentials. Dry runs and cache-complete replays do not make a provider call.

## Bounded restoration check

Run commands from the V2 project root.

First run the offline runner tests:

```bash
uv run --project . pytest tests/test_rand_mcnally_runner.py
```

### 1. Dry run

Render and inspect the exact two-page request without calling Gemini or writing a cache/export run:

```bash
uv run code/1-extract-data.py \
  --year 1881 \
  --edition 1 \
  --page 84 \
  --page 143 \
  --max-requests 2 \
  --dry-run
```

Review the printed source hash, ordered page list, render hashes, contract signature, settings, cache state, and pending request count. Stop if the scope is not exactly these two pages or if any model setting differs from the runtime contract above.

### 2. Live smoke run

Only after the dry-run output is reviewed, run the same bounded cohort through Flex. This command can make at most two requests:

```bash
uv run code/1-extract-data.py \
  --year 1881 \
  --edition 1 \
  --page 84 \
  --page 143 \
  --max-requests 2
```

Do not add retry, range, batch, nonbank, or broader-selection behavior. A failed page remains an explicit failed result for review.

### 3. Cache replay

Repeat the cohort with a zero-request ceiling. Success proves both pages can be reconstructed from their immutable caches without ADC or provider access:

```bash
uv run code/1-extract-data.py \
  --year 1881 \
  --edition 1 \
  --page 84 \
  --page 143 \
  --max-requests 0
```

## Outputs

The runner writes only beneath `/home/sergio/data/rand-mcnally-v2`:

- renders: `rendered-pages/targeted/<render-signature>/<source-hash>/<document>/page-<page>.<ext>`;
- immutable page caches: `data-extraction/cache/targeted/<contract-signature>/<source-hash>/<document>/page-<six-digit-page>/<render-hash>.json`;
- provider-error records: the same page directory with `.error-<run-id>.json` suffixes;
- immutable runs: `data-extraction/exports/targeted/<run-id>/` containing `banks.tsv`, `correspondents.tsv`, `tokens.tsv`, `errors.tsv`, `pages.jsonl`, `contract.json`, and `run.json`.

The dry run may create deterministic render files, but it creates no model cache or export run. Each live or cache-only invocation creates a new immutable export directory; neither publishes a canonical dataset.

## Acceptance checkpoint

Commit this restoration checkpoint only when all of the following are true:

- the offline runner tests pass;
- the dry run selects exactly pages 84 and 143 from the staged 1881-1 source and reports zero provider calls;
- the live run uses the exact runtime contract above and makes no more than two uncached requests;
- both pages complete successfully with hashes and token accounting; any page error blocks this checkpoint;
- the zero-request replay reports zero provider requests and two cache hits;
- `banks.tsv`, `correspondents.tsv`, `tokens.tsv`, `errors.tsv`, `pages.jsonl`, and `contract.json` are byte-identical between the live and replay runs; `run.json` differs only because it records a new run ID, timestamps, and live-versus-cache accounting;
- new renders, caches, and exports exist only under the V2 external root;
- the legacy project and shared dependency repositories remain unchanged; and
- the new output has been inspected against the two full page images and the copied legacy reference files.

## Known gaps and next stage

- Only the 1881-1 source is staged; 55 configured legacy source choices are currently missing from the legacy working tree, and many later FRASER multipart sources may need reacquisition.
- An owner-managed recovery copy is still in progress under `/home/sergio/data/rand-mcnally-v1`, with expected `downloads`, `cache`, and `extracted_images` subdirectories. Do not audit or copy from it until the transfer is complete. It may omit at most two scans processed immediately before the Linux migration; this has not yet been verified.
- Provider IDs, URLs, and an exact publication date for the staged HathiTrust copy are not recoverable from the local files and remain blank rather than inferred.
- The copied legacy prompt/schema are being restored for compatibility; they are not yet the revised multi-regime extraction contract or a gold-standard validation.
- The 106,948-page denominator and 5,347-page cap are provisional until the complete physical-page manifest and duplicate policy are reviewed.
- Candidate scoring for the eventual targeted rerun has not been designed. Statistical anomalies will be review leads, never automatic corrections.

After this checkpoint is committed and the V1 transfer finishes, the next phase is to inventory the recovered PDFs, old `gemini-3-flash-preview` cache, and extracted images; reconcile them with the migration inventory; and then design the evidence-based page-ranking and review process. No broader extraction begins in this restoration phase.
