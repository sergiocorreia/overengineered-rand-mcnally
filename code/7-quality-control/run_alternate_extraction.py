#!/usr/bin/env python3
"""Run a bounded candidate-only Standard/high alternate extraction.

Dry run is the default.  ``--execute`` is the only mode that may call a model;
it requires an explicit request ceiling.  Results use an alternate cache and
export tree and can never update the baseline ``current`` pointer.
"""

import argparse
import copy
import csv
import importlib.util
import json
import os
import secrets
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from merge_segmented_extraction import canonical_json, merge_segments
from plan_alternate_extraction import build_plan, read_queue, unique_in_order

from histdata_pipeline.config import ProjectConfig, load_project_config
from histdata_pipeline.provenance import atomic_write_json, atomic_write_text, sha256_file, stable_hash

TOKEN_FIELDS = ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")


@dataclass(frozen=True)
class Stage3Modules:
    contract: ModuleType
    pipeline: ModuleType


@dataclass(frozen=True)
class RequestSpec:
    page: Any
    segment_index: int
    images: tuple[Path, ...]
    image_hashes: tuple[str, ...]
    request_hash: str
    cache_path: Path
    band_y0: int | None = None
    band_y1: int | None = None


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load stage-3 module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_stage3(root: Path) -> Stage3Modules:
    stage = root / "code/3-extraction"
    return Stage3Modules(
        contract=_load_module("template_stage3_contract", stage / "contract.py"),
        pipeline=_load_module("template_stage3_pipeline", stage / "pipeline.py"),
    )


def resolve_execution_mode(*, execute: bool, cache_only: bool, retry_errors: bool, max_requests: int | None) -> str:
    if execute and cache_only:
        raise ValueError("--execute and --cache-only are mutually exclusive")
    if retry_errors and not execute:
        raise ValueError("--retry-errors is allowed only with --execute")
    if execute and max_requests is None:
        raise ValueError("--execute requires an explicit --max-requests ceiling")
    return "execute" if execute else ("cache-only" if cache_only else "dry-run")


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, Sequence):
        return tuple(str(part) for part in value if str(part))
    raise ValueError(f"expected string/list, got {type(value).__name__}")


def anchor_fields(config: ProjectConfig) -> tuple[str, ...]:
    alternate = config.table("alternate_extraction")
    dataset = config.table("dataset")
    explicit = _strings(alternate.get("overlap_anchor_fields"))
    if explicit:
        return explicit
    keys = _strings(dataset.get("keys"))
    if keys:
        return keys
    entities = _strings(dataset.get("entity_keys"))
    time_key = str(dataset.get("time_key", "")).strip()
    inferred = entities + ((time_key,) if time_key else ())
    return inferred or ("entity_raw", "period_raw")


def build_alternate_config(config: ProjectConfig, *, dpi: int) -> ProjectConfig:
    values = copy.deepcopy(config.values)
    model = values.setdefault("model", {})
    extraction = values.setdefault("extraction", {})
    if not isinstance(model, dict) or not isinstance(extraction, dict):
        raise ValueError("project.toml model and extraction sections must be tables")
    model["think_level"] = "high"
    model["default_service"] = "standard"
    extraction["render_dpi"] = dpi
    return ProjectConfig(root=config.root, values=values)


