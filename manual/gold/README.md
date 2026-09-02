# Gold extraction fixture

Use the record reviewer to transcribe every in-scope value on the risk-based calibration pages directly from the images. Do not copy model output into the gold fixture without independent image review.

- `gold.jsonl` contains one complete reviewed page extraction per line, with `page_id`, `source_sha256`, and `extraction`.
- `expectations.tsv` contains critical field checks that must pass.
- `production_gate.json` records contract/evidence, render/preflight, approved-request, calibration, trial, cache-reuse, and cost-review gates. `validate_gold.py` resets downstream approvals when material evidence changes. Copy final preflight values only after reviewing that exact no-model preview, and record the timezone-aware review time in `cost_reviewed_at`.
