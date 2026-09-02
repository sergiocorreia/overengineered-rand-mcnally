* Verify that common.do fails closed without installing a missing dependency.
	version 19
	local starting_directory `"`c(pwd)'"'
	capture confirm file "project.toml"
	if (c(rc)) {
		capture confirm file "../project.toml"
		if (c(rc)) {
			display as error "Run test_common_fail_closed.do from the project root or code directory."
			exit 601
		}
		cd ".."
	}

	local original_plus `"`c(sysdir_plus)'"'
	local empty_plus `"`c(pwd)'/temp/stata-empty-plus/"'
	capture mkdir "temp"
	capture mkdir "temp/stata-empty-plus"
	sysdir set PLUS `"`empty_plus'"'
	capture noisily do "code/common.do"
	local dependency_rc = c(rc)
	sysdir set PLUS `"`original_plus'"'
	cd `"`starting_directory'"'

	assert `dependency_rc' == 601
	display as result "Missing Stata dependencies fail closed without installation."
