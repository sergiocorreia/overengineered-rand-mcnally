# Stata Style Guide for Historical Data Projects

This guide is the shared reference for editing Stata 19 do-files in projects created from the historical data-extraction template. It preserves a direct research-code style while making raw evidence, normalization, reconciliation, and coauthor-facing outputs consistent across projects.

## File Skeleton

Run Stata commands from the project root. Every normal do-file should start with the project header, declare Stata 19, load `code/common.do`, and then use section headers. Use a literal tab for indented commands.

```stata
* ===========================================================================
* Title of the do-file
* ===========================================================================
	version 19.0
	do "code/common.do"


// --------------------------------------------------------------------------
// Section header
// --------------------------------------------------------------------------

	use "$data/input-file.dta", clear
```

Use the top `* ===` header only for the file title. Use `// ---` headers for major sections. Keep section names short and descriptive.

For numbered scripts, preserve the surrounding naming and numbering convention. If a script is part of a series, match the structure of nearby scripts before introducing a new pattern.

## Formatting and Naming

- Indent executable code one literal tab inside each section, loop, `preserve` block, or program block.
- Use lowercase variable names with underscores: `entity_id`, `state_abbrev`, `is_reviewed`.
- Avoid spaces in filenames.
- Prefer concise code over defensive boilerplate for stable, hand-maintained inputs.
- Keep readable Stata commands on one line when they are roughly 100 characters or shorter. Do not introduce multiline continuations only to fit an 80-character or 70-character line limit.
- Use comments to explain decisions, data quirks, and non-obvious assumptions; do not narrate ordinary Stata syntax.
- Use `loc` / `local` for temporary script settings and lists. Use project globals only when they come from `common.do` or are intentionally shared.
- Prefer `gen` to `generate`.
- Spell out `rename`; do not use `ren` or other abbreviations for it.
- Use `su` for summarize commands in new code. If existing code already says `summarize`, leave it as-is rather than shortening it during unrelated edits.
- End batch-style do-files with `exit` when that is the local convention.

For predictable numeric loops, prefer `forvalues` over `foreach ... of numlist`. Use `foreach ... of numlist` only when the sequence is irregular enough that `forvalues` is impractical.

Prefer:

```stata
	forvalues num = 1/10 {
		...
	}
```

Instead of:

```stata
	foreach num of numlist 1/10 {
		...
	}
```

## Project Setup and Paths

Always rely on the root `code/common.do` for project setup. It handles standard settings, offline dependency checks from `code/requirements.txt`, graph defaults, and shared paths. Run do-files from the project root so this path is stable.

`common.do`, `build.do`, and ordinary stage/test commands never install or update Stata packages. They fail closed with a bootstrap instruction when a requirement is missing or outdated. Dependency installation is a separate, deliberate environment operation:

```bash
/usr/local/stata19/stata-mp -b do code/bootstrap_stata.do
```

Review `code/requirements.txt` before running bootstrap. The command may access the network and modify the user's Stata PLUS directory, so it must not appear inside a build, test, startup file, or automated reproduction command. After bootstrap, validate the environment without network access:

```bash
/usr/local/stata19/stata-mp -b do code/test_common.do
```

Use the globals created by `common.do` rather than hard-coded project paths:

- `$root` for the project root
- `$code` for Stata code
- `$data` for generated or canonical data
- `$sources` for source inputs
- `$input` for small auxiliary research inputs
- `$manual` for durable human decisions and correction ledgers
- `$temp` for temporary project artifacts
- `$output` for review, QC, table, figure, and release artifacts
- `$figures` for figures
- `$tables` for table artifacts

Do not hard-code a Dropbox username, project slug, or stage path inside an ordinary do-file. If a machine-specific external path is unavoidable, centralize it in `project.toml`, an ignored local configuration, or `common.do` and fail clearly when it is unavailable.

## Preferred Commands and Packages

Prefer the fast project-standard tools when they fit the job:

