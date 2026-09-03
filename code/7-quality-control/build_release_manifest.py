#!/usr/bin/env python3
"""Build or verify a deterministic, hash-based release manifest."""

import argparse
import csv
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from qc_core import canonical_json, content_hash, file_hash
from run_quality_control import baseline_extraction_path, qc_input_receipt, verify_correction_receipt

from histdata_pipeline.config import load_project_config

REPORT_NAMES = (
    "flags.tsv",
    "review_queue.tsv",
    "coverage.tsv",
    "source_support.tsv",
    "summary.json",
    "release_gate.tsv",
    "decision_accounting.tsv",
    "release_accounting.tsv",
    "record-review-differences.tsv",
    "record-review-blocking.tsv",
    "correction-differences.tsv",
    "correction-receipt.json",
)


def resolve_path(root: Path, configured: Any, default: str) -> Path:
    candidate = Path(str(configured or default)).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def read_release_gate(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if len(rows) != 1:
        raise ValueError("release_gate.tsv must contain exactly one result row")
    return dict(rows[0])


def build_payload(root: Path) -> tuple[dict[str, Any], Path]:
    project_config = load_project_config(root, require_initialized=False)
    root = project_config.root
    config_path = root / "project.toml"
    raw = project_config.values
    project = raw.get("project", {})
    quality = raw.get("quality", {})
    slug = str(project.get("slug", project.get("name", root.name)))
    input_path = resolve_path(root, quality.get("input_tsv"), f"data/{slug}.tsv")
    decisions_path = resolve_path(root, quality.get("decisions_tsv"), "manual/qc_decisions.tsv")
    corrections_path = resolve_path(root, quality.get("corrections_tsv"), "manual/record_corrections.tsv")
    output_directory = resolve_path(root, quality.get("output_directory"), "output/quality-control")
    manifest_path = (output_directory / "release_manifest.json").expanduser().absolute()
    project_config.checked_write_path(manifest_path)
    gate = read_release_gate(output_directory / "release_gate.tsv")
    correction_receipt = verify_correction_receipt(root, output_directory)
    current_qc_inputs = qc_input_receipt(
        root=root,
        input_path=input_path,
        decisions_path=decisions_path,
        output_directory=output_directory,
        correction_receipt_signature=str(correction_receipt["receipt_signature"]),
    )
    stale_gate_fields = [
        field
        for field, value in current_qc_inputs.items()
        if gate.get(field, "") != ("" if value is None else str(value))
    ]
    if stale_gate_fields:
        raise ValueError(f"release gate is stale for current inputs: {', '.join(stale_gate_fields)}")

    baseline_input = Path(str(correction_receipt["baseline_input"]["path"]))
    external_baseline = baseline_extraction_path(root, raw)
    final_dta = root / "data" / f"{slug}.dta"

    material_paths = {
        "project_config": config_path,
        "baseline_extraction": external_baseline,
        "reviewed_extraction": baseline_input,
        "analytical_dataset": input_path,
        "analytical_dataset_stata": final_dta,
        **{f"qc_{Path(name).stem}": output_directory / name for name in REPORT_NAMES},
    }
    if decisions_path.exists():
        material_paths["qc_decisions"] = decisions_path
    if corrections_path.exists():
        material_paths["record_corrections"] = corrections_path
    record_review_directory = root / "manual/record-reviews"
    if record_review_directory.is_dir():
        for index, path in enumerate(sorted(record_review_directory.rglob("*.json")), start=1):
            if path.is_file():
                material_paths[f"record_review_{index:05d}"] = path
    missing = [str(path) for path in material_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"release artifacts are missing: {', '.join(missing)}")

    artifacts = {
        label: {
            "path": str(path.resolve().relative_to(root)) if path.resolve().is_relative_to(root) else str(path.resolve()),
            "sha256": file_hash(path),
            "bytes": path.stat().st_size,
        }
        for label, path in sorted(material_paths.items())
    }
    body: dict[str, Any] = {
        "schema_version": 1,
        "project_slug": slug,
        "release_status": gate["release_status"],
        "blocking_open_count": int(gate["blocking_open_count"]),
        "artifacts": artifacts,
    }
    body["manifest_signature"] = content_hash(body)
    return body, manifest_path


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--verify", action="store_true", help="Verify the existing manifest instead of replacing it")
    parser.add_argument("--allow-failed-release", action="store_true", help="Permit a manifest whose release gate is fail")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    expected, manifest_path = build_payload(root)
    if arguments.verify:
        actual = json.loads(manifest_path.read_text(encoding="utf-8"))
        if canonical_json(actual) != canonical_json(expected):
            raise SystemExit("Release manifest verification failed: an artifact, configuration, or gate changed.")
        print(f"Verified release manifest {manifest_path}")
    else:
        write_json_atomic(manifest_path, expected)
        print(f"Wrote release manifest {manifest_path}")
    if expected["release_status"] != "pass" and not arguments.allow_failed_release:
        raise SystemExit("Release manifest records a failed release gate.")


if __name__ == "__main__":
    main()
