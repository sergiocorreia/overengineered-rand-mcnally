* Verify final keys, row accounting, raw values, and provenance.
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	confirm file "$standardized_dta"
	use "$standardized_dta", clear
	local source_observations = c(N)
	confirm file "$final_dta"
	confirm file "$final_tsv"
	use "$final_dta", clear
	assert c(N) > 0
	assert c(N) <= `source_observations'
	gisid $analysis_keys
	foreach field of global raw_fields {
		confirm variable `field'
	}
	foreach field of global provenance_fields {
		confirm variable `field'
		assert !mi(`field')
	}
	display as result "Reconciliation contract checks passed."