- Use `gisid` rather than `isid` unless a built-in-only check is needed.
- Use `gegen`, `gcollapse`, `gcontract`, and `gdistinct` for grouped operations and counts.
- Use `join` from `ftools` when it is clearer than `merge` and the join contract can be stated directly.
- Use `reghdfe` and `ppmlhdfe` for high-dimensional fixed-effect regressions.
- Use `rangestat` for rolling or windowed event calculations when that is already the local pattern.

Built-in Stata commands are fine when they are clearer, required by syntax, or not performance-sensitive. Do not replace a readable built-in command with a package command just for ornament.

When a project contains historical U.S. places or banks, use Banknorm's
`standardize_cities` or `standardize_banks` through its installed Stata
interface. Preserve the raw source strings, use `newname=field` inside `gen()`,
and request `is_valid` so unmatched values remain auditable:

```stata
	standardize_cities, state(state_raw) city(city_raw) ///
		gen(state_std=state city_std=city city_id city_valid=is_valid)

	* Use this instead when the records also contain bank names.
	standardize_banks, state(state_raw) city(city_raw) bank(bank_raw) ///
		date(report_date) gen(state_std=state city_std=city city_id ///
		bank_std=name bank_id bank_valid=is_valid city_valid=city_found)
```

The `date()` option is optional and takes a numeric Stata daily date.
`standardize_banks` also standardizes the associated city and can return the
same city metadata as `standardize_cities`.

For quantile bins, prefer `gegen ... = xtile(...)` from `gtools` over Stata's built-in `egen ... = xtile(...)`. It is much faster, supports `by(...)`, and chooses the appropriate storage type by default, so do not predeclare `int`, `long`, or another type just for the generated category.

Prefer:

```stata
	gegen fund_cat = xtile(fundamentals), n(10)
	gegen fund_cat = xtile(fundamentals), n(10) by(year)
```

Instead of:

```stata
	egen fund_cat = xtile(fundamentals), n(10)
```

Use `gquantiles` directly when its quantile-specific syntax is clearer or when using features beyond `xtile()` categories.

## Types and Missing Values

Declare variable types when the type is known ahead of time:

```stata
	gen byte is_selected = !mi(selected_record_id)
	gen int year = year(date)
	gen long row_id = _n
	gen double value_share = value / total_value
```

Use `byte` for dummies and small categorical flags, `int` for years/months/small counts, `long` for row IDs and Stata daily dates when appropriate, and `double` for large identifiers or calculations where precision matters. `common.do` sets the default numeric type to double, but explicit types still make intent clearer.

Prefer the `mi()` function to `missing()`, and use compact missing-value checks:

```stata
	drop if mi(entity_id, date, record_type)
```

Prefer this over:

```stata
	drop if mi(entity_id) | mi(date) | mi(record_type)
```

For row totals, be precise. Stata's `egen rowtotal()` treats missing inputs as zero by default. With the `missing` option, it returns missing only when all inputs are missing. Therefore:

```stata
	egen y = rowtotal(x1 x2), missing
```

is a replacement for a manual all-missing correction, not for a rule that requires `y` to be missing when any input is missing. If the intended rule is "missing if any component is missing," write that rule explicitly or use a row-missing check.

## Data Workflow

Keep data transformations direct and auditable:

- Load one clear input at the start of each section.
- Assert key invariants near the point where they become true: nonmissing IDs, uniqueness, valid date ranges, expected merge coverage.
- Use `tempfile` for intermediate datasets that do not need to survive the run.
- Use `$temp` only for debug artifacts or temporary files useful outside the
  current Stata session. Earlier versions of this project saved many datasets
  under `$temp` that were never used afterward; when a saved file is only passed
  between steps inside the same do-file, prefer a `tempfile`.
- When accumulating small result datasets from loops, especially regression
  coefficients, table rows, or metrics, prefer Stata frames with `frame post`
  over repeatedly saving one tempfile per loop iteration and later stitching
  them together with `use` / `append`. Create one results frame, post rows into
  it as estimates are produced, then `frame change` or `frame copy` for figure
  and table construction.
- Use `preserve` / `restore` for short side exports or checks that should not disrupt the main dataset.
- Use `compress` before saving durable datasets.
- Use `format` for IDs/dates before export when Stata's display format would otherwise create hard-to-read output.

