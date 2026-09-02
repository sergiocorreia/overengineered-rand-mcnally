* ==========================================================================
* Validate the flat extraction contract and build a provenance-rich working file
* ==========================================================================
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	capture mkdir "$standardization_temp"
	capture mkdir "$standardization_output"
	confirm file "$extraction_flat_tsv"
	* apply_record_reviews verifies the immutable current run before publishing.
	capture erase "$reviewed_extraction_tsv"
	capture erase "$record_review_diff_tsv"
	capture erase "$record_review_flags_tsv"
	shell "$python_exec" "$code/4-standardization/apply_record_reviews.py" ///
		--output "$reviewed_extraction_tsv" --diff "$record_review_diff_tsv" ///
		--flags "$record_review_flags_tsv"
	confirm file "$reviewed_extraction_tsv"
	confirm file "$record_review_diff_tsv"
	confirm file "$record_review_flags_tsv"
	import delimited using "$record_review_flags_tsv", ///
		varnames(1) stringcols(_all) bindquote(strict) clear
	quietly count
	local open_record_reviews = r(N)
	if (`open_record_reviews') {
		display as error "Flagged/excluded record reviews remain open: `open_record_reviews'"
		exit 459
	}
	capture erase "$corrected_extraction_tsv"
	capture erase "$correction_diff_tsv"
	capture erase "$qc_output/correction-receipt.json"
	shell "$python_exec" "$code/4-standardization/apply_corrections.py" ///
		--root "$root" --input "$reviewed_extraction_tsv" ///
		--output "$corrected_extraction_tsv" --diff "$correction_diff_tsv"
	confirm file "$corrected_extraction_tsv"
	confirm file "$correction_diff_tsv"
	confirm file "$qc_output/correction-receipt.json"

	import delimited using "$corrected_extraction_tsv", ///
		varnames(1) stringcols(_all) bindquote(strict) clear

	foreach field of global required_extraction_fields {
		confirm variable `field'
	}
	if ("$exact_extraction_fields" != "") {
		ds
		local actual_fields "`r(varlist)'"
		local expected_fields "$exact_extraction_fields"
		local missing_fields : list expected_fields - actual_fields
		local extra_fields : list actual_fields - expected_fields
		assert `"`missing_fields'"' == ""
		assert `"`extra_fields'"' == ""
	}

	assert !mi($record_id_field)
	gisid $record_id_field
	foreach field of global provenance_fields {
		assert !mi(`field')
	}
	foreach field of global raw_fields {
		confirm string variable `field'
	}

	* Normalized analytical values become numeric only after raw strings survive.
	foreach field of global value_fields {
		capture confirm numeric variable `field'
		if (c(rc)) destring `field', replace ignore(",")
	}

	* Banknorm is opt-in in project.toml and uses its external cache. The shared
	* package and its cache are never modified by this scaffold.
	if ($use_banknorm) {
		confirm variable $banknorm_state_field $banknorm_city_field
		if ("$banknorm_bank_field" == "") {
			capture which standardize_cities
			if (c(rc)) {
				display as error "Banknorm's standardize_cities command is unavailable."
				exit 199
			}
			standardize_cities, state($banknorm_state_field) ///
				city($banknorm_city_field) gen($banknorm_state_output=state ///
				$banknorm_city_output=city $banknorm_city_id_output=city_id ///
				city_banknorm_valid=is_valid)
		}
		else {
			capture which standardize_banks
			if (c(rc)) {
				display as error "Banknorm's standardize_banks command is unavailable."
				exit 199
			}
			local date_option
			if ("$banknorm_date_field" != "") local date_option "date($banknorm_date_field)"
			standardize_banks, state($banknorm_state_field) ///
				city($banknorm_city_field) bank($banknorm_bank_field) `date_option' ///
				gen($banknorm_state_output=state $banknorm_city_output=city ///
				$banknorm_city_id_output=city_id $banknorm_bank_output=name ///
				$banknorm_bank_id_output=bank_id bank_banknorm_valid=is_valid ///
				city_banknorm_valid=city_found)
		}
	}

	* Unmatched Banknorm identities remain in the audit universe and receive an
	* explicit review export rather than being silently dropped.
	gen byte __identity_review = 0
	capture confirm variable city_banknorm_valid
	if (!c(rc)) replace __identity_review = 1 if city_banknorm_valid != 1
	capture confirm variable bank_banknorm_valid
	if (!c(rc)) replace __identity_review = 1 if bank_banknorm_valid != 1
	quietly count if __identity_review
	if (r(N)) {
		preserve
			keep if __identity_review
			export delimited using "$standardization_output/identity-review.tsv", ///
				delimiter(tab) replace
		restore
	}
	drop __identity_review

	compress
	save "$standardized_dta", replace
	export delimited using "$standardized_tsv", delimiter(tab) replace
	display as result "Standardization retained " c(N) " source observations."
