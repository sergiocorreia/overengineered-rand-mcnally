#!/usr/bin/env -S uv run --project .
"""Run a bounded, provenance-signed Rand McNally page extraction."""

import csv
import fcntl
import importlib
import importlib.metadata
import json
import math
import os
import re
import threading
import tomllib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Fallback activates only after a decoder mismatch; each volume was visually checked.
JPX_FALLBACK_ALLOWLIST: set[tuple[int, int]] = {(1881, 1), (1888, 2), (1916, 1)}
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
QUEUE_REQUIRED_FIELDS = (
    "selection_rank",
    "page_id",
    "pdf_relative_path",
    "physical_page",
    "source_id",
    "source_sha256",
    "year",
    "edition",
    "pdf_part",
    "page_evidence_sha256",
)
QUEUE_RECEIPT_NAME = "ranking_receipt.json"
QUEUE_RECEIPT_SCHEMA = "rand-mcnally-rerun-ranking/v2"
PAID_STATUSES = frozenset({"completed", "failed", "failed_paid", "provider_error"})
NONPAID_STATUSES = frozenset({"planned", "authorized", "pending", "cancelled", "skipped"})
ABSOLUTE_RERUN_CEILING = 5_347
DEFAULT_WILSON_Z = 1.96
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
# Queueing, worker count, and export layout are not extraction-contract inputs.
# This is the audited runner revision whose extraction-affecting logic remains in use.
EXTRACTION_IMPLEMENTATION_SHA256 = "c0b5c5dcf75001c158f6c04d6b4bf36485a0f47725b8bf8ada9a60bec44909a5"
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
    selection_rank: int = 0
    page_evidence_sha256: str = ""
    source: SourceConfig | None = None
    document: Document | None = None
    schema: type[BaseModel] | None = None
    prompt: str = ""
    definition: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PageOutcome:
    plan: PagePlan
    result: yachay.OCRResult | None
    cache_status: str
    error_type: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class QueuePage:
    selection_rank: int
    year: int
    edition: int
    pdf_part: int
    page_id: str
    pdf_relative_path: str
    physical_page: int
    source_id: str
    source_sha256: str
    page_evidence_sha256: str


@dataclass(frozen=True, slots=True)
class QueueEvidence:
    queue_path: Path
    receipt_path: Path
    queue_relative_path: str
    queue_sha256: str
    queue_bytes: int
    queue_rows: int
    receipt_signature: str
    denominator: int
    fraction: float
    hard_ceiling: int
    computed_cap: int


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def checked_write_path(config: ProjectConfig, path: Path) -> Path:
    """Allow writes only in V2 or its external root, never in immutable V1 trees."""

    resolved = path.expanduser().resolve()
    restoration = config.table("restoration")
    legacy_root = Path(str(restoration.get("legacy_root", ""))).expanduser().resolve()
    if restoration.get("legacy_root_read_only") is not True:
        raise ValueError("project.toml must declare restoration.legacy_root_read_only = true")
    if is_relative_to(resolved, legacy_root):
        raise ValueError(f"Refusing a write inside the immutable legacy project: {resolved}")
    recovered_v1_root = Path(str(restoration.get("recovered_v1_root", ""))).expanduser().resolve()
    if restoration.get("recovered_v1_root_read_only") is not True:
        raise ValueError("project.toml must declare restoration.recovered_v1_root_read_only = true")
    if is_relative_to(resolved, recovered_v1_root):
        raise ValueError(f"Refusing a write inside the immutable recovered V1 tree: {resolved}")
    if not (is_relative_to(resolved, config.root.resolve()) or is_relative_to(resolved, config.external_root)):
        raise ValueError(f"Write path is outside the V2 project and external root: {resolved}")
    return resolved


def validate_write_destinations(config: ProjectConfig) -> None:
    """Reject every configured extraction write root before doing any work."""

    cache_root = config.external_path("cache_subdirectory", "data-extraction/cache")
    for destination in (
        config.external_root,
        config.external_path("render_subdirectory", "rendered-pages"),
        cache_root,
        cache_root.parent / "paid-page-ledger",
        config.external_path("export_subdirectory", "data-extraction/exports") / "targeted",
    ):
        checked_write_path(config, destination)


def _project_file(config: ProjectConfig, raw_path: str, *, label: str) -> Path:
    """Resolve a receipt-bound path without permitting absolute paths or traversal."""

    if not raw_path or raw_path != raw_path.strip():
        raise ValueError(f"{label} must be a nonblank normalized path")
    candidate = Path(raw_path)
    if candidate.is_absolute() or "\\" in raw_path or ".." in candidate.parts:
        raise ValueError(f"{label} must be a project-relative POSIX path: {raw_path!r}")
    resolved = (config.root / candidate).resolve()
    if not is_relative_to(resolved, config.root.resolve()):
        raise ValueError(f"{label} escapes the V2 project: {raw_path!r}")
    if resolved.relative_to(config.root.resolve()).as_posix() != raw_path:
        raise ValueError(f"{label} is not normalized: {raw_path!r}")
    return resolved


def _required_int(raw: Any, *, label: str, minimum: int) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an integer") from error
    if str(value) != str(raw).strip() or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _required_sha256(raw: Any, *, label: str) -> str:
    value = str(raw)
    if SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def computed_rerun_cap(denominator: int, fraction: float, hard_ceiling: int) -> int:
    if denominator <= 0:
        raise ValueError("verified denominator must be positive")
    if not 0 < fraction <= 0.05:
        raise ValueError("rerun fraction must be positive and no greater than 5%")
    if hard_ceiling <= 0:
        raise ValueError("hard ceiling must be positive")
    return min(ABSOLUTE_RERUN_CEILING, hard_ceiling, math.floor(denominator * fraction))