Historical extraction stages have additional invariants:

- Assert the complete flat extraction contract—required columns, types, statuses, and provenance—immediately after import.
- Preserve every raw field and the source, page, record, source-hash, render-hash, and contract-signature variables through standardization.
- Apply human corrections from keyed TSV overlays, never from hard-coded `replace` statements. Require the expected prior value and relevant hashes to match before replacement; fail on a stale or duplicate correction.
- Retain all source observations. Export invalid, unmatched, ambiguous, and conflicting records for review instead of silently dropping them.
- When publication vintages repeat an analytical key, rank eligible candidates under an explicit deterministic rule, preserve support counts and candidate IDs, and never average disagreements.
- For projects without repeated vintages, keep the reconciliation stage as a tested pass-through so the build order remains stable.
- Write intermediates beneath `$temp` and review/diagnostic artifacts beneath `$output`. Write only flat, coauthor-ready `.dta` and `.tsv` files directly beneath `$data`; do not create stage subdirectories in `data/`.

Use `c(N)` when you need the current dataset's observation count without an
`if` or `in` qualifier. Do not run `count` only to copy `r(N)`:

```stata
	assert c(N)>0
	local observation_count = c(N)
```

Prefer:

```stata
	frame create results str32(Y Z era) int(h) double(b se b_u b_l)

	foreach Y of local lhs_list {
		foreach Z of local rhs_list {
			forvalues h = 0/5 {
				qui reghdfe F`h'`Y' `Z', absorb(entity_id year) vce(dkraay 3)
				local b = _b[`Z']
				local se = _se[`Z']
				frame post results ///
					("`Y'") ("`Z'") ("all") (`h') ///
					(`b') (`se') (`b' + 1.96 * `se') (`b' - 1.96 * `se')
			}
		}
	}

	frame change results
```

Instead of creating `coefs_*.dta` files inside the loop, opening each one,
adding one observation with `set obs`, saving it again, and appending all of
those files later.

Prefer early filtering when the input has an authoritative validity flag:

```stata
	use "$input/benchmark.dta", clear
	keep if is_valid == 1
```

## Merges, Joins, and IDs

### Analytical and source identifiers

Choose the analytical key explicitly in the project README and `project.toml`.
Keep source identifiers separate from standardized analytical identifiers:

- preserve the exact raw identity string;
- retain stable source/page/record IDs and hashes through every stage;
- use a documented crosswalk or Banknorm result for a standardized entity ID;
- never create an identifier from a numeric rescaling or row order unless that
  construction is the declared, tested contract; and
- export unmatched, ambiguous, and conflicting identities for review rather
  than dropping them.

Assert the key only after the stage that makes it valid. Repeated publication
vintages legitimately repeat an analytical key in the source-observation file;
they become unique only after deterministic reconciliation or reviewed
exclusion.

Make merge contracts explicit. In `merge` commands, use `keepusing(...)` whenever only specific variables are needed from the using dataset, so it is clear which fields each merge loaded. After merges, inspect or assert `_merge` before dropping it unless `nogen` is used intentionally.

```stata
	merge m:1 entity_id date using "`events'", keep(master match) keepusing(record_type)
	tab _merge
	assert _merge != 2
	drop _merge
```

Use `gisid` after deduplication or construction of a key:

```stata
	bys entity_id date record_type (source_record_id): keep if _n==1
	gisid entity_id date record_type
```

When using `join`, state the fields being joined and the key in a compact form:

```stata
	join comparison_*, from("$temp/entity_comparisons") by(entity_id date) keep(master match using)
```

## Refactoring Existing Do-Files

Keep refactors narrow. Preserve the workflow shape, output paths, and public-facing artifacts unless the requested change requires otherwise.

Some variable names and file names may have changed over the life of the
project. When cleaning old code, verify names against current inputs and nearby
scripts rather than preserving stale names just because they appear in the old
do-file.

For repetitive renames, prefer grouped renames or `ds ... , not` loops over long generated-looking blocks:

```stata
	rename (city state record_type) (city_name state_abbrev record_type_raw)
	ds entity_id, not
	foreach var of varlist `r(varlist)' {
		rename `var' gt_`var'
	}
