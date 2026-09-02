* Verify Stata 19, project paths, generated settings, and the graph scheme.
	version 19
	capture confirm file "project.toml"
	if (c(rc)) {
		capture confirm file "../project.toml"
		if (c(rc)) {
			display as error "Run test_common.do from the project root or code directory."
			exit 601
		}
		cd ".."
	}

	do "code/common.do"
	assert c(stata_version) >= 19
	assert `"`c(scheme)'"' == "cleanplots_ev2"
	foreach name in root code data temp input output figures tables sources manual {
		assert `"$`name'"' != ""
	}
	confirm file "$root/project.toml"
	confirm file "$code/requirements.txt"
	confirm file "$code/bootstrap_stata.do"
	confirm file "$code/scheme-cleanplots_ev2.scheme"
	which require
	which gisid
	display as result "Shared offline Stata 19 setup checks passed."
