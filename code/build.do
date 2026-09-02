* ==========================================================================
* Build standardized data, reconcile vintages, explore, QC, and release
* ==========================================================================
	version 19
	local starting_directory `"`c(pwd)'"'

	capture confirm file "project.toml"
	if (c(rc)) {
		capture confirm file "../project.toml"
		if (c(rc)) {
			display as error "Run build.do from the project root or code directory."
			exit 601
		}
		cd ".."
	}

	do "code/common.do"
	do "$code/4-standardization/standardize.do"
	do "$code/4-standardization/test_standardize.do"
	do "$code/5-reconciliation/reconcile.do"
	do "$code/5-reconciliation/test_reconcile.do"
	do "$code/6-exploration/explore.do"
	do "$code/6-exploration/test_explore.do"
	do "$code/7-quality-control/quality_control.do"
	do "$code/7-quality-control/test_quality_control.do"

	cd `"`starting_directory'"'
	display as result "Complete seven-stage build passed the release gate."

	exit
