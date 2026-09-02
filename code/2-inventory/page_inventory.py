"""Fail-closed all-page inventory, OCR evidence, overrides, and readiness gates."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import selection_rules
from pypdf import PdfReader

SOURCE_MANIFEST_FIELDS = (
    "source_order",
    "source_id",
    "provider",
    "provider_id",
    "title",
    "source_date",
    "item_url",
    "download_url",
    "acquisition_method",
    "filename",
    "expected_sha256",
    "min_pages",
    "max_pages",
    "notes",
)
INVENTORY_FIELDS = SOURCE_MANIFEST_FIELDS + (
    "status",
    "pdf_relative_path",
    "size_bytes",
    "physical_pages",
    "actual_sha256",
    "checked_at",
)
# Backward-compatible constant names now point to the exact public contracts.
SOURCE_MANIFEST_REQUIRED = SOURCE_MANIFEST_FIELDS
INVENTORY_REQUIRED = INVENTORY_FIELDS
SOURCE_OVERRIDE_FIELDS = ("source_id", "expected_source_sha256", "classification", "notes")
PAGE_OVERRIDE_FIELDS = ("page_id", "expected_source_sha256", "classification", "notes")
GOLD_FIELDS = ("page_id", "expected_classification", "risk_labels", "notes")
PAGE_FIELDS = (
    "manifest_index",
    "source_order",
    "source_id",
    "provider",
    "title",
    "source_date",
    "filename",
    "pdf_relative_path",
    "source_sha256",
    "pdf_size_bytes",
    "pdf_page_count",
    "page",
    "page_id",
    "embedded_text_sha256",
    "ocr_method",
    "ocr_text_sha256",
    "ocr_cache_relative_path",
    "automatic_classification",
    "automatic_score",
    "automatic_reasons",
    "source_manual_classification",
    "page_manual_classification",
    "final_type",
    "classification_source",
    "manual_notes",
)
VALID_FINAL = {"selected", "excluded", "flagged", "unreviewed"}
MANUAL_CLASSIFICATIONS = {"selected", "excluded", "flagged"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOCRO_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    source_order: int
    source_id: str
    filename: str
    source_date: str


@dataclass(frozen=True, slots=True)
class InventorySource:
    source_order: int
    source_id: str
    provider: str
    title: str
    source_date: str
    filename: str
    status: str
    pdf_relative_path: str
    size_bytes: int
    physical_pages: int
    actual_sha256: str


@dataclass(frozen=True, slots=True)
class Override:
    identity: str
    expected_source_sha256: str
    classification: str
    notes: str


@dataclass(frozen=True, slots=True)
class GoldExpectation:
    page_id: str
    expected_classification: str
    risk_labels: str
    notes: str


@dataclass(frozen=True, slots=True)
class PageRecord:
    manifest_index: int
    source_order: int
    source_id: str
    provider: str
    title: str
    source_date: str
    filename: str
    pdf_relative_path: str
    source_sha256: str
    pdf_size_bytes: int
    pdf_page_count: int
    page: int
    page_id: str
    embedded_text_sha256: str
    ocr_method: str
    ocr_text_sha256: str
    ocr_cache_relative_path: str
    automatic_classification: str
    automatic_score: float
    automatic_reasons: str
    source_manual_classification: str
    page_manual_classification: str
    final_type: str
    classification_source: str
    manual_notes: str


class Rules(Protocol):
    def classify_page(self, text: str, context: dict[str, Any]) -> dict[str, object]: ...

    def needs_locro(self, text: str, decision: dict[str, object], context: dict[str, Any]) -> bool: ...


EmbeddedExtractor = Callable[[Path], list[str]]
LocroRunner = Callable[[Path, list[int]], dict[int, str]]


def _snapshot(path: Path, delimiter: str = "\t") -> tuple[tuple[str, ...], list[dict[str, str]]]:
    content = path.read_bytes().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    return tuple(reader.fieldnames or ()), list(reader)


def _exact_fields(fields: tuple[str, ...], expected: tuple[str, ...], path: Path) -> None:
    if fields != expected:
        raise ValueError(f"Expected columns {expected} in {path}, got {fields}")


def _canonical_source_date(raw: str, *, context: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"Invalid source_date at {context}: {value!r}; expected YYYY-MM-DD or blank")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"Invalid source_date at {context}: {value!r}; expected YYYY-MM-DD or blank") from error


def _safe_pdf_path(raw: str, *, label: str) -> str:
    if not raw or "\\" in raw:
        raise ValueError(f"Unsafe {label}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.suffix.casefold() != ".pdf":
        raise ValueError(f"Unsafe {label}: {raw!r}")
    return path.as_posix()


def load_source_identities(path: Path) -> list[SourceIdentity]:
    fields, rows = _snapshot(path)
    _exact_fields(fields, SOURCE_MANIFEST_FIELDS, path)
    identities: list[SourceIdentity] = []
    for line_number, row in enumerate(rows, 2):
        try:
            order = int(row["source_order"])
        except ValueError as error:
            raise ValueError(f"Invalid source_order at {path}:{line_number}") from error
        identities.append(
            SourceIdentity(
                order,
                row["source_id"].strip(),
                _safe_pdf_path(row["filename"].strip(), label="filename"),
                _canonical_source_date(row["source_date"], context=f"{path}:{line_number}"),
            )
        )
    if not identities:
        raise ValueError(f"Source manifest is empty: {path}")
    identities.sort(key=lambda row: row.source_order)
    if len({row.source_order for row in identities}) != len(identities):
        raise ValueError("Duplicate source_order in source manifest")
    if len({row.source_id for row in identities}) != len(identities):
        raise ValueError("Duplicate source_id in source manifest")
    return identities


def load_inventory(path: Path) -> list[InventorySource]:
    fields, rows = _snapshot(path)
    _exact_fields(fields, INVENTORY_FIELDS, path)
    sources: list[InventorySource] = []
    for line_number, row in enumerate(rows, 2):
        try:
            order = int(row["source_order"])
            size = int(row["size_bytes"])
            pages = int(row["physical_pages"])
        except ValueError as error:
            raise ValueError(f"Invalid numeric inventory value at {path}:{line_number}") from error
        digest = row["actual_sha256"].strip().casefold()
        if SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"Invalid actual_sha256 at {path}:{line_number}")
        if size < 1 or pages < 1:
            raise ValueError(f"Nonpositive inventory size/page count at {path}:{line_number}")
        relative = _safe_pdf_path(row["pdf_relative_path"].strip(), label="pdf_relative_path")
        if not relative.startswith("pdfs/"):
            raise ValueError(f"pdf_relative_path must begin with pdfs/: {relative}")
        sources.append(
            InventorySource(
                source_order=order,
                source_id=row["source_id"].strip(),
                provider=row["provider"].strip(),
                title=row["title"].strip(),
                source_date=_canonical_source_date(row["source_date"], context=f"{path}:{line_number}"),
                filename=_safe_pdf_path(row["filename"].strip(), label="filename"),
                status=row["status"].strip(),
                pdf_relative_path=relative,
                size_bytes=size,
                physical_pages=pages,
                actual_sha256=digest,
            )
        )
    sources.sort(key=lambda row: row.source_order)
    for label, values in (
        ("source_order", [row.source_order for row in sources]),
        ("source_id", [row.source_id for row in sources]),
        ("pdf_relative_path", [row.pdf_relative_path.casefold() for row in sources]),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {label} in source inventory")
    return sources


def reconcile_sources(
    identities: list[SourceIdentity],
    inventory: list[InventorySource],
    *,
    require_all: bool,
) -> list[InventorySource]:
    expected = {row.source_id: row for row in identities}
    actual = {row.source_id: row for row in inventory}
    unknown = sorted(set(actual) - set(expected))
    if unknown:
        raise ValueError(f"Inventory contains unknown sources: {unknown}")
    for source_id, row in actual.items():
        identity = expected[source_id]
        if (row.source_order, row.filename, row.source_date) != (
            identity.source_order,
            identity.filename,
            identity.source_date,
        ):
            raise ValueError(f"Inventory identity drift for {source_id}")
    missing = sorted(set(expected) - set(actual))
    if require_all and missing:
        raise ValueError(f"Source inventory is incomplete; missing: {missing}")
    return [actual[row.source_id] for row in identities if row.source_id in actual]


def resolve_pdf(pdf_root: Path, source: InventorySource) -> Path:
    root = pdf_root.resolve(strict=True)
    relative = PurePosixPath(source.pdf_relative_path).relative_to("pdfs")
    try:
        path = (root / relative).resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Inventory PDF is missing: {root / relative}") from error
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Inventory PDF escapes {root}: {path}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_embedded_pages(pdf_path: Path) -> list[str]:
    try:
        reader = PdfReader(pdf_path, strict=False)
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as error:
        raise RuntimeError(f"Could not extract embedded PDF text from {pdf_path}: {error}") from error


def run_locro_pages(pdf_path: Path, pages: list[int]) -> dict[int, str]:
    if not pages:
        return {}
    executable = shutil.which("locro")
    if executable is None:
        raise RuntimeError("Locro is required for the requested OCR mode but was not found")
    spec = ",".join(str(page) for page in pages)
    try:
        result = subprocess.run(
            [executable, "ocr", str(pdf_path), "--pages", spec, "--text"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown Locro error").strip()
        raise RuntimeError(f"Locro failed for {pdf_path.name}: {detail[-1000:]}") from error
    output = re.sub(r"\nDone\. \d+ page\(s\),.*\Z", "", result.stdout, flags=re.DOTALL)
    values = output.split("\f")
    if len(values) != len(pages):
        raise RuntimeError(f"Locro returned {len(values)} pages for {len(pages)} requested pages")
    return dict(zip(pages, values, strict=True))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_path(cache_root: Path, source_hash: str, method: str, page: int) -> Path:
    return cache_root / source_hash / method / f"page-{page:06d}.txt"


def _decision(raw: dict[str, object], *, page_id: str) -> tuple[str, float, list[str]]:
    classification = str(raw.get("classification", "unreviewed"))
    if classification not in VALID_FINAL:
        raise ValueError(f"selection_rules returned invalid classification for {page_id}: {classification}")
    try:
        score = float(raw.get("score", 0.0))
    except (TypeError, ValueError) as error:
        raise ValueError(f"selection_rules returned invalid score for {page_id}") from error
    raw_reasons = raw.get("reasons", [])
    if not isinstance(raw_reasons, list) or not all(isinstance(value, str) for value in raw_reasons):
        raise ValueError(f"selection_rules returned invalid reasons for {page_id}")
    return classification, score, raw_reasons


def _load_overrides(path: Path | None, *, source_level: bool) -> dict[str, Override]:
    if path is None or not path.exists():
        return {}
    expected_fields = SOURCE_OVERRIDE_FIELDS if source_level else PAGE_OVERRIDE_FIELDS
    fields, rows = _snapshot(path)
    if fields != expected_fields:
        raise ValueError(f"Expected override columns {expected_fields}, got {fields}")
    key_field = "source_id" if source_level else "page_id"
    result: dict[str, Override] = {}
    for line_number, row in enumerate(rows, 2):
        identity = row[key_field].strip()
        digest = row["expected_source_sha256"].strip().casefold()
        classification = row["classification"].strip()
        if not identity or identity in result:
            raise ValueError(f"Missing or duplicate {key_field} at {path}:{line_number}: {identity!r}")
        if SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"Invalid expected_source_sha256 at {path}:{line_number}")
        if classification not in MANUAL_CLASSIFICATIONS:
            raise ValueError(f"Invalid manual classification at {path}:{line_number}: {classification!r}")
        result[identity] = Override(identity, digest, classification, row["notes"].strip())
    return result


def load_source_overrides(path: Path | None) -> dict[str, Override]:
    return _load_overrides(path, source_level=True)


def load_page_overrides(path: Path | None) -> dict[str, Override]:
    return _load_overrides(path, source_level=False)


def load_gold(path: Path) -> list[GoldExpectation]:
    fields, rows = _snapshot(path)
    if fields != GOLD_FIELDS:
        raise ValueError(f"Expected gold columns {GOLD_FIELDS}, got {fields}")
    expectations: list[GoldExpectation] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows, 2):
        page_id = row["page_id"].strip()
        expected = row["expected_classification"].strip()
        if not page_id or page_id in seen:
            raise ValueError(f"Missing or duplicate gold page_id at {path}:{line_number}")
        if expected not in {"selected", "excluded"}:
            raise ValueError(f"Gold classification must be selected or excluded at {path}:{line_number}")
        if not row["risk_labels"].strip():
            raise ValueError(f"Gold risk_labels are required at {path}:{line_number}")
        seen.add(page_id)
        expectations.append(GoldExpectation(page_id, expected, row["risk_labels"].strip(), row["notes"].strip()))
    return expectations


def build_page_records(
    sources: list[InventorySource],
    pdf_root: Path,
    cache_root: Path,
    *,
    source_overrides_path: Path | None = None,
    page_overrides_path: Path | None = None,
    ocr_mode: str = "embedded",
    rules: Rules = selection_rules,
    embedded_extractor: EmbeddedExtractor = extract_embedded_pages,
    locro_runner: LocroRunner = run_locro_pages,
    allow_out_of_scope_overrides: bool = False,
) -> list[PageRecord]:
    """Build every physical page in canonical source order; nothing auto-finalizes."""

    if ocr_mode not in {"embedded", "targeted", "full"}:
        raise ValueError(f"Unknown OCR mode: {ocr_mode}")
    source_overrides = load_source_overrides(source_overrides_path)
    page_overrides = load_page_overrides(page_overrides_path)
    unknown_sources = sorted(set(source_overrides) - {source.source_id for source in sources})
    if unknown_sources and not allow_out_of_scope_overrides:
        raise ValueError(f"Unknown source override IDs: {unknown_sources}")
    records: list[PageRecord] = []

    for source in sources:
        pdf_path = resolve_pdf(pdf_root, source)
        if pdf_path.stat().st_size != source.size_bytes or sha256_file(pdf_path) != source.actual_sha256:
            raise ValueError(f"Source inventory is stale for {source.source_id}")
        embedded_pages = embedded_extractor(pdf_path)
        if len(embedded_pages) != source.physical_pages:
            raise ValueError(
                f"Page count drift for {source.source_id}: inventory={source.physical_pages}, extracted={len(embedded_pages)}"
            )
        source_override = source_overrides.get(source.source_id)
        if source_override and source_override.expected_source_sha256 != source.actual_sha256:
            raise ValueError(f"Stale source override for {source.source_id}")

        provisional: dict[int, tuple[dict[str, object], dict[str, Any]]] = {}
        locro_requested: list[int] = []
        for page, text in enumerate(embedded_pages, 1):
            embedded_cache = _cache_path(cache_root, source.actual_sha256, "embedded", page)
            if not embedded_cache.exists():
                _atomic_text(embedded_cache, text)
            context = {
                "source_id": source.source_id,
                "source_date": source.source_date,
                "page": page,
                "page_count": source.physical_pages,
                "page_id": f"{source.pdf_relative_path}#page={page}",
            }
            raw = rules.classify_page(text, context)
            provisional[page] = (raw, context)
            if ocr_mode == "full" or (ocr_mode == "targeted" and rules.needs_locro(text, raw, context)):
                locro_requested.append(page)

        locro_text: dict[int, str] = {}
        missing_locro: list[int] = []
        for page in locro_requested:
            path = _cache_path(cache_root, source.actual_sha256, "locro", page)
            if path.exists():
                locro_text[page] = path.read_text(encoding="utf-8")
            else:
                missing_locro.append(page)
        if missing_locro:
            for offset in range(0, len(missing_locro), LOCRO_BATCH_SIZE):
                requested = missing_locro[offset : offset + LOCRO_BATCH_SIZE]
                fresh = locro_runner(pdf_path, requested)
                if set(fresh) != set(requested):
                    raise RuntimeError(f"Locro did not return the requested pages for {source.source_id}")
                for page, text in fresh.items():
                    path = _cache_path(cache_root, source.actual_sha256, "locro", page)
                    _atomic_text(path, text)
                    locro_text[page] = text

        for page, embedded in enumerate(embedded_pages, 1):
            page_id = f"{source.pdf_relative_path}#page={page}"
            context = provisional[page][1]
            if page in locro_text:
                text = f"{embedded}\n{locro_text[page]}" if embedded.strip() else locro_text[page]
                raw = rules.classify_page(text, context)
                method = "embedded+locro" if embedded.strip() else "locro"
                cache_relative = str(_cache_path(cache_root, source.actual_sha256, "locro", page).relative_to(cache_root))
            else:
                text = embedded
                raw = provisional[page][0]
                method = "embedded"
                cache_relative = str(_cache_path(cache_root, source.actual_sha256, "embedded", page).relative_to(cache_root))
            automatic, score, reasons = _decision(raw, page_id=page_id)
            page_override = page_overrides.get(page_id)
            if page_override and page_override.expected_source_sha256 != source.actual_sha256:
                raise ValueError(f"Stale page override for {page_id}")
            if page_override:
                final = page_override.classification
                classification_source = "manual_page"
                manual_notes = page_override.notes
            elif source_override:
                final = source_override.classification
                classification_source = "manual_source"
                manual_notes = source_override.notes
            else:
                final = "unreviewed"
                classification_source = "unreviewed"
                manual_notes = ""
            record = PageRecord(
                manifest_index=len(records),
                source_order=source.source_order,
                source_id=source.source_id,
                provider=source.provider,
                title=source.title,
                source_date=source.source_date,
                filename=source.filename,
                pdf_relative_path=source.pdf_relative_path,
                source_sha256=source.actual_sha256,
                pdf_size_bytes=source.size_bytes,
                pdf_page_count=source.physical_pages,
                page=page,
                page_id=page_id,
                embedded_text_sha256=_hash_text(embedded),
                ocr_method=method,
                ocr_text_sha256=_hash_text(text),
                ocr_cache_relative_path=cache_relative,
                automatic_classification=automatic,
                automatic_score=score,
                automatic_reasons=";".join(reasons),
                source_manual_classification=source_override.classification if source_override else "",
                page_manual_classification=page_override.classification if page_override else "",
                final_type=final,
                classification_source=classification_source,
                manual_notes=manual_notes,
            )
            evidence_path = cache_root / source.actual_sha256 / "evidence" / f"page-{page:06d}.json"
            _atomic_text(
                evidence_path,
                json.dumps(
                    {
                        "automatic_classification": automatic,
                        "automatic_reasons": reasons,
                        "automatic_score": score,
                        "embedded_text_sha256": record.embedded_text_sha256,
                        "ocr_method": method,
                        "ocr_text_sha256": record.ocr_text_sha256,
                        "page_id": page_id,
                        "source_date": source.source_date,
                        "source_sha256": source.actual_sha256,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            records.append(record)

    known_pages = {record.page_id for record in records}
    unknown_pages = sorted(set(page_overrides) - known_pages)
    if unknown_pages and not allow_out_of_scope_overrides:
        raise ValueError(f"Unknown page override IDs: {unknown_pages}")
    return records


def apply_manual_overrides(
    records: list[PageRecord],
    *,
    source_overrides_path: Path | None,
    page_overrides_path: Path | None,
) -> list[PageRecord]:
    """Reapply the current human snapshot without rerunning OCR or selection."""

    source_overrides = load_source_overrides(source_overrides_path)
    page_overrides = load_page_overrides(page_overrides_path)
    source_hashes: dict[str, str] = {}
    for record in records:
        previous = source_hashes.setdefault(record.source_id, record.source_sha256)
        if previous != record.source_sha256:
            raise ValueError(f"Source hash changes within page manifest for {record.source_id}")
    unknown_sources = sorted(set(source_overrides) - set(source_hashes))
    unknown_pages = sorted(set(page_overrides) - {record.page_id for record in records})
    if unknown_sources:
        raise ValueError(f"Unknown source override IDs: {unknown_sources}")
    if unknown_pages:
        raise ValueError(f"Unknown page override IDs: {unknown_pages}")
    for source_id, override in source_overrides.items():
        if override.expected_source_sha256 != source_hashes[source_id]:
            raise ValueError(f"Stale source override for {source_id}")

    updated: list[PageRecord] = []
    for record in records:
        source_override = source_overrides.get(record.source_id)
        page_override = page_overrides.get(record.page_id)
        if page_override and page_override.expected_source_sha256 != record.source_sha256:
            raise ValueError(f"Stale page override for {record.page_id}")
        if page_override:
            final = page_override.classification
            provenance = "manual_page"
            notes = page_override.notes
        elif source_override:
            final = source_override.classification
            provenance = "manual_source"
            notes = source_override.notes
        else:
            final = "unreviewed"
            provenance = "unreviewed"
            notes = ""
        updated.append(
            replace(
                record,
                source_manual_classification=source_override.classification if source_override else "",
                page_manual_classification=page_override.classification if page_override else "",
                final_type=final,
                classification_source=provenance,
                manual_notes=notes,
            )
        )
    return updated


def validate_sources_current(records: list[PageRecord], sources: list[InventorySource], pdf_root: Path) -> None:
    """Prove that selected-page provenance still matches every inventoried source file."""

    by_source = {source.source_id: source for source in sources}
    record_sources = {record.source_id for record in records}
    if record_sources != set(by_source):
        raise ValueError(
            f"Page/source inventory mismatch: missing={sorted(set(by_source) - record_sources)}, "
            f"extra={sorted(record_sources - set(by_source))}"
        )
    for source_id, source in by_source.items():
        pdf_path = resolve_pdf(pdf_root, source)
        digest = sha256_file(pdf_path)
        rows = [record for record in records if record.source_id == source_id]
        if (
            digest != source.actual_sha256
            or pdf_path.stat().st_size != source.size_bytes
            or {row.source_sha256 for row in rows} != {source.actual_sha256}
            or {row.source_date for row in rows} != {source.source_date}
            or {row.pdf_page_count for row in rows} != {source.physical_pages}
        ):
            raise ValueError(f"Source or page manifest is stale for {source_id}")


def merge_page_updates(existing: list[PageRecord], updates: list[PageRecord]) -> list[PageRecord]:
    """Merge a bounded rescoring/OCR cohort into the complete ordered manifest."""

    if not existing or not updates:
        raise ValueError("Both the complete page manifest and bounded updates must be nonempty")
    by_id = {record.page_id: record for record in existing}
    if len(by_id) != len(existing):
        raise ValueError("Complete page manifest contains duplicate page IDs")
    update_by_id = {record.page_id: record for record in updates}
    if len(update_by_id) != len(updates):
        raise ValueError("Bounded page updates contain duplicate page IDs")
    unknown = sorted(set(update_by_id) - set(by_id))
    if unknown:
        raise ValueError(f"Bounded page updates are not in the complete manifest: {unknown[:3]}")

    merged: list[PageRecord] = []
    for record in existing:
        update = update_by_id.get(record.page_id)
        if update is None:
            merged.append(record)
            continue
        if (
            update.source_id != record.source_id
            or update.source_sha256 != record.source_sha256
            or update.source_date != record.source_date
            or update.page != record.page
            or update.pdf_page_count != record.pdf_page_count
        ):
            raise ValueError(f"Bounded page update has stale identity/provenance: {record.page_id}")
        merged.append(replace(update, manifest_index=record.manifest_index))
    return merged


def atomic_write_pages(path: Path, records: Iterable[PageRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=PAGE_FIELDS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(asdict(record) for record in records)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_page_records(path: Path) -> list[PageRecord]:
    fields, rows = _snapshot(path)
    if fields != PAGE_FIELDS:
        raise ValueError(f"Expected page columns {PAGE_FIELDS}, got {fields}")
    records: list[PageRecord] = []
    for line_number, row in enumerate(rows, 2):
        try:
            records.append(
                PageRecord(
                    **{
                        **row,
                        "manifest_index": int(row["manifest_index"]),
                        "source_order": int(row["source_order"]),
                        "source_date": _canonical_source_date(
                            row["source_date"],
                            context=f"{path}:{line_number}",
                        ),
                        "pdf_size_bytes": int(row["pdf_size_bytes"]),
                        "pdf_page_count": int(row["pdf_page_count"]),
                        "page": int(row["page"]),
                        "automatic_score": float(row["automatic_score"]),
                    }
                )
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid page manifest row at {path}:{line_number}") from error
    return records


def validate_extraction_ready(
    records: list[PageRecord],
    *,
    expected_source_ids: Iterable[str],
    gold_path: Path,
) -> list[PageRecord]:
    """Refuse extraction until the complete manifest and positive/negative gold pass."""

    if not records:
        raise ValueError("Page manifest is empty")
    if [record.manifest_index for record in records] != list(range(len(records))):
        raise ValueError("Page manifest indices are not contiguous and ordered")
    if len({record.page_id for record in records}) != len(records):
        raise ValueError("Page manifest contains duplicate page IDs")
    expected = set(expected_source_ids)
    actual = {record.source_id for record in records}
    if actual != expected:
        raise ValueError(f"Page manifest source set is incomplete: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    unresolved = [record.page_id for record in records if record.final_type in {"unreviewed", "flagged"}]
    invalid = [record.page_id for record in records if record.final_type not in VALID_FINAL]
    if invalid:
        raise ValueError(f"Page manifest has invalid classifications: {invalid[:3]}")
    if unresolved:
        raise ValueError(f"Page manifest has {len(unresolved)} unresolved pages; first: {unresolved[:3]}")

    by_source: dict[str, list[PageRecord]] = {}
    for record in records:
        by_source.setdefault(record.source_id, []).append(record)
    for source_id, pages in by_source.items():
        expected_pages = list(range(1, pages[0].pdf_page_count + 1))
        if [row.page for row in pages] != expected_pages:
            raise ValueError(f"Page manifest is incomplete or unordered for {source_id}")
        if len({row.source_sha256 for row in pages}) != 1:
            raise ValueError(f"Source hash changes within page manifest for {source_id}")
        if len({row.source_date for row in pages}) != 1:
            raise ValueError(f"source_date changes within page manifest for {source_id}")

    gold = load_gold(gold_path)
    gold_classes = {row.expected_classification for row in gold}
    if gold_classes != {"selected", "excluded"}:
        raise ValueError("Gold fixtures must contain at least one selected and one excluded page")
    by_page = {record.page_id: record for record in records}
    errors = []
    for expectation in gold:
        record = by_page.get(expectation.page_id)
        if record is None:
            errors.append(f"missing {expectation.page_id}")
        elif record.final_type != expectation.expected_classification:
            errors.append(
                f"{expectation.page_id}: expected {expectation.expected_classification}, got {record.final_type}"
            )
    if errors:
        raise ValueError(f"Gold page expectations failed: {errors[:5]}")
    selected = [record for record in records if record.final_type == "selected"]
    if not selected:
        raise ValueError("No pages are selected for extraction")
    return selected


def create_selection_snapshot(paths: Iterable[Path], run_root: Path) -> Path:
    identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = (run_root / identity).resolve()
    root = run_root.resolve()
    if not run_dir.is_relative_to(root):
        raise ValueError(f"Selection snapshot escapes {root}")
    run_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        if path.exists():
            shutil.copy2(path, run_dir / path.name)
    return run_dir
