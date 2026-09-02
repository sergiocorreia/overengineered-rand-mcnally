import csv
import json
from pathlib import Path

import pytest
from apply_corrections import DIFF_FIELDS, build_correction_receipt, write_json_atomic
from build_release_manifest import build_payload
from run_quality_control import run


def write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_synthetic_cross_section_pipeline_and_manifest_are_deterministic(tmp_path: Path) -> None:
    external_root = tmp_path / "external"
    (tmp_path / "project.toml").write_text(
        f"""
[project]
slug = "synthetic"

[storage]
external_data_root = "{external_root}"

[extraction]
current_tsv = "data-extraction/exports/current/flat.tsv"

[dataset]
shape = "cross-section"
keys = ["record_id"]
value_fields = ["amount"]
record_id_field = "record_id"
source_page_field = "page_id"

[quality]
input_tsv = "data/synthetic.tsv"
decisions_tsv = "manual/qc_decisions.tsv"
corrections_tsv = "manual/record_corrections.tsv"
output_directory = "output/quality-control"
provenance_fields = ["record_id", "page_id", "source_sha256", "contract_signature"]

[quality.cross_section]
group_fields = ["group"]
robust_z = 8

[quality.release]
blocking_severities = ["blocking"]
""".lstrip(),
        encoding="utf-8",
    )
    rows = [
        {
            "record_id": f"r{index}",
            "amount": value,
            "group": "same",
            "page_id": f"p{index}",
            "source_sha256": "source",
            "contract_signature": "contract",
        }
        for index, value in enumerate(["10", "10", "10", "10", "100"], start=1)
    ]
    input_path = tmp_path / "data/synthetic.tsv"
    output = tmp_path / "output/quality-control"
    write_tsv(input_path, rows)
    baseline_path = external_root / "data-extraction/exports/current/flat.tsv"
    write_tsv(baseline_path, rows)
    counterfactual_path = tmp_path / "temp/4-standardization/corrected-extraction.tsv"
    write_tsv(counterfactual_path, rows)
    (tmp_path / "data/synthetic.dta").write_bytes(b"synthetic-stata-fixture\n")
    output.mkdir(parents=True, exist_ok=True)
    (output / "record-review-differences.tsv").write_text("page_id\trecord_id\tfield\n", encoding="utf-8")
    (output / "record-review-blocking.tsv").write_text("page_id\treview_status\n", encoding="utf-8")
    difference_path = output / "correction-differences.tsv"
    difference_path.write_text("\t".join(DIFF_FIELDS) + "\n", encoding="utf-8")
    receipt = build_correction_receipt(
        project_config=tmp_path / "project.toml",
        baseline_input=baseline_path,
        correction_ledger=tmp_path / "manual/record_corrections.tsv",
        repaired_counterfactual=counterfactual_path,
        differences=difference_path,
        correction_count=0,
        record_id_field="record_id",
        source_hash_field="source_sha256",
        contract_field="contract_signature",
    )
    write_json_atomic(output / "correction-receipt.json", receipt)

    status, blockers = run(tmp_path, input_path, tmp_path / "manual/qc_decisions.tsv", output)

    assert status == "pass"
    assert not blockers
    assert (output / "review_queue.tsv").exists()
    assert (output / "source_support.tsv").exists()
    assert (output / "release_accounting.tsv").exists()
    first, path = build_payload(tmp_path)
    second, _ = build_payload(tmp_path)
    assert path == output / "release_manifest.json"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["release_status"] == "pass"
    original_input = input_path.read_bytes()
    input_path.write_bytes(original_input + b"\n")
    with pytest.raises(ValueError, match="release gate is stale"):
        build_payload(tmp_path)
    input_path.write_bytes(original_input)

    original_baseline = baseline_path.read_bytes()
    baseline_path.write_bytes(original_baseline + b"\n")
    with pytest.raises(ValueError, match="stale correction lineage"):
        build_payload(tmp_path)
