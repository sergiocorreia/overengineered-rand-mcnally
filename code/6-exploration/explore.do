* ==========================================================================
* Deterministic first-pass exploration of values, coverage, and source support
* ==========================================================================
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	capture mkdir "$exploration_output"
	confirm file "$final_dta"
	use "$final_dta", clear
	gisid $analysis_keys

	frame create exploration_summary str32 variable long observations nonmissing ///
		double mean sd minimum median maximum
	foreach field of global value_fields {
		confirm numeric variable `field'
		quietly count
		local observations = r(N)
		quietly count if !mi(`field')
		local nonmissing = r(N)
		quietly summarize `field', detail
		frame post exploration_summary ("`field'") (`observations') (`nonmissing') ///
			(r(mean)) (r(sd)) (r(min)) (r(p50)) (r(max))
	}
	frame exploration_summary: export delimited using ///
		"$exploration_output/exploration-summary.tsv", delimiter(tab) replace

	preserve
		contract $source_page_field, freq(observations)
		rename observations source_observations
		gsort -source_observations $source_page_field
		export delimited using "$exploration_output/source-page-support.tsv", ///
			delimiter(tab) replace
	restore

	if ("$dataset_shape" == "panel") {
		preserve
			keep $entity_keys $time_key
			duplicates drop
			contract $time_key, freq(entity_count)
			sort $time_key
			export delimited using "$exploration_output/exploration-coverage.tsv", ///
				delimiter(tab) replace
			capture confirm numeric variable $time_key
			if (!c(rc)) {
				twoway connected entity_count $time_key, ///
					ytitle("Reporting entities") xtitle("$time_key") ///
					title("Analytical coverage") name(coverage, replace)
				graph export "$exploration_output/exploration-coverage.png", replace width(2400)
			}
		restore
	}
	display as result "Exploration outputs written to $exploration_output."
