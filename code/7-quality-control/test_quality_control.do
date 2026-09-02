* Verify that the current reports and manifest still match the analytical data.
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	foreach artifact in flags.tsv review_queue.tsv coverage.tsv source_support.tsv ///
		summary.json release_gate.tsv decision_accounting.tsv ///
		release_accounting.tsv record-review-differences.tsv record-review-blocking.tsv ///
		correction-differences.tsv correction-receipt.json release_manifest.json {
		confirm file "$qc_output/`artifact'"
	}
	import delimited using "$qc_output/release_gate.tsv", ///
		varnames(1) stringcols(_all) clear
	assert c(N) == 1
	assert release_status == "pass"
	capture erase "$temp/release-manifest-test-verified.ok"
	shell "$python_exec" "$code/7-quality-control/build_release_manifest.py" ///
		--root "$root" --verify && /usr/bin/touch "$temp/release-manifest-test-verified.ok"
	confirm file "$temp/release-manifest-test-verified.ok"
	erase "$temp/release-manifest-test-verified.ok"
	display as result "Quality-control release checks passed."
