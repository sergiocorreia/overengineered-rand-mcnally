"""Read-only inventory of the recovered V1 sources and Gemini cache."""

import csv
import hashlib
import io
import json
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from legacy_cache_inventory import CacheArtifact, scan_cache
from pypdf import PdfReader
from pypdf.errors import PdfReadError

SCHEMA_VERSION = 2
EXPECTED_RECOVERY_DIRECTORIES = ("downloads", "cache", "extracted_images")
KNOWN_INVALID_EDITIONS = {"1803-1"}
KNOWN_UNPROCESSED_EDITIONS = {"1941-1", "1941-2", "1942-1", "1942-2"}
EXPECTED_METADATA_COUNTS = (129, 84, 44)
MIGRATION_FIELDS = (
    "manifest_order",
    "year",
    "edition",
    "configured_source",
    "range_start",
    "range_end",
    "configured_range_kind",
    "manifest_row_status",
    "output_page_count",
)
RAW_CROSSWALK_FIELDS = (
    "year",
    "edition",
    "configured_source",
    "legacy_page_start",
    "legacy_page_end",
    "source_relative_path",
    "v2_pdf_relative_path",
    "physical_page_offset",
    "source_sha256",
    "physical_page_count",
    "evidence",
    "note",
)
CACHE_MANIFEST_FIELDS = (
    "purpose",
    "cache_relative_path",
    "kind",
    "size_bytes",
    "content_sha256",
    "cache_group",
    "page",
    "edition_id",
    "page_id",
    "mapping_status",
    "json_status",
    "model",
)
PAGE_MAPPING_FIELDS = (
    "purpose",
    "manifest_index",
    "edition_id",
    "configured_source",
    "configured_range_kind",
    "source_origin",
    "v1_copy_status",
    "source_sha256",
    "crosswalk_sha256",
    "pdf_relative_path",
    "physical_page",
    "page_id",
    "cache_group",
    "cache_page",
)
PARTIAL_NAME = re.compile(
    r"(?:\.(?:part|partial|tmp|temp|crdownload|download)|~|\.(?:pdf|json|jp2|jpe?g|png|webp)\.[a-z0-9]{5,12})$",
    re.IGNORECASE,
)
RAW_IMAGE_EXTENSIONS = {".jp2", ".jpg", ".jpeg", ".png", ".webp"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Edition:
    manifest_index: int
    year: int
    edition: int
    configured_source: str
    range_start: int | None
    range_end: int | None
    range_kind: str
    row_status: str
    output_page_count: int

    @property
    def edition_id(self) -> str:
        return f"{self.year}-{self.edition}"


@dataclass(frozen=True, slots=True)
class PageMapping:
    manifest_index: int
    edition_id: str
    configured_source: str
    configured_range_kind: str
    source_origin: str
    v1_copy_status: str
    source_sha256: str
    crosswalk_sha256: str
    pdf_relative_path: str
    physical_page: int
    page_id: str
    cache_group: str
    cache_page: int


@dataclass(frozen=True, slots=True)
class RawCrosswalk:
    edition_id: str
    configured_source: str
    legacy_page_start: int
    legacy_page_end: int
    source_relative_path: str
    v2_pdf_relative_path: str
    physical_page_offset: int
    source_sha256: str
    physical_page_count: int
    evidence: str


@dataclass(frozen=True, slots=True)
class AuditBundle:
    report: dict[str, Any]
    pages: list[PageMapping]
    cache_artifacts: list[CacheArtifact]


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative_pdf(raw: str, *, label: str) -> str:
    if not raw or "\\" in raw:
        raise ValueError(f"Unsafe {label}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or path.suffix.casefold() != ".pdf" or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe {label}: {raw!r}")
    return path.as_posix()


def _sha256_file(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Source changed while hashing: {path}")
    return digest.hexdigest()


def _read_editions(path: Path) -> list[Edition]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = set(reader.fieldnames or ())
        missing = set(MIGRATION_FIELDS) - fields
        if missing:
            raise ValueError(f"Missing columns in {path}: {sorted(missing)}")
        rows = list(reader)

    editions: list[Edition] = []
    for line_number, row in enumerate(rows, 2):
        try:
            manifest_index = int(row["manifest_order"])
            year = int(row["year"])
            edition = int(row["edition"])
            output_page_count = int(row["output_page_count"])
            range_start = int(row["range_start"]) if row["range_start"].strip() else None
            range_end = int(row["range_end"]) if row["range_end"].strip() else None
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid numeric value at {path}:{line_number}") from error
        status = row["manifest_row_status"].strip()
        if edition not in {1, 2} or status not in {"configured", "blank_source_placeholder", "invalid"}:
            raise ValueError(f"Invalid edition/status at {path}:{line_number}")
        if status == "configured" and (range_start is None or range_end is None or range_start > range_end):
            raise ValueError(f"Invalid configured range at {path}:{line_number}")
        editions.append(
            Edition(
                manifest_index=manifest_index,
                year=year,
                edition=edition,
                configured_source=row["configured_source"].strip(),
                range_start=range_start,
                range_end=range_end,
                range_kind=row["configured_range_kind"].strip(),
                row_status=status,
                output_page_count=output_page_count,
            )
        )
    if len({row.manifest_index for row in editions}) != len(editions):
        raise ValueError(f"Duplicate manifest_order in {path}")
    if len({row.edition_id for row in editions}) != len(editions):
        raise ValueError(f"Duplicate edition in {path}")
    return sorted(editions, key=lambda row: row.manifest_index)


def _read_raw_crosswalk(
    path: Path | None,
    editions: list[Edition],
    legacy_sources_root: Path,
    *,
    page_counter: Callable[[Path], int],
) -> tuple[dict[str, RawCrosswalk], dict[str, Any]]:
    if path is None or not path.is_file():
        return {}, {"present": False, "path": str(path.resolve()) if path else "", "sha256": "", "row_count": 0, "editions": []}
    payload = path.read_bytes()
    crosswalk_sha256 = hashlib.sha256(payload).hexdigest()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if tuple(reader.fieldnames or ()) != RAW_CROSSWALK_FIELDS:
            raise ValueError(f"Expected raw crosswalk columns {RAW_CROSSWALK_FIELDS} in {path}")
        rows = list(reader)
    raw_editions = {row.edition_id: row for row in editions if row.row_status == "configured" and row.range_kind == "raw_scan_index"}
    crosswalks: dict[str, RawCrosswalk] = {}
    targets: set[str] = set()
    for line_number, row in enumerate(rows, 2):
        try:
            year = int(row["year"])
            edition = int(row["edition"])
            legacy_start = int(row["legacy_page_start"])
            legacy_end = int(row["legacy_page_end"])
            offset = int(row["physical_page_offset"])
            expected_pages = int(row["physical_page_count"])
        except ValueError as error:
            raise ValueError(f"Invalid numeric raw crosswalk value at {path}:{line_number}") from error
        edition_id = f"{year}-{edition}"
        configured = raw_editions.get(edition_id)
        if configured is None or edition_id in crosswalks:
            raise ValueError(f"Raw crosswalk edition is unknown or duplicated at {path}:{line_number}: {edition_id}")
        configured_source = row["configured_source"].strip()
        if configured_source != configured.configured_source:
            raise ValueError(f"Raw crosswalk configured_source mismatch for {edition_id}")
        if (legacy_start, legacy_end) != (configured.range_start, configured.range_end):
            raise ValueError(f"Raw crosswalk range mismatch for {edition_id}")
        source_relative = _safe_relative_pdf(row["source_relative_path"].strip(), label="raw crosswalk source_relative_path")
        target_relative = _safe_relative_pdf(row["v2_pdf_relative_path"].strip(), label="raw crosswalk v2_pdf_relative_path")
        if target_relative in targets:
            raise ValueError(f"Duplicate raw crosswalk target path: {target_relative}")
        targets.add(target_relative)
        expected_sha256 = row["source_sha256"].strip().casefold()
        if SHA256_RE.fullmatch(expected_sha256) is None or expected_pages < 1 or not row["evidence"].strip():
            raise ValueError(f"Incomplete reviewed raw crosswalk evidence for {edition_id}")
        source_path = (legacy_sources_root / source_relative).resolve()
        if not source_path.is_relative_to(legacy_sources_root) or not source_path.is_file():
            raise ValueError(f"Raw crosswalk source is missing or escapes legacy sources: {source_relative}")
        actual_sha256 = _sha256_file(source_path)
        actual_pages = page_counter(source_path)
        if actual_sha256 != expected_sha256:
            raise ValueError(f"Raw crosswalk source SHA-256 mismatch for {edition_id}")
        if actual_pages != expected_pages:
            raise ValueError(f"Raw crosswalk source page-count mismatch for {edition_id}")
        if legacy_start + offset < 1 or legacy_end + offset > actual_pages:
            raise ValueError(f"Raw crosswalk offset leaves the physical PDF range for {edition_id}")
        crosswalks[edition_id] = RawCrosswalk(
            edition_id,
            configured_source,
            legacy_start,
            legacy_end,
            source_relative,
            target_relative,
            offset,
            actual_sha256,
            actual_pages,
            row["evidence"].strip(),
        )
    return crosswalks, {
        "present": True,
        "path": str(path.resolve()),
        "sha256": crosswalk_sha256,
        "row_count": len(crosswalks),
        "source_origin": "reviewed_manual_crosswalk",
        "legacy_source_root": str(legacy_sources_root),
        "editions": sorted(crosswalks),
    }


def _tree_snapshot(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        return {
            "exists": False,
            "directory_count": 0,
            "file_count": 0,
            "size_bytes": 0,
            "latest_mtime_ns": 0,
            "metadata_sha256": _sha256_payload([]),
            "partial_file_count": 0,
            "partial_file_samples": [],
            "scan_errors": ["missing_directory"],
        }

    records: list[tuple[str, str, int, int]] = []
    partial: list[str] = []
    errors: list[str] = []
    directory_count = 1
    file_count = 0
    size_bytes = 0
    latest_mtime_ns = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.lstat()
            except OSError as error:
                errors.append(f"{relative}:{type(error).__name__}")
                continue
            kind = "symlink" if path.is_symlink() else "directory"
            records.append((relative, kind, stat.st_size, stat.st_mtime_ns))
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
            directory_count += kind == "directory"
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.lstat()
            except OSError as error:
                errors.append(f"{relative}:{type(error).__name__}")
                continue
            kind = "symlink" if path.is_symlink() else "file"
            records.append((relative, kind, stat.st_size, stat.st_mtime_ns))
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
            if kind == "file":
                file_count += 1
                size_bytes += stat.st_size
                if PARTIAL_NAME.search(name):
                    partial.append(relative)
    return {
        "exists": True,
        "directory_count": directory_count,
        "file_count": file_count,
        "size_bytes": size_bytes,
        "latest_mtime_ns": latest_mtime_ns,
        "metadata_sha256": _sha256_payload(records),
        "partial_file_count": len(partial),
        "partial_file_samples": partial[:20],
        "scan_errors": errors[:20],
    }


def _recovery_tree(recovery_root: Path) -> dict[str, dict[str, Any]]:
    return {name: _tree_snapshot(recovery_root / name) for name in EXPECTED_RECOVERY_DIRECTORIES}


def _tree_signature(tree: dict[str, dict[str, Any]]) -> str:
    material = {name: tree[name]["metadata_sha256"] for name in EXPECTED_RECOVERY_DIRECTORIES}
    return _sha256_payload(material)


def _pdf_page_count(path: Path) -> int:
    return len(PdfReader(path).pages)


def _raw_indices(path: Path) -> set[int]:
    result: set[int] = set()
    for image in path.iterdir():
        if not image.is_file() or image.suffix.casefold() not in RAW_IMAGE_EXTENSIONS or "_" not in image.stem:
            continue
        try:
            result.add(int(image.stem.rsplit("_", 1)[1]))
        except ValueError:
            continue
    return result


def _inspect_sources(
    editions: list[Edition],
    downloads_root: Path,
    legacy_sources_root: Path,
    raw_crosswalks: dict[str, RawCrosswalk],
    crosswalk_sha256: str,
    *,
    page_counter: Callable[[Path], int],
) -> tuple[list[dict[str, Any]], list[PageMapping]]:
    sources: list[dict[str, Any]] = []
    pages: list[PageMapping] = []
    for edition in editions:
        if edition.row_status != "configured":
            continue
        assert edition.range_start is not None and edition.range_end is not None
        base = f"{edition.edition_id}-{edition.configured_source}"
        cache_base = edition.edition_id
        status = "missing"
        detail = "configured source is absent"
        relative_path = f"{base}.pdf"
        v1_expected_path = relative_path
        origin_relative_path = relative_path
        source_origin = "v1_recovery"
        v1_copy_status = "missing"
        source_sha256 = ""
        page_crosswalk_sha256 = ""
        mapped_before = len(pages)

        if edition.range_kind == "physical_pdf_page":
            v1_path = downloads_root / relative_path
            legacy_path = legacy_sources_root / relative_path
            pdf_path = v1_path
            if not v1_path.exists() and legacy_path.is_file():
                pdf_path = legacy_path
                source_origin = "legacy_fallback"
                source_sha256 = _sha256_file(pdf_path)
            if pdf_path.is_file():
                v1_copy_status = "available" if pdf_path == v1_path else "missing"
                try:
                    count = page_counter(pdf_path)
                    if edition.range_end > count:
                        status = "incomplete"
                        detail = f"configured page {edition.range_end} exceeds PDF page count {count}"
                    else:
                        status = "mapped"
                        detail = f"PDF has {count} physical pages"
                        for page in range(edition.range_start, edition.range_end + 1):
                            pages.append(
                                PageMapping(
                                    edition.manifest_index,
                                    edition.edition_id,
                                    edition.configured_source,
                                    edition.range_kind,
                                    source_origin,
                                    v1_copy_status,
                                    source_sha256,
                                    "",
                                    relative_path,
                                    page,
                                    f"{relative_path}#page={page}",
                                    cache_base,
                                    page,
                                )
                            )
                except (OSError, PdfReadError, ValueError) as error:
                    status = "unreadable"
                    detail = f"{type(error).__name__} while reading configured PDF"
        elif edition.range_kind == "multipart_pdf_position":
            relative_path = base
            v1_expected_path = relative_path
            origin_relative_path = relative_path
            folder = downloads_root / relative_path
            if folder.is_dir():
                v1_copy_status = "incomplete"
                pdfs = sorted(folder.glob("*.pdf"))
                if edition.range_end > len(pdfs):
                    status = "incomplete"
                    detail = f"configured part {edition.range_end} exceeds {len(pdfs)} PDFs"
                else:
                    status = "mapped"
                    v1_copy_status = "available"
                    detail = f"mapped configured PDF positions {edition.range_start}-{edition.range_end}"
                    for part in range(edition.range_start, edition.range_end + 1):
                        pdf_path = pdfs[part - 1]
                        pdf_relative = pdf_path.relative_to(downloads_root).as_posix()
                        try:
                            count = page_counter(pdf_path)
                        except (OSError, PdfReadError, ValueError) as error:
                            status = "unreadable"
                            detail = f"{type(error).__name__} while reading configured part {part}"
                            del pages[mapped_before:]
                            break
                        for page in range(1, count + 1):
                            pages.append(
                                PageMapping(
                                    edition.manifest_index,
                                    edition.edition_id,
                                    edition.configured_source,
                                    edition.range_kind,
                                    source_origin,
                                    v1_copy_status,
                                    "",
                                    "",
                                    pdf_relative,
                                    page,
                                    f"{pdf_relative}#page={page}",
                                    f"{cache_base}-part-{part}",
                                    page,
                                )
                            )
        elif edition.range_kind == "raw_scan_index":
            v1_expected_path = base
            relative_path = base
            origin_relative_path = base
            folder = downloads_root / base
            if folder.is_dir():
                present = _raw_indices(folder)
                missing = [index for index in range(edition.range_start, edition.range_end + 1) if index not in present]
                if missing:
                    v1_copy_status = "incomplete"
                    detail = f"{len(missing)} configured raw scan indices are absent; first={missing[:5]}"
                else:
                    v1_copy_status = "available"
            crosswalk = raw_crosswalks.get(edition.edition_id)
            if crosswalk is None:
                status = "identity_unresolved" if v1_copy_status == "available" else v1_copy_status
            else:
                status = "mapped"
                source_origin = "reviewed_raw_crosswalk"
                source_sha256 = crosswalk.source_sha256
                page_crosswalk_sha256 = crosswalk_sha256
                relative_path = crosswalk.v2_pdf_relative_path
                origin_relative_path = crosswalk.source_relative_path
                detail = f"reviewed raw-to-PDF mapping; V1 copy status={v1_copy_status}"
                for legacy_page in range(edition.range_start, edition.range_end + 1):
                    physical_page = legacy_page + crosswalk.physical_page_offset
                    pages.append(
                        PageMapping(
                            edition.manifest_index,
                            edition.edition_id,
                            edition.configured_source,
                            edition.range_kind,
                            source_origin,
                            v1_copy_status,
                            source_sha256,
                            page_crosswalk_sha256,
                            relative_path,
                            physical_page,
                            f"{relative_path}#page={physical_page}",
                            cache_base,
                            legacy_page,
                        )
                    )
        else:
            status = "unsupported_range_kind"
            detail = edition.range_kind

        candidates = sorted(path.relative_to(downloads_root).as_posix() for path in downloads_root.glob(f"{edition.edition_id}-*"))
        alternates = [path for path in candidates if path != relative_path]
        sources.append(
            {
                "manifest_index": edition.manifest_index,
                "edition_id": edition.edition_id,
                "configured_source": edition.configured_source,
                "configured_range_kind": edition.range_kind,
                "configured_range": [edition.range_start, edition.range_end],
                "source_relative_path": relative_path,
                "v1_expected_path": v1_expected_path,
                "v1_copy_status": v1_copy_status,
                "source_origin": source_origin,
                "origin_relative_path": origin_relative_path,
                "source_sha256": source_sha256,
                "crosswalk_sha256": page_crosswalk_sha256,
                "available_alternate_paths": alternates,
                "status": status,
                "mapped_page_count": len(pages) - mapped_before,
                "detail": detail,
            }
        )
    return sources, pages


def _load_previous(path: Path, recovery_root: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unreadable previous snapshot: {path}") from error
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported previous snapshot schema: {path}")
    if report.get("recovery_root") != str(recovery_root.resolve()):
        raise ValueError("Previous snapshot belongs to a different recovery root")
    return report


def audit_recovery(
    *,
    project_root: Path,
    legacy_root: Path,
    recovery_root: Path,
    migration_inventory: Path,
    raw_crosswalk: Path | None = None,
    previous_snapshot: Path | None = None,
    minimum_quiet_seconds: int = 60,
    scanned_at: datetime | None = None,
    page_counter: Callable[[Path], int] = _pdf_page_count,
    expected_metadata_counts: tuple[int, int, int] = EXPECTED_METADATA_COUNTS,
) -> AuditBundle:
    """Inspect immutable inputs and return a report without writing to either input tree."""

    project_root = project_root.resolve()
    legacy_root = legacy_root.resolve()
    recovery_root = recovery_root.resolve()
    if not legacy_root.is_dir() or not recovery_root.is_dir():
        raise ValueError("The configured legacy and recovery roots must both exist")
    roots = (project_root, legacy_root, recovery_root)
    overlapping = any(
        left == right or left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    )
    if overlapping:
        raise ValueError("Project, legacy, and recovery roots must be separate")
    if minimum_quiet_seconds < 0:
        raise ValueError("minimum_quiet_seconds must be nonnegative")

    migration_payload = migration_inventory.read_bytes()
    migration_sha256 = hashlib.sha256(migration_payload).hexdigest()
    editions = _read_editions(migration_inventory)
    legacy_sources_root = (legacy_root / "sources").resolve()
    raw_crosswalks, crosswalk_report = _read_raw_crosswalk(
        raw_crosswalk,
        editions,
        legacy_sources_root,
        page_counter=page_counter,
    )
    before = _recovery_tree(recovery_root)
    sources, pages = _inspect_sources(
        editions,
        recovery_root / "downloads",
        legacy_sources_root,
        raw_crosswalks,
        str(crosswalk_report["sha256"]),
        page_counter=page_counter,
    )
    page_lookup = {(page.cache_group, page.cache_page): (page.edition_id, page.page_id) for page in pages}
    cache, cache_artifacts = scan_cache(recovery_root / "cache", page_lookup)
    after = _recovery_tree(recovery_root)
    scan_stable = _tree_signature(before) == _tree_signature(after)
    scan_stable = scan_stable and not any(item["scan_errors"] for item in after.values())
    scan_stable = scan_stable and cache["changed_during_hash_count"] == 0

    now_value = scanned_at or datetime.now(UTC)
    if now_value.tzinfo is None or now_value.utcoffset() is None:
        raise ValueError("scanned_at must be timezone-aware")
    now = now_value.astimezone(UTC)
    transfer_signature = _sha256_payload(
        {
            "tree": _tree_signature(after),
            "cache_content": cache["content_manifest_sha256"],
            "migration_inventory": migration_sha256,
            "raw_crosswalk": crosswalk_report["sha256"],
            "page_mapping": _sha256_payload([asdict(page) for page in pages]),
        }
    )
    previous = _load_previous(previous_snapshot, recovery_root) if previous_snapshot else None
    quiet_seconds = None
    unchanged_since_previous = False
    if previous is not None:
        try:
            prior_time = datetime.fromisoformat(str(previous["scanned_at"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Previous snapshot has an invalid scanned_at timestamp") from error
        if prior_time.tzinfo is None or prior_time.utcoffset() is None:
            raise ValueError("Previous snapshot scanned_at must be timezone-aware")
        quiet_seconds = (now - prior_time.astimezone(UTC)).total_seconds()
        unchanged_since_previous = (
            previous.get("transfer_signature") == transfer_signature
            and bool(previous.get("scan_stable"))
            and quiet_seconds >= minimum_quiet_seconds
        )

    invalid = sorted(row.edition_id for row in editions if row.row_status == "invalid")
    unprocessed = sorted(row.edition_id for row in editions if row.row_status == "configured" and row.output_page_count == 0)
    unknown_invalid = sorted(set(invalid) - KNOWN_INVALID_EDITIONS)
    unexpected_unprocessed = sorted(set(unprocessed) - KNOWN_UNPROCESSED_EDITIONS)
    source_counts = Counter(source["status"] for source in sources)
    source_origin_counts = Counter(source["source_origin"] for source in sources)
    v1_copy_counts = Counter(source["v1_copy_status"] for source in sources)
    metadata_counts = (
        len(editions),
        sum(row.row_status == "configured" for row in editions),
        sum(row.row_status == "blank_source_placeholder" for row in editions),
    )
    partial_count = sum(tree["partial_file_count"] for tree in after.values())
    missing_layout = [name for name, tree in after.items() if not tree["exists"]]
    gates = {
        "scan_stable": scan_stable,
        "transfer_unchanged_for_minimum_interval": unchanged_since_previous,
        "expected_recovery_layout_present": not missing_layout,
        "no_partial_transfer_artifacts": partial_count == 0,
        "all_configured_sources_mapped": source_counts["mapped"] == len(sources),
        "cache_artifacts_readable": cache["unreadable_count"] == 0 and cache["invalid_json_count"] == 0,
        "metadata_universe_complete": metadata_counts == expected_metadata_counts,
        "metadata_exceptions_explicitly_known": set(invalid) == KNOWN_INVALID_EDITIONS and not unexpected_unprocessed,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "read_only_recovery_diagnostic",
        "scanned_at": now.isoformat(),
        "project_root": str(project_root),
        "legacy_root": str(legacy_root),
        "recovery_root": str(recovery_root),
        "inputs_are_read_only": True,
        "migration_inventory": {
            "path": str(migration_inventory.resolve()),
            "sha256": migration_sha256,
        },
        "raw_scan_pdf_crosswalk": crosswalk_report,
        "scan_stable": scan_stable,
        "tree": after,
        "transfer_signature": transfer_signature,
        "comparison": {
            "previous_snapshot": str(previous_snapshot.resolve()) if previous_snapshot else "",
            "quiet_seconds": quiet_seconds,
            "minimum_quiet_seconds": minimum_quiet_seconds,
            "unchanged_since_previous": unchanged_since_previous,
        },
        "metadata": {
            "row_count": len(editions),
            "configured_edition_count": sum(row.row_status == "configured" for row in editions),
            "blank_placeholder_count": sum(row.row_status == "blank_source_placeholder" for row in editions),
            "invalid_editions": invalid,
            "known_invalid_editions": sorted(KNOWN_INVALID_EDITIONS & set(invalid)),
            "unknown_invalid_editions": unknown_invalid,
            "unprocessed_editions": unprocessed,
            "known_unprocessed_editions": sorted(KNOWN_UNPROCESSED_EDITIONS & set(unprocessed)),
            "unexpected_unprocessed_editions": unexpected_unprocessed,
            "expected_counts": list(expected_metadata_counts),
            "legacy_output_page_count": sum(row.output_page_count for row in editions),
        },
        "sources": {
            "status_counts": dict(sorted(source_counts.items())),
            "source_origin_counts": dict(sorted(source_origin_counts.items())),
            "v1_copy_status_counts": dict(sorted(v1_copy_counts.items())),
            "mapped_physical_page_count": len(pages),
            "page_mapping_sha256": _sha256_payload([asdict(page) for page in pages]),
            "editions": sources,
        },
        "cache": cache,
        "partial_transfer_artifact_count": partial_count,
        "missing_layout_directories": missing_layout,
        "gates": gates,
        "finalizable": all(gates.values()),
    }
    return AuditBundle(report, pages, cache_artifacts)


def checked_output_path(path: Path, project_root: Path) -> Path:
    """Resolve an output beneath project output/, refusing symlink escapes."""

    project_root = project_root.resolve()
    output_root = (project_root / "output").resolve()
    if not output_root.is_relative_to(project_root):
        raise ValueError(f"Project output directory escapes the project root: {output_root}")
    candidate = path if path.is_absolute() else project_root / path
    resolved = candidate.resolve()
    if not resolved.is_relative_to(output_root):
        raise ValueError(f"Recovery audit outputs must remain under {output_root}: {path}")
    return resolved


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_report(path: Path, report: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _tsv(rows: list[dict[str, object]], fields: tuple[str, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def write_page_mapping(path: Path, pages: list[PageMapping]) -> None:
    rows = [{"purpose": "recovery_mapping_not_extraction_input", **asdict(page)} for page in pages]
    _atomic_write(path, _tsv(rows, PAGE_MAPPING_FIELDS))


def write_cache_manifest(path: Path, artifacts: list[CacheArtifact]) -> None:
    rows = [{"purpose": "diagnostic_only_not_a_reusable_cache", **asdict(artifact)} for artifact in artifacts]
    _atomic_write(path, _tsv(rows, CACHE_MANIFEST_FIELDS))
