* ==========================================================================
* Shared Stata 19 setup for the historical data-extraction pipeline
* ==========================================================================
	version 19
	clear all
	capture log close _all
	graph close _all
	set type double
	set varabbrev off
	set emptycells drop
	set trace off
	capture cls


// -------------------------------------------------------------------------
// Project root, dependency contract, and generated project settings
// -------------------------------------------------------------------------
	local invocation_directory `"`c(pwd)'"'
	capture confirm file "project.toml"
	if (c(rc)) {
		capture confirm file "../project.toml"
		if (c(rc)) {
			display as error "Run this do-file from the project root or its code directory."
			exit 601
		}
		cd ".."
	}
	global root `"`c(pwd)'"'
	cd `"`invocation_directory'"'
	global code "$root/code"
	global data "$root/data"
	global temp "$root/temp"
	global log "$root/log"
	global input "$root/input"
	global output "$root/output"
	global figures "$output/figures"

	global sources "$root/sources"
	global manual "$root/manual"
	global tables "$output/tables"
	global qc_output "$output/quality-control"

	foreach directory in "$data" "$temp" "$log" "$output" "$figures" ///
		"$tables" "$qc_output" {
		capture mkdir `"`directory'"'
	}

	* Ordinary builds are offline and never install or update user ado-files.
	capture which require
	if (c(rc)) {
		display as error "The Stata dependency checker 'require' is not installed."
		display as error "Review and run code/bootstrap_stata.do explicitly, then retry."
		exit 601
	}
	capture noisily require using "$code/requirements.txt"
	local dependency_rc = c(rc)
	if (`dependency_rc') {
		display as error "Required Stata packages are missing or out of date."
		display as error "Review and run code/bootstrap_stata.do explicitly; ordinary builds never install packages."
		exit `dependency_rc'
	}

	global python_exec "$root/.venv/bin/python"
	capture confirm file "$python_exec"
	if (c(rc)) global python_exec "python3"

	capture erase "$temp/stata-project-config.do"
	shell "$python_exec" "$code/4-standardization/export_stata_config.py" ///
		--root "$root" --output "$temp/stata-project-config.do"
	confirm file "$temp/stata-project-config.do"
	do "$temp/stata-project-config.do"


// -------------------------------------------------------------------------
// Python and Banknorm use the project environment and external cache only
// -------------------------------------------------------------------------
	capture confirm file "$root/.venv/bin/python"
	if (!c(rc)) {
		quietly python query
		local python_initialized = r(initialized)
		local python_exec_actual `"`r(execpath)'"'
		if (`python_initialized') assert `"`python_exec_actual'"'==`"$root/.venv/bin/python"'
		else python set exec "$root/.venv/bin/python"
	}
	python: import os; os.environ["BANKNORM_CACHE_DIR"] = "$banknorm_cache"


// -------------------------------------------------------------------------
// Shared graph style
// -------------------------------------------------------------------------
	adopath ++ "$code"
	set scheme cleanplots_ev2
	graph set window fontface "Arial"
	graph set print fontface "Arial"
