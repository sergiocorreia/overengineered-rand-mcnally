* ==========================================================================
* Explicit network bootstrap for project Stata dependencies
* ==========================================================================
	version 19
	local invocation_directory `"`c(pwd)'"'

	capture confirm file "project.toml"
	if (c(rc)) {
		capture confirm file "../project.toml"
		if (c(rc)) {
			display as error "Run bootstrap_stata.do from the project root or code directory."
			exit 601
		}
		cd ".."
	}
	global root `"`c(pwd)'"'
	global code "$root/code"

	display as error "This explicit bootstrap may access the network and modify your personal Stata PLUS directory."
	display as error "Ordinary common.do, build.do, and stage tests never install packages."

	capture which require
	if (c(rc)) ssc install require
	require using "$code/requirements.txt", install

	cd `"`invocation_directory'"'
	display as result "Stata dependencies installed and validated. Rerun code/test_common.do offline."
