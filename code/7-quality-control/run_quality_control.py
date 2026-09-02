#!/usr/bin/env python3
"""Run deterministic QC and write a generated review queue.

The command detects and reports anomalies.  It never applies a correction or
promotes an alternate extraction.  Blocking cases remain blocking until their
evidence-bound decisions are valid.
"""

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from qc_core import FLAG_FIELDS, adjudicate_flags, content_hash, file_hash, load_config, parse_decisions, release_status, run_checks


def read_tsv(path: Path, *, optional: bool = False) -> list[dict[str, str]]:
    if optional and not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _atomic_path(destination: Path) -> tuple[int, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    return descriptor, Path(name)


def write_tsv_atomic(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
    *,
    read_only: bool = False,
) -> None:
    rows = list(rows)
    descriptor, temporary = _atomic_path(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        if read_only:
            temporary.chmod(0o444)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary = _atomic_path(path)
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


def resolve_path(root: Path, configured: Any, default: str) -> Path:
    candidate = Path(str(configured or default)).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def configured_paths(root: Path) -> tuple[Path, Path, Path]:
    import tomllib

    raw = tomllib.loads((root / "project.toml").read_text(encoding="utf-8"))
    quality = raw.get("quality", {})
    project = raw.get("project", {})
    slug = str(project.get("slug", project.get("name", root.name)))
    input_path = resolve_path(root, quality.get("input_tsv"), f"data/{slug}.tsv")
    decisions_path = resolve_path(root, quality.get("decisions_tsv"), "manual/qc_decisions.tsv")
    output_directory = resolve_path(root, quality.get("output_directory"), "output/quality-control")
    return input_path, decisions_path, output_directory


def _metadata_matches(entry: Mapping[str, Any], path: Path, *, required: bool = True) -> None:
    if not isinstance(entry, Mapping):
        raise ValueError(f"correction receipt metadata for {path} is not an object")
    logical = path.expanduser().absolute()
    exists = logical.is_file()
    if required and not exists:
        raise FileNotFoundError(logical)
    expected = {
        "path": str(logical),
        "resolved_path": str(logical.resolve()),
        "sha256": file_hash(logical) if exists else None,
        "bytes": logical.stat().st_size if exists else 0,
    }
    mismatches = [field for field, value in expected.items() if entry.get(field) != value]
    if mismatches:
        raise ValueError(f"stale correction lineage for {logical}: {', '.join(mismatches)} changed")


def baseline_extraction_path(root: Path, raw: Mapping[str, Any]) -> Path:
    storage = raw.get("storage", {})
    extraction = raw.get("extraction", {})
    if not isinstance(storage, Mapping) or not isinstance(extraction, Mapping):
        raise ValueError("project.toml storage and extraction sections must be tables")
    external_root = Path(str(storage.get("external_data_root", ""))).expanduser()
    if not external_root.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    configured = Path(str(extraction.get("current_tsv", "data-extraction/exports/current/flat.tsv")))
    if configured.is_absolute():
        raise ValueError("extraction.current_tsv must be relative to storage.external_data_root")
    external_root = external_root.resolve()
    candidate = (external_root / configured).absolute()
    if not candidate.resolve().is_relative_to(external_root):
        raise ValueError("extraction.current_tsv escapes storage.external_data_root")
    return candidate


def _validate_correction_rows(corrections: Sequence[Mapping[str, str]], differences: Sequence[Mapping[str, str]]) -> None:
    material_corrections = [row for row in corrections if any(str(value).strip() for value in row.values())]
    material_differences = [row for row in differences if any(str(value).strip() for value in row.values())]
    correction_by_id = {
        str(row.get("correction_id", "")).strip(): row
        for row in material_corrections
    }
    difference_by_id = {
        str(row.get("correction_id", "")).strip(): row
        for row in material_differences
    }
    if "" in correction_by_id or "" in difference_by_id:
        raise ValueError("correction and difference rows require correction_id")
    if len(correction_by_id) != len(material_corrections) or len(difference_by_id) != len(material_differences):
        raise ValueError("correction_id values must be unique")
    if correction_by_id.keys() != difference_by_id.keys():
        raise ValueError("correction ledger and applied-differences ledger contain different correction IDs")
    comparisons = {
        "record_id": "record_id",
        "field": "field",
        "expected_old_value": "before",
        "replacement_value": "after",
        "expected_source_hash": "source_hash",
        "expected_contract_signature": "contract_signature",
        "evidence_page": "evidence_page",
        "reason": "reason",
        "review_date": "review_date",
    }
    for correction_id, correction in correction_by_id.items():
        difference = difference_by_id[correction_id]
        mismatches = [
            correction_field
            for correction_field, difference_field in comparisons.items()
            if str(correction.get(correction_field, "")) != str(difference.get(difference_field, ""))
        ]
        if mismatches:
            raise ValueError(f"applied difference {correction_id} disagrees with its correction ledger: {', '.join(mismatches)}")


def verify_correction_receipt(root: Path, output_directory: Path) -> dict[str, Any]:
    """Reject a changed baseline, overlay, counterfactual, or keyed difference."""

    import tomllib

    receipt_path = output_directory / "correction-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("correction-receipt.json has an unsupported schema")
    signature = str(receipt.get("receipt_signature", ""))
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_signature"}
    if signature != content_hash(unsigned):
        raise ValueError("correction-receipt.json signature is invalid")

    config_path = root / "project.toml"
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    quality = raw.get("quality", {})
    if not isinstance(quality, Mapping):
        raise ValueError("project.toml [quality] must be a table")
    corrections_path = resolve_path(root, quality.get("corrections_tsv"), "manual/record_corrections.tsv")
    difference_path = output_directory / "correction-differences.tsv"

    _metadata_matches(receipt.get("project_config", {}), config_path)
    correction_baseline = receipt.get("baseline_input", {})
    if not isinstance(correction_baseline, Mapping) or not correction_baseline.get("path"):
        raise ValueError("correction receipt has no baseline input path")
    _metadata_matches(correction_baseline, Path(str(correction_baseline["path"])))
    _metadata_matches(receipt.get("correction_ledger", {}), corrections_path, required=False)
    counterfactual = receipt.get("repaired_counterfactual", {})
    if not isinstance(counterfactual, Mapping) or not counterfactual.get("path"):
        raise ValueError("correction receipt has no repaired counterfactual path")
    _metadata_matches(counterfactual, Path(str(counterfactual["path"])))
    _metadata_matches(receipt.get("differences", {}), difference_path)

    correction_rows = read_tsv(corrections_path, optional=True)
    difference_rows = read_tsv(difference_path)
    _validate_correction_rows(correction_rows, difference_rows)
    material_corrections = [row for row in correction_rows if any(str(value).strip() for value in row.values())]
    material_differences = [row for row in difference_rows if any(str(value).strip() for value in row.values())]
    if int(receipt.get("correction_count", -1)) != len(material_corrections) or len(material_corrections) != len(material_differences):
        raise ValueError("correction receipt counts do not match the current keyed ledgers")
    return receipt


def qc_input_receipt(
    *,
    root: Path,
    input_path: Path,
    decisions_path: Path,
    output_directory: Path,
    correction_receipt_signature: str,
) -> dict[str, Any]:
    import tomllib

    raw = tomllib.loads((root / "project.toml").read_text(encoding="utf-8"))
    baseline = baseline_extraction_path(root, raw)
    review_differences = output_directory / "record-review-differences.tsv"
    review_blockers = output_directory / "record-review-blocking.tsv"
    if not review_differences.is_file() or not review_blockers.is_file():
        raise FileNotFoundError("record-review audit outputs are required before quality control")
    manual_reviews = root / "manual/record-reviews"
    manual_review_hashes = {
        str(path.relative_to(root)): file_hash(path)
        for path in sorted(manual_reviews.rglob("*.json"))
        if path.is_file()
    } if manual_reviews.is_dir() else {}
    current_pointer = root / "data/extraction_current.json"
    run_receipt = baseline.parent / "run.json"
    payload: dict[str, Any] = {
        "project_config_sha256": file_hash(root / "project.toml"),
        "input_sha256": file_hash(input_path),
        "decisions_sha256": file_hash(decisions_path) if decisions_path.is_file() else None,
        "baseline_extraction_sha256": file_hash(baseline),
        "baseline_extraction_resolved_path": str(baseline.resolve()),
        "current_pointer_sha256": file_hash(current_pointer) if current_pointer.is_file() else None,
        "extraction_run_receipt_sha256": file_hash(run_receipt) if run_receipt.is_file() else None,
        "record_review_differences_sha256": file_hash(review_differences),
        "record_review_blockers_sha256": file_hash(review_blockers),
        "record_review_decisions_signature": content_hash(manual_review_hashes),
        "correction_receipt_signature": correction_receipt_signature,
    }
    payload["qc_input_signature"] = content_hash(payload)
    return payload


def build_summary(
    rows: Sequence[Mapping[str, str]],
    flags: Sequence[Mapping[str, str]],
    status: str,
    blockers: Sequence[str],
    decisions: Mapping[str, Any],
) -> dict[str, Any]:
    by_check = Counter(flag["check_type"] for flag in flags)
    by_severity = Counter(flag["severity"] for flag in flags)
    by_disposition = Counter(flag["disposition"] for flag in flags)
    return {
        "release_status": status,
        "row_count": len(rows),
        "flag_count": len(flags),
        "blocking_open_count": len(blockers),
        "blocking_case_ids": list(blockers),
        "decision_count": len(decisions),
        "counts_by_check": dict(sorted(by_check.items())),
        "counts_by_severity": dict(sorted(by_severity.items())),
        "counts_by_disposition": dict(sorted(by_disposition.items())),
    }


def build_source_support(
    rows: Sequence[Mapping[str, str]],
    flags: Sequence[Mapping[str, str]],
    *,
    page_field: str,
    blocking_severities: Sequence[str],
) -> list[dict[str, str]]:
    pages: dict[str, dict[str, Any]] = {}
    for row in rows:
        page_id = str(row.get(page_field, ""))
        if not page_id:
            continue
        entry = pages.setdefault(page_id, {"rows": 0, "hashes": set(), "flags": 0, "open_blocking": 0})
        entry["rows"] += 1
        source_hash = str(row.get("source_sha256", ""))
        if source_hash:
            entry["hashes"].add(source_hash)
    for flag in flags:
        page_id = flag.get("page_id", "")
        if not page_id:
            continue
        entry = pages.setdefault(page_id, {"rows": 0, "hashes": set(), "flags": 0, "open_blocking": 0})
        entry["flags"] += 1
        if flag.get("severity") in blocking_severities and flag.get("decision_status") != "reviewed":
            entry["open_blocking"] += 1
    return [
        {
            "page_id": page_id,
            "source_sha256": " ".join(sorted(entry["hashes"])),
            "row_count": str(entry["rows"]),
            "flag_count": str(entry["flags"]),
            "open_blocking_count": str(entry["open_blocking"]),
        }
        for page_id, entry in sorted(pages.items())
    ]


def run(root: Path, input_path: Path, decisions_path: Path, output_directory: Path) -> tuple[str, list[str]]:
    config = load_config(root / "project.toml")
    correction_receipt = verify_correction_receipt(root, output_directory)
    input_receipt = qc_input_receipt(
        root=root,
        input_path=input_path,
        decisions_path=decisions_path,
        output_directory=output_directory,
        correction_receipt_signature=str(correction_receipt["receipt_signature"]),
    )
    rows = read_tsv(input_path)
    decision_rows = read_tsv(decisions_path, optional=True)
    difference_rows = read_tsv(output_directory / "correction-differences.tsv", optional=True)
    applied_corrections = {
        str(row.get("correction_id", "")).strip(): row
        for row in difference_rows
        if str(row.get("correction_id", "")).strip()
    }
    decisions = parse_decisions(decision_rows)
    detected, coverage = run_checks(rows, config)
    flags, _ = adjudicate_flags(
        detected,
        decisions,
        config,
        applied_corrections=applied_corrections,
    )
    status, blockers = release_status(flags, config)

    output_directory.mkdir(parents=True, exist_ok=True)
    write_tsv_atomic(output_directory / "flags.tsv", flags, FLAG_FIELDS)
    queue = [flag for flag in flags if flag["decision_status"] != "reviewed"]
    write_tsv_atomic(output_directory / "review_queue.tsv", queue, FLAG_FIELDS, read_only=True)
    write_tsv_atomic(output_directory / "coverage.tsv", coverage, ("period", "entity_count", "is_observed"))
    summary = build_summary(rows, flags, status, blockers, decisions)
    summary.update(input_receipt)
    write_json_atomic(output_directory / "summary.json", summary)
    support = build_source_support(
        rows,
        flags,
        page_field=config.source_page_field,
        blocking_severities=config.blocking_severities,
    )
    write_tsv_atomic(
        output_directory / "source_support.tsv",
        support,
        ("page_id", "source_sha256", "row_count", "flag_count", "open_blocking_count"),
    )
    write_tsv_atomic(
        output_directory / "release_gate.tsv",
        [
            {
                "release_status": status,
                "blocking_open_count": len(blockers),
                "blocking_case_ids": " ".join(blockers),
                **input_receipt,
            }
        ],
        (
            "release_status",
            "blocking_open_count",
            "blocking_case_ids",
            "project_config_sha256",
            "input_sha256",
            "decisions_sha256",
            "baseline_extraction_sha256",
            "baseline_extraction_resolved_path",
            "current_pointer_sha256",
            "extraction_run_receipt_sha256",
            "record_review_differences_sha256",
            "record_review_blockers_sha256",
            "record_review_decisions_signature",
            "correction_receipt_signature",
            "qc_input_signature",
        ),
    )
    historical_decisions = [
        {
            "case_id": case_id,
            "current_case": "1" if case_id in {flag["case_id"] for flag in detected} else "0",
            "disposition": decision.disposition,
            "expected_evidence_hash": decision.expected_evidence_hash,
        }
        for case_id, decision in sorted(decisions.items())
    ]
    write_tsv_atomic(
        output_directory / "decision_accounting.tsv",
        historical_decisions,
        ("case_id", "current_case", "disposition", "expected_evidence_hash"),
    )
    write_tsv_atomic(
        output_directory / "release_accounting.tsv",
        [
            {"metric": "analytical_rows", "value": len(rows)},
            {"metric": "detected_cases", "value": len(flags)},
            {"metric": "reviewed_cases", "value": sum(flag["decision_status"] == "reviewed" for flag in flags)},
            {"metric": "open_blocking_cases", "value": len(blockers)},
            {"metric": "release_status", "value": status},
        ],
        ("metric", "value"),
    )
    return status, blockers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root containing project.toml")
    parser.add_argument("--input", type=Path, help="Override quality.input_tsv")
    parser.add_argument("--decisions", type=Path, help="Override quality.decisions_tsv")
    parser.add_argument("--output-directory", type=Path, help="Override quality.output_directory")
    parser.add_argument("--allow-failed-release", action="store_true", help="Write reports but return success when blocking cases remain")
    arguments = parser.parse_args()

    root = arguments.root.resolve()
    configured_input, configured_decisions, configured_output = configured_paths(root)
    status, blockers = run(
        root,
        (arguments.input or configured_input).resolve(),
        (arguments.decisions or configured_decisions).resolve(),
        (arguments.output_directory or configured_output).resolve(),
    )
    print(f"Quality-control release status: {status}; open blocking cases: {len(blockers)}")
    if status == "fail" and not arguments.allow_failed_release:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
