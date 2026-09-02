# Data Quality Guide for Historical Datasets

This guide describes a reusable way to find, investigate, repair, and document inconsistencies in historical panel and cross-sectional datasets assembled from scanned sources. It complements the project’s extraction, Python, and Stata guides. Those guides govern source-to-record contracts and implementation style; this guide governs evidence, diagnosis, adjudication, and release readiness after observations exist.

The central rule is simple: an anomaly is a reason to investigate, not permission to alter a value. A dataset is ready for analysis when suspicious patterns are either corrected from evidence, excluded by a recorded rule, or retained as source-verified facts.

## 1. Define the analytical object and source support

Quality checks must use the same unit, key, date, grouping, and inclusion rules as the intended analysis. A count of raw transcription rows is not analytical coverage when one release repeats earlier periods, one table contains several columns, one entity appears under several spellings, or unresolved records remain. Before plotting anything, write down:

- the unit of observation;
- the canonical time variable, or that time is not part of a cross-sectional key;
- the entity identifier;
- which source vintages are eligible;
- the rule for selecting among repeated values;
- the meaning of missing, zero, blank, dash, unreadable, and excluded;
- whether totals describe raw source rows, eligible candidates, or the strict regression sample.

For a panel, maintain two time spines. The **analytical spine** contains every intended period, including periods with zero observations. The **source-support spine** records when contemporary publications exist and which dates appear only in later comparisons or retrospective tables. Do not call a pre-publication or documented interruption period a missing release. Conversely, a complete analytical spine assembled mostly from later comparison columns can conceal missing contemporary pages.

For a cross-section, maintain an **analytical support frame** and a **source-support frame**. The analytical frame defines the entities or groups that could appear under the study's inclusion rule. The source frame records which books, tables, jurisdictions, categories, or alphabetical ranges were actually available. A group with no rows is different from a group outside the source scope, and both differ from an omitted continuation page.

Keep separate names for raw printed dates and canonical analytical dates. Coauthor-facing coverage counts unique usable analytical keys; it does not count raw source rows, duplicate scans, repeated publication vintages, or repeated table totals.

## 2. Inspect the data at six linked levels

No single level is sufficient. Run checks from the source upward and keep every aggregate signal traceable downward.

### Page level

Check identity, classification, relevance, rendering, scan quality, row count, cache status, and duplicate images. Inspect adjacent pages and distant references marked `continued`, `concluded`, or `brought forward`. A dense page can report success while stopping after one table block; compare returned entities and bounding boxes with the full visible table.

### Issue level

Check that expected pages are present, dates and units agree, and duplicate entity-period rows represent a genuine revision or second table rather than duplicate pixels. Selected pages must use one PDF copy. Search missing material in this order: the next page after the selected cluster, unselected candidates, explicit references, then full-PDF headings and missing-entity names. Add a page only after visually confirming its period and values.

### Observation level

Validate identity, date, units, sign, raw amount, normalized value, frequency, and missing status. Preserve raw strings. Removing commas is deterministic; choosing between damaged digits requires source adjudication.

### Entity-series level

Inspect gaps, entry and exit spells, adjacent-period changes, identity shifts, repeated-vintage agreement, heaping, and suspicious repetition. Entry and exit are not automatically errors; reporting panels change.

For amount triage, prefer a two-sided signal: the entity moves sharply away from its prior level and reverses in the next exact period. Adjust both changes for the median movement across other entities, then compare the residual with the entity's local robust distribution. Common market-wide jumps and sustained level shifts are not isolated OCR errors. A flag on the middle period can originate in either adjacent endpoint, so inspect all three observations.

### Panel level

Verify unique keys, valid identities, canonical dates, exclusions, and complete time support. Calculate retained entities, stable-cohort omissions, exclusions, conflicts, verified extremes, zero-coverage periods, adjacent coverage ratios, and entries/exits. Thresholds identify candidates; they never authorize manufacturing observations.

### Cross-section and group level

Verify the declared key, sampling frame, group identifiers, exclusions, and support across source/page/table regimes. Calculate retained and unresolved entities by geography, category, source, alphabetical range, and page where those dimensions are meaningful. Check whether missing groups align with omitted pages, headings, continuations, or provider gaps.

Use robust within-group distributions only when the group is large and substantively comparable. Median/MAD, IQR, or rank-based signals can identify review candidates; small or structurally different groups should be reported rather than forced through a common outlier rule. Check totals against visible components, mutually exclusive categories against declared universes, ratios against their source numerators/denominators, and duplicated identities across adjoining tables. Do not manufacture a balancing residual when components fail an accounting identity.

### Aggregate level

Plot levels, growth, coverage, exclusions, and contemporary-versus-historical source shares together, with and without dominant entities. Aggregate movements can reflect economic change, composition, date misalignment, duplicates, or missing pages. Comparable external totals are checks, never substitutes for source values.