def build_alternate_contract(stage3: Stage3Modules, config: ProjectConfig, plan: Mapping[str, Any]) -> Any:
    baseline = stage3.contract.build_contract(config, service="standard")
    record_list_field = str(config.table("extraction").get("record_list_field", "records"))
    if plan["segmented"]:
        prompt_suffix = (
            "\n\n## Bounded alternate band review\n"
            "When two images are supplied, the first is repeated table-header context only and the second is the current "
            f"overlapping band. When one image is supplied, it is the current band. Emit entries in `{record_list_field}` "
            "from the current band only. "
            "Transcribe every complete visible record in that band, preserve uncertainty, and do not reconcile overlaps."
        )
    else:
        prompt_suffix = (
            "\n\n## Bounded alternate full-page review\n"
            "This is an evidence-review candidate. Inspect the complete high-resolution page, preserve uncertainty, "
            f"return observations through `{record_list_field}`, and do not repair values from plausibility or outside "
            "knowledge."
        )
    payload = {
        **baseline.payload,
        "alternate_extraction": {
            "pipeline_version": "alternate-extraction-v1",
            "plan_signature": plan["plan_signature"],
            "service": "standard",
            "think_level": "high",
            "dpi": plan["dpi"],
            "segmented": plan["segmented"],
            "band_height": plan["band_height"],
            "band_overlap": plan["band_overlap"],
            "header_height": plan["header_height"],
            "overlap_anchor_fields": plan["overlap_anchor_fields"],
            "prompt_suffix": prompt_suffix,
        },
    }
    return stage3.contract.ExtractionContract(
        signature=stable_hash(payload),
        payload=payload,
        prompt=baseline.prompt + prompt_suffix,
        schema=baseline.schema,
    )


def compute_bands(height: int, band_height: int, overlap: int) -> list[tuple[int, int]]:
    if height <= 0 or band_height <= 0 or not 0 < overlap < band_height:
        raise ValueError("invalid page height or band geometry")
    bands: list[tuple[int, int]] = []
    start = 0
    while start < height:
        end = min(start + band_height, height)
        bands.append((start, end))
        if end == height:
            break
        start = end - overlap
    return bands


def alternate_cache_path(
    config: ProjectConfig,
    *,
    plan_signature: str,
    contract_signature: str,
    page_cache_key: str,
    segment_index: int,
    request_hash: str,
) -> Path:
    root = config.external_path("cache_subdirectory", "data-extraction/cache")
    return (
        root
        / "alternate"
        / plan_signature
        / contract_signature
        / page_cache_key
        / f"segment-{segment_index:04d}-{request_hash}.json"
    )


def _crop_image(source_path: Path, destination: Path, *, y0: int, y1: int) -> str:
    import pymupdf

    source = pymupdf.Pixmap(str(source_path))
    if not 0 <= y0 < y1 <= source.height:
        raise ValueError(f"crop [{y0}, {y1}) is outside image height {source.height}")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.jpg")
        cropped = pymupdf.Pixmap(source, source.width, source.height, pymupdf.IRect(0, y0, source.width, y1))
        cropped.save(temporary, output="jpeg", jpg_quality=95)
        temporary.replace(destination)
    return sha256_file(destination)


def _prepare_requests(
    stage3: Stage3Modules,
    config: ProjectConfig,
    pages: Sequence[Any],
    contract: Any,
    plan: Mapping[str, Any],
) -> list[RequestSpec]:
    verified: dict[tuple[str, str], Path] = {}
    requests: list[RequestSpec] = []
    for page in pages:
        key = (page.pdf_relative_path, page.source_sha256)
        verified.setdefault(key, stage3.pipeline.resolve_source(config, page))
        rendered = stage3.pipeline.render_page(config, page, verified_source=verified[key])
        if not plan["segmented"]:
            request_hash = stable_hash([rendered.sha256])
            requests.append(
                RequestSpec(
                    page=page,
                    segment_index=0,
                    images=(rendered.path,),
                    image_hashes=(rendered.sha256,),
                    request_hash=request_hash,
                    cache_path=alternate_cache_path(
                        config,
                        plan_signature=str(plan["plan_signature"]),
                        contract_signature=contract.signature,
                        page_cache_key=page.cache_key,
                        segment_index=0,
                        request_hash=request_hash,
                    ),
                )
            )
            continue

        import pymupdf

        pixmap = pymupdf.Pixmap(str(rendered.path))
        planned_height = int(plan["page_height"])
        if pixmap.height > planned_height:
            raise ValueError(
                f"{page.page_id} rendered height {pixmap.height} exceeds reviewed --page-height-px {planned_height}; re-plan first"
            )
        band_directory = rendered.path.parent / "alternate-bands" / contract.signature
        header_end = min(int(plan["header_height"]), pixmap.height)
        header_path = band_directory / f"{page.cache_key}-header-000000-{header_end:06d}.jpg"
        header_hash = _crop_image(rendered.path, header_path, y0=0, y1=header_end)
        bands = compute_bands(pixmap.height, int(plan["band_height"]), int(plan["band_overlap"]))
        for segment_index, (y0, y1) in enumerate(bands):
            band_path = band_directory / f"{page.cache_key}-band-{segment_index:04d}-{y0:06d}-{y1:06d}.jpg"
            band_hash = _crop_image(rendered.path, band_path, y0=y0, y1=y1)
            images = (band_path,) if segment_index == 0 else (header_path, band_path)
            hashes = (band_hash,) if segment_index == 0 else (header_hash, band_hash)
            request_hash = stable_hash(list(hashes))
            requests.append(
                RequestSpec(
                    page=page,
                    segment_index=segment_index,
                    images=images,
                    image_hashes=hashes,
                    request_hash=request_hash,
                    cache_path=alternate_cache_path(
                        config,
                        plan_signature=str(plan["plan_signature"]),
                        contract_signature=contract.signature,
                        page_cache_key=page.cache_key,
                        segment_index=segment_index,
                        request_hash=request_hash,
                    ),
                    band_y0=y0,
                    band_y1=y1,
                )
            )
    return requests


