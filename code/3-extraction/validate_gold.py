#!/usr/bin/env python3
"""Validate independent gold transcriptions against the current Flex contract and cache."""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from contract import build_contract
from pipeline import load_page_cache, page_cache_path, read_reviewed_pages, render_page, resolve_source
from selection_gate import evidence_bytes, evidence_text, production_evidence, validate_selection_current

from histdata_pipeline.config import load_project_config
from histdata_pipeline.provenance import atomic_write_json


def _read_jsonl(content: str, *, label: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label}:{line_number}: invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label}:{line_number}: each line must be an object")
        values.append(value)
    return values


def _scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def validate() -> dict[str, Any]:
    config = load_project_config()
    gate_path = (config.root / "manual" / "gold" / "production_gate.json").absolute()
    config.checked_write_path(gate_path)
    record_list_field = str(config.table("extraction").get("record_list_field", "records"))
    contract = build_contract(config, service="flex")
    selection_evidence = validate_selection_current(config)
    evidence = production_evidence(
        config,
        contract_signature=contract.signature,
        selection_evidence=selection_evidence,
    )
    production_files = evidence["files"]
    gold_rows = _read_jsonl(evidence_text(production_files["gold_jsonl"]), label="manual/gold/gold.jsonl")
    if not gold_rows:
        raise ValueError("manual/gold/gold.jsonl is empty; independently transcribe the risk-based sample first")
    calibration = list(csv.DictReader(io.StringIO(evidence_text(production_files["calibration_fixture"])), delimiter="\t"))
    if not calibration or any(not row.get("coverage_labels", "").strip() for row in calibration):
        raise ValueError("Every calibration page needs non-empty risk coverage_labels")
    calibration_ids = [str(row["page_id"]).strip() for row in calibration]
    if len(set(calibration_ids)) != len(calibration_ids):
        raise ValueError("calibration_pages.tsv contains duplicate page IDs")

    with tempfile.TemporaryDirectory(prefix="gold-pages-") as temporary:
        pages_path = Path(temporary) / "pages.tsv"
        pages_path.write_bytes(evidence_bytes(selection_evidence["pages"]))
        page_lookup = {page.page_id: page for page in read_reviewed_pages(pages_path)}
    gold_lookup: dict[str, dict[str, Any]] = {}
    discrepancies: list[str] = []
    cache_hits = 0
    for line_number, row in enumerate(gold_rows, start=1):
        page_id = str(row.get("page_id", ""))
        if not page_id or page_id in gold_lookup:
            raise ValueError(f"gold.jsonl line {line_number}: page_id is missing or duplicated")
        if page_id not in calibration_ids:
            raise ValueError(f"Gold page is not in calibration_pages.tsv: {page_id}")
        page = page_lookup.get(page_id)
        if page is None:
            raise ValueError(f"Gold page is not in the fail-closed reviewed page manifest: {page_id}")
        if row.get("source_sha256") != page.source_sha256:
            raise ValueError(f"Gold source hash is stale for {page_id}")
        extraction = contract.schema.model_validate(row.get("extraction")).model_dump(mode="json")
        normalized_gold = {**row, "extraction": extraction}
        gold_lookup[page_id] = normalized_gold

        source_path = resolve_source(config, page)
        rendered = render_page(config, page, verified_source=source_path)
        cache_path = page_cache_path(
            config,
            contract_signature=contract.signature,
            page=page,
            render_sha256=rendered.sha256,
        )
        cached = load_page_cache(
            cache_path,
            contract_signature=contract.signature,
            page=page,
            render_sha256=rendered.sha256,
        )
        if cached is None or cached.get("status") != "ok":
            discrepancies.append(f"{page_id}: no successful cache for the current contract/render")
            continue
        cache_hits += 1
        cached_extraction = contract.schema.model_validate(cached.get("extraction")).model_dump(mode="json")
        if cached_extraction != extraction:
            discrepancies.append(f"{page_id}: cached extraction does not exactly match the complete gold transcription")

    if set(calibration_ids) != set(gold_lookup):
        missing = set(calibration_ids) - set(gold_lookup)
        extra = set(gold_lookup) - set(calibration_ids)
        raise ValueError(f"Gold must cover every calibration page; missing={sorted(missing)}, extra={sorted(extra)}")

    expectations = list(csv.DictReader(io.StringIO(evidence_text(production_files["gold_expectations"])), delimiter="\t"))
    if not expectations:
        raise ValueError("manual/gold/expectations.tsv needs independently checked critical values")
    for row_number, expectation in enumerate(expectations, start=2):
        page_id = str(expectation.get("page_id", ""))
        try:
            record_index = int(str(expectation.get("record_index", "")))
        except ValueError as error:
            raise ValueError(f"expectations.tsv line {row_number}: record_index must be a 1-based integer") from error
        field = str(expectation.get("field", ""))
        if page_id not in gold_lookup or not field:
            raise ValueError(f"expectations.tsv line {row_number}: unknown page or empty field")
        records = gold_lookup[page_id]["extraction"].get(record_list_field, [])
        if not 1 <= record_index <= len(records) or field not in records[record_index - 1]:
            raise ValueError(f"expectations.tsv line {row_number}: unknown record/field")
        actual = _scalar_text(records[record_index - 1][field])
        if actual != str(expectation.get("expected_value", "")):
            discrepancies.append(f"expectations.tsv line {row_number}: expected {expectation.get('expected_value')!r}, gold has {actual!r}")

    old_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    same_evidence = old_gate.get("contract_signature") == contract.signature and old_gate.get("evidence_signature") == evidence["signature"]
    gate = {
        "contract_signature": contract.signature,
        "evidence_signature": evidence["signature"],
        "render_signature": old_gate.get("render_signature", "") if same_evidence else "",
        "preflight_signature": old_gate.get("preflight_signature", "") if same_evidence else "",
        "approved_max_requests": old_gate.get("approved_max_requests", 0) if same_evidence else 0,
        "gold_passed": not discrepancies,
        "trial_passed": old_gate.get("trial_passed") is True if same_evidence else False,
        "cache_reuse_passed": old_gate.get("cache_reuse_passed") is True if same_evidence else False,
        "cost_reviewed": old_gate.get("cost_reviewed") is True if same_evidence else False,
        "cost_reviewed_at": old_gate.get("cost_reviewed_at", "") if same_evidence else "",
        "notes": "Gold validation is automatic; the remaining gates require contemporaneous human evidence.",
    }
    atomic_write_json(gate_path, gate)
    result = {
        "contract_signature": contract.signature,
        "gold_pages": len(gold_lookup),
        "critical_expectations": len(expectations),
        "current_cache_hits": cache_hits,
        "gold_passed": not discrepancies,
        "discrepancies": discrepancies,
    }
    if discrepancies:
        raise ValueError("Gold validation failed:\n- " + "\n- ".join(discrepancies))
    return result


def main() -> None:
    try:
        result = validate()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