For a cross-section, plot distributions and support by meaningful group, source, page, and table regime. Compare aggregates with and without dominant observations, but keep any log or inverse-hyperbolic-sine view clearly separate from source verification. Digit preference or heaping can be real reporting behavior; it becomes an extraction concern when it clusters by page, model batch, or scan regime rather than by the historical reporting process.

## 3. Choose shape-specific checks deliberately

Every project should implement common structural checks first:

- missing or repeated analytical keys;
- unknown or unresolved entities;
- missing source/page/record provenance;
- unselected, duplicate, stale, or failed pages reaching analytical observations;
- stale correction overlays and changed expected prior values;
- configured type, sign, range, unit, and accounting constraints;
- extraction status and unresolved review queues;
- suspicious clusters by source page, table block, provider, render, or contract; and
- disagreements among repeated source observations.

Then add checks appropriate to the analytical shape.

For a **panel**, useful advisory signals include:

- explicit time-spine gaps and zero-coverage periods;
- abrupt changes in coverage or source composition;
- entry and exit spells;
- isolated two-sided reversals adjusted for common movement;
- large one-sided changes;
- persistent level shifts;
- suspicious repetition or heaping within an entity series; and
- disagreement among repeated publication vintages.

For a **cross-section**, useful advisory signals include:

- duplicate entities or keys across pages, tables, and spelling variants;
- missing groups or abrupt support changes across alphabetical/geographic/table boundaries;
- robust outliers within sufficiently comparable groups;
- impossible or surprising ratios and shares;
- visible total/component accounting inconsistencies;
- incompatible units or scale shifts across source regimes;
- terminal-digit heaping and suspicious repeated values; and
- clusters of extremes or missing fields on one page, extraction batch, or table block.

Thresholds belong in `project.toml` with names, units, groups, and rationales. A threshold copied from another dataset is not evidence. Structural failures may block automatically; statistical rules generally generate review cases until source evidence establishes a durable project-specific policy.

## 4. Separate detection from adjudication

Detection is mechanical and intentionally broad. Adjudication determines what the source supports.

Examples of detection signals are a market-adjusted isolated reversal, two raw header dates in one issue, a sudden stable-cohort coverage loss, a grouped robust outlier, an accounting mismatch, heavy reliance on later comparison columns, a repeated analytical key, or disagreement among publication vintages. None of these alone establishes a typo.

Cluster value signals by source page and period. Several affected entities on one page are evidence of a possible row displacement, column displacement, or truncated extraction—not several independent economic shocks. When one anomaly exposes a shifted table, adjudicate the complete affected block.

For each signal, assign one of these outcomes:

- **corrected:** evidence establishes a different value, date, identity, or page classification;
- **resolved:** a documented deterministic or source-based rule selects among candidates;
- **excluded:** evidence is insufficient for a defensible analytical value;
- **source verified:** the surprising value is genuinely printed and retained;
- **expected gap:** the absence is historically explained and remains missing;
- **open:** further review is required and the release gate must fail.

Every generated case receives a stable case ID derived from the check type, affected key/page, and evidence identity rather than row order. The queue is generated and read-only. Never encode a reviewed decision only inside code or by editing the queue. Put it in a durable, keyed decision file with the disposition, reason, evidence page, relevant hashes, and reviewer or review date when the project tracks those fields. Preserve a resolved case even if the anomaly disappears after rebuilding.

## 5. Trace every anomaly through the evidence chain

Investigate from the analytical signal back to the primary source:

1. Locate the analytical or aggregate row that triggered the signal.
2. Find the selected normalized observation and reconciliation metadata.
3. Compare every candidate source vintage, not only the selected row.
4. Inspect the raw transcription fields.
5. Inspect the stored model response or OCR output when applicable.
6. Inspect the rendered page image used for extraction.
7. Inspect the source PDF when rendering, cropping, page order, or duplication is in doubt.
8. Compare adjacent pages, adjacent issues, and external tables when the source itself is ambiguous.

The chain must retain stable identifiers and hashes. An analytical row should link to a source observation, that observation to a source page, and the page to its PDF, rendered image, extraction contract, and cache record.

For a panel value anomaly, inspect the previous, flagged, and following analytical observations; every candidate source vintage for those endpoints; neighboring printed rows; and the whole source table block. For a cross-sectional anomaly, inspect comparable group members, duplicated identities, visible totals/components, neighboring rows and columns, and the complete table block. Do not repair only the conspicuous observation while leaving related row associations unchecked.

## 6. Choose the smallest evidence-based remedy

Use the remedy that addresses the actual failure layer:

| Failure | Preferred remedy |
|---|---|
| Wrong page included or omitted | Correct the page or issue manifest. |
| Explicit continuation is absent | Add it only after confirming the intended period and relevant amounts visually. |
| Consistent parse or date rule is wrong | Correct deterministic normalization and add a regression test. |
| One documented source fact was transcribed wrongly | Add an evidence-bound manual overlay; never rewrite the model cache. |
| Render is clipped, rotated, or unreadable although the PDF is sound | Rerender the affected page. |
| Source scan quality is inadequate | Rescan or obtain a better source copy if available. |
| Dense extraction stops early | Segment into overlapping bands with repeated headers; merge only agreeing overlaps. |
| Rows or columns are displaced | Reconstruct and visually check the complete affected block. |
| OCR or model failed on a bounded set | Rerun only justified pages into a compatible or new contract namespace. |
| Apparent source printing error | Preserve both observations and select an exact corroborating reprint if available. |
| Repeated vintages disagree without a defensible winner | Exclude the analytical value and preserve every candidate. |
| Extreme is clearly printed | Retain and mark it source verified. |

Do not rerun an entire corpus when the evidence identifies a bounded set of pages. Preserve immutable source files, model caches, and completed audit cohorts. A changed prompt, schema, model, reasoning level, media setting, rendering rule, or material normalization rule requires a new contract or versioned output rather than silently overwriting evidence.

## 7. Never use winsorization as error correction

Winsorization changes observations because they are large, not because evidence shows they are wrong. It can hide transcription mistakes, erase genuine crises, and make source problems harder to diagnose. If an extreme is wrong, correct it from the source. If it is ambiguous, exclude it under a recorded rule. If it is genuine, retain it and make robustness choices at the analysis stage.

The same principle applies to imputation. A historically explained missing release, entity, category, or component should remain missing in the primary data. Model-based, interpolated, or residual values, if ever needed, belong in separate analysis products with explicit assumptions.

## 8. Compare the repair with a counterfactual

Keep source files and model caches immutable. Store corrections separately and rebuild deterministically. Each repair needs a stable ID, case type, disposition, entity/period when applicable, exact evidence pages, page sets before and after, evidence transcription, review note, timestamp, and source/cache/contract hashes. Reject stale overlays.

Preserve the original anomaly case even when a successful repair makes it disappear. Link the final decision to that durable identity so disappearance cannot count as resolution by itself.

Before accepting a repair, rebuild both the proposed version and a frozen or cache-only counterfactual. Compare:

- row counts and unique keys;
- selected source observation IDs;
- selected values;
- reconciliation labels and support counts;
- exclusions and review flags;
- coverage by period or cross-sectional group;
- aggregate totals and charts.

State explicitly what changed and what did not. Permit downstream changes only for ledger-authorized pages, observations, and analytical keys. A page-classification repair may correctly change provenance while leaving every selected value unchanged. That is evidence of a targeted repair, not evidence that nothing happened.

## 9. Use durable release gates

Automated checks should classify results as `pass`, `reviewed`, or `fail` and report the observed value, expected rule, and explanation. A reviewed condition remains visible but does not block release. An unresolved structural anomaly must block release.

Recommended common blocking conditions include:

- invalid or repeated analytical keys;
- noncanonical panel dates when the dataset is a panel;
- unexplained zero-coverage or abrupt interior coverage/support gaps;
- a duplicate or unselected page reaching source observations;
- an unresolved within-source collision entering the analytical data;
- unexplained exclusions;
- an unresolved value tie entering the analytical data;
- an isolated extreme without source verification;
- a missing required identity or value in a strict analytical row;
- a selected page without a usable extraction or reviewed disposition;
- stale or unprovenanced manual overlays;
- clustered value anomalies without whole-block review;
- downstream changes outside the repair ledger's authorized lineage.

Expected historical gaps, deliberately excluded ties, and source-verified extremes should be exported as reviewed flags rather than erased. Lower volatility, greater coverage, better accounting balance, or agreement with expected trends is never evidence that a repair is correct.

The release step should write deterministic check results, advisory flags, coverage/support artifacts, exact repair differences, release accounting, and a hash-based manifest. Any `open` blocking case fails release. The manifest binds the principal data and its audit companions to their inputs and QC state; it does not make an unresolved dataset ready by itself.

## 10. Compact failure patterns

| Signal | Possible failure | Evidence-based remedy |
|---|---|---|
| Raw coverage spike | Duplicate scan or repeated table | Compare images/PDFs; correct the manifest. |
| Raw-date dip but canonical coverage is stable | Mixed printed header dates | Preserve raw dates and apply reviewed canonical mapping. |
| Many stable entities absent for one period | Omitted continuation or partial extraction | Confirm the intended period and missing entities on the page; repair selection or extraction. |
| Several extremes share a page | Shifted row or column associations | Compare printed order and review the whole block. |
| One implausible printed amount | Source typo or genuine extreme | Use an exact later reprint if it visibly corroborates a correction; otherwise retain as printed. |
| One cross-sectional group is absent | Omitted table block, continuation, or true source omission | Inspect the support frame and complete adjacent pages; correct selection only from visible evidence. |
| Components do not equal a printed total | Transcription error, differing universe, rounding, or source inconsistency | Inspect labels/footnotes and every component; never fill a residual automatically. |
| Extreme values cluster in one extraction batch | Scale, column, crop, or render failure | Compare full pages and contract/render provenance; review the affected block. |

