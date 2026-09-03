* ==========================================================================
* Export compact, read-only legacy evidence for page-review prioritization
* ==========================================================================
	version 19.0
	do "code/common.do"


// -------------------------------------------------------------------------
// Paths and immutable input contract
// -------------------------------------------------------------------------

	assert `"$legacy_root"' != ""
	assert `"$legacy_root"' != `"$root"'
	confirm file "$legacy_data/rand-pages-raw-partial.dta"
	confirm file "$legacy_data/rand-banks-raw-partial.dta"
	confirm file "$legacy_data/rand-banks-compact.dta"

	local destination "$review_priority_external/legacy-inputs"
	capture mkdir "$review_priority_external"
	capture mkdir `"`destination'"'


// -------------------------------------------------------------------------
// Complete page ledger
// -------------------------------------------------------------------------

	use "$legacy_data/rand-pages-raw-partial.dta", clear
	assert inrange(year, 1879, 1940)
	assert inlist(edition, 1, 2)
	gisid year edition pdf_part pdf_page
	gen int wave = 2 * year + edition
	sort wave pdf_part pdf_page
	export delimited using `"`destination'/legacy_pages.tsv"', delimiter(tab) replace


// -------------------------------------------------------------------------
// Raw rows by page: use these counts before downstream exclusions
// -------------------------------------------------------------------------

	use "$legacy_data/rand-banks-raw-partial.dta", clear
	assert inrange(year, 1879, 1940)
	gen byte one = 1
	gen byte raw_missing_state = mi(ustrtrim(state))
	gen byte raw_missing_city = mi(ustrtrim(city))
	gen byte raw_missing_name = mi(ustrtrim(name))
	gen byte raw_invalid_transit = !mi(ustrtrim(transit_number)) & !ustrregexm(ustrtrim(transit_number), "^[0-9]{2}-[0-9]+$")
	gcollapse (sum) raw_rows=one raw_missing_state raw_missing_city raw_missing_name raw_invalid_transit, by(year edition pdf_part pdf_page)
	gen int wave = 2 * year + edition
	gisid year edition pdf_part pdf_page
	sort wave pdf_part pdf_page
	export delimited using `"`destination'/raw_page_quality.tsv"', delimiter(tab) replace


// -------------------------------------------------------------------------
// Cleaned-row quality clusters and later balance-sheet checks
// -------------------------------------------------------------------------

	use "$legacy_data/rand-banks-compact.dta", clear
	keep if is_nonbank == 0 & is_branch == 0
	gen byte one = 1
	gen byte invalid_city = ok_city != 1
	gen byte invalid_name = ok_name != 1
	gen byte invalid_established = ok_established != 1
	gen byte established_after_issue = !mi(established_year) & established_year > year
	gen byte established_before_1776 = !mi(established_year) & established_year < 1776
	gen byte statement_after_issue = !mi(statement_date) & year(statement_date) > year + 1

	egen int resource_fields = rownonmiss(cash us_gov_securities other_securities loans bonds other_assets)
	egen double resource_sum = rowtotal(cash us_gov_securities other_securities loans bonds other_assets), missing
	gen double resource_error_share = abs(resource_sum - totals) / totals if year >= 1934 & totals > 0 & resource_fields >= 3
	gen byte accounting_mismatch_1934 = resource_error_share > 0.01 & !mi(resource_error_share)

	gcollapse (sum) clean_rows=one invalid_city invalid_name invalid_transit invalid_established ///
		established_after_issue established_before_1776 statement_after_issue accounting_mismatch_1934 ///
		(max) max_resource_error_share=resource_error_share, by(year edition pdf_part pdf_page)
	gen int wave = 2 * year + edition
	gisid year edition pdf_part pdf_page
	sort wave pdf_part pdf_page
	export delimited using `"`destination'/clean_page_quality.tsv"', delimiter(tab) replace


