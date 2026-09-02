* ==========================================================================
* Reconcile repeated source vintages without averaging disagreements
* ==========================================================================
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	capture mkdir "$reconciliation_output"
	confirm file "$standardized_dta"
	use "$standardized_dta", clear
	foreach field of global analysis_keys {
		confirm variable `field'
		assert !mi(`field')
	}
	foreach field of global provenance_fields {
		confirm variable `field'
		assert !mi(`field')
	}

	if (!$repeated_vintages) {
		gisid $analysis_keys
	}
	else {
		confirm variable $source_priority_field
		bysort $analysis_keys: gen long __vintage_count = _N
		gen byte __vintage_conflict = 0
		foreach field of global value_fields {
			confirm numeric variable `field'
			bysort $analysis_keys: egen double __minimum = min(`field')
			bysort $analysis_keys: egen double __maximum = max(`field')
			bysort $analysis_keys: egen long __nonmissing = count(`field')
			replace __vintage_conflict = 1 if __vintage_count > 1 & ///
				(__minimum != __maximum | inrange(__nonmissing, 1, __vintage_count - 1))
			drop __minimum __maximum __nonmissing
		}

		quietly count if __vintage_conflict
		if (r(N)) {
			preserve
				keep if __vintage_conflict
				sort $analysis_keys $source_priority_field $source_page_field $record_id_field
				export delimited using "$reconciliation_output/reconciliation-conflicts.tsv", ///
					delimiter(tab) replace
			restore
			display as error "Repeated vintages disagree; review the exported candidates."
			exit 459
		}

		* Exact agreements use the configured priority, then stable provenance.
		sort $analysis_keys $source_priority_field $source_page_field $record_id_field
		by $analysis_keys: keep if _n == 1
		drop __vintage_count __vintage_conflict
		gisid $analysis_keys
	}

	compress
	save "$final_dta", replace
	export delimited using "$final_tsv", delimiter(tab) replace
	display as result "Reconciliation wrote " c(N) " analytical observations."
