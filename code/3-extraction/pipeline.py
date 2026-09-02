"""Stable page identities, rendering, cache envelopes, and deterministic exports."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.extraction_fields import FLAT_PROVENANCE_FIELDS, GENERATED_FLAT_FIELDS, RESERVED_MODEL_FIELDS
from histdata_pipeline.provenance import atomic_write_json, sha256_file, stable_hash

REQUIRED_PAGE_COLUMNS = {
    "manifest_index",
    "page_id",
    "pdf_relative_path",
    "source_sha256",
    "source_date",
    "page",
    "final_type",
    "classification_source",
    "manual_notes",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SelectedPage:
    manifest_index: int
    page_id: str
    pdf_relative_path: str
    source_sha256: str
    page: int
    final_type: str
    values: dict[str, str]

    @property
    def cache_key(self) -> str:
        return stable_hash({"page_id": self.page_id, "source_sha256": self.source_sha256})[:32]


@dataclass(frozen=True, slots=True)
class RenderedPage:
    path: Path
    sha256: str


def _parse_positive_integer(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an integer, got {value!r}") from error
    if result < 1:
        raise ValueError(f"{label} must be positive, got {result}")
    return result


def _parse_nonnegative_integer(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an integer, got {value!r}") from error
    if result < 0:
        raise ValueError(f"{label} must be nonnegative, got {result}")
    return result


def read_page_manifest(path: Path, *, allowed_final_types: frozenset[str]) -> list[SelectedPage]:
    """Load a fail-closed page manifest and preserve its declared order."""
    if not allowed_final_types or not allowed_final_types <= {"selected", "excluded"}:
        raise ValueError("Allowed page types must be a nonempty subset of selected/excluded")
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_PAGE_COLUMNS - columns
        if missing:
            raise ValueError(f"Selected-page manifest is missing columns: {', '.join(sorted(missing))}")
        pages: list[SelectedPage] = []
        seen_ids: set[str] = set()
        seen_indexes: set[int] = set()
        for line_number, raw in enumerate(reader, start=2):
            values = {str(key): str(value or "") for key, value in raw.items()}
            page = _parse_positive_integer(values["page"], f"line {line_number} page")
            index = _parse_nonnegative_integer(values["manifest_index"], f"line {line_number} manifest_index")
            relative = values["pdf_relative_path"]
            expected_id = f"{relative}#page={page}"
            if values["page_id"] != expected_id:
                raise ValueError(f"line {line_number}: page_id must be {expected_id!r}")
            if values["final_type"] not in allowed_final_types:
                allowed = ", ".join(sorted(allowed_final_types))
                raise ValueError(f"line {line_number}: final_type must be one of [{allowed}]")
            if not values["classification_source"]:
                raise ValueError(f"line {line_number}: classification_source is required")
            if SHA256_PATTERN.fullmatch(values["source_sha256"]) is None:
                raise ValueError(f"line {line_number}: source_sha256 must be a SHA-256 digest")
            source_date = values["source_date"].strip()
            if source_date:
                try:
                    parsed_source_date = date.fromisoformat(source_date)
                except ValueError as error:
                    raise ValueError(f"line {line_number}: source_date must be canonical YYYY-MM-DD or blank") from error
                if parsed_source_date.isoformat() != source_date:
                    raise ValueError(f"line {line_number}: source_date must be canonical YYYY-MM-DD or blank")
            if values["page_id"] in seen_ids or index in seen_indexes:
                raise ValueError(f"line {line_number}: duplicate page identity or manifest_index")
            seen_ids.add(values["page_id"])
            seen_indexes.add(index)
            pages.append(
                SelectedPage(
                    manifest_index=index,
                    page_id=values["page_id"],
                    pdf_relative_path=relative,
                    source_sha256=values["source_sha256"],
                    page=page,
                    final_type=values["final_type"],
                    values=values,
                )
            )
    pages.sort(key=lambda item: item.manifest_index)
    return pages


def read_selected_pages(path: Path) -> list[SelectedPage]:
    """Load the extraction manifest, which may contain selected pages only."""
    return read_page_manifest(path, allowed_final_types=frozenset({"selected"}))


def read_reviewed_pages(path: Path) -> list[SelectedPage]:
    """Load resolved positive and negative pages for risk-based calibration."""
    return read_page_manifest(path, allowed_final_types=frozenset({"selected", "excluded"}))


def resolve_source(config: ProjectConfig, page: SelectedPage) -> Path:
    """Resolve a relative source within the configured PDF root and verify immutability."""
    root = config.pdf_directory.resolve()
    relative = PurePosixPath(page.pdf_relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"Unsafe source-relative path: {page.pdf_relative_path}")
    # Acquisition identities are relative to the storage root (`pdfs/...`),
    # while config.pdf_directory is already that storage root's PDF folder.
    if relative.parts and relative.parts[0] == "pdfs":
        relative = PurePosixPath(*relative.parts[1:])
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Source escapes configured PDF directory: {page.pdf_relative_path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = sha256_file(path)
    if actual != page.source_sha256:
        raise ValueError(f"Source hash changed for {page.page_id}: expected {page.source_sha256}, got {actual}")
    return path


def render_page(config: ProjectConfig, page: SelectedPage, *, verified_source: Path | None = None) -> RenderedPage:
    """Render one physical page deterministically and hash the exact model bytes."""
    import fitz

    destination, dpi, extension = render_destination(config, page)
    image_format = extension
    if not destination.is_file():
        source_path = verified_source or resolve_source(config, page)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        with fitz.open(source_path) as document:
            if page.page > document.page_count:
                raise ValueError(f"{page.page_id} exceeds {document.page_count} physical pages")
            pixmap = document.load_page(page.page - 1).get_pixmap(dpi=dpi, alpha=False)
            pixmap.save(temporary, output="jpeg" if image_format == "jpg" else "png")
        temporary.replace(destination)
    return RenderedPage(destination, sha256_file(destination))


def render_destination(config: ProjectConfig, page: SelectedPage) -> tuple[Path, int, str]:
    """Return the configured render path without doing any work."""
    extraction = config.table("extraction")
    dpi = int(extraction.get("render_dpi", 220))
    image_format = str(extraction.get("render_format", "jpeg")).lower()
    if image_format not in {"jpeg", "jpg", "png"}:
        raise ValueError("extraction.render_format must be jpeg, jpg, or png")
    extension = "jpg" if image_format in {"jpeg", "jpg"} else "png"
    render_root = config.external_path("render_subdirectory", "rendered-pages")
    destination = render_root / page.source_sha256 / f"dpi-{dpi}" / f"page-{page.page:06d}.{extension}"
    return destination, dpi, extension


def page_cache_path(
    config: ProjectConfig,
    *,
    contract_signature: str,
    page: SelectedPage,
    render_sha256: str,
    namespace: str = "baseline",
) -> Path:
    root = config.external_path("cache_subdirectory", "data-extraction/cache")
    return root / namespace / contract_signature / page.cache_key / f"{render_sha256}.json"


def load_page_cache(
    path: Path,
    *,
    contract_signature: str,
    page: SelectedPage,
    render_sha256: str,
) -> dict[str, Any] | None:
    candidate = path
    if not candidate.is_file():
        errors = sorted(path.parent.glob(f"{path.stem}.error-*.json")) if path.parent.is_dir() else []
        if not errors:
            return None
        candidate = errors[-1]
    value = json.loads(candidate.read_text(encoding="utf-8"))
    expected = {
        "contract_signature": contract_signature,
        "page_id": page.page_id,
        "source_sha256": page.source_sha256,
        "render_sha256": render_sha256,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"Cache identity mismatch at {candidate}: {key}")
    if value.get("status") not in {"ok", "error"}:
        raise ValueError(f"Invalid cache status at {candidate}")
    return value


def page_error_cache_path(success_path: Path, attempt_id: str) -> Path:
    """Keep every failed attempt without replacing an earlier error or later success."""
    return success_path.with_name(f"{success_path.stem}.error-{attempt_id}.json")


def write_page_cache(path: Path, envelope: dict[str, Any]) -> None:
    """Publish one immutable page result; refuse to replace different content."""
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if stable_hash(existing) != stable_hash(envelope):
            raise FileExistsError(f"Refusing to replace immutable cache: {path}")
        return
    atomic_write_json(path, envelope)


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (date, datetime, Decimal, Path)):
        return str(value)
    return value


def flatten_envelope(envelope: dict[str, Any], *, record_list_field: str = "records") -> list[dict[str, Any]]:
    """Create provenance-rich flat rows, including explicit rows for page failures."""
    provenance = {key: envelope.get(key, "") for key in FLAT_PROVENANCE_FIELDS}
    extraction = envelope.get("extraction") or {}
    if not isinstance(extraction, dict):
        raise ValueError("Extraction payload must be an object")
    page_fields = {key: value for key, value in extraction.items() if key != record_list_field}
    page_collisions = sorted(set(page_fields) & RESERVED_MODEL_FIELDS)
    if page_collisions:
        raise ValueError(f"Model page fields collide with runner-owned fields: {', '.join(page_collisions)}")
    records = extraction.get(record_list_field, [])
    if not isinstance(records, list):
        raise ValueError(f"Extraction field {record_list_field!r} must be a list")
    if not records:
        return [{**page_fields, **provenance, "record_index": "", "record_id": ""}]
    flattened: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Extraction record {index} must be an object")
        record_collisions = sorted(set(record) & RESERVED_MODEL_FIELDS)
        if record_collisions:
            raise ValueError(f"Model record fields collide with runner-owned fields: {', '.join(record_collisions)}")
        flattened.append(
            {
                **page_fields,
                **record,
                **provenance,
                GENERATED_FLAT_FIELDS[0]: index,
                GENERATED_FLAT_FIELDS[1]: f"{envelope['page_id']}#record={index:04d}",
            }
        )
    return flattened


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], *, fieldnames: Iterable[str] = ()) -> None:
    materialized = [jsonable(row) for row in rows]
    fields: list[str] = list(fieldnames)
    for row in materialized:
        fields.extend(key for key in row if key not in fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        for row in materialized:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
    temporary.replace(path)
