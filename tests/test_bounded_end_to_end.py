"""Exercise the complete synthetic pipeline without network or model calls."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest

from histdata_pipeline.provenance import stable_hash
from tools.initialize_project import render_files, replace_setting


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError(f"Expected one configuration marker: {old}")
    return text.replace(old, new)


def _run(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project / "src")
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(f"Command failed ({' '.join(arguments)}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result


def _write_tsv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _make_project(template: Path, project: Path, external: Path) -> None:
    shutil.copytree(
        template,
        project,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.pyc",
            "*.log",
        ),
    )
    project_config = project / "project.toml"
    project_config.write_text(
        replace_setting(project_config.read_text(encoding="utf-8"), "template", "initialized", "false"),
        encoding="utf-8",
    )
    rendered = render_files(
        project,
        name="Synthetic Historical Table",
        slug="synthetic-historical-table",
        description="Offline two-page fixture for the complete extraction pipeline.",
        dataset_shape="panel",
        pdf_storage="project",
        external_root=external,
    )
    for path, content in rendered.items():
        path.write_text(content, encoding="utf-8")

    config_path = project / "project.toml"
    config = config_path.read_text(encoding="utf-8")
    config = replace_setting(config, "dataset", "keys", '["entity", "period"]')
    config = replace_setting(config, "dataset", "entity_keys", '["entity"]')
    config = replace_setting(config, "dataset", "time_key", '"period"')
    config = replace_setting(config, "dataset", "value_fields", '["value"]')
    config = replace_setting(config, "quality.panel", "expected_frequency", "1.0")
    config_path.write_text(config, encoding="utf-8")

    prompt_path = project / "code/3-extraction/definitions/prompt.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    replacements = {
        "[ENTITY × PERIOD × MEASURE]": "city by date by printed amount",
        "[TARGET TABLE OR FORM AREA]": "the synthetic city table",
        "[EXACT ROWS, ENTITIES, COLUMNS, ACCOUNT TYPES, AND PERIODS]": "the City A row, date, and amount",
        "[TOTALS, SUBTOTALS, SUMMARY PANELS, ADMINISTRATIVE MARKS, AND NEIGHBORING MATERIAL]": "the index page and headings",
    }
    for marker, replacement in replacements.items():
        prompt = _replace_once(prompt, marker, replacement)
    prompt_path.write_text(prompt, encoding="utf-8")
    schema_path = project / "code/3-extraction/definitions/schema.py"
    schema_path.write_text(
        _replace_once(
            schema_path.read_text(encoding="utf-8"),
            "Replace the example fields with the smallest flat analytical record for the project.",
            "Synthetic city-date-amount extraction contract.",
        ),
        encoding="utf-8",
    )

    for directory in ("data", "input", "manual", "output", "sources/pdfs", "temp"):
        (project / directory).mkdir(parents=True, exist_ok=True)
    external.mkdir(parents=True)
    (project / ".venv").symlink_to(template / ".venv", target_is_directory=True)


def _make_pdf(path: Path) -> str:
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "TARGET TABLE\nCity A   1900-01-01   10")
    second = document.new_page()
    second.insert_text((72, 72), "INDEX PAGE\nNo target records")
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_current_contract_cache(project: Path) -> dict[str, object]:
    helper = r"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root / "code/3-extraction"))

from contract import build_contract
from extract_records import _base_envelope
from histdata_pipeline.config import load_project_config
from histdata_pipeline.provenance import atomic_write_json
from pipeline import page_cache_path, read_selected_pages, render_page, resolve_source, write_page_cache

config = load_project_config(root)
contract = build_contract(config, service="flex")
page = read_selected_pages(root / "data/selected_pages.tsv")[0]
source = resolve_source(config, page)
rendered = render_page(config, page, verified_source=source)
extraction = contract.schema.model_validate(
    {
        "document_status": "target",
        "scan_quality": "clear",
        "records": [
            {
                "entity_raw": "City A",
                "entity": "City A",
                "period_raw": "1900-01-01",
                "period": "1900-01-01",
                "value_raw": "10",
                "value": "10",
                "value_status": "observed",
                "correction_raw": None,
                "note": None,
                "supplemental_facts": [],
                "uncertain_fields": [],
            }
        ],
        "page_note": None,
        "unmapped_text": [],
    }
).model_dump(mode="json")
now = datetime.now(UTC).isoformat()
envelope = {
    **_base_envelope(page, contract, rendered.sha256, rendered.path),
    "status": "ok",
    "error_type": "",
    "error_message": "",
    "extraction": extraction,
    "usage": {"input_tokens": 1, "output_tokens": 1, "thoughts_tokens": 0, "total_tokens": 2},
    "provider_call_started": False,
    "usage_known": True,
    "started_at": now,
    "completed_at": now,
    "duration_seconds": 0.0,
}
cache = page_cache_path(
    config,
    contract_signature=contract.signature,
    page=page,
    render_sha256=rendered.sha256,
)
write_page_cache(cache, envelope)
atomic_write_json(
    root / "temp/synthetic-seed.json",
    {
        "contract_signature": contract.signature,
        "page_id": page.page_id,
        "source_sha256": page.source_sha256,
        "render_sha256": rendered.sha256,
        "extraction": extraction,
    },
)
"""
    _run(project, "-c", helper, str(project))
    return json.loads((project / "temp/synthetic-seed.json").read_text(encoding="utf-8"))