def _required_float(raw: Any, *, label: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{label} must be >= {minimum}{upper}")
    return value


def validate_queue_trial_policy(config: ProjectConfig, receipt: dict[str, Any], queue_rows: int) -> None:
    """Require the signed queue to use the exact configured calibration and trial policy."""

    if receipt.get("schema_version") != QUEUE_RECEIPT_SCHEMA:
        raise ValueError(f"Ranking receipt must use schema {QUEUE_RECEIPT_SCHEMA}")
    review = config.table("review_prioritization")
    integer_policy = {
        "sample_pages": _required_int(review.get("calibration_pages"), label="configured calibration_pages", minimum=1),
        "documented_pages": _required_int(review.get("calibration_documented"), label="configured calibration_documented", minimum=1),
        "candidate_pages": _required_int(review.get("calibration_candidates"), label="configured calibration_candidates", minimum=1),
        "control_pages": _required_int(review.get("calibration_controls"), label="configured calibration_controls", minimum=1),
        "minimum_candidate_reviews": _required_int(
            review.get("minimum_candidate_reviews"), label="configured minimum_candidate_reviews", minimum=1
        ),
        "trial_max_pages": _required_int(review.get("trial_max_pages"), label="configured trial_max_pages", minimum=1),
    }
    if integer_policy["sample_pages"] != sum(
        integer_policy[key] for key in ("documented_pages", "candidate_pages", "control_pages")
    ):
        raise ValueError("Configured calibration strata do not sum to calibration_pages")
    numeric_policy = {
        "minimum_observed_precision": _required_float(
            review.get("minimum_observed_precision"),
            label="configured minimum_observed_precision",
            minimum=0.0,
            maximum=1.0,
        ),
        "minimum_wilson_lower": _required_float(
            review.get("minimum_wilson_lower_95"),
            label="configured minimum_wilson_lower_95",
            minimum=0.0,
            maximum=1.0,
        ),
        "wilson_z": _required_float(
            review.get("wilson_z", DEFAULT_WILSON_Z), label="configured wilson_z", minimum=0.0
        ),
    }
    expected_policy: dict[str, int | float] = {**integer_policy, **numeric_policy}
    actual_policy = receipt.get("calibration_policy")
    if not isinstance(actual_policy, dict) or set(actual_policy) != set(expected_policy):
        raise ValueError("Ranking receipt calibration_policy does not match project policy")
    for key, expected in expected_policy.items():
        if key in integer_policy:
            actual: int | float = _required_int(actual_policy.get(key), label=f"receipt calibration_policy.{key}", minimum=1)
        else:
            actual = _required_float(
                actual_policy.get(key),
                label=f"receipt calibration_policy.{key}",
                minimum=0.0,
                maximum=None if key == "wilson_z" else 1.0,
            )
        if actual != expected:
            raise ValueError(f"Ranking receipt calibration_policy.{key} differs from project policy")
    if receipt.get("calibration_gate_passed") is not True:
        raise ValueError("Ranking receipt calibration gate did not pass")
    candidate = receipt.get("calibration_results", {}).get("candidate")
    if not isinstance(candidate, dict) or candidate.get("gate_passed") is not True:
        raise ValueError("Ranking receipt candidate calibration gate did not pass")
    if queue_rows > integer_policy["trial_max_pages"]:
        raise ValueError(
            f"Signed queue has {queue_rows} rows, exceeding configured trial_max_pages {integer_policy['trial_max_pages']}"
        )


def validate_queue_execution_policy(
    config: ProjectConfig,
    plans: list[PagePlan],
    workers: int,
    *,
    require_successful_ramp: bool,
) -> None:
    """Enforce the reviewed ramp and worker ceilings before a queue can go live."""

    review = config.table("review_prioritization")
    trial_max = _required_int(review.get("trial_max_pages"), label="configured trial_max_pages", minimum=1)
    ramp_pages = _required_int(review.get("trial_ramp_pages"), label="configured trial_ramp_pages", minimum=1)
    ramp_workers = _required_int(review.get("trial_ramp_workers"), label="configured trial_ramp_workers", minimum=1)
    trial_workers = _required_int(review.get("trial_workers"), label="configured trial_workers", minimum=1)
    if ramp_pages >= trial_max:
        raise ValueError("configured trial_ramp_pages must be smaller than trial_max_pages")
    if ramp_workers > trial_workers:
        raise ValueError("configured trial_ramp_workers must not exceed trial_workers")

    worker_ceiling = ramp_workers if len(plans) <= ramp_pages else trial_workers
    if workers > worker_ceiling:
        cohort = "ramp" if len(plans) <= ramp_pages else "trial"
        raise ValueError(f"--workers {workers} exceeds the configured {cohort} ceiling {worker_ceiling}")
    if not require_successful_ramp or len(plans) <= ramp_pages:
        return

    ramp = plans[:ramp_pages]
    unsuccessful = [plan.page_id for plan in ramp if plan.cached_result is None]
    if unsuccessful:
        errors = sum(bool(plan.cached_error_type) for plan in ramp)
        missing = len(unsuccessful) - errors
        raise RuntimeError(
            "Live remainder blocked: the first "
            f"{ramp_pages} ramp pages require successful result-cache hits ({errors} cached errors, {missing} pending). "
            "A failed ramp needs a new explicit owner decision."
        )


def load_signed_queue(
    config: ProjectConfig,
    queue_path: Path,
    *,
    limit: int | None,
) -> tuple[QueueEvidence, list[QueuePage]]:
    """Load a deterministic selection queue only after verifying all signed evidence."""

    candidate_queue = queue_path.expanduser()
    if not candidate_queue.is_absolute():
        candidate_queue = config.root / candidate_queue
    resolved_queue = candidate_queue.resolve()
    project_root = config.root.resolve()
    if not is_relative_to(resolved_queue, project_root):
        raise ValueError("--queue must resolve inside the V2 project")
    if not resolved_queue.is_file():
        raise FileNotFoundError(f"Selection queue is absent: {resolved_queue}")
    queue_relative = resolved_queue.relative_to(project_root).as_posix()
    if Path(queue_relative).name != resolved_queue.name:
        raise AssertionError("Queue path normalization failed")
    queue_bytes = resolved_queue.read_bytes()
    queue_digest = sha256_file(resolved_queue)
    try:
        reader = csv.DictReader(StringIO(queue_bytes.decode("utf-8")), delimiter="\t")
        fieldnames = tuple(reader.fieldnames or ())
        missing_fields = [field for field in QUEUE_REQUIRED_FIELDS if field not in fieldnames]
        if missing_fields:
            raise ValueError(f"Selection queue is missing fields: {', '.join(missing_fields)}")
        raw_rows = list(reader)
    except UnicodeDecodeError as error:
        raise ValueError(f"Selection queue is not UTF-8: {resolved_queue}") from error
    if not raw_rows:
        raise ValueError("Selection queue contains no pages")

    rows: list[QueuePage] = []
    page_ids: set[str] = set()
    for expected_rank, row in enumerate(raw_rows, start=1):
        rank = _required_int(row.get("selection_rank"), label="selection_rank", minimum=1)
        if rank != expected_rank:
            raise ValueError("selection_rank must be consecutive and match queue row order")
        year = _required_int(row.get("year"), label=f"queue row {rank} year", minimum=1)
        edition = _required_int(row.get("edition"), label=f"queue row {rank} edition", minimum=1)
        if edition not in (1, 2):
            raise ValueError(f"queue row {rank} edition must be 1 or 2")
        pdf_part = _required_int(row.get("pdf_part"), label=f"queue row {rank} pdf_part", minimum=0)
        page = _required_int(row.get("physical_page"), label=f"queue row {rank} physical_page", minimum=1)
        relative_path = str(row.get("pdf_relative_path") or "")
        pdf_path = Path(relative_path)
        if (
            not relative_path
            or relative_path != relative_path.strip()
            or pdf_path.is_absolute()
            or "\\" in relative_path
            or ".." in pdf_path.parts
            or pdf_path.as_posix() != relative_path
            or pdf_path.suffix.lower() != ".pdf"
        ):
            raise ValueError(f"queue row {rank} has an invalid pdf_relative_path")
        page_id = str(row.get("page_id") or "")
        if page_id != f"{relative_path}#page={page}":
            raise ValueError(f"queue row {rank} page_id does not match its source path and physical page")
        if page_id in page_ids:
            raise ValueError(f"Duplicate canonical page_id in selection queue: {page_id}")
        page_ids.add(page_id)
        source_id = str(row.get("source_id") or "")
        if not source_id or source_id != source_id.strip():
            raise ValueError(f"queue row {rank} source_id must be nonblank without surrounding whitespace")
        rows.append(
            QueuePage(
                selection_rank=rank,
                year=year,
                edition=edition,
                pdf_part=pdf_part,
                page_id=page_id,
                pdf_relative_path=relative_path,
                physical_page=page,
                source_id=source_id,
                source_sha256=_required_sha256(row.get("source_sha256"), label=f"queue row {rank} source_sha256"),
                page_evidence_sha256=_required_sha256(row.get("page_evidence_sha256"), label=f"queue row {rank} page_evidence_sha256"),
            )
        )

    receipt_path = (resolved_queue.parent / QUEUE_RECEIPT_NAME).resolve()
    if not is_relative_to(receipt_path, project_root):
        raise ValueError("Ranking receipt must resolve inside the V2 project")
    if not receipt_path.is_file():
        raise FileNotFoundError(f"Signed ranking receipt is absent: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unreadable ranking receipt: {receipt_path}") from error
    if not isinstance(receipt, dict):
        raise ValueError("Ranking receipt must be a JSON object")
    signature = receipt.get("receipt_signature")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_signature"}
    if not isinstance(signature, str) or signature != stable_hash(unsigned):
        raise ValueError("Ranking receipt signature is invalid")

    if receipt.get("selected_queue_path") != queue_relative:
        raise ValueError("Ranking receipt selected_queue_path does not match --queue")
    if receipt.get("selected_queue_sha256") != queue_digest:
        raise ValueError("Selection queue SHA-256 does not match its ranking receipt")
    if receipt.get("selected_queue_bytes") != len(queue_bytes):
        raise ValueError("Selection queue byte count does not match its ranking receipt")
    if receipt.get("selected_queue_rows") != len(rows):
        raise ValueError("Selection queue row count does not match its ranking receipt")
    validate_queue_trial_policy(config, receipt, len(rows))

    restoration = config.table("restoration")
    denominator = _required_int(receipt.get("denominator"), label="receipt denominator", minimum=1)
    try:
        fraction = float(receipt.get("fraction"))
    except (TypeError, ValueError) as error:
        raise ValueError("receipt fraction must be numeric") from error
    hard_ceiling = _required_int(receipt.get("hard_ceiling"), label="receipt hard_ceiling", minimum=1)
    expected_fraction = float(restoration.get("provisional_rerun_fraction", 0.05))
    expected_hard_ceiling = min(
        ABSOLUTE_RERUN_CEILING,
        int(restoration.get("provisional_rerun_ceiling", ABSOLUTE_RERUN_CEILING)),
    )
    if not math.isclose(fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("Ranking receipt fraction differs from project policy")
    if hard_ceiling != expected_hard_ceiling:
        raise ValueError("Ranking receipt hard ceiling differs from project policy")
    computed_cap = computed_rerun_cap(denominator, fraction, hard_ceiling)
    if receipt.get("computed_cap") != computed_cap:
        raise ValueError("Ranking receipt computed_cap is incorrect")

    input_hashes = receipt.get("input_sha256s")
    if not isinstance(input_hashes, dict) or not input_hashes:
        raise ValueError("Ranking receipt input_sha256s must be a nonempty object")
    for raw_path, expected_hash in sorted(input_hashes.items()):
        input_path = _project_file(config, str(raw_path), label="ranking input path")
        if not input_path.is_file():
            raise FileNotFoundError(f"Ranking input is absent: {input_path}")
        digest = _required_sha256(expected_hash, label=f"ranking input {raw_path} SHA-256")
        if sha256_file(input_path) != digest:
            raise ValueError(f"Ranking input changed after selection: {raw_path}")

    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be positive")
        if limit > len(rows):
            raise ValueError(f"--limit {limit} exceeds the signed queue's {len(rows)} rows")
        rows = rows[:limit]
    evidence = QueueEvidence(
        queue_path=resolved_queue,
        receipt_path=receipt_path,
        queue_relative_path=queue_relative,
        queue_sha256=queue_digest,
        queue_bytes=len(queue_bytes),
        queue_rows=len(raw_rows),
        receipt_signature=signature,
        denominator=denominator,
        fraction=fraction,
        hard_ceiling=hard_ceiling,
        computed_cap=computed_cap,
    )
    return evidence, rows


def load_manifest_by_filename(path: Path, filename: str, *, required: bool = True) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as source:
        matches = [row for row in csv.DictReader(source, delimiter="\t") if row["filename"].strip() == filename]
    if len(matches) > 1 or (required and not matches):
        raise ValueError(f"Expected one source-manifest row for {filename}, found {len(matches)}")
    return matches[0] if matches else {}


def default_source_id(year: int, edition: int, source_name: str) -> str:
    source_token = re.sub(r"[^a-z0-9]+", "_", source_name.casefold()).strip("_")
    if not source_token:
        raise ValueError("Source name cannot form a stable source_id")
    return f"rand_mcnally_{year}_{edition}_{source_token}"


def load_source_config(
    config: ProjectConfig,
    year: int,
    edition: int,
    *,
    require_manifest: bool = True,
) -> SourceConfig:
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
        manifest = load_manifest_by_filename(SOURCE_MANIFEST_FILE, pdf_path.name, required=require_manifest)
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
        source_id=manifest.get("source_id", default_source_id(year, edition, source_name)),
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


def resolve_queue_document(config: ProjectConfig, source: SourceConfig, row: QueuePage) -> Document:
    """Resolve the queue's exact PDF identity; never infer a multipart filename."""

    if row.source_id != source.source_id:
        raise ValueError(f"Queue source_id mismatch for {row.page_id}")
    if source.is_single_pdf and row.pdf_part != 0:
        raise ValueError(f"Single-PDF queue row must use pdf_part=0: {row.page_id}")
    if not source.is_single_pdf and row.pdf_part < 1:
        raise ValueError(f"Multipart queue row must use a positive pdf_part: {row.page_id}")
    if not source.is_single_pdf and not source.start <= row.pdf_part <= source.end:
        raise ValueError(f"Queue pdf_part is outside the configured range for {row.page_id}")

    pdf_root = config.pdf_directory.resolve()
    pdf_path = (pdf_root / row.pdf_relative_path).resolve()
    if not is_relative_to(pdf_path, pdf_root):
        raise ValueError(f"Queue PDF escapes the configured V2 PDF root: {row.page_id}")
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Queue PDF is absent: {pdf_path}")
    if source.is_single_pdf:
        if pdf_path != source.path.resolve():
            raise ValueError(f"Queue PDF does not match configured source: {row.page_id}")
    elif not is_relative_to(pdf_path, source.path.resolve()):
        raise ValueError(f"Queue PDF is outside its configured multipart source: {row.page_id}")

    source_sha256 = sha256_file(pdf_path)
    if source_sha256 != row.source_sha256:
        raise ValueError(f"Queue source SHA-256 is stale for {row.page_id}")
    if source.expected_sha256 and source_sha256 != source.expected_sha256:
        raise ValueError(f"Source-manifest SHA-256 mismatch for {row.page_id}")
    with pymupdf.open(pdf_path) as pdf:
        page_count = len(pdf)
    if row.physical_page > page_count:
        raise ValueError(f"Queue page is outside the PDF's physical range for {row.page_id}")
    if source.is_single_pdf and source.source != "archive-raw" and not source.start <= row.physical_page <= source.end:
        raise ValueError(f"Queue page is outside the configured extraction range for {row.page_id}")
    if source.min_pages is not None and page_count < source.min_pages:
        raise ValueError(f"Page count is below the source-manifest minimum for {row.page_id}")
    if source.max_pages is not None and page_count > source.max_pages:
        raise ValueError(f"Page count is above the source-manifest maximum for {row.page_id}")
    return Document(
        path=pdf_path,
        relative_path=row.pdf_relative_path,
        part=row.pdf_part,
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
        "runner_sha256": EXTRACTION_IMPLEMENTATION_SHA256,
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
    for selection_rank, page in enumerate(pages, start=1):
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
                selection_rank=selection_rank,
                source=source,
                document=document,
                schema=schema,
                prompt=prompt,
                definition=definition,
            )
        )
    return {"definition_signature": definition_signature, "definition": definition}, plans


def prepare_queue_plans(
    config: ProjectConfig,
    rows: list[QueuePage],
    settings: ModelSettings,
) -> list[PagePlan]:
    """Resolve and prepare a mixed-source queue while retaining its signed order."""

    yachay_info = yachay_provenance(config)
    cache_root = config.external_path("cache_subdirectory", "data-extraction/cache")
    source_cache: dict[tuple[int, int], SourceConfig] = {}
    definition_cache: dict[tuple[int, int], tuple[type[BaseModel], str, dict[str, Any], str]] = {}
    document_cache: dict[tuple[int, int, int, str, str], Document] = {}
    plans: list[PagePlan] = []
    for row in rows:
        source_key = (row.year, row.edition)
        source = source_cache.get(source_key)
        if source is None:
            source = load_source_config(config, row.year, row.edition, require_manifest=False)
            source_cache[source_key] = source
        if source_key not in definition_cache:
            schema, prompt, schema_source = load_definition(source.variant)
            definition = definition_payload(config, source, schema, prompt, schema_source, settings, yachay_info)
            definition_cache[source_key] = (schema, prompt, definition, stable_hash(definition))
        schema, prompt, definition, definition_signature = definition_cache[source_key]

        document_key_value = (row.year, row.edition, row.pdf_part, row.pdf_relative_path, row.source_sha256)
        document = document_cache.get(document_key_value)
        if document is None:
            document = resolve_queue_document(config, source, row)
            document_cache[document_key_value] = document
        render_path = render_page(config, source, document, row.physical_page, yachay_info)
        render_sha256 = sha256_file(render_path)
        payload = {
            "definition_signature": definition_signature,
            "page_id": row.page_id,
            "source_id": source.source_id,
            "source_relative_path": document.relative_path,
            "source_sha256": document.source_sha256,
            "part": document.part,
            "physical_page": row.physical_page,
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
            / f"page-{row.physical_page:06d}"
            / f"{render_sha256}.json",
        )
        cached_error = load_cached_error(cache_path, payload, signature)
        if cache_path.is_file() and cached_error is not None:
            raise RuntimeError(f"Both result and error caches exist for {row.page_id}")
        cached = load_cached_result(cache_path, payload, signature, schema) if cache_path.is_file() else None
        plans.append(
            PagePlan(
                page=row.physical_page,
                page_id=row.page_id,
                render_path=render_path,
                render_sha256=render_sha256,
                contract_signature=signature,
                contract_payload=payload,
                cache_path=cache_path,
                cached_result=cached,
                cached_error_type=cached_error[0] if cached_error else "",
                cached_error_message=cached_error[1] if cached_error else "",
                selection_rank=row.selection_rank,
                page_evidence_sha256=row.page_evidence_sha256,
                source=source,
                document=document,
                schema=schema,
                prompt=prompt,
                definition=definition,
            )
        )
    return plans


def require_user_adc(project_id: str, adc_path: Path | None = None) -> None:
    """Verify only non-secret local ADC metadata; Yachay loads the credential itself."""

    if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
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
        # Yachay counts total attempts here, so one means no retry.
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


def paid_ledger_directory(config: ProjectConfig) -> Path:
    cache_root = config.external_path("cache_subdirectory", "data-extraction/cache")
    return checked_write_path(config, cache_root.parent / "paid-page-ledger")


def _manual_paid_pages(config: ProjectConfig) -> tuple[set[str], str]:
    ledger_path = config.root / "manual" / "rerun_pages.tsv"
    if not ledger_path.is_file():
        return set(), "absent"
    with ledger_path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = set(reader.fieldnames or ())
        if not {"page_id", "status"}.issubset(fields):
            raise ValueError(f"Manual rerun ledger lacks page_id/status: {ledger_path}")
        paid: set[str] = set()
        for line, row in enumerate(reader, start=2):
            page_id = str(row.get("page_id") or "").strip()
            status = str(row.get("status") or "").strip().lower()
            if not page_id:
                raise ValueError(f"Blank page_id in manual rerun ledger at line {line}")
            if status in PAID_STATUSES:
                paid.add(page_id)
            elif status not in NONPAID_STATUSES:
                raise ValueError(f"Unknown manual rerun ledger status {status!r} at line {line}")
    return paid, sha256_file(ledger_path)


def _reservation_records(config: ProjectConfig) -> tuple[list[dict[str, Any]], set[str]]:
    directory = paid_ledger_directory(config)
    if not directory.is_dir():
        return [], set()
    records: list[dict[str, Any]] = []
    reserved: set[str] = set()
    for path in sorted(directory.glob("reservation-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unreadable paid-page reservation: {path}") from error
        if not isinstance(payload, dict):
            raise RuntimeError(f"Paid-page reservation must be an object: {path}")
        verify_record_hash(payload, path)
        if payload.get("ledger_format") != 1:
            raise RuntimeError(f"Unsupported paid-page reservation format: {path}")
        page_ids = payload.get("page_ids")
        if not isinstance(page_ids, list) or not page_ids or any(not isinstance(value, str) or not value for value in page_ids):
            raise RuntimeError(f"Invalid page_ids in paid-page reservation: {path}")
        if len(page_ids) != len(set(page_ids)):
            raise RuntimeError(f"Duplicate page_ids within paid-page reservation: {path}")
        overlap = reserved.intersection(page_ids)
        if overlap:
            raise RuntimeError(f"Page appears in multiple paid-page reservations: {sorted(overlap)[0]}")
        reserved.update(page_ids)
        records.append(payload)
    return records, reserved


def _budget_policy(config: ProjectConfig, evidence: QueueEvidence | None) -> tuple[int, float, int, int]:
    restoration = config.table("restoration")
    if evidence is not None:
        return evidence.denominator, evidence.fraction, evidence.hard_ceiling, evidence.computed_cap
    denominator = int(restoration.get("provisional_page_denominator", 0))
    fraction = float(restoration.get("provisional_rerun_fraction", 0.05))
    hard_ceiling = min(
        ABSOLUTE_RERUN_CEILING,
        int(restoration.get("provisional_rerun_ceiling", ABSOLUTE_RERUN_CEILING)),
    )
    return denominator, fraction, hard_ceiling, computed_rerun_cap(denominator, fraction, hard_ceiling)


def validate_paid_budget(
    config: ProjectConfig,
    pending: Iterable[PagePlan],
    evidence: QueueEvidence | None,
) -> dict[str, Any]:
    """Fail closed on duplicate attempts and the 5% unique-page ceiling."""

    pending_ids = [plan.page_id for plan in pending]
    if len(pending_ids) != len(set(pending_ids)):
        raise ValueError("Pending extraction contains duplicate page IDs")
    manual_paid, manual_hash = _manual_paid_pages(config)
    _, reserved = _reservation_records(config)
    duplicate_attempts = reserved.intersection(pending_ids)
    if duplicate_attempts:
        raise RuntimeError(f"A paid-page reservation already exists for {sorted(duplicate_attempts)[0]}")
    denominator, fraction, hard_ceiling, cap = _budget_policy(config, evidence)
    new_unique = set(pending_ids).difference(manual_paid).difference(reserved)
    used = manual_paid | reserved
    if len(used | new_unique) > cap:
        raise RuntimeError(f"5% page ceiling blocks this run: {len(used)} already counted + {len(new_unique)} new > cap {cap}")
    return {
        "denominator": denominator,
        "fraction": fraction,
        "hard_ceiling": hard_ceiling,
        "computed_cap": cap,
        "manual_ledger_sha256": manual_hash,
        "prior_paid_unique": len(used),
        "new_unique_pages": sorted(new_unique),
        "remaining_after": cap - len(used | new_unique),
    }


def reserve_paid_pages(
    config: ProjectConfig,
    run_id: str,
    pending: list[PagePlan],
    evidence: QueueEvidence | None,
) -> Path | None:
    """Atomically reserve uncached pages before any provider request is submitted."""

    if not pending:
        return None
    directory = paid_ledger_directory(config)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        budget = validate_paid_budget(config, pending, evidence)
        unsigned = {
            "ledger_format": 1,
            "reservation_id": stable_hash(
                {
                    "run_id": run_id,
                    "page_ids": [plan.page_id for plan in pending],
                    "queue_receipt_signature": evidence.receipt_signature if evidence else None,
                }
            )[:24],
            "created_at": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "queue_path": evidence.queue_relative_path if evidence else None,
            "queue_sha256": evidence.queue_sha256 if evidence else None,
            "queue_receipt_signature": evidence.receipt_signature if evidence else None,
            "manual_ledger_sha256": budget["manual_ledger_sha256"],
            "denominator": budget["denominator"],
            "fraction": budget["fraction"],
            "hard_ceiling": budget["hard_ceiling"],
            "computed_cap": budget["computed_cap"],
            "prior_paid_unique": budget["prior_paid_unique"],
            "new_unique_page_ids": budget["new_unique_pages"],
            "remaining_after": budget["remaining_after"],
            "page_ids": [plan.page_id for plan in pending],
            "contract_signatures": [plan.contract_signature for plan in pending],
        }
        payload = {**unsigned, "record_sha256": stable_hash(unsigned)}
        path = directory / f"reservation-{unsigned['reservation_id']}.json"
        if path.exists():
            raise RuntimeError(f"Paid-page reservation ID collision: {path}")
        atomic_write_json(path, payload)
        return path


def execute_page_plans(
    plans: list[PagePlan],
    settings: ModelSettings,
    *,
    workers: int,
    run_id: str,
) -> tuple[list[PageOutcome], int]:
    """Extract uncached pages concurrently and return outcomes in queue order."""

    if workers < 1:
        raise ValueError("--workers must be positive")
    indexed_outcomes: dict[int, PageOutcome] = {}
    pending: list[tuple[int, PagePlan]] = []
    for index, plan in enumerate(plans):
        if plan.cached_result is not None:
            indexed_outcomes[index] = PageOutcome(plan=plan, result=plan.cached_result, cache_status="hit")
        elif plan.cached_error_type:
            indexed_outcomes[index] = PageOutcome(
                plan=plan,
                result=None,
                cache_status="error-hit",
                error_type=plan.cached_error_type,
                error_message=plan.cached_error_message,
            )
        else:
            if plan.schema is None or not plan.prompt:
                raise ValueError(f"Page plan lacks an extraction definition: {plan.page_id}")
            pending.append((index, plan))
    if not pending:
        return [indexed_outcomes[index] for index in range(len(plans))], 0

    worker_state = threading.local()
    counter_lock = threading.Lock()
    provider_calls = 0

    def extract_one(plan: PagePlan) -> PageOutcome:
        nonlocal provider_calls
        extractor = getattr(worker_state, "extractor", None)
        if extractor is None:
            extractor = make_extractor(settings)
            worker_state.extractor = extractor
        try:
            with counter_lock:
                provider_calls += 1
            result = extractor.extract(
                image=plan.render_path,
                prompt=plan.prompt,
                schema=plan.schema,
                name=None,
                page=plan.page,
            )
            if result is None:
                raise RuntimeError("Yachay returned no result")
        except Exception as error:
            cache_error(plan, run_id, error)
            return PageOutcome(
                plan=plan,
                result=None,
                cache_status="error",
                error_type=type(error).__name__,
                error_message=str(error),
            )
        cache_result(plan, result)
        return PageOutcome(plan=plan, result=result, cache_status="fresh")

    with ThreadPoolExecutor(max_workers=min(workers, len(pending)), thread_name_prefix="rand-mcnally-ocr") as pool:
        future_indexes = {pool.submit(extract_one, plan): index for index, plan in pending}
        for future in as_completed(future_indexes):
            indexed_outcomes[future_indexes[future]] = future.result()
    return [indexed_outcomes[index] for index in range(len(plans))], provider_calls


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


def _require_plan_context(plan: PagePlan) -> tuple[SourceConfig, Document]:
    if plan.source is None or plan.document is None:
        raise ValueError(f"Page plan lacks source provenance: {plan.page_id}")
    return plan.source, plan.document


def mixed_output_fieldnames(plans: list[PagePlan]) -> tuple[list[str], list[str]]:
    bank_prefix = ["row_id", "source_id", "year", "edition", "pdf_relative_path", "part", "pdf_page", "page_id", "index"]
    correspondent_prefix = [
        "row_id",
        "source_id",
        "year",
        "edition",
        "pdf_relative_path",
        "part",
        "pdf_page",
        "page_id",
        "corr_index",
    ]
    bank_fields: list[str] = []
    correspondent_fields: list[str] = []
    seen_variants: set[str] = set()
    for plan in plans:
        source, _ = _require_plan_context(plan)
        if source.variant in seen_variants:
            continue
        seen_variants.add(source.variant)
        variant_bank, variant_correspondent = output_fieldnames(source.variant)
        for field in variant_bank[5:]:
            if field not in bank_fields:
                bank_fields.append(field)
        for field in variant_correspondent[3:]:
            if field not in correspondent_fields:
                correspondent_fields.append(field)
    return [*bank_prefix, *bank_fields], [*correspondent_prefix, *correspondent_fields]


def flatten_mixed_results(
    outcomes: list[PageOutcome],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    bank_rows: list[dict[str, Any]] = []
    correspondent_rows: list[dict[str, Any]] = []
    token_rows: list[dict[str, Any]] = []
    row_id = 0
    for outcome in outcomes:
        source, document = _require_plan_context(outcome.plan)
        result = outcome.result
        if result is None:
            continue
        token_rows.append(
            {
                "selection_rank": outcome.plan.selection_rank,
                "page_id": outcome.plan.page_id,
                "source_id": source.source_id,
                "year": source.year,
                "edition": source.edition,
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
                "source_id": source.source_id,
                "year": source.year,
                "edition": source.edition,
                "pdf_relative_path": document.relative_path,
                "part": document.part,
                "pdf_page": outcome.plan.page,
                "page_id": outcome.plan.page_id,
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
                    "source_id": source.source_id,
                    "year": source.year,
                    "edition": source.edition,
                    "pdf_relative_path": document.relative_path,
                    "part": document.part,
                    "pdf_page": outcome.plan.page,
                    "page_id": outcome.plan.page_id,
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


def write_queue_run_outputs(
    config: ProjectConfig,
    run_id: str,
    outcomes: list[PageOutcome],
    evidence: QueueEvidence,
    started_at: str,
    provider_calls: int,
    workers: int,
    reservation_path: Path | None,
) -> Path:
    """Export mixed schema regimes in signed queue order with a deterministic union schema."""

    run_root = config.external_path("export_subdirectory", "data-extraction/exports") / "targeted"
    run_dir = checked_write_path(config, run_root / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    plans = [outcome.plan for outcome in outcomes]
    bank_rows, correspondent_rows, token_rows = flatten_mixed_results(outcomes)
    bank_fields, correspondent_fields = mixed_output_fieldnames(plans)
    token_fields = (
        "selection_rank",
        "page_id",
        "source_id",
        "year",
        "edition",
        *TOKEN_FIELDS,
    )
    write_tsv(run_dir / "banks.tsv", bank_fields, bank_rows)
    write_tsv(run_dir / "correspondents.tsv", correspondent_fields, correspondent_rows)
    write_tsv(run_dir / "tokens.tsv", token_fields, token_rows)
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
            "selection_rank": outcome.plan.selection_rank,
            "page_evidence_sha256": outcome.plan.page_evidence_sha256,
            "result": outcome.result.to_dict(),
        }
        page_lines.append(json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str))
    atomic_write_text(run_dir / "pages.jsonl", "".join(f"{line}\n" for line in page_lines))

    definitions: list[dict[str, Any]] = []
    seen_definitions: set[str] = set()
    for plan in plans:
        signature = str(plan.contract_payload["definition_signature"])
        if signature not in seen_definitions:
            if plan.definition is None:
                raise ValueError(f"Page plan lacks its contract definition: {plan.page_id}")
            seen_definitions.add(signature)
            definitions.append({"definition_signature": signature, "definition": plan.definition})
    contract_payload = {
        "contract_format": 2,
        "queue": {
            "path": evidence.queue_relative_path,
            "sha256": evidence.queue_sha256,
            "receipt_signature": evidence.receipt_signature,
        },
        "definitions": definitions,
        "pages": [
            {
                **plan.contract_payload,
                "contract_signature": plan.contract_signature,
                "selection_rank": plan.selection_rank,
                "page_evidence_sha256": plan.page_evidence_sha256,
            }
            for plan in plans
        ],
    }
    atomic_write_json(run_dir / "contract.json", contract_payload)
    output_names = ("banks.tsv", "correspondents.tsv", "tokens.tsv", "errors.tsv", "pages.jsonl", "contract.json")
    output_hashes = {name: sha256_file(run_dir / name) for name in output_names}
    receipt = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "failed" if errors else "success",
        "queue_path": evidence.queue_relative_path,
        "queue_sha256": evidence.queue_sha256,
        "queue_receipt_signature": evidence.receipt_signature,
        "queue_rows": evidence.queue_rows,
        "selected_rows": len(outcomes),
        "computed_cap": evidence.computed_cap,
        "requested_pages": [plan.page_id for plan in plans],
        "cache_hits": sum(outcome.cache_status == "hit" for outcome in outcomes),
        "cached_errors": sum(outcome.cache_status == "error-hit" for outcome in outcomes),
        "fresh_results": sum(outcome.cache_status == "fresh" for outcome in outcomes),
        "provider_calls": provider_calls,
        "workers": workers,
        "paid_page_reservation": (reservation_path.relative_to(config.external_root).as_posix() if reservation_path is not None else None),
        "page_status": [
            {
                "selection_rank": plan.selection_rank,
                "page_id": plan.page_id,
                "contract_signature": plan.contract_signature,
                "cache_status": outcome.cache_status,
                "error_type": outcome.error_type,
            }
            for plan, outcome in zip(plans, outcomes, strict=True)
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
    workers: int = 1,
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
        "workers": workers,
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
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def print_queue_preflight(
    evidence: QueueEvidence,
    settings: ModelSettings,
    plans: list[PagePlan],
    max_requests: int,
    workers: int,
    budget: dict[str, Any],
) -> None:
    payload = {
        "queue_path": evidence.queue_relative_path,
        "queue_sha256": evidence.queue_sha256,
        "queue_receipt_signature": evidence.receipt_signature,
        "signed_queue_rows": evidence.queue_rows,
        "selected_rows": len(plans),
        "page_ids": [plan.page_id for plan in plans],
        "contract_signatures": [plan.contract_signature for plan in plans],
        "cache_hits": sum(plan.cached_result is not None for plan in plans),
        "cached_errors": sum(bool(plan.cached_error_type) for plan in plans),
        "pending_requests": sum(plan.cached_result is None and not plan.cached_error_type for plan in plans),
        "max_requests": max_requests,
        "workers": workers,
        "provider_calls": 0,
        "page_budget": budget,
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
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def run_targeted(
    *,
    year: int,
    edition: int,
    pages: list[int],
    part: int | None,
    max_requests: int,
    dry_run: bool,
    workers: int = 1,
) -> Path | None:
    if max_requests < 0:
        raise ValueError("--max-requests must be nonnegative")
    if workers < 1:
        raise ValueError("--workers must be positive")
    config = load_project_config(CODE_DIR)
    validate_write_destinations(config)
    source = load_source_config(config, year, edition)
    document = resolve_document(config, source, part)
    ordered_pages = validate_selected_pages(config, source, document, pages)
    schema, prompt, schema_source = load_definition(source.variant)
    settings = model_settings(config)
    definition, plans = prepare_page_plans(config, source, document, ordered_pages, schema, prompt, schema_source, settings)
    pending = [plan for plan in plans if plan.cached_result is None and not plan.cached_error_type]
    if len(pending) > max_requests:
        raise RuntimeError(f"Request ceiling blocks this run: {len(pending)} uncached pages > --max-requests {max_requests}")
    if pending:
        validate_paid_budget(config, pending, None)
    print_preflight(source, document, settings, plans, max_requests, workers)
    if dry_run:
        return None

    started_at = datetime.now(UTC).isoformat()
    run_id = utc_run_id()
    if pending:
        require_user_adc(settings.project_id)
        make_extractor(settings)
        reserve_paid_pages(config, run_id, pending, None)
    outcomes, provider_calls = execute_page_plans(plans, settings, workers=workers, run_id=run_id)

    run_dir = write_run_outputs(config, run_id, source, document, definition, outcomes, started_at, provider_calls)
    print(f"Run output: {run_dir}", flush=True)
    if any(outcome.error_type for outcome in outcomes):
        raise RuntimeError(f"Bounded extraction failed; inspect {run_dir / 'errors.tsv'}")
    return run_dir


def run_queue(
    *,
    queue_path: Path,
    limit: int | None,
    max_requests: int,
    workers: int | None,
    dry_run: bool,
) -> Path | None:
    if max_requests < 0:
        raise ValueError("--max-requests must be nonnegative")
    config = load_project_config(CODE_DIR)
    effective_workers = workers if workers is not None else int(config.table("extraction").get("default_workers", 1))
    if effective_workers < 1:
        raise ValueError("--workers must be positive")
    validate_write_destinations(config)
    evidence, rows = load_signed_queue(config, queue_path, limit=limit)
    settings = model_settings(config)
    plans = prepare_queue_plans(config, rows, settings)
    pending = [plan for plan in plans if plan.cached_result is None and not plan.cached_error_type]
    if len(pending) > max_requests:
        raise RuntimeError(f"Request ceiling blocks this run: {len(pending)} uncached pages > --max-requests {max_requests}")
    budget = validate_paid_budget(config, pending, evidence)
    validate_queue_execution_policy(
        config,
        plans,
        effective_workers,
        require_successful_ramp=not dry_run and bool(pending),
    )
    print_queue_preflight(evidence, settings, plans, max_requests, effective_workers, budget)
    if dry_run:
        return None

    started_at = datetime.now(UTC).isoformat()
    run_id = utc_run_id()
    reservation_path: Path | None = None
    if pending:
        require_user_adc(settings.project_id)
        make_extractor(settings)
        reservation_path = reserve_paid_pages(config, run_id, pending, evidence)
    outcomes, provider_calls = execute_page_plans(plans, settings, workers=effective_workers, run_id=run_id)
    run_dir = write_queue_run_outputs(
        config,
        run_id,
        outcomes,
        evidence,
        started_at,
        provider_calls,
        effective_workers,
        reservation_path,
    )
    print(f"Run output: {run_dir}", flush=True)
    if any(outcome.error_type for outcome in outcomes):
        raise RuntimeError(f"Bounded extraction failed; inspect {run_dir / 'errors.tsv'}")
    return run_dir


@app.command()
def main(
    max_requests: Annotated[int, typer.Option("--max-requests", help="Hard ceiling on uncached page requests.")],
    queue: Annotated[Path | None, typer.Option("--queue", help="Signed selected_pages.tsv inside the V2 project.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Use only the first N signed queue rows.")] = None,
    workers: Annotated[int | None, typer.Option("--workers", help="Concurrent thread-local OCR clients.")] = None,
    year: Annotated[int | None, typer.Option("--year", "-y", help="Publication year for direct smoke mode.")] = None,
    edition: Annotated[int | None, typer.Option("--edition", "-e", help="Edition number for direct smoke mode.")] = None,
    page: Annotated[
        list[int] | None,
        typer.Option("--page", "-p", help="Physical PDF page; repeat exactly as authorized in direct smoke mode."),
    ] = None,
    part: Annotated[int | None, typer.Option("--part", help="One-based PDF part for multipart sources.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Render and preflight without constructing a provider client.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable detailed Yachay logs.")] = False,
) -> None:
    yachay.set_log_level("DEBUG" if verbose else "INFO")
    try:
        if queue is not None:
            if year is not None or edition is not None or page or part is not None:
                raise ValueError("--queue cannot be combined with --year, --edition, --page, or --part")
            run_queue(
                queue_path=queue,
                limit=limit,
                max_requests=max_requests,
                workers=workers,
                dry_run=dry_run,
            )
        else:
            if limit is not None:
                raise ValueError("--limit requires --queue")
            if year is None or edition is None or not page:
                raise ValueError("Direct smoke mode requires --year, --edition, and repeatable --page")
            run_targeted(
                year=year,
                edition=edition,
                pages=page,
                part=part,
                max_requests=max_requests,
                dry_run=dry_run,
                workers=workers if workers is not None else 1,
            )
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
        logger.error(str(error))
        raise typer.Exit(1) from error


if __name__ == "__main__":
    app()