def _load_cache(request: RequestSpec, contract_signature: str, *, retry_errors: bool) -> dict[str, Any] | None:
    candidate = request.cache_path
    if not candidate.exists():
        errors = sorted(candidate.parent.glob(f"{candidate.stem}.error-*.json")) if candidate.parent.is_dir() else []
        if not errors or retry_errors:
            return None
        candidate = errors[-1]
    value = json.loads(candidate.read_text(encoding="utf-8"))
    expected = {
        "page_id": request.page.page_id,
        "segment_index": request.segment_index,
        "request_hash": request.request_hash,
        "contract_signature": contract_signature,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError(f"alternate cache identity mismatch at {candidate}")
    if value.get("status") not in {"ok", "error"}:
        raise ValueError(f"invalid alternate cache status at {candidate}")
    return value


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if stable_hash(existing) != stable_hash(payload):
            raise FileExistsError(f"refusing to replace immutable alternate cache {path}")
        return
    atomic_write_json(path, payload)


def _base_envelope(request: RequestSpec, contract: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_only": True,
        "automatic_promotion": False,
        "page_id": request.page.page_id,
        "pdf_relative_path": request.page.pdf_relative_path,
        "physical_page": request.page.page,
        "source_sha256": request.page.source_sha256,
        "source_date": request.page.values.get("source_date", ""),
        "segment_index": request.segment_index,
        "band_y0": request.band_y0,
        "band_y1": request.band_y1,
        "image_paths": [str(path) for path in request.images],
        "image_sha256": list(request.image_hashes),
        "request_hash": request.request_hash,
        "contract_signature": contract.signature,
        "plan_signature": plan["plan_signature"],
        "service": "standard",
        "think_level": "high",
    }


def _extract_request(client: Any, request: RequestSpec, contract: Any, plan: Mapping[str, Any], attempt_id: str) -> dict[str, Any]:
    base = _base_envelope(request, contract, plan)
    provider_call_started = False
    result: Any | None = None
    try:
        image_argument: Path | tuple[Path, ...] = request.images[0] if len(request.images) == 1 else request.images
        provider_call_started = True
        result = client.extract(
            image_argument,
            contract.prompt,
            contract.schema,
            name=f"alternate-{contract.signature[:12]}-{request.page.cache_key}",
            page=request.page.page * 10_000 + request.segment_index,
        )
        if result is None:
            raise RuntimeError("Yachay returned no alternate result")
        extraction = contract.schema.model_validate(result.data).model_dump(mode="json")
        usage = {field: getattr(result, field, None) for field in TOKEN_FIELDS}
        envelope = {
            **base,
            "status": "ok",
            "error_type": "",
            "error_message": "",
            "extraction": extraction,
            "usage": usage,
            "provider_call_started": True,
            "usage_known": all(value is not None for value in usage.values()),
        }
        _write_immutable(request.cache_path, envelope)
        return envelope
    except Exception as error:  # noqa: BLE001 - every bounded failure remains explicit evidence
        if result is not None:
            usage = {field: getattr(result, field, None) for field in TOKEN_FIELDS}
            usage_known = all(value is not None for value in usage.values())
        elif provider_call_started:
            # A raised provider request may still incur tokens. Unknown usage
            # must never be recorded as a verified zero.
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
        }
        error_path = request.cache_path.with_name(f"{request.cache_path.stem}.error-{attempt_id}.json")
        _write_immutable(error_path, envelope)
        return envelope


