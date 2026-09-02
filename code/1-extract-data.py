#!/usr/bin/env -S uv run --project .
"""Run a bounded, provenance-signed Rand McNally page extraction."""

import csv
import importlib
import importlib.metadata
import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Annotated, Any

import pymupdf
import typer
import yachay
from loguru import logger
from pydantic import BaseModel

from histdata_pipeline.config import ProjectConfig, load_project_config
from histdata_pipeline.provenance import atomic_write_json, atomic_write_text, git_revision, sha256_file, stable_hash

CODE_DIR = Path(__file__).resolve().parent
PAGE_RANGES_FILE = CODE_DIR / "page-ranges.tsv"
SOURCE_MANIFEST_FILE = CODE_DIR.parent / "sources" / "source_manifest.tsv"
MIN_EXTRACTED_IMAGE_BYTES = 15 * 1024
JPX_FALLBACK_ALLOWLIST: set[tuple[int, int]] = {(1881, 1), (1916, 1)}
BALANCE_SHEET_FIELDS = {
    "capital",
    "capital_preferred",
    "surplus",
    "undivided_profits",
    "deposits",
    "other_liabs",
    "totals",
    "loans",
    "bonds",
    "other_assets",
    "cash",
    "us_gov_securities",
    "other_securities",
    "other_resources",
}
TOKEN_FIELDS = (
    "part",
    "source",
    "filename",
    "page",
    "is_advertisment",
    "input_tokens",
    "thoughts_tokens",
    "output_tokens",
    "total_tokens",
)
ERROR_FIELDS = ("page_id", "error_type", "error_message")
app = typer.Typer(add_completion=False, help="Extract only explicitly authorized Rand McNally pages.")


@dataclass(frozen=True, slots=True)
class SourceConfig:
    year: int
    edition: int
    source: str
    start: int
    end: int
    variant: str
    unit: int
    rotation: int
    path: Path
    is_single_pdf: bool
    source_id: str
    expected_sha256: str
    min_pages: int | None = None
    max_pages: int | None = None


@dataclass(frozen=True, slots=True)
class Document:
    path: Path
    relative_path: str
    part: int
    page_count: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ModelSettings:
    project_id: str
    location: str
    model: str
    think_level: str
    max_output_tokens: int
    media_resolution: str
    service: str


@dataclass(frozen=True, slots=True)
class PagePlan:
    page: int
    page_id: str
    render_path: Path
    render_sha256: str
    contract_signature: str
    contract_payload: dict[str, Any]
    cache_path: Path
    cached_result: yachay.OCRResult | None
    cached_error_type: str = ""
    cached_error_message: str = ""


@dataclass(frozen=True, slots=True)
class PageOutcome:
    plan: PagePlan
    result: yachay.OCRResult | None
    cache_status: str
    error_type: str = ""
    error_message: str = ""


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def checked_write_path(config: ProjectConfig, path: Path) -> Path:
    """Allow writes only in V2 or its external root, never in the legacy tree."""

    resolved = path.expanduser().resolve()
    restoration = config.table("restoration")
    legacy_root = Path(str(restoration.get("legacy_root", ""))).expanduser().resolve()
    if restoration.get("legacy_root_read_only") is not True:
        raise ValueError("project.toml must declare restoration.legacy_root_read_only = true")
    if is_relative_to(resolved, legacy_root):
        raise ValueError(f"Refusing a write inside the immutable legacy project: {resolved}")
    if not (is_relative_to(resolved, config.root.resolve()) or is_relative_to(resolved, config.external_root)):
        raise ValueError(f"Write path is outside the V2 project and external root: {resolved}")
    return resolved


def load_manifest_by_filename(path: Path, filename: str) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as source:
        matches = [row for row in csv.DictReader(source, delimiter="\t") if row["filename"].strip() == filename]
    if len(matches) != 1:
        raise ValueError(f"Expected one source-manifest row for {filename}, found {len(matches)}")
    return matches[0]


