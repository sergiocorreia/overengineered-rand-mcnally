# Bounded OCR smoke-test review

Status: accepted for the migration checkpoint on 2026-09-02. This is compatibility evidence, not gold data and not authorization for more OCR.

## Evidence

- Live run: `/home/sergio/data/rand-mcnally-v2/data-extraction/exports/targeted/20260902T055210.158908Z`
- Cache-only replay: `/home/sergio/data/rand-mcnally-v2/data-extraction/exports/targeted/20260902T055700.463810Z`
- Live accounting: two fresh pages and two provider calls.
- Replay accounting: two cache hits, zero pending requests, and zero provider calls.
- The two runs have identical `banks.tsv`, `correspondents.tsv`, `tokens.tsv`, `errors.tsv`, `pages.jsonl`, and `contract.json` hashes.
- Both page payloads validate against `schema_1879.Page`; neither hit the 64,000-token output limit; `errors.tsv` has no error rows.

## Full-page inspection

Page 84 is a clear Alabama table. All visible rows from Abbeville through Oxford are represented: 36 bank rows and 59 correspondent rows.

Page 143 is a fainter Kansas table. The JPX consistency check detected a bad direct extraction and used the approved raster fallback. All visible rows from Scandia through Wyandotte are represented: 40 bank rows and 77 correspondent rows.

No table section is visibly omitted or invented on either page.

## Comparison with the legacy output

The copied legacy TSVs were used only as a comparison aid. Both versions have identical bank and correspondent counts on both pages, with no missing or extra row indexes.

- Page 84: V2 reads the Merchants' & Planters' N. Bk. surplus as `13,316`, matching the scan; legacy has `18,316`. V2 also preserves the printed spelling `Frederick Wolffe`, where legacy has `Frederick Wolfe`.
- Page 84: three institutions named “Savings Bank” changed from legacy `bank_type=savings` to V2 `commercial`. They appear in the ordinary state table rather than a savings-bank sub-table, so the V2 value follows the current schema definition.
- Both pages: several “See Card” strings moved from `president` to `misc_notes`; this is a semantic placement change, not lost text.
- Page 143: V2 omitted the visible qualifier `Actg.` from `Amos E. Wilson, Actg.` for Silas B. Warren & Co. This is a real field-level regression to carry into the accuracy-improvement phase.
- Page 143: V2 dropped a final period from `& Co.` in four correspondent names. No correspondent was omitted.
- Page 143: H. R. Prather & Co. remains in the ordinary table; V2 leaves the default `bank_type=commercial` while retaining `(Brokers)` in the printed bank name. Legacy classified it as `broker`.

The noted qualifier and punctuation losses do not block the restoration smoke test, but they must not be treated as approved corrections or gold truth.