def _record_anchor(record: Mapping[str, Any], fields: Sequence[str]) -> str:
    values = {field: record.get(field) for field in fields}
    if not any(value is not None and value != "" for value in values.values()):
        raise ValueError(f"record has no configured overlap anchor values: {', '.join(fields)}")
    return canonical_json(values)


def _candidate_outputs(
    envelopes: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    *,
    record_list_field: str,
    page_status_field: str,
    target_page_status: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    segment_rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    fields = tuple(str(field) for field in plan["overlap_anchor_fields"])
    for envelope in envelopes:
        if envelope["status"] != "ok":
            conflicts.append(
                {
                    "page_id": envelope["page_id"],
                    "segment_index": envelope["segment_index"],
                    "conflict_type": "request_error",
                    "detail": f"{envelope['error_type']}: {envelope['error_message']}",
                }
            )
            continue
        extraction = envelope.get("extraction") or {}
        if extraction.get(page_status_field) != target_page_status:
            conflicts.append(
                {
                    "page_id": envelope["page_id"],
                    "segment_index": envelope["segment_index"],
                    "conflict_type": "non_target_candidate",
                    "detail": str(extraction.get(page_status_field, "")),
                }
            )
        records = extraction.get(record_list_field, [])
        if plan["segmented"] and not records:
            conflicts.append(
                {
                    "page_id": envelope["page_id"],
                    "segment_index": envelope["segment_index"],
                    "conflict_type": "empty_segment",
                    "detail": "A segmented candidate must return at least one overlap-checkable record per band.",
                }
            )
        for record in records:
            try:
                anchor = _record_anchor(record, fields)
            except ValueError as error:
                conflicts.append(
                    {
                        "page_id": envelope["page_id"],
                        "segment_index": envelope["segment_index"],
                        "conflict_type": "missing_record_anchor",
                        "detail": str(error),
                    }
                )
                continue
            segment_rows.append(
                {
                    "page_id": envelope["page_id"],
                    "segment_index": envelope["segment_index"],
                    "record_anchor": anchor,
                    "record": record,
                }
            )
    if not plan["segmented"]:
        merged = [
            {
                "page_id": row["page_id"],
                "record_anchor": row["record_anchor"],
                "record": row["record"],
                "source_segments": [0],
                "candidate_only": True,
            }
            for row in segment_rows
        ]
        return segment_rows, merged, conflicts
    try:
        expected_counts: dict[str, int] = {}
        for envelope in envelopes:
            page_id = str(envelope["page_id"])
            expected_counts[page_id] = expected_counts.get(page_id, 0) + 1
        merged, overlap_conflicts = merge_segments(segment_rows, expected_segment_counts=expected_counts)
        conflicts.extend(overlap_conflicts)
    except ValueError as error:
        merged = []
        conflicts.append({"page_id": "", "segment_index": "", "conflict_type": "merge_validation_error", "detail": str(error)})
    return segment_rows, merged, conflicts


def _usage_is_known(envelope: Mapping[str, Any]) -> bool:
    if "usage_known" in envelope:
        return envelope.get("usage_known") is True
    usage = envelope.get("usage") or {}
    return all(usage.get(field) is not None for field in TOKEN_FIELDS)


def _sum_known_usage(envelopes: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = list(envelopes)
    return {
        field: sum(int((envelope.get("usage") or {}).get(field) or 0) for envelope in values)
        for field in TOKEN_FIELDS
    }


def _usage_accounting(
    envelopes: Sequence[Mapping[str, Any]],
    *,
    fresh_keys: frozenset[tuple[str, int]],
    pricing: Mapping[str, Any],
) -> dict[str, Any]:
    fresh = [
        envelope
        for envelope in envelopes
        if (str(envelope["page_id"]), int(envelope["segment_index"])) in fresh_keys
    ]
    exported_usage = _sum_known_usage(envelopes)
    request_usage = _sum_known_usage(fresh)
    unknown_exported = sum(not _usage_is_known(envelope) for envelope in envelopes)
    unknown_requests = sum(not _usage_is_known(envelope) for envelope in fresh)
    known_request_cost = (
        request_usage["input_tokens"] * float(pricing.get("input_per_million", 0.0))
        + request_usage["output_tokens"] * float(pricing.get("output_per_million", 0.0))
        + request_usage["thoughts_tokens"] * float(pricing.get("thinking_per_million", 0.0))
    ) / 1_000_000
    return {
        "token_usage": exported_usage,
        "token_usage_complete": unknown_exported == 0,
        "unknown_exported_usage_requests": unknown_exported,
        "request_token_usage": request_usage,
        "request_token_usage_complete": unknown_requests == 0,
        "unknown_request_usage_requests": unknown_requests,
        "pricing_as_of": pricing.get("as_of", ""),
        "known_minimum_incremental_request_cost": known_request_cost,
        "estimated_incremental_request_cost": known_request_cost if unknown_requests == 0 else None,
    }


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    atomic_write_text(path, "".join(canonical_json(row) + "\n" for row in rows))


def _write_candidate_tsv(path: Path, candidates: Sequence[Mapping[str, Any]], envelopes: Sequence[Mapping[str, Any]]) -> None:
    provenance = {
        str(envelope["page_id"]): {
            "source_sha256": envelope["source_sha256"],
            "contract_signature": envelope["contract_signature"],
            "plan_signature": envelope["plan_signature"],
        }
        for envelope in envelopes
    }
    rows = [
        {
            "page_id": candidate["page_id"],
            "record_anchor": candidate["record_anchor"],
            **provenance[str(candidate["page_id"])],
            "source_segments": canonical_json(candidate["source_segments"]),
            "candidate_only": "1",
            **dict(candidate["record"]),
        }
        for candidate in candidates
    ]
    fields: list[str] = []
    for row in rows:
        fields.extend(field for field in row if field not in fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fields, lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: canonical_json(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
    temporary.replace(path)


def _resolve_pages(config: ProjectConfig, stage3: Stage3Modules, arguments: argparse.Namespace) -> list[Any]:
    dataset = config.table("dataset")
    quality = config.table("quality")
    page_ids = list(arguments.page_id)
    if arguments.case_id:
        output = config.project_path(str(quality.get("output_directory", "output/7-quality-control")))
        queue_path = arguments.queue_tsv or output / "review_queue.tsv"
        queue = read_queue(config.project_path(queue_path))
        page_field = str(dataset.get("source_page_field", "page_id"))
        for case_id in arguments.case_id:
            if case_id not in queue:
                raise ValueError(f"explicit case {case_id} is absent from the current review queue")
            page_id = queue[case_id].get(page_field, queue[case_id].get("page_id", ""))
            if not page_id:
                raise ValueError(f"case {case_id} has no page identity")
            page_ids.append(page_id)
    wanted = set(unique_in_order(page_ids))
    if not wanted:
        raise ValueError("at least one explicit page or case is required")
    selected_path = config.project_path(str(config.table("extraction").get("selected_pages", "data/selected_pages.tsv")))
    all_pages = stage3.pipeline.read_selected_pages(selected_path)
    pages = [page for page in all_pages if page.page_id in wanted]
    missing = wanted - {page.page_id for page in pages}
    if missing:
        raise ValueError(f"unknown selected page IDs: {', '.join(sorted(missing))}")
    return pages


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    mode = resolve_execution_mode(
        execute=arguments.execute,
        cache_only=arguments.cache_only,
        retry_errors=arguments.retry_errors,
        max_requests=arguments.max_requests,
    )
    config = load_project_config(arguments.root.resolve())
    stage3 = load_stage3(config.root)
    pages = _resolve_pages(config, stage3, arguments)
    alternate_settings = config.table("alternate_extraction")
    fields = anchor_fields(config)
    if arguments.segmented and arguments.page_height_px is None:
        raise ValueError("--page-height-px is required with --segmented")
    plan = build_plan(
        project_slug=config.slug,
        page_ids=[page.page_id for page in pages],
        case_ids=unique_in_order(arguments.case_id),
        configured=alternate_settings,
        segmented=arguments.segmented,
        page_height=arguments.page_height_px,
        request_ceiling=arguments.max_requests,
        header_height=arguments.header_height_px,
        anchor_fields=fields,
    )
    alternate_config = build_alternate_config(config, dpi=int(plan["dpi"]))
    contract = build_alternate_contract(stage3, alternate_config, plan)
    summary: dict[str, Any] = {
        **plan,
        "mode": mode,
        "contract_signature": contract.signature,
        "model_requests": 0,
        "run_directory": None,
        "current_updated": False,
    }
    if mode == "dry-run":
        return summary
    if mode == "execute":
        pricing = config.table("pricing")
        pricing_as_of = str(pricing.get("as_of", "")).strip()
        try:
            pricing_date_valid = bool(pricing_as_of) and date.fromisoformat(pricing_as_of).isoformat() == pricing_as_of
        except ValueError:
            pricing_date_valid = False
        rates = [
            float(pricing.get("input_per_million", 0.0)),
            float(pricing.get("output_per_million", 0.0)),
            float(pricing.get("thinking_per_million", 0.0)),
        ]
        if any(rate < 0 for rate in rates) or not pricing_date_valid or not any(rate > 0 for rate in rates):
            raise ValueError(
                "Alternate requests are blocked until [pricing].as_of is an ISO date and at least one nonzero rate is configured"
            )

    requests = _prepare_requests(stage3, alternate_config, pages, contract, plan)
    if len(requests) > int(plan["effective_request_ceiling"]):
        raise ValueError(
            f"actual band geometry requires {len(requests)} requests, exceeding reviewed ceiling {plan['effective_request_ceiling']}"
        )
    cached: dict[tuple[str, int], dict[str, Any]] = {}
    pending: list[RequestSpec] = []
    for request in requests:
        value = _load_cache(request, contract.signature, retry_errors=arguments.retry_errors)
        if value is None:
            pending.append(request)
        else:
            cached[(request.page.page_id, request.segment_index)] = value
    if mode == "execute" and len(pending) > int(plan["effective_request_ceiling"]):
        raise ValueError("uncached request count exceeds the reviewed request ceiling")

    attempt_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(3)
    fresh: dict[tuple[str, int], dict[str, Any]] = {}
    if mode == "execute" and pending:
        from yachay import OCR

        model = alternate_config.table("model")
        extraction = alternate_config.table("extraction")
        client = OCR(
            project_id=str(model.get("project_id") or "") or None,
            model=str(model.get("name")),
            location=str(model.get("location", "global")),
            temperature=float(model.get("temperature", 0.2)),
            max_output_tokens=int(model.get("max_output_tokens", 64_000)),
            think_level="high",
            use_flex=False,
            retry_errors=arguments.retry_errors,
            raise_errors=True,
            media_resolution=str(extraction.get("media_resolution", "ultra_high")),
        )
        for request in pending:
            fresh[(request.page.page_id, request.segment_index)] = _extract_request(
                client,
                request,
                contract,
                plan,
                f"{attempt_id}-{request.page.cache_key[:8]}-{request.segment_index:04d}",
            )

    envelopes: list[dict[str, Any]] = []
    for request in requests:
        key = (request.page.page_id, request.segment_index)
        if key in fresh:
            envelopes.append(fresh[key])
        elif key in cached:
            envelopes.append(cached[key])
        else:
            envelopes.append(
                {
                    **_base_envelope(request, contract, plan),
                    "status": "error",
                    "error_type": "AlternateCacheMiss",
                    "error_message": "Cache-only alternate reconstruction found no immutable result",
                    "extraction": None,
                    "usage": {field: 0 for field in TOKEN_FIELDS},
                    "provider_call_started": False,
                    "usage_known": True,
                }
            )

    extraction_settings = config.table("extraction")
    segment_rows, candidates, conflicts = _candidate_outputs(
        envelopes,
        plan,
        record_list_field=str(extraction_settings.get("record_list_field", "records")),
        page_status_field=str(extraction_settings.get("page_status_field", "document_status")),
        target_page_status=str(extraction_settings.get("target_page_status", "target")),
    )
    export_root = config.external_path("alternate_export_subdirectory", "data-extraction/alternate-exports")
    run_directory = export_root / attempt_id
    run_directory.mkdir(parents=True, exist_ok=False)
    atomic_write_json(run_directory / "plan.json", dict(plan))
    atomic_write_json(run_directory / "contract.json", contract.payload)
    _write_jsonl(run_directory / "requests.jsonl", envelopes)
    _write_jsonl(run_directory / "segment-records.jsonl", segment_rows)
    _write_jsonl(run_directory / "overlap-conflicts.jsonl", conflicts)
    errors = sum(envelope["status"] == "error" for envelope in envelopes)
    status = "blocked" if errors or conflicts else "candidate_ready_for_human_review"
    if status != "blocked":
        _write_jsonl(run_directory / "candidate_records.jsonl", candidates)
        _write_candidate_tsv(run_directory / "candidate_records.tsv", candidates, envelopes)
    usage_accounting = _usage_accounting(
        envelopes,
        fresh_keys=frozenset(fresh),
        pricing=config.table("pricing"),
    )
    run_metadata = {
        "schema_version": 1,
        "run_id": attempt_id,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate_only": True,
        "automatic_promotion": False,
        "promotion_policy": "human evidence review plus keyed correction overlay",
        "status": status,
        "service": "standard",
        "think_level": "high",
        "contract_signature": contract.signature,
        "plan_signature": plan["plan_signature"],
        "selected_pages": len(pages),
        "planned_request_ceiling": plan["effective_request_ceiling"],
        "request_units": len(requests),
        "model_requests": len(pending) if mode == "execute" else 0,
        "error_requests": errors,
        "overlap_conflicts": len(conflicts),
        "candidate_records": len(candidates) if status != "blocked" else 0,
        **usage_accounting,
        "current_updated": False,
    }
    atomic_write_json(run_directory / "run.json", run_metadata)
    return {
        **summary,
        "mode": mode,
        "run_directory": str(run_directory),
        "model_requests": run_metadata["model_requests"],
        "error_requests": errors,
        "overlap_conflicts": len(conflicts),
        "candidate_status": status,
        "request_token_usage_complete": run_metadata["request_token_usage_complete"],
        "estimated_incremental_request_cost": run_metadata["estimated_incremental_request_cost"],
        "current_updated": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--page-id", action="append", default=[], help="Explicit selected page ID; repeat as needed")
    parser.add_argument("--case-id", action="append", default=[], help="Explicit current QC case ID; repeat as needed")
    parser.add_argument("--queue-tsv", type=Path, help="Current generated queue used to resolve explicit case IDs")
    parser.add_argument("--segmented", action="store_true", help="Use overlapping bands with repeated header context")
    parser.add_argument("--page-height-px", type=int, help="Reviewed maximum rendered height; required for segmented mode")
    parser.add_argument("--header-height-px", type=int, help="Repeated top header context; defaults to configured overlap")
    parser.add_argument("--max-requests", type=int, help="Explicit ceiling; required with --execute")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="Authorize the bounded Standard/high model requests")
    modes.add_argument("--cache-only", action="store_true", help="Build a candidate run from alternate caches only")
    parser.add_argument("--retry-errors", action="store_true", help="Retry immutable alternate error caches; requires --execute")
    return parser


def main() -> None:
    try:
        result = execute(build_parser().parse_args())
    except (OSError, ValueError, RuntimeError, TypeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
