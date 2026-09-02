* ==========================================================================
* Structural Stata assertions plus deterministic Python QC and release gate
* ==========================================================================
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	confirm file "$final_dta"
	use "$final_dta", clear
	gisid $analysis_keys
	foreach field of global provenance_fields {
		confirm variable `field'
		assert !mi(`field')
	}
	foreach field of global value_fields {
		confirm variable `field'
	}

	do "$code/7-quality-control/domain_checks.do"
	foreach generated in flags.tsv review_queue.tsv coverage.tsv source_support.tsv ///
		summary.json release_gate.tsv decision_accounting.tsv release_accounting.tsv {
		capture erase "$qc_output/`generated'"
	}
	shell "$python_exec" "$code/7-quality-control/run_quality_control.py" ///
		--root "$root" --allow-failed-release
	confirm file "$qc_output/release_gate.tsv"

	preserve
		import delimited using "$qc_output/release_gate.tsv", ///
			varnames(1) stringcols(_all) clear
		assert c(N) == 1
		assert release_status == "pass"
	restore

	capture erase "$qc_output/release_manifest.json"
	shell "$python_exec" "$code/7-quality-control/build_release_manifest.py" --root "$root"
	confirm file "$qc_output/release_manifest.json"
	capture erase "$temp/release-manifest-verified.ok"
	shell "$python_exec" "$code/7-quality-control/build_release_manifest.py" ///
		--root "$root" --verify && /usr/bin/touch "$temp/release-manifest-verified.ok"
	confirm file "$temp/release-manifest-verified.ok"
	erase "$temp/release-manifest-verified.ok"
	display as result "Quality control passed; release manifest verified."
