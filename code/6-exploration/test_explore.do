* Verify deterministic review artifacts without imposing empirical conclusions.
	version 19
	if ("$root" == "") do "code/common.do"
	else do "$code/common.do"

	confirm file "$exploration_output/exploration-summary.tsv"
	confirm file "$exploration_output/source-page-support.tsv"
	if ("$dataset_shape" == "panel") {
		confirm file "$exploration_output/exploration-coverage.tsv"
	}
	display as result "Exploration artifact checks passed."
