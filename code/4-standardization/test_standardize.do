* Verify the durable standardization contract against the current output.
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	confirm file "$standardized_dta"
	confirm file "$standardized_tsv"
	confirm file "$correction_diff_tsv"
	confirm file "$qc_output/correction-receipt.json"
	confirm file "$record_review_diff_tsv"
	confirm file "$record_review_flags_tsv"
	use "$standardized_dta", clear
	assert c(N) > 0
	gisid $record_id_field
	foreach field of global raw_fields {
		confirm string variable `field'
	}
	foreach field of global provenance_fields {
		confirm variable `field'
		assert !mi(`field')
	}
	display as result "Standardization contract checks passed."
