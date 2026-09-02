"""Immutable extraction-run export, accounting, and publication helpers."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contract import ExtractionContract
from pipeline import SelectedPage, flatten_envelope, jsonable, write_tsv

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import atomic_write_json, atomic_write_text, sha256_file

TOKEN_FIELDS = ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")


def _atomic_write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for value in values),
    )


def review_reason(
    envelope: dict[str, Any],
    *,
    record_list_field: str,
    page_status_field: str,
    target_page_status: str,
    scan_quality_field: str,
    clear_scan_quality: str,
    uncertain_fields_field: str,
) -> str:
    """Summarize deterministic reasons why a page needs human review."""
    if envelope["status"] == "error":
        return "extraction_error"
    extraction = envelope.get("extraction") or {}
    reasons: list[str] = []
    if extraction.get(page_status_field) != target_page_status:
        reasons.append(f"{page_status_field}:{extraction.get(page_status_field)}")
    if extraction.get(scan_quality_field) != clear_scan_quality:
        reasons.append(f"{scan_quality_field}:{extraction.get(scan_quality_field)}")
    if any(record.get(uncertain_fields_field) for record in extraction.get(record_list_field, [])):
        reasons.append("uncertain_fields")
    return ";".join(reasons)


def _known_usage(envelope: dict[str, Any]) -> bool:
    if envelope.get("usage_known") is False:
        return False
    usage = envelope.get("usage") or {}
    return all(
        isinstance(usage.get(field), int)
        and not isinstance(usage.get(field), bool)
        and usage[field] >= 0
        for field in TOKEN_FIELDS
    )


def _summed_usage(envelopes: list[dict[str, Any]], page_ids: frozenset[str] | None = None) -> dict[str, int]:
    selected = envelopes if page_ids is None else [envelope for envelope in envelopes if envelope["page_id"] in page_ids]
    return {
        field: sum(
            value
            for envelope in selected
            if isinstance((value := (envelope.get("usage") or {}).get(field)), int)
            and not isinstance(value, bool)
            and value >= 0
        )
        for field in TOKEN_FIELDS
    }


def write_run(
    config: ProjectConfig,
    contract: ExtractionContract,
    pages: list[SelectedPage],
    envelopes: list[dict[str, Any]],
    *,
    run_id: str,
    service: str,
    cache_only: bool,
    requested: int,
    fresh_page_ids: frozenset[str] = frozenset(),
    cached_page_ids: frozenset[str] = frozenset(),
    selection_evidence: dict[str, Any] | None = None,
    production_evidence_payload: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
) -> Path:
    """Write one immutable, internally hashed run directory."""
    export_root = config.external_path("export_subdirectory", "data-extraction/exports")
    run_directory = export_root / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    record_field = str(config.table("extraction").get("record_list_field", "records"))
    _atomic_write_jsonl(run_directory / "nested.jsonl", envelopes)
    flat_rows = [row for envelope in envelopes for row in flatten_envelope(envelope, record_list_field=record_field)]
    write_tsv(run_directory / "flat.tsv", flat_rows)
    write_tsv(
        run_directory / "tokens.tsv",
        [
            {
                "page_id": envelope["page_id"],
                "request_made": envelope["page_id"] in fresh_page_ids,
                "cache_reused": envelope["page_id"] in cached_page_ids,
                "cache_path": envelope.get("cache_path", ""),
                "request_duration_seconds": envelope.get("request_duration_seconds", ""),
                "provider_call_started": envelope.get("provider_call_started", ""),
                "usage_known": envelope.get("usage_known", ""),
                **(envelope.get("usage") or {}),
            }
            for envelope in envelopes
        ],
        fieldnames=(
            "page_id",
            "request_made",
            "cache_reused",
            "cache_path",
            "request_duration_seconds",
            "provider_call_started",
            "usage_known",
            *TOKEN_FIELDS,
        ),
    )
    write_tsv(
        run_directory / "errors.tsv",
        [
            {
                "page_id": envelope["page_id"],
                "error_type": envelope.get("error_type", ""),
                "error_message": envelope.get("error_message", ""),
            }
            for envelope in envelopes
            if envelope["status"] == "error"
        ],
        fieldnames=("page_id", "error_type", "error_message"),
    )
    extraction_config = config.table("extraction")
    review_rows = [
        {
            "page_id": envelope["page_id"],
            "source_sha256": envelope["source_sha256"],
            "render_sha256": envelope["render_sha256"],
            "contract_signature": envelope["contract_signature"],
            "reason": reason,
        }
        for envelope in envelopes
        if (
            reason := review_reason(
                envelope,
                record_list_field=record_field,
                page_status_field=str(extraction_config.get("page_status_field", "document_status")),
                target_page_status=str(extraction_config.get("target_page_status", "target")),
                scan_quality_field=str(extraction_config.get("scan_quality_field", "scan_quality")),
                clear_scan_quality=str(extraction_config.get("clear_scan_quality", "clear")),
                uncertain_fields_field=str(extraction_config.get("uncertain_fields_field", "uncertain_fields")),
            )
        )
    ]
    write_tsv(
        run_directory / "review_queue.tsv",
        review_rows,
        fieldnames=("page_id", "source_sha256", "render_sha256", "contract_signature", "reason"),
    )
    write_tsv(run_directory / "manifest.tsv", [page.values for page in pages])
    atomic_write_json(run_directory / "contract.json", {"signature": contract.signature, "payload": contract.payload})
    atomic_write_json(run_directory / "selection_evidence.json", selection_evidence or {})
    atomic_write_json(run_directory / "production_evidence.json", production_evidence_payload or {})
    atomic_write_json(run_directory / "preflight.json", preflight or {})

    errors = sum(envelope["status"] == "error" for envelope in envelopes)
    exported_usage = _summed_usage(envelopes)
    request_usage = _summed_usage(envelopes, fresh_page_ids)
    unknown_request_usage = sum(envelope["page_id"] in fresh_page_ids and not _known_usage(envelope) for envelope in envelopes)
    unknown_exported_usage = sum(not _known_usage(envelope) for envelope in envelopes)
    pricing = config.table("pricing")
    known_request_cost = (
        request_usage["input_tokens"] * float(pricing.get("input_per_million", 0.0))
        + request_usage["output_tokens"] * float(pricing.get("output_per_million", 0.0))
        + request_usage["thoughts_tokens"] * float(pricing.get("thinking_per_million", 0.0))
    ) / 1_000_000
    cached_successes = sum(envelope["page_id"] in cached_page_ids and envelope["status"] == "ok" for envelope in envelopes)
    cached_errors = sum(envelope["page_id"] in cached_page_ids and envelope["status"] == "error" for envelope in envelopes)
    run = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "project_slug": config.slug,
        "contract_signature": contract.signature,
        "service": service,
        "cache_only": cache_only,
        "selected_pages": len(pages),
        "exported_pages": len(envelopes),
        "exported_records": sum(bool(row.get("record_id")) for row in flat_rows),
        "model_requests": requested,
        "cached_pages": len(cached_page_ids),
        "cached_success_pages": cached_successes,
        "cached_error_pages": cached_errors,
        "skipped_error_pages": sum(envelope["status"] == "error" and envelope["page_id"] not in fresh_page_ids for envelope in envelopes),
        "successful_pages": len(pages) - errors,
        "error_pages": errors,
        "request_token_usage": request_usage,
        "request_token_usage_complete": unknown_request_usage == 0,
        "unknown_request_usage_pages": unknown_request_usage,
        "exported_historical_token_usage": exported_usage,
        "exported_historical_token_usage_complete": unknown_exported_usage == 0,
        "unknown_exported_usage_pages": unknown_exported_usage,
        "pricing_as_of": pricing.get("as_of", ""),
        "known_minimum_incremental_request_cost": known_request_cost,
        "estimated_incremental_request_cost": known_request_cost if unknown_request_usage == 0 else None,
        "selection_signature": (selection_evidence or {}).get("signature", ""),
        "evidence_signature": (production_evidence_payload or {}).get("signature", ""),
        "render_signature": (preflight or {}).get("render_signature", ""),
        "preflight_signature": (preflight or {}).get("preflight_signature", ""),
        "manifest_sha256": sha256_file(run_directory / "manifest.tsv"),
        "nested_sha256": sha256_file(run_directory / "nested.jsonl"),
        "flat_sha256": sha256_file(run_directory / "flat.tsv"),
        "tokens_sha256": sha256_file(run_directory / "tokens.tsv"),
        "errors_sha256": sha256_file(run_directory / "errors.tsv"),
        "review_queue_sha256": sha256_file(run_directory / "review_queue.tsv"),
        "contract_sha256": sha256_file(run_directory / "contract.json"),
        "selection_evidence_sha256": sha256_file(run_directory / "selection_evidence.json"),
        "production_evidence_sha256": sha256_file(run_directory / "production_evidence.json"),
        "preflight_sha256": sha256_file(run_directory / "preflight.json"),
    }
    atomic_write_json(run_directory / "run.json", run)
    return run_directory


def publish_current(config: ProjectConfig, run_directory: Path, contract_signature: str) -> None:
    """Atomically move the canonical current pointer to a complete full run."""
    export_root = run_directory.parent
    temporary = export_root / f".current-{os.getpid()}-{secrets.token_hex(2)}"
    temporary.symlink_to(run_directory.name, target_is_directory=True)
    temporary.replace(export_root / "current")
    atomic_write_json(
        config.root / "data" / "extraction_current.json",
        {
            "run_directory": str(run_directory),
            "flat_tsv": str(run_directory / "flat.tsv"),
            "contract_signature": contract_signature,
            "run_sha256": sha256_file(run_directory / "run.json"),
        },
    )