These patterns require different remedies. Statistical expectations alone establish none of them. The bank-debits appendix shows how one project applied this principle; its thresholds and empirical counts are not defaults.

## 11. Coauthor handoff checklist

Before handing off a historical dataset, provide:

- one clearly named primary analytical file and its unit of observation;
- key variables, time semantics when applicable, and the coverage/support frame;
- row, entity, period, and meaningful group counts as applicable;
- uniqueness, support, and date-frequency guarantees;
- missingness and exclusion rules;
- confirmation that no imputation or winsorization was applied, or exact documentation if it was;
- a coverage/support file containing explicit missing periods or groups;
- quality checks and anomaly flags with resolution status;
- provenance links from analytical rows to observations, pages, images, and PDFs;
- the build command and passing offline regression tests;
- a short list of expected gaps, excluded conflicts, and retained verified extremes; and
- chart definitions that match the analytical sample.

The handoff standard is not “no unusual values.” It is “no unexplained structural anomalies, no silent conflicts, and every consequential decision traceable to evidence.”

## Appendix A: Bank-debits case study (not template defaults)

This appendix preserves project-specific lessons because they are useful examples of evidence-based adjudication. Every percentage, fold-change threshold, page limit, row count, and empirical result below belongs only to the G.6 bank-debits project. A new project must derive and document its own checks from its unit, frequency, sources, and observed failure modes.

The G.6 bank-debits panel exposed a second class of coverage artifact after the duplicate-page and raw-date repairs. A 90-percent adjacent-week rule did not flag the January 1937 and March 1940 drops because the identity loss persisted across neighboring releases. The remedy was not a still lower threshold. The project decomposed missingness by identity status, inspected entity spells and page layouts, and tightened the general unexplained-interior-dip gate to 95 percent while separately requiring review of every coverage change of at least 10 cities.

The identity review used an evidence hierarchy. Printed state suffixes were parsed even without commas and with dotted or spaced abbreviations. Visible ditto marks inherited only the preceding state within the same page and physical row order; page boundaries reset the state. Bare ambiguous names used a preceding/target/following layout signature only when at least two independent issues agreed unanimously. Exact recurring OCR spellings used state-specific mappings, and one-off cases used page-and-row evidence. Model-supplied state alone and unrestricted fuzzy matching were never sufficient.

Every current-value identity receives one of four durable dispositions: `resolved`, `excluded_local_area`, `excluded_unreadable`, or `excluded_unresolved`. Weekly QC reports the decomposition, and the entity-spelling artifact preserves raw label, model label, normalized city-state, method, issue support, first and last week, and status. This matters because a single aggregate coverage count cannot distinguish a missing page from a failed identity parse.

After rebuilding, the two affected weeks each contain 271 cities. The source evidence also falsified the preliminary 300-city assumption: six real, sparsely reported early centers replace two false city-state identities, yielding 304 valid cities. A quality target is not permission to discard source-supported rows.

Amount screening was broadened beyond isolated reversals. The blocking queue includes two-sided isolated changes of at least 2.5-fold when adjacent weeks are mutually similar, adjacent one-sided changes of at least fivefold, stable four-week median shifts of at least threefold, and nonpositive values. Persistent-shift detection is especially important because an OCR error or source-definition change can create a stable new level that an isolated-spike test will miss.

Direct image review adjudicated all 106 flagged amount values. Verified amounts remain exact, including source-marked noncomparabilities and revisions. Model-assisted OCR is reserved for ambiguous scans: it receives a targeted 400-DPI row crop plus page context, has an immutable prompt/image/model signature, is limited to 75 unique pages and one attempt per page, and is advisory only. A durable human decision is required before any model suggestion can affect data. In this release direct inspection was sufficient, so zero model calls were made.

Source verification and econometric treatment are different decisions. Verification answers what the historical page says. Logs, inverse-hyperbolic-sine transformations, leverage diagnostics, sample restrictions, or robustness trimming answer how an empirical specification should handle the verified distribution. Never make the released data look well behaved by converting the latter into undocumented data cleaning.

The frozen counterfactual ledger makes the consequence of the repair explicit: 301,910 prior keys remain, 301,908 with unchanged amounts; 4,533 keys are added as identity restorations; 20 false identity keys are removed and traced to their immutable source rows; and two Columbus, Ohio transcriptions change under keyed direct-source corrections. No other existing amount changes.