```

Before mass-prefixing, `keep` only the variables needed downstream. Stata variable names are limited to 32 characters, and prefixing every column can silently turn a clean idea into a brittle one.

Avoid speculative fallback branches for stable, hand-maintained inputs. If the Excel schema is fixed, code to the actual schema and let Stata fail clearly if the input changes.

## Output Conventions

When a do-file creates a paper or review artifact, it should usually do two things:

- Print key counts and tabulations to Stata Results so the result is visible immediately.
- Write the durable artifact to `$tables`, `$figures`, `$output`, or `$temp` as appropriate.

For paper tables, prefer one compact primary include file that can be used inside a surrounding LaTeX table environment. If a detailed full-list table is useful, write it as a secondary artifact rather than making it the primary output.

When the project declares the `post_scalar.ado` helper in `code/requirements.txt`, use it for individual scalar values that LaTeX will consume later instead of repeatedly open-coding `file open` / `file write` / `file close`. It writes to `$tables`, replaces existing files, and expects the filename extension to be provided:

```stata
	count if is_selected == 1
	local N_selected = r(N)
	post_scalar, scalar(`N_selected') file("N_selected.tex")
```

Use the optional `format()` argument when the default `%9.0fc` is not appropriate.

For validation and review workflows, include row-level debug exports when aggregate counts are not enough. A good pattern is one compact summary plus one or more TSVs listing the exact rows to inspect.

## Verification

For code changes, verify with the real Linux Stata 19 executable from the project root:

```bash
/usr/local/stata19/stata-mp -b do code/4-standardization/standardize.do
```

Then inspect the `.log`, the Stata Results summaries, and any generated artifacts. If a refactor is supposed to preserve behavior, compare key row counts and output files before and after.

For documentation-only changes, verify that examples match `code/common.do`, `code/bootstrap_stata.do`, `code/requirements.txt`, the Stata 19 executable, the graph scheme, and current do-file conventions. Do not run formatters or touch unrelated files.

## Graph and Chart Style

The active project setup uses graph defaults from `common.do`, including the project graph scheme. Let `common.do` set the default scheme; specify a scheme inside graph commands only when there is a concrete reason.

Common graph patterns:

- Use `twoway bar` for layered bars, usually with `barwidth(1)` or `barwidth(1.05)`, `base(0)`, and no visible outlines.
- Use `graph bar, stack asyvars` for stacked category bars with explicit `bar(N, color(...))` choices.
- Use `twoway connected` for time-series lines with markers.
- Use `twoway scatter` and `spmap` for map-style visualizations, with transparency and jitter when needed.

Legend conventions:

```stata
	legend(pos(6) rows(1) span order(1 "Observed" 2 "Excluded"))
```

Use `legend(off)` for simple single-series or self-evident two-series charts. Keep legends below the chart when present.

Axis and label conventions:

- Use explicit `ytitle()` and `xtitle()` unless the surrounding context makes an axis truly obvious.
- Use short `title()` text and sparse `note()` text for caveats.
- Avoid `subtitle()` and `caption()` unless matching an existing nearby figure.
- Use round year intervals in `xlabel()`, such as `1840(20)1950`.
- Use formatted y-axis labels for counts or currency, such as `ylabel(, format("%8.0fc"))`.

Color conventions:

- Prefer Stata named colors already used in the project: `red`, `blue`, `orange`, `green`, `gs10`, `cranberry`, `forest_green`, `lavender`, `dknavy`, `black`.
- Use transparency with `%NN`, such as `orange%25` or `blue%50`.
- Avoid hex colors unless a specific external style requires them.

Export figures as PNG unless a surrounding workflow clearly uses another format:

```stata
	graph export "$figures/my-figure.png", replace
```

When exporting a PDF, rely on the `.pdf` extension; do not add the redundant `as(pdf)` option:

```stata
	graph export "$figures/my-figure.pdf", replace
```

Use `$figures` or another path derived from `common.do`; avoid ad hoc relative export paths in new code.
