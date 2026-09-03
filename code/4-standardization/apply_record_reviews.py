#!/usr/bin/env python3
"""Apply accepted, hash-pinned record reviews without changing model caches."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from histdata_pipeline.config import ProjectConfig, load_project_config
from histdata_pipeline.provenance import stable_hash

STAGE_THREE = Path(__file__).resolve().parents[1] / "3-extraction"
if str(STAGE_THREE) not in sys.path:
    sys.path.insert(0, str(STAGE_THREE))
from contract import build_contract  # noqa: E402
from pipeline import flatten_envelope, write_tsv  # noqa: E402
from run_integrity import verify_current  # noqa: E402

DIFF_FIELDS = (
    "page_id",
    "record_id",
    "field",
    "operation",
    "before",
    "after",
    "source_sha256",
    "render_sha256",
    "contract_signature",
    "model_extraction_sha256",
    "reviewed_extraction_sha256",
    "review_notes",
    "reviewed_at",
)
FLAG_FIELDS = (
    "page_id",
    "review_status",
    "source_sha256",
    "render_sha256",
    "contract_signature",
    "review_notes",
    "reviewed_at",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _review_files(root: Path, contract_signature: str) -> list[Path]:
    directory = root / "manual" / "record-reviews" / contract_signature
    return sorted(path for path in directory.glob("*.json") if path.is_file()) if directory.is_dir() else []


def _field_differences(
    baseline: list[dict[str, Any]],
    reviewed: list[dict[str, Any]],
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    before = {str(row.get("record_id", "")): row for row in baseline if row.get("record_id")}
    after = {str(row.get("record_id", "")): row for row in reviewed if row.get("record_id")}
    differences: list[dict[str, Any]] = []
    common = {
        key: decision.get(key, "")
        for key in (
            "page_id",
            "source_sha256",
            "render_sha256",
            "contract_signature",
            "model_extraction_sha256",
            "reviewed_extraction_sha256",
            "review_notes",
            "reviewed_at",
        )
    }
    for record_id in sorted(set(before) | set(after)):
        if record_id not in before:
            differences.append({**common, "record_id": record_id, "field": "", "operation": "row_added", "before": "", "after": after[record_id]})
            continue
        if record_id not in after:
            differences.append({**common, "record_id": record_id, "field": "", "operation": "row_removed", "before": before[record_id], "after": ""})
            continue
        for field in sorted(set(before[record_id]) | set(after[record_id])):
            if before[record_id].get(field) != after[record_id].get(field):
                differences.append(
                    {
                        **common,
                        "record_id": record_id,
                        "field": field,
                        "operation": "field_changed",
                        "before": before[record_id].get(field, ""),
                        "after": after[record_id].get(field, ""),
                    }
                )
    return differences


def apply_reviews(
    envelopes: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    schema: type[Any],
    record_list_field: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_page = {str(envelope["page_id"]): envelope for envelope in envelopes}
    reviewed_by_page: dict[str, dict[str, Any]] = {}
    seen_pages: set[str] = set()
    flags: list[dict[str, Any]] = []
    for decision in decisions:
        page_id = str(decision.get("page_id", ""))
        baseline = by_page.get(page_id)
        if baseline is None or page_id in seen_pages:
            raise ValueError(f"Record review targets an unknown or duplicate page: {page_id}")
        seen_pages.add(page_id)
        for field in ("source_sha256", "render_sha256", "contract_signature"):
            if decision.get(field) != baseline.get(field):
                raise ValueError(f"Stale record review for {page_id}: {field}")
        baseline_hash = stable_hash(baseline.get("extraction"))
        if decision.get("model_extraction_sha256") != baseline_hash:
            raise ValueError(f"Stale record review for {page_id}: baseline extraction")
        extraction = schema.model_validate(decision.get("extraction")).model_dump(mode="json")
        if decision.get("reviewed_extraction_sha256") != stable_hash(extraction):
            raise ValueError(f"Record review content hash mismatch for {page_id}")
        status = str(decision.get("review_status", ""))
        notes = str(decision.get("review_notes", "")).strip()
        reviewed_at = str(decision.get("reviewed_at", "")).strip()
        try:
            reviewed_datetime = datetime.fromisoformat(reviewed_at)
        except ValueError as error:
            raise ValueError(f"Record review for {page_id} has an invalid reviewed_at timestamp") from error
        if reviewed_datetime.tzinfo is None:
            raise ValueError(f"Record review for {page_id} must use a timezone-aware reviewed_at timestamp")
        if (stable_hash(extraction) != baseline_hash or status != "accepted") and not notes:
            raise ValueError(f"Changed, flagged, or excluded record review for {page_id} requires an evidence note")
        if status == "accepted":
            reviewed_by_page[page_id] = {**baseline, "extraction": extraction}
        elif status in {"flagged", "excluded"}:
            flags.append({field: decision.get(field, "") for field in FLAG_FIELDS})
        else:
            raise ValueError(f"Invalid record review status for {page_id}: {status}")

    output_rows: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    decisions_by_page = {str(row["page_id"]): row for row in decisions}
    for baseline in envelopes:
        page_id = str(baseline["page_id"])
        reviewed = reviewed_by_page.get(page_id, baseline)
        baseline_rows = flatten_envelope(baseline, record_list_field=record_list_field)
        reviewed_rows = flatten_envelope(reviewed, record_list_field=record_list_field)
        output_rows.extend(reviewed_rows)
        if page_id in reviewed_by_page:
            differences.extend(_field_differences(baseline_rows, reviewed_rows, decisions_by_page[page_id]))
    return output_rows, differences, flags


def checked_output_paths(config: ProjectConfig, output: Path, diff: Path, flags: Path) -> tuple[Path, Path, Path]:
    """Validate the complete write set before publishing any review artifact."""

    return tuple(config.checked_write_path(path) for path in (output, diff, flags))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diff", type=Path, required=True)
    parser.add_argument("--flags", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_project_config()
        output_path, diff_path, flags_path = checked_output_paths(config, args.output, args.diff, args.flags)
        export_root = config.external_path("export_subdirectory", "data-extraction/exports")
        run_directory = (export_root / "current").resolve(strict=True)
        run = verify_current(config, run_directory)
        contract = build_contract(config, service=str(run["service"]))
        if contract.signature != run["contract_signature"]:
            raise ValueError("Current extraction contract differs from the current project definition")
        envelopes = _read_jsonl(run_directory / "nested.jsonl")
        decisions = [json.loads(path.read_text(encoding="utf-8")) for path in _review_files(config.root, contract.signature)]
        rows, differences, flags = apply_reviews(
            envelopes,
            decisions,
            schema=contract.schema,
            record_list_field=str(config.table("extraction").get("record_list_field", "records")),
        )
        write_tsv(output_path, rows)
        write_tsv(diff_path, differences, fieldnames=DIFF_FIELDS)
        write_tsv(flags_path, flags, fieldnames=FLAG_FIELDS)
        if flags:
            raise ValueError(f"{len(flags)} flagged/excluded record review(s) remain blocking; see {flags_path}")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Applied {len(decisions)} record review decision(s); accepted differences: {len(differences)}")


if __name__ == "__main__":
    main()
