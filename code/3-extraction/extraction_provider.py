"""Provider request and immutable per-page envelope helpers."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contract import ExtractionContract
from pipeline import (
    SelectedPage,
    page_cache_path,
    page_error_cache_path,
    write_page_cache,
)

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import stable_hash

TOKEN_FIELDS = ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")


def _usage_is_known(usage: dict[str, Any]) -> bool:
    return all(
        isinstance(usage.get(field), int)
        and not isinstance(usage.get(field), bool)
        and usage[field] >= 0
        for field in TOKEN_FIELDS
    )


def base_envelope(
    page: SelectedPage,
    contract: ExtractionContract,
    render_hash: str,
    render_path: Path | str,
) -> dict[str, Any]:
    """Build runner-owned provenance for one currently reviewed page."""
    return {
        "manifest_index": page.manifest_index,
        "page_id": page.page_id,
        "pdf_relative_path": page.pdf_relative_path,
        "physical_page": page.page,
        "source_sha256": page.source_sha256,
        "render_sha256": render_hash,
        "render_path": str(render_path),
        "contract_signature": contract.signature,
        "source_id": page.values.get("source_id", ""),
        "provider": page.values.get("provider", ""),
        "title": page.values.get("title", ""),
        "source_date": page.values.get("source_date", ""),
        "final_type": page.final_type,
        "classification_source": page.values.get("classification_source", ""),
        "manual_notes": page.values.get("manual_notes", ""),
        "ocr_method": page.values.get("ocr_method", ""),
        "ocr_text_sha256": page.values.get("ocr_text_sha256", ""),
        "page_manifest_sha256": stable_hash(page.values),
        "page_manifest": page.values,
    }


def structural_error_envelope(
    page: SelectedPage,
    contract: ExtractionContract,
    error: Exception,
) -> dict[str, Any]:
    """Keep all reviewed page lineage when rendering or cache inspection fails."""
    return {
        **base_envelope(page, contract, "", ""),
        "status": "error",
        "error_type": type(error).__name__,
        "error_message": str(error),
        "extraction": None,
        "usage": {field: 0 for field in TOKEN_FIELDS},
        "provider_call_started": False,
        "usage_known": True,
    }


def overlay_current_page(
    envelope: dict[str, Any],
    page: SelectedPage,
    contract: ExtractionContract,
    render_path: Path,
    render_hash: str,
) -> dict[str, Any]:
    """Attach current reviewed lineage without mutating an immutable model cache."""
    return {**envelope, **base_envelope(page, contract, render_hash, render_path)}


def extract_one(
    config: ProjectConfig,
    contract: ExtractionContract,
    page: SelectedPage,
    render_path: Path,
    render_hash: str,
    *,
    service: str,
    retry_errors: bool,
    attempt_id: str,
) -> dict[str, Any]:
    """Make one model request and immediately publish its immutable envelope."""
    from yachay import OCR

    model = config.table("model")
    extraction = config.table("extraction")
    cache_path = page_cache_path(
        config,
        contract_signature=contract.signature,
        page=page,
        render_sha256=render_hash,
    )
    base = base_envelope(page, contract, render_hash, render_path)
    started_at = datetime.now(UTC)
    started_clock = time.perf_counter()
    provider_call_started = False
    result: Any | None = None
    try:
        client = OCR(
            project_id=str(model.get("project_id") or "") or None,
            model=str(model.get("name")),
            location=str(model.get("location", "global")),
            temperature=float(model.get("temperature", 0.2)),
            max_output_tokens=int(model.get("max_output_tokens", 64_000)),
            think_level=str(model.get("think_level", "medium")),
            use_flex=service == "flex",
            retry_errors=retry_errors,
            raise_errors=True,
            media_resolution=str(extraction.get("media_resolution", "ultra_high")),
        )
        provider_call_started = True
        result = client.extract(render_path, contract.prompt, contract.schema, name=page.cache_key, page=page.page)
        if result is None:
            raise RuntimeError("Yachay returned no result")
        validated = contract.schema.model_validate(result.data).model_dump(mode="json")
        usage = {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "thoughts_tokens": result.thoughts_tokens,
            "total_tokens": result.total_tokens,
        }
        envelope = {
            **base,
            "status": "ok",
            "error_type": "",
            "error_message": "",
            "extraction": validated,
            "usage": usage,
            "provider_call_started": True,
            "usage_known": _usage_is_known(usage),
            "request_started_at": started_at.isoformat(),
            "request_completed_at": datetime.now(UTC).isoformat(),
            "request_duration_seconds": round(time.perf_counter() - started_clock, 6),
            "cache_path": str(cache_path),
            "actual_model_settings": {
                "model": result.model,
                "flex": result.flex,
                "temperature": result.temperature,
                "max_output_tokens": result.max_output_tokens,
                "think_level": result.think_level,
                "media_resolution": result.media_resolution,
            },
        }
        write_page_cache(cache_path, envelope)
        return envelope
    except Exception as error:  # noqa: BLE001 - every page failure must be an explicit audited row
        if result is not None:
            usage = {field: getattr(result, field, None) for field in TOKEN_FIELDS}
            usage_known = _usage_is_known(usage)
        elif provider_call_started:
            # A raised provider call may still have incurred usage. Unknown is
            # deliberately not represented as a verified zero.
            usage = {field: None for field in TOKEN_FIELDS}
            usage_known = False
        else:
            usage = {field: 0 for field in TOKEN_FIELDS}
            usage_known = True
        envelope = {
            **base,
            "status": "error",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "extraction": None,
            "usage": usage,
            "provider_call_started": provider_call_started,
            "usage_known": usage_known,
            "request_started_at": started_at.isoformat(),
            "request_completed_at": datetime.now(UTC).isoformat(),
            "request_duration_seconds": round(time.perf_counter() - started_clock, 6),
            "cache_path": str(page_error_cache_path(cache_path, attempt_id)),
            "actual_model_settings": {},
        }
        write_page_cache(Path(str(envelope["cache_path"])), envelope)
        return envelope