def load_source_config(config: ProjectConfig, year: int, edition: int) -> SourceConfig:
    if edition not in (1, 2):
        raise ValueError("edition must be 1 or 2")
    selected: dict[str, str] | None = None
    with PAGE_RANGES_FILE.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            raw_year = (row.get("year") or "").strip()
            if raw_year.startswith("x"):
                continue
            try:
                row_key = (int(raw_year), int(row["edition"]))
            except (KeyError, ValueError):
                continue
            if row_key == (year, edition):
                selected = row
                break
    if selected is None:
        raise ValueError(f"Year {year} edition {edition} is absent from {PAGE_RANGES_FILE}")

    source_name = (selected.get("source") or "").strip().lower()
    if not source_name:
        raise ValueError(f"Year {year} edition {edition} is a blank source placeholder")
    if source_name == "missing":
        raise FileNotFoundError(f"Year {year} edition {edition} is marked missing")
    try:
        start = int(selected["start"])
        end = int(selected["end"])
        unit = int(selected["unit"])
        rotation = int(selected["rotation"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"Invalid numeric source metadata for {year}-{edition}") from error
    variant = (selected.get("variant") or "").strip()
    if not variant:
        raise ValueError(f"Missing schema regime for {year}-{edition}")

    pdf_root = config.pdf_directory
    pdf_path = pdf_root / f"{year}-{edition}-{source_name}.pdf"
    directory_path = pdf_root / f"{year}-{edition}-{source_name}"
    if pdf_path.is_file():
        path = pdf_path
        is_single_pdf = True
        manifest = load_manifest_by_filename(SOURCE_MANIFEST_FILE, pdf_path.name)
    elif directory_path.is_dir():
        path = directory_path
        is_single_pdf = False
        manifest = {}
    else:
        raise FileNotFoundError(f"Configured V2 source is absent: {pdf_path} or {directory_path}")

    return SourceConfig(
        year=year,
        edition=edition,
        source=source_name,
        start=start,
        end=end,
        variant=variant,
        unit=unit,
        rotation=rotation,
        path=path,
        is_single_pdf=is_single_pdf,
        source_id=manifest.get("source_id", f"rand_mcnally_{year}_{edition}_{source_name}"),
        expected_sha256=manifest.get("expected_sha256", ""),
        min_pages=int(manifest["min_pages"]) if manifest.get("min_pages", "").strip() else None,
        max_pages=int(manifest["max_pages"]) if manifest.get("max_pages", "").strip() else None,
    )


def resolve_document(config: ProjectConfig, source: SourceConfig, part: int | None) -> Document:
    pdf_root = config.pdf_directory.resolve()
    if source.is_single_pdf:
        if part is not None:
            raise ValueError("--part is only valid for a configured directory of PDFs")
        pdf_path = source.path
        part_number = 0
    else:
        if source.source == "archive-raw":
            raise ValueError("archive-raw sources are not enabled in this bounded restoration phase")
        if part is None:
            raise ValueError("--part is required for a configured directory of PDFs")
        if not source.start <= part <= source.end:
            raise ValueError(f"part {part} is outside the configured range {source.start}-{source.end}")
        pdfs = sorted(source.path.glob("*.pdf"))
        if part > len(pdfs):
            raise FileNotFoundError(f"part {part} is absent from {source.path}; found {len(pdfs)} PDFs")
        pdf_path = pdfs[part - 1]
        part_number = part

    resolved = pdf_path.resolve()
    if not is_relative_to(resolved, pdf_root):
        raise ValueError(f"Source escapes the configured V2 PDF root: {resolved}")
    with pymupdf.open(resolved) as pdf:
        page_count = len(pdf)
    source_sha256 = sha256_file(resolved)
    if source.expected_sha256 and source_sha256 != source.expected_sha256:
        raise ValueError(f"SHA-256 mismatch for {source.source_id}")
    if source.min_pages is not None and page_count < source.min_pages:
        raise ValueError(f"Page count {page_count} is below the manifest minimum {source.min_pages} for {source.source_id}")
    if source.max_pages is not None and page_count > source.max_pages:
        raise ValueError(f"Page count {page_count} is above the manifest maximum {source.max_pages} for {source.source_id}")
    return Document(
        path=resolved,
        relative_path=resolved.relative_to(pdf_root).as_posix(),
        part=part_number,
        page_count=page_count,
        source_sha256=source_sha256,
    )


def validate_selected_pages(config: ProjectConfig, source: SourceConfig, document: Document, pages: list[int]) -> list[int]:
    if not pages:
        raise ValueError("At least one --page is required")
    if len(pages) != len(set(pages)):
        raise ValueError("Duplicate --page values are not allowed")
    ordered = sorted(pages)
    for page in ordered:
        if page < 1 or page > document.page_count:
            raise ValueError(f"Page {page} is outside the PDF's physical range 1-{document.page_count}")
        if source.is_single_pdf and not source.start <= page <= source.end:
            raise ValueError(f"Page {page} is outside the configured extraction range {source.start}-{source.end}")

    restoration = config.table("restoration")
    smoke_key = (int(restoration.get("smoke_year", -1)), int(restoration.get("smoke_edition", -1)))
    smoke_pages = sorted(int(value) for value in restoration.get("smoke_pages", []))
    if (source.year, source.edition) != smoke_key or ordered != smoke_pages:
        raise ValueError(f"This restoration checkpoint permits only {smoke_key[0]}-{smoke_key[1]} pages {smoke_pages}")
    return ordered


def load_definition(variant: str) -> tuple[type[BaseModel], str, str]:
    prompt_path = CODE_DIR / f"prompt_{variant}.md"
    schema_path = CODE_DIR / f"schema_{variant}.py"
    if not prompt_path.is_file() or not schema_path.is_file():
        raise FileNotFoundError(f"Missing prompt/schema for regime {variant}")
    module = importlib.import_module(f"schema_{variant}")
    schema = module.Page
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise TypeError(f"schema_{variant}.Page is not a Pydantic model")
    return schema, prompt_path.read_text(encoding="utf-8"), schema_path.read_text(encoding="utf-8")


def output_fieldnames(variant: str) -> tuple[list[str], list[str]]:
    module = importlib.import_module(f"schema_{variant}")
    bank_fields = [field for field in module.Bank.model_fields if field != "correspondents"]
    return (
        ["row_id", "year", "part", "pdf_page", "index", *bank_fields],
        ["row_id", "part", "corr_index", *module.Correspondent.model_fields],
    )


def model_settings(config: ProjectConfig) -> ModelSettings:
    model = config.table("model")
    extraction = config.table("extraction")
    settings = ModelSettings(
        project_id=str(model.get("project_id", "")).strip(),
        location=str(model.get("location", "")).strip(),
        model=str(model.get("name", "")).strip(),
        think_level=str(model.get("think_level", "")).strip(),
        max_output_tokens=int(model.get("max_output_tokens", 0)),
        media_resolution=str(extraction.get("media_resolution", "")).strip(),
        service=str(model.get("default_service", "")).strip(),
    )
    if not settings.project_id:
        raise ValueError("model.project_id is required")
    if settings.service != "flex":
        raise ValueError("This restoration checkpoint requires Flex service")
    return settings


def yachay_provenance(config: ProjectConfig) -> dict[str, Any]:
    with (config.root / "pyproject.toml").open("rb") as source:
        pyproject = tomllib.load(source)
    source_spec = pyproject.get("tool", {}).get("uv", {}).get("sources", {}).get("yachay", {})
    source_path = Path(str(source_spec.get("path", ""))).expanduser().resolve()
    revision = git_revision(source_path)
    if revision.get("dirty") is True:
        raise RuntimeError(f"Refusing an extraction with a dirty shared Yachay checkout: {source_path}")
    return {"version": importlib.metadata.version("yachay"), "revision": revision}


def document_key(relative_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", relative_path.removesuffix(".pdf"))


def render_settings(source: SourceConfig) -> dict[str, Any]:
    return {
        "method": "yachay.process_pdf",
        "upscale_factor": 1.0,
        "detect_watermarks": False,
        "rotation": source.rotation,
        "min_image_bytes": MIN_EXTRACTED_IMAGE_BYTES,
        "allow_jpx_fallback": (source.year, source.edition) in JPX_FALLBACK_ALLOWLIST,
    }


def find_rendered_page(directory: Path, page: int) -> Path | None:
    candidates = sorted(path for path in directory.glob(f"page-{page}.*") if path.is_file() and path.suffix != ".cached")
    if len(candidates) > 1:
        raise RuntimeError(f"Multiple render files exist for page {page}: {candidates}")
    return candidates[0] if candidates else None


def render_page(
    config: ProjectConfig,
    source: SourceConfig,
    document: Document,
    page: int,
    yachay_info: dict[str, Any],
) -> Path:
    settings = render_settings(source)
    render_signature = stable_hash({"settings": settings, "yachay": yachay_info})
    render_root = config.external_path("render_subdirectory", "rendered-pages")
    directory = checked_write_path(
        config,
        render_root / "targeted" / render_signature / document.source_sha256 / document_key(document.relative_path),
    )
    existing = find_rendered_page(directory, page)
    if existing is None:
        yachay.process_pdf(
            document.path,
            output_dir=directory,
            upscale_factor=1.0,
            first_page=page,
            last_page=page,
            skip_pages=None,
            prefix="",
            rotation=source.rotation,
            detect_watermarks=False,
            min_image_bytes=MIN_EXTRACTED_IMAGE_BYTES,
            verbose=True,
            allow_jpx_fallback=settings["allow_jpx_fallback"],
        )
        existing = find_rendered_page(directory, page)
    if existing is None:
        raise RuntimeError(f"No page image was produced for {document.relative_path} page {page}")
    if existing.stat().st_size < MIN_EXTRACTED_IMAGE_BYTES:
        raise ValueError(f"Rendered image is suspiciously small: {existing}")
    return existing


def definition_payload(
    config: ProjectConfig,
    source: SourceConfig,
    schema: type[BaseModel],
    prompt: str,
    schema_source: str,
    settings: ModelSettings,
    yachay_info: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pipeline_version": config.table("extraction").get("pipeline_version"),
        "runner_sha256": sha256_file(Path(__file__)),
        "prompt": prompt,
        "schema_source": schema_source,
        "schema_json": schema.model_json_schema(),
        "model": {
            "project_id": settings.project_id,
            "location": settings.location,
            "name": settings.model,
            "think_level": settings.think_level,
            "max_output_tokens": settings.max_output_tokens,
            "temperature": None,
            "media_resolution": settings.media_resolution,
            "service": settings.service,
        },
        "render": render_settings(source),
        "source_configuration": {
            "year": source.year,
            "edition": source.edition,
            "source": source.source,
            "configured_start": source.start,
            "configured_end": source.end,
            "schema_regime": source.variant,
            "unit_multiplier": source.unit,
            "rotation": source.rotation,
        },
        "yachay": yachay_info,
    }


def verify_record_hash(payload: dict[str, Any], path: Path) -> None:
    expected = payload.get("record_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "record_sha256"}
    if not isinstance(expected, str) or expected != stable_hash(unsigned):
        raise RuntimeError(f"Cache content hash mismatch for {path}")


def load_cached_result(path: Path, plan_payload: dict[str, Any], signature: str, schema: type[BaseModel]) -> yachay.OCRResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unreadable cache record: {path}") from error
    verify_record_hash(payload, path)
    expected = {
        "cache_format": 1,
        "contract_signature": signature,
        "page_id": plan_payload["page_id"],
        "source_sha256": plan_payload["source_sha256"],
        "render_sha256": plan_payload["render_sha256"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(f"Cache identity mismatch for {path}: {key}")
    try:
        return yachay.OCRResult.from_dict(payload["result"], schema)
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"Cache schema mismatch for {path}") from error


def load_cached_error(
    result_path: Path,
    plan_payload: dict[str, Any],
    signature: str,
) -> tuple[str, str] | None:
    error_paths = sorted(result_path.parent.glob(f"{plan_payload['render_sha256']}.error-*.json"))
    if not error_paths:
        return None
    if len(error_paths) > 1:
        raise RuntimeError(f"Multiple immutable error records exist for {plan_payload['page_id']}: {error_paths}")
    path = error_paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unreadable cache error record: {path}") from error
    verify_record_hash(payload, path)
    expected = {
        "cache_format": 1,
        "contract_signature": signature,
        "page_id": plan_payload["page_id"],
        "source_sha256": plan_payload["source_sha256"],
        "render_sha256": plan_payload["render_sha256"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(f"Cache error identity mismatch for {path}: {key}")
    error_type = payload.get("error_type")
    error_message = payload.get("error_message")
    if not isinstance(error_type, str) or not error_type or not isinstance(error_message, str):
        raise RuntimeError(f"Cache error payload is incomplete: {path}")
    return error_type, error_message


def prepare_page_plans(
    config: ProjectConfig,
    source: SourceConfig,
    document: Document,
    pages: list[int],
    schema: type[BaseModel],
    prompt: str,
    schema_source: str,
    settings: ModelSettings,
) -> tuple[dict[str, Any], list[PagePlan]]:
    yachay_info = yachay_provenance(config)
    definition = definition_payload(config, source, schema, prompt, schema_source, settings, yachay_info)
    definition_signature = stable_hash(definition)
    cache_root = config.external_path("cache_subdirectory", "data-extraction/cache")
    plans: list[PagePlan] = []
    for page in pages:
        render_path = render_page(config, source, document, page, yachay_info)
        render_sha256 = sha256_file(render_path)
        page_id = f"{document.relative_path}#page={page}"
        payload = {
            "definition_signature": definition_signature,
            "page_id": page_id,
            "source_id": source.source_id,
            "source_relative_path": document.relative_path,
            "source_sha256": document.source_sha256,
            "part": document.part,
            "physical_page": page,
            "render_sha256": render_sha256,
        }
        signature = stable_hash({"definition": definition, "page": payload})
        cache_path = checked_write_path(
            config,
            cache_root
            / "targeted"
            / signature
            / document.source_sha256
            / document_key(document.relative_path)
            / f"page-{page:06d}"
            / f"{render_sha256}.json",
        )
        cached_error = load_cached_error(cache_path, payload, signature)
        if cache_path.is_file() and cached_error is not None:
            raise RuntimeError(f"Both result and error caches exist for {page_id}")
        cached = load_cached_result(cache_path, payload, signature, schema) if cache_path.is_file() else None
        plans.append(
            PagePlan(
                page=page,
                page_id=page_id,
                render_path=render_path,
                render_sha256=render_sha256,
                contract_signature=signature,
                contract_payload=payload,
                cache_path=cache_path,
                cached_result=cached,
                cached_error_type=cached_error[0] if cached_error else "",
                cached_error_message=cached_error[1] if cached_error else "",
            )
        )
    return {"definition_signature": definition_signature, "definition": definition}, plans


def require_user_adc(project_id: str, adc_path: Path | None = None) -> None:
    """Verify only non-secret local ADC metadata; Yachay loads the credential itself."""

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is set. Unset it; this project uses gcloud user ADC.")
    if adc_path is None:
        config_root = Path(os.environ.get("CLOUDSDK_CONFIG", Path.home() / ".config" / "gcloud")).expanduser()
        adc_path = config_root / "application_default_credentials.json"
    try:
        metadata = json.loads(adc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("User ADC is unavailable. Run: gcloud auth application-default login") from error
    if metadata.get("type") != "authorized_user":
        raise RuntimeError("Expected gcloud user ADC, not service-account credentials.")
    if metadata.get("quota_project_id") != project_id:
        raise RuntimeError(f"Configure the ADC quota project with: gcloud auth application-default set-quota-project {project_id}")


def make_extractor(settings: ModelSettings) -> yachay.OCR:
    return yachay.OCR(
        project_id=settings.project_id,
        location=settings.location,
        model=settings.model,
        temperature=None,
        max_output_tokens=settings.max_output_tokens,
        think_level=settings.think_level,
        media_resolution=settings.media_resolution,
        use_flex=settings.service == "flex",
        retry_errors=False,
        raise_errors=True,
        call_delay=0.0,
        rate_limit_retries=1,
        server_retries=1,
        transient_retries=0,
    )


def cache_result(plan: PagePlan, result: yachay.OCRResult) -> None:
    unsigned = {
        "cache_format": 1,
        "contract_signature": plan.contract_signature,
        "page_id": plan.page_id,
        "source_sha256": plan.contract_payload["source_sha256"],
        "render_sha256": plan.render_sha256,
        "result": result.to_dict(),
    }
    payload = {**unsigned, "record_sha256": stable_hash(unsigned)}
    if plan.cache_path.exists():
        existing = json.loads(plan.cache_path.read_text(encoding="utf-8"))
        if stable_hash(existing) != stable_hash(payload):
            raise RuntimeError(f"Refusing to overwrite a conflicting immutable cache: {plan.cache_path}")
        return
    atomic_write_json(plan.cache_path, payload)


def cache_error(plan: PagePlan, run_id: str, error: Exception) -> None:
    unsigned = {
        "cache_format": 1,
        "run_id": run_id,
        "contract_signature": plan.contract_signature,
        "page_id": plan.page_id,
        "source_sha256": plan.contract_payload["source_sha256"],
        "render_sha256": plan.render_sha256,
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    payload = {**unsigned, "record_sha256": stable_hash(unsigned)}
    path = plan.cache_path.with_name(f"{plan.render_sha256}.error-{run_id}.json")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if stable_hash(existing) != stable_hash(payload):
            raise RuntimeError(f"Refusing to overwrite a conflicting immutable cache error: {path}")
        return
    atomic_write_json(path, payload)


def write_tsv(path: Path, fieldnames: list[str] | tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter="\t", lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(path, buffer.getvalue())


def flatten_results(
    source: SourceConfig,
    document: Document,
    outcomes: list[PageOutcome],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    bank_rows: list[dict[str, Any]] = []
    correspondent_rows: list[dict[str, Any]] = []
    token_rows: list[dict[str, Any]] = []
    row_id = 0
    for outcome in outcomes:
        result = outcome.result
        if result is None:
            continue
        token_rows.append(
            {
                "part": document.part,
                "source": source.source,
                "filename": document.path.name,
                "page": outcome.plan.page,
                "is_advertisment": int(result.data.is_advertisment),
                "input_tokens": result.input_tokens,
                "thoughts_tokens": result.thoughts_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.total_tokens,
            }
        )
        if result.data.is_advertisment:
            continue
        for index, bank in enumerate(result.data.banks, start=1):
            row_id += 1
            bank_data = bank.model_dump()
            correspondents = bank_data.pop("correspondents")
            bank_row = {
                "row_id": row_id,
                "year": source.year,
                "part": document.part,
                "pdf_page": outcome.plan.page,
                "index": index,
                **bank_data,
            }
            for key, value in list(bank_row.items()):
                if value is None:
                    bank_row[key] = ""
                elif isinstance(value, bool):
                    bank_row[key] = int(value)
            if source.unit != 1:
                for field in BALANCE_SHEET_FIELDS:
                    if isinstance(bank_row.get(field), (int, float)):
                        bank_row[field] *= source.unit
            bank_rows.append(bank_row)
            for correspondent_index, correspondent in enumerate(correspondents, start=1):
                correspondent_row = {
                    "row_id": row_id,
                    "part": document.part,
                    "corr_index": correspondent_index,
                    **correspondent,
                }
                for key, value in list(correspondent_row.items()):
                    if value is None:
                        correspondent_row[key] = ""
                correspondent_rows.append(correspondent_row)
    return bank_rows, correspondent_rows, token_rows


def utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def write_run_outputs(
    config: ProjectConfig,
    run_id: str,
    source: SourceConfig,
    document: Document,
    definition: dict[str, Any],
    outcomes: list[PageOutcome],
    started_at: str,
    provider_calls: int,
) -> Path:
    run_root = config.external_path("export_subdirectory", "data-extraction/exports") / "targeted"
    run_dir = checked_write_path(config, run_root / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    bank_rows, correspondent_rows, token_rows = flatten_results(source, document, outcomes)
    bank_fields, correspondent_fields = output_fieldnames(source.variant)
    write_tsv(run_dir / "banks.tsv", bank_fields, bank_rows)
    write_tsv(run_dir / "correspondents.tsv", correspondent_fields, correspondent_rows)
    write_tsv(run_dir / "tokens.tsv", TOKEN_FIELDS, token_rows)
    errors = [
        {"page_id": outcome.plan.page_id, "error_type": outcome.error_type, "error_message": outcome.error_message}
        for outcome in outcomes
        if outcome.error_type
    ]
    write_tsv(run_dir / "errors.tsv", ERROR_FIELDS, errors)
    page_lines = []
    for outcome in outcomes:
        if outcome.result is None:
            continue
        envelope = {
            **outcome.plan.contract_payload,
            "contract_signature": outcome.plan.contract_signature,
            "result": outcome.result.to_dict(),
        }
        page_lines.append(json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str))
    atomic_write_text(run_dir / "pages.jsonl", "".join(f"{line}\n" for line in page_lines))
    contract_payload = {
        **definition,
        "pages": [{**outcome.plan.contract_payload, "contract_signature": outcome.plan.contract_signature} for outcome in outcomes],
    }
    atomic_write_json(run_dir / "contract.json", contract_payload)
    output_names = ("banks.tsv", "correspondents.tsv", "tokens.tsv", "errors.tsv", "pages.jsonl", "contract.json")
    output_hashes = {name: sha256_file(run_dir / name) for name in output_names}
    receipt = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "failed" if errors else "success",
        "source_id": source.source_id,
        "source_relative_path": document.relative_path,
        "source_sha256": document.source_sha256,
        "requested_pages": [outcome.plan.page for outcome in outcomes],
        "cache_hits": sum(outcome.cache_status == "hit" for outcome in outcomes),
        "fresh_results": sum(outcome.cache_status == "fresh" for outcome in outcomes),
        "provider_calls": provider_calls,
        "page_status": [
            {
                "page_id": outcome.plan.page_id,
                "contract_signature": outcome.plan.contract_signature,
                "cache_status": outcome.cache_status,
                "error_type": outcome.error_type,
            }
            for outcome in outcomes
        ],
        "output_hashes": output_hashes,
    }
    atomic_write_json(run_dir / "run.json", receipt)
    return run_dir


def print_preflight(
    source: SourceConfig,
    document: Document,
    settings: ModelSettings,
    plans: list[PagePlan],
    max_requests: int,
) -> None:
    payload = {
        "source_id": source.source_id,
        "source_path": str(document.path),
        "source_sha256": document.source_sha256,
        "physical_pages": document.page_count,
        "selected_pages": [plan.page for plan in plans],
        "page_ids": [plan.page_id for plan in plans],
        "render_sha256": {str(plan.page): plan.render_sha256 for plan in plans},
        "contract_signatures": {str(plan.page): plan.contract_signature for plan in plans},
        "cache_hits": sum(plan.cached_result is not None for plan in plans),
        "cached_errors": sum(bool(plan.cached_error_type) for plan in plans),
        "pending_requests": sum(plan.cached_result is None and not plan.cached_error_type for plan in plans),
        "max_requests": max_requests,
        "provider_calls": 0,
        "model": {
            "project_id": settings.project_id,
            "location": settings.location,
            "name": settings.model,
            "think_level": settings.think_level,
            "max_output_tokens": settings.max_output_tokens,
            "temperature": None,
            "media_resolution": settings.media_resolution,
            "service": settings.service,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def run_targeted(
    *,
    year: int,
    edition: int,
    pages: list[int],
    part: int | None,
    max_requests: int,
    dry_run: bool,
) -> Path | None:
    if max_requests < 0:
        raise ValueError("--max-requests must be nonnegative")
    config = load_project_config(CODE_DIR)
    checked_write_path(config, config.external_root)
    source = load_source_config(config, year, edition)
    document = resolve_document(config, source, part)
    ordered_pages = validate_selected_pages(config, source, document, pages)
    schema, prompt, schema_source = load_definition(source.variant)
    settings = model_settings(config)
    definition, plans = prepare_page_plans(config, source, document, ordered_pages, schema, prompt, schema_source, settings)
    pending = [plan for plan in plans if plan.cached_result is None and not plan.cached_error_type]
    if len(pending) > max_requests:
        raise RuntimeError(f"Request ceiling blocks this run: {len(pending)} uncached pages > --max-requests {max_requests}")
    print_preflight(source, document, settings, plans, max_requests)
    if dry_run:
        return None

    if pending:
        require_user_adc(settings.project_id)
        extractor = make_extractor(settings)
    else:
        extractor = None
    started_at = datetime.now(UTC).isoformat()
    run_id = utc_run_id()
    outcomes: list[PageOutcome] = []
    provider_calls = 0
    for plan in plans:
        if plan.cached_result is not None:
            outcomes.append(PageOutcome(plan=plan, result=plan.cached_result, cache_status="hit"))
            continue
        if plan.cached_error_type:
            outcomes.append(
                PageOutcome(
                    plan=plan,
                    result=None,
                    cache_status="error-hit",
                    error_type=plan.cached_error_type,
                    error_message=plan.cached_error_message,
                )
            )
            continue
        if extractor is None:
            raise AssertionError("Provider client is absent for an uncached page")
        try:
            provider_calls += 1
            result = extractor.extract(image=plan.render_path, prompt=prompt, schema=schema, name=None, page=plan.page)
            if result is None:
                raise RuntimeError("Yachay returned no result")
            cache_result(plan, result)
            outcomes.append(PageOutcome(plan=plan, result=result, cache_status="fresh"))
        except Exception as error:
            cache_error(plan, run_id, error)
            outcomes.append(
                PageOutcome(
                    plan=plan,
                    result=None,
                    cache_status="error",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            )
            continue

    run_dir = write_run_outputs(config, run_id, source, document, definition, outcomes, started_at, provider_calls)
    print(f"Run output: {run_dir}")
    if any(outcome.error_type for outcome in outcomes):
        raise RuntimeError(f"Bounded extraction failed; inspect {run_dir / 'errors.tsv'}")
    return run_dir


@app.command()
def main(
    year: Annotated[int, typer.Option("--year", "-y", help="Publication year.")],
    edition: Annotated[int, typer.Option("--edition", "-e", help="Edition number (1 or 2).")],
    page: Annotated[list[int], typer.Option("--page", "-p", help="Physical PDF page; repeat exactly as authorized.")],
    max_requests: Annotated[int, typer.Option("--max-requests", help="Hard ceiling on uncached page requests.")],
    part: Annotated[int | None, typer.Option("--part", help="One-based PDF part for multipart sources.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Render and preflight without constructing a provider client.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable detailed Yachay logs.")] = False,
) -> None:
    yachay.set_log_level("DEBUG" if verbose else "INFO")
    try:
        run_targeted(
            year=year,
            edition=edition,
            pages=page,
            part=part,
            max_requests=max_requests,
            dry_run=dry_run,
        )
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
        logger.error(str(error))
        raise typer.Exit(1) from error


if __name__ == "__main__":
    app()