// -------------------------------------------------------------------------
// Unambiguous identity panel used for capital reversals and interior gaps
// -------------------------------------------------------------------------

	use "$legacy_data/rand-banks-compact.dta", clear
	keep if is_nonbank == 0 & is_branch == 0 & ok_city == 1 & ok_name == 1
	drop if mi(state, city, name)
	gen int wave = 2 * year + edition
	bys state city name wave: gen int identity_wave_count = _N
	keep if identity_wave_count == 1
	drop identity_wave_count

	bys wave charter_number: gen int charter_wave_count = _N if charter_number > 0 & !mi(charter_number)
	bys wave transit_number: gen int transit_wave_count = _N if !mi(ustrtrim(transit_number))
	gen byte charter_unique = charter_wave_count == 1 & !mi(charter_wave_count)
	gen byte transit_unique = transit_wave_count == 1 & !mi(transit_wave_count)
	drop charter_wave_count transit_wave_count

	keep state city name transit_number charter_number charter_unique transit_unique capital established_year ///
		year edition wave pdf_part pdf_page index
	sort state city name wave
	tempfile identity_panel
	save `identity_panel'


// -------------------------------------------------------------------------
// Two-sided capital anomalies
// -------------------------------------------------------------------------

	by state city name: gen int previous_wave = wave[_n - 1]
	by state city name: gen int following_wave = wave[_n + 1]
	by state city name: gen double previous_capital = capital[_n - 1]
	by state city name: gen double following_capital = capital[_n + 1]
	gen byte exact_neighbors = previous_wave == wave - 1 & following_wave == wave + 1
	gen byte equal_positive_outer = exact_neighbors & !mi(previous_capital, following_capital) & ///
		previous_capital > 0 & previous_capital == following_capital
	gen byte capital_missing_middle = equal_positive_outer & mi(capital)
	gen double capital_factor = max(capital / previous_capital, previous_capital / capital) if equal_positive_outer & capital > 0
	gen byte capital_factor_10 = capital_factor >= 10 & !mi(capital_factor)
	gen byte capital_factor_2 = capital_factor >= 2 & capital_factor < 10 & !mi(capital_factor)
	keep if capital_missing_middle | capital_factor_2 | capital_factor_10
	keep year edition wave pdf_part pdf_page index state city name transit_number charter_number ///
		capital previous_capital following_capital capital_missing_middle capital_factor_2 capital_factor_10 capital_factor
	sort wave pdf_part pdf_page index
	export delimited using `"`destination'/capital_signals.tsv"', delimiter(tab) replace


