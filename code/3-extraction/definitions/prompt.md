You are transcribing one scanned historical page into the supplied schema.

Before using this prompt, replace every bracketed instruction with the project's visible target observation and exclusions.

## Target

Return one flat record for each [ENTITY × PERIOD × MEASURE] observation visibly printed in [TARGET TABLE OR FORM AREA]. Preserve physical reading order.

Set `document_status` to `target` only when the supplied page contains the requested material. Use `no_relevant_material` for a clearly unrelated page and `uncertain` when damage or ambiguity prevents that decision. Do not emit records for a non-target or uncertain page.

## Include and exclude

- Include: [EXACT ROWS, ENTITIES, COLUMNS, ACCOUNT TYPES, AND PERIODS].
- Exclude: [TOTALS, SUBTOTALS, SUMMARY PANELS, ADMINISTRATIVE MARKS, AND NEIGHBORING MATERIAL].
- Use only the supplied image. Never infer from filenames, nearby pages, totals, geography, plausibility, or outside knowledge.
- Do not calculate, impute, reconcile, deduplicate, or silently repair a printed value.

## Transcription rules

- Preserve exact raw entity, period, and value text before normalization.
- A printed zero is `value_status: observed` and is not a blank. Keep a dash, printed `None`, illegible fragment, not-applicable mark, and untouched blank distinct with `value_status: dash`, `textual_none`, `unreadable`, `not_applicable`, or `blank`. Preserve the exact token in `value_raw` (empty only for a true blank) and keep `value` null for every non-observed state.
- Preserve partial dates at the precision shown. If a complete date is not visible, leave the normalized date null.
- Preserve cents and the printed scale. Do not add precision that is not visible.
- For crossed-out or overwritten entries, preserve the readable original and replacement in `correction_raw`; never place a value under a visibly superseded concept.
- Preserve faint readable text and mark uncertain fields instead of guessing.
- Preserve substantive footnotes, marginal entries, continuation text, and supplemental facts without paraphrase.
- Ignore filing, received, record-entry, routing, declassification, scanner, viewer, and archive marks unless the research scope explicitly includes them.
- Put substantive entered text that cannot be mapped safely in `unmapped_text`.

Return only schema-valid structured output.
