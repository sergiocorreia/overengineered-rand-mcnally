#!/usr/bin/env python3
"""Apply evidence-bound field corrections without changing extraction caches."""

import argparse
import csv
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from histdata_pipeline.config import ProjectConfig, load_project_config

DIFF_FIELDS = (
    "correction_id",
    "record_id",
    "field",
    "before",
    "after",
    "source_hash",
    "contract_signature",
    "evidence_page",
    "reason",
    "review_date",
)


@dataclass(frozen=True)
class Correction:
    correction_id: str
    record_id: str
    field: str
    expected_old_value: str
    replacement_value: str
    expected_source_hash: str
    expected_contract_signature: str
    evidence_page: str
    reason: str
    review_date: str


def read_tsv(path: Path, *, optional: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    if optional and not path.exists():
        return [], []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no TSV header")
        return [dict(row) for row in reader], list(reader.fieldnames)


def write_tsv_atomic(path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def file_receipt(path: Path, *, required: bool = True) -> dict[str, Any]:
    """Describe logical and resolved paths so a moved ``current`` pointer is stale."""

    logical = path.expanduser().absolute()
    exists = logical.is_file()
    if required and not exists:
        raise FileNotFoundError(logical)
    return {
        "path": str(logical),
        "resolved_path": str(logical.resolve()),
        "sha256": file_hash(logical) if exists else None,
        "bytes": logical.stat().st_size if exists else 0,
    }


def build_correction_receipt(
    *,
    project_config: Path,
    baseline_input: Path,
    correction_ledger: Path,
    repaired_counterfactual: Path,
    differences: Path,
    correction_count: int,
    record_id_field: str,
    source_hash_field: str,
    contract_field: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "project_config": file_receipt(project_config),
        "baseline_input": file_receipt(baseline_input),
        "correction_ledger": file_receipt(correction_ledger, required=False),
        "repaired_counterfactual": file_receipt(repaired_counterfactual),
        "differences": file_receipt(differences),
        "correction_count": correction_count,
        "record_id_field": record_id_field,
        "source_hash_field": source_hash_field,
        "contract_signature_field": contract_field,
    }
    body["receipt_signature"] = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return body


def parse_corrections(rows: Iterable[Mapping[str, Any]]) -> list[Correction]:
    corrections: list[Correction] = []
    identifiers: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for row_number, row in enumerate(rows, start=2):
        if not any(str(value).strip() for value in row.values()):
            continue
        disposition = str(row.get("disposition", "corrected")).strip()
        if disposition != "corrected":
            raise ValueError(f"correction row {row_number} must have disposition='corrected'")
        correction = Correction(
            correction_id=str(row.get("correction_id", "")).strip(),
            record_id=str(row.get("record_id", "")).strip(),
            field=str(row.get("field", "")).strip(),
            expected_old_value=str(row.get("expected_old_value", "")),
            replacement_value=str(row.get("replacement_value", "")),
            expected_source_hash=str(row.get("expected_source_hash", row.get("source_hash", ""))).strip(),
            expected_contract_signature=str(row.get("expected_contract_signature", row.get("contract_signature", ""))).strip(),
            evidence_page=str(row.get("evidence_page", "")).strip(),
            reason=str(row.get("reason", "")).strip(),
            review_date=str(row.get("review_date", "")).strip(),
        )
        missing = [
            field_name
            for field_name in (
                "correction_id",
                "record_id",
                "field",
                "expected_source_hash",
                "expected_contract_signature",
                "evidence_page",
                "reason",
                "review_date",
            )
            if not getattr(correction, field_name)
        ]
        if missing:
            raise ValueError(f"correction row {row_number} is missing: {', '.join(missing)}")
        if correction.expected_old_value == correction.replacement_value:
            raise ValueError(f"correction row {row_number} is a no-op; before and after values are identical")
        try:
            date.fromisoformat(correction.review_date)
        except ValueError as error:
            raise ValueError(f"correction row {row_number} has a non-ISO review_date") from error
        if correction.correction_id in identifiers:
            raise ValueError(f"correction_id {correction.correction_id} is repeated")
        target = (correction.record_id, correction.field)
        if target in targets:
            raise ValueError(f"record/field target {target!r} is corrected more than once")
        identifiers.add(correction.correction_id)
        targets.add(target)
        corrections.append(correction)
    return corrections


def apply_corrections(
    rows: Sequence[Mapping[str, str]],
    corrections: Sequence[Correction],
    *,
    record_id_field: str,
    source_hash_field: str = "source_sha256",
    contract_field: str = "contract_signature",
    protected_fields: Iterable[str] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    result = [dict(row) for row in rows]
    by_record: dict[str, dict[str, str]] = {}
    for row in result:
        record_id = row.get(record_id_field, "")
        if not record_id:
            raise ValueError(f"input row has blank {record_id_field}")
        if record_id in by_record:
            raise ValueError(f"input {record_id_field} {record_id!r} is repeated")
        by_record[record_id] = row

    protected = {record_id_field, source_hash_field, contract_field, *protected_fields}
    differences: list[dict[str, str]] = []
    for correction in corrections:
        if correction.field in protected:
            raise ValueError(f"correction {correction.correction_id} targets protected field {correction.field}")
        row = by_record.get(correction.record_id)
        if row is None:
            raise ValueError(f"correction {correction.correction_id} targets missing record {correction.record_id}")
        if correction.field not in row:
            raise ValueError(f"correction {correction.correction_id} targets missing field {correction.field}")
        checks = {
            "old value": (row[correction.field], correction.expected_old_value),
            "source hash": (row.get(source_hash_field, ""), correction.expected_source_hash),
            "contract signature": (row.get(contract_field, ""), correction.expected_contract_signature),
        }
        stale = [name for name, (actual, expected) in checks.items() if actual != expected]
        if stale:
            details = "; ".join(f"{name}: actual={checks[name][0]!r}, expected={checks[name][1]!r}" for name in stale)
            raise ValueError(f"stale correction {correction.correction_id}: {details}")
        before = row[correction.field]
        row[correction.field] = correction.replacement_value
        differences.append(
            {
                "correction_id": correction.correction_id,
                "record_id": correction.record_id,
                "field": correction.field,
                "before": before,
                "after": correction.replacement_value,
                "source_hash": correction.expected_source_hash,
                "contract_signature": correction.expected_contract_signature,
                "evidence_page": correction.evidence_page,
                "reason": correction.reason,
                "review_date": correction.review_date,
            }
        )
    return result, differences


def resolve_path(root: Path, configured: Any, default: str) -> Path:
    candidate = Path(str(configured or default)).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def checked_output_paths(config: ProjectConfig, output: Path, diff: Path, receipt: Path) -> tuple[Path, Path, Path]:
    """Validate the complete write set before publishing any correction artifact."""

    return tuple(config.checked_write_path(path) for path in (output, diff, receipt))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--input", type=Path, required=True, help="Immutable flat extraction TSV")
    parser.add_argument("--output", type=Path, required=True, help="Repaired counterfactual TSV")
    parser.add_argument("--diff", type=Path, required=True, help="Exact keyed before/after differences")
    parser.add_argument("--corrections", type=Path, help="Override quality.corrections_tsv")
    parser.add_argument("--receipt", type=Path, help="Hash-bound lineage receipt; defaults beside --diff")
    arguments = parser.parse_args()

    config = load_project_config(arguments.root)
    root = config.root
    dataset = config.table("dataset")
    quality = config.table("quality")
    record_id_field = str(dataset.get("record_id_field", "record_id"))
    source_hash_field = str(quality.get("source_hash_field", "source_sha256"))
    contract_field = str(quality.get("contract_signature_field", "contract_signature"))
    protected_fields = tuple(
        str(field)
        for field in quality.get(
            "provenance_fields",
            (
                record_id_field,
                str(dataset.get("source_page_field", "page_id")),
                source_hash_field,
                "render_sha256",
                contract_field,
                "extraction_status",
            ),
        )
    )
    corrections_path = arguments.corrections or resolve_path(root, quality.get("corrections_tsv"), "manual/record_corrections.tsv")

    input_path = arguments.input.expanduser().absolute()
    requested_output = arguments.output.expanduser().absolute()
    requested_diff = arguments.diff.expanduser().absolute()
    corrections_path = corrections_path.expanduser().absolute()
    requested_receipt = (arguments.receipt or requested_diff.with_name("correction-receipt.json")).expanduser().absolute()
    output_path, diff_path, receipt_path = checked_output_paths(config, requested_output, requested_diff, requested_receipt)
    rows, fieldnames = read_tsv(input_path)
    correction_rows, _ = read_tsv(corrections_path, optional=True)
    corrections = parse_corrections(correction_rows)
    corrected, differences = apply_corrections(
        rows,
        corrections,
        record_id_field=record_id_field,
        source_hash_field=source_hash_field,
        contract_field=contract_field,
        protected_fields=protected_fields,
    )
    write_tsv_atomic(output_path, corrected, fieldnames)
    write_tsv_atomic(diff_path, differences, DIFF_FIELDS)
    receipt = build_correction_receipt(
        project_config=root / "project.toml",
        baseline_input=input_path,
        correction_ledger=corrections_path,
        repaired_counterfactual=output_path,
        differences=diff_path,
        correction_count=len(corrections),
        record_id_field=record_id_field,
        source_hash_field=source_hash_field,
        contract_field=contract_field,
    )
    write_json_atomic(receipt_path, receipt)
    print(f"Applied {len(differences)} stale-checked correction(s); extraction input was not modified. Wrote lineage receipt {receipt_path}.")


if __name__ == "__main__":
    main()