def test_bounded_offline_pipeline_from_pdf_to_verified_release(tmp_path: Path) -> None:
    template = Path(__file__).resolve().parents[1]
    project = tmp_path / "synthetic-project"
    external = tmp_path / "synthetic-external"
    _make_project(template, project, external)

    pdf = project / "sources/pdfs/book.pdf"
    source_hash = _make_pdf(pdf)
    source_fields = (project / "sources/source_manifest.tsv").read_text(encoding="utf-8").splitlines()[0].split("\t")
    _write_tsv(
        project / "sources/source_manifest.tsv",
        source_fields,
        [
            {
                "source_order": 1,
                "source_id": "synthetic-book",
                "provider": "synthetic",
                "provider_id": "fixture-1",
                "title": "Synthetic Book",
                "source_date": "1900-01-01",
                "item_url": "",
                "download_url": "",
                "acquisition_method": "manual",
                "filename": "book.pdf",
                "expected_sha256": source_hash,
                "min_pages": 2,
                "max_pages": 2,
                "notes": "Generated offline fixture",
            }
        ],
    )
    _run(project, "code/1-download/download_sources.py", "--inventory-only", "--all")
    _run(project, "code/2-inventory/build_manifest.py", "--all")

    with (project / "data/pages.tsv").open(encoding="utf-8", newline="") as source:
        pages = list(csv.DictReader(source, delimiter="\t"))
    assert len(pages) == 2
    _write_tsv(
        project / "manual/page_overrides.tsv",
        ["page_id", "expected_source_sha256", "classification", "notes"],
        [
            {
                "page_id": pages[0]["page_id"],
                "expected_source_sha256": source_hash,
                "classification": "selected",
                "notes": "Visible synthetic target table",
            },
            {
                "page_id": pages[1]["page_id"],
                "expected_source_sha256": source_hash,
                "classification": "excluded",
                "notes": "Visible synthetic index page",
            },
        ],
    )
    _write_tsv(
        project / "manual/gold/page_selection.tsv",
        ["page_id", "expected_classification", "risk_labels", "notes"],
        [
            {
                "page_id": pages[0]["page_id"],
                "expected_classification": "selected",
                "risk_labels": "positive,target-table",
                "notes": "Independently inspected fixture",
            },
            {
                "page_id": pages[1]["page_id"],
                "expected_classification": "excluded",
                "risk_labels": "negative,index",
                "notes": "Independently inspected fixture",
            },
        ],
    )
    _run(project, "code/2-inventory/export_selected_pages.py")

    seed = _seed_current_contract_cache(project)
    _write_tsv(
        project / "code/3-extraction/fixtures/calibration_pages.tsv",
        ["page_id", "coverage_labels", "notes"],
        [{"page_id": seed["page_id"], "coverage_labels": "clear-positive", "notes": "Synthetic gold"}],
    )
    _write_tsv(
        project / "code/3-extraction/fixtures/trial_pages.tsv",
        ["page_id", "coverage_labels", "notes"],
        [{"page_id": seed["page_id"], "coverage_labels": "cache-only-trial", "notes": "Synthetic trial"}],
    )
    (project / "manual/gold/gold.jsonl").write_text(
        json.dumps(
            {
                "page_id": seed["page_id"],
                "source_sha256": seed["source_sha256"],
                "extraction": seed["extraction"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_tsv(
        project / "manual/gold/expectations.tsv",
        ["page_id", "record_index", "field", "expected_value", "critical"],
        [
            {
                "page_id": seed["page_id"],
                "record_index": 1,
                "field": "value_raw",
                "expected_value": "10",
                "critical": "true",
            }
        ],
    )
    _run(project, "code/3-extraction/validate_gold.py")
    gate_path = project / "manual/gold/production_gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate.update(
        {
            "render_signature": stable_hash(
                [
                    {
                        "page_id": seed["page_id"],
                        "source_sha256": seed["source_sha256"],
                        "render_sha256": seed["render_sha256"],
                    }
                ]
            ),
            "approved_max_requests": 0,
            "trial_passed": True,
            "cache_reuse_passed": True,
            "cost_reviewed": True,
            "cost_reviewed_at": datetime.now(UTC).isoformat(),
            "notes": "Synthetic offline fixture; no provider request was made.",
        }
    )
    gate_path.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    extraction = _run(project, "code/3-extraction/extract_records.py", "--all", "--cache-only", "--flex")
    result = json.loads(extraction.stdout[extraction.stdout.index("{") :])
    assert result["model_requests"] == 0
    assert result["current_updated"] is True
    _run(project, "code/3-extraction/verify_run.py")

    if "[restoration]" in (template / "project.toml").read_text(encoding="utf-8"):
        pytest.skip("The generic Stata release fixture is outside this initialized OCR-restoration phase")

    stata = Path("/usr/local/stata19/stata-mp")
    if not stata.is_file():
        pytest.skip("Stata 19 is unavailable; PDF-to-current-extraction smoke passed")
    build = subprocess.run(
        [str(stata), "-b", "do", "code/build.do"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0
    log = (project / "build.log").read_text(encoding="utf-8")
    assert "Complete seven-stage build passed the release gate." in log
    release = json.loads((project / "output/7-quality-control/release_manifest.json").read_text(encoding="utf-8"))
    assert release["release_status"] == "pass"
    assert (project / "data/synthetic-historical-table.dta").is_file()
    assert (project / "data/synthetic-historical-table.tsv").is_file()