// -------------------------------------------------------------------------
// Present-absent-present gaps with source-support and lexical page anchors
// -------------------------------------------------------------------------

	use `identity_panel', clear
	tempfile middle_presence previous next gaps state_support page_anchors previous_support next_support previous_page_anchor next_page_anchor

	preserve
		keep state city name wave
		gen byte middle_present = 1
		save `middle_presence'
	restore

	preserve
		rename (wave pdf_part pdf_page transit_number charter_number charter_unique transit_unique) ///
			(previous_wave previous_part previous_page previous_transit previous_charter previous_charter_unique previous_transit_unique)
		replace previous_wave = previous_wave + 1
		rename previous_wave wave
		keep state city name wave previous_*
		save `previous'
	restore

	preserve
		rename (wave pdf_part pdf_page transit_number charter_number charter_unique transit_unique) ///
			(following_wave following_part following_page following_transit following_charter following_charter_unique following_transit_unique)
		replace following_wave = following_wave - 1
		rename following_wave wave
		keep state city name wave following_*
		save `next'
	restore

	use `previous', clear
	merge 1:1 state city name wave using `next', keep(match) nogen
	merge 1:1 state city name wave using `middle_presence', keep(master) nogen

	gen byte charter_agrees = previous_charter > 0 & previous_charter == following_charter & ///
		previous_charter_unique == 1 & following_charter_unique == 1
	gen byte transit_agrees = !mi(ustrtrim(previous_transit)) & previous_transit == following_transit & ///
		previous_transit_unique == 1 & following_transit_unique == 1

	preserve
		use `identity_panel', clear
		gen byte one = 1
		gcollapse (sum) state_rows=one, by(wave state)
		save `state_support'
	restore

	preserve
		use `state_support', clear
		rename state_rows previous_state_rows
		replace wave = wave + 1
		save `previous_support'
	restore

	preserve
		use `state_support', clear
		rename state_rows following_state_rows
		replace wave = wave - 1
		save `next_support'
	restore

	merge m:1 wave state using `state_support', keep(master match) nogen
	merge m:1 wave state using `previous_support', keep(master match) nogen
	merge m:1 wave state using `next_support', keep(master match) nogen
	gen double support_ratio = state_rows / min(previous_state_rows, following_state_rows)
	gen byte support_complete = !mi(state_rows, previous_state_rows, following_state_rows) & support_ratio >= 0.80
	keep if support_complete

	gen str244 sort_key = ustrlower(ustrtrim(city)) + "|" + ustrlower(ustrtrim(name))
	sort wave state sort_key
	gen long gap_index = _n
	save `gaps'

	use `identity_panel', clear
	keep wave state city name pdf_part pdf_page
	gen str244 sort_key = ustrlower(ustrtrim(city)) + "|" + ustrlower(ustrtrim(name))
	gen byte is_anchor = 1
	gen long gap_index = .
	save `page_anchors'

	use `gaps', clear
	keep gap_index wave state sort_key
	gen byte is_anchor = 0
	gen byte pdf_part = .
	gen int pdf_page = .
	append using `page_anchors'
	sort wave state sort_key is_anchor
	by wave state: gen byte previous_anchor_part = pdf_part if is_anchor
	by wave state: gen int previous_anchor_page = pdf_page if is_anchor
	by wave state: replace previous_anchor_part = previous_anchor_part[_n - 1] if mi(previous_anchor_part) & _n > 1
	by wave state: replace previous_anchor_page = previous_anchor_page[_n - 1] if mi(previous_anchor_page) & _n > 1
	keep if is_anchor == 0
	keep gap_index previous_anchor_part previous_anchor_page
	save `previous_page_anchor'

	use `gaps', clear
	keep gap_index wave state sort_key
	gen byte is_anchor = 0
	gen byte pdf_part = .
	gen int pdf_page = .
	append using `page_anchors'
	gsort wave state -sort_key -is_anchor
	by wave state: gen byte following_anchor_part = pdf_part if is_anchor
	by wave state: gen int following_anchor_page = pdf_page if is_anchor
	by wave state: replace following_anchor_part = following_anchor_part[_n - 1] if mi(following_anchor_part) & _n > 1
	by wave state: replace following_anchor_page = following_anchor_page[_n - 1] if mi(following_anchor_page) & _n > 1
	keep if is_anchor == 0
	keep gap_index following_anchor_part following_anchor_page
	save `next_page_anchor'

	use `gaps', clear
	merge 1:1 gap_index using `previous_page_anchor', assert(match) nogen
	merge 1:1 gap_index using `next_page_anchor', assert(match) nogen

	gen byte localized_part_a = previous_anchor_part if previous_anchor_part == following_anchor_part & ///
		previous_anchor_page == following_anchor_page
	gen int localized_page_a = previous_anchor_page if !mi(localized_part_a)
	gen double localized_weight_a = 1 if !mi(localized_part_a)

	replace localized_part_a = previous_anchor_part if mi(localized_part_a) & ///
		previous_anchor_part == following_anchor_part & following_anchor_page == previous_anchor_page + 1
	replace localized_page_a = previous_anchor_page if mi(localized_page_a) & !mi(localized_part_a)
	replace localized_weight_a = 0.5 if mi(localized_weight_a) & !mi(localized_part_a)
	gen byte localized_part_b = following_anchor_part if localized_weight_a == 0.5
	gen int localized_page_b = following_anchor_page if localized_weight_a == 0.5
	gen double localized_weight_b = 0.5 if localized_weight_a == 0.5

	keep gap_index wave state city name previous_* following_* charter_agrees transit_agrees support_ratio ///
		localized_part_a localized_page_a localized_weight_a localized_part_b localized_page_b localized_weight_b
	sort wave state city name
	export delimited using `"`destination'/gap_signals.tsv"', delimiter(tab) replace

	display as result "Legacy review inputs exported beneath `destination'."
	exit
