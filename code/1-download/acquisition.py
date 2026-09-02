"""Validated, resumable acquisition of source PDFs declared in a TSV manifest."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import requests
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_FIELDS = (
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
INVENTORY_FIELDS = SOURCE_FIELDS + (
    "status",
    "pdf_relative_path",
    "size_bytes",
    "physical_pages",
    "actual_sha256",
    "checked_at",
)
ACQUISITION_METHODS = {"direct", "manual"}
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One immutable source named by the canonical source manifest."""

    source_order: int
    source_id: str
    provider: str
    provider_id: str
    title: str
    source_date: str
    item_url: str
    download_url: str
    acquisition_method: str
    filename: str
    expected_sha256: str
    min_pages: int
    max_pages: int | None
    notes: str


@dataclass(frozen=True, slots=True)
class PdfAudit:
    """Structural and content metadata for one local PDF."""

    size_bytes: int
    physical_pages: int
    actual_sha256: str


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """The auditable result of attempting one manifest record."""

    record: SourceRecord
    status: str
    audit: PdfAudit | None
    error: str = ""


class ResponseLike(Protocol):
    status_code: int
    headers: dict[str, str]

    def __enter__(self) -> ResponseLike: ...

    def __exit__(self, *_args: object) -> None: ...

    def raise_for_status(self) -> None: ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...


class SessionLike(Protocol):
    def get(self, url: str, **kwargs: Any) -> ResponseLike: ...


def build_session(user_agent: str = "historical-data-extraction-template/0.1 (academic research)") -> requests.Session:
    """Return an identifiable HTTPS session with conservative retries."""

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"Accept-Encoding": "identity", "User-Agent": user_agent})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _read_tsv_snapshot(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Read a small TSV once so concurrent edits cannot mix manifest versions."""

    content = path.read_bytes().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    return tuple(reader.fieldnames or ()), list(reader)


def _safe_relative_pdf(raw: str, *, line_number: int) -> str:
    if not raw or "\\" in raw:
        raise ValueError(f"Unsafe PDF filename at line {line_number}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.suffix.casefold() != ".pdf":
        raise ValueError(f"Unsafe PDF filename at line {line_number}: {raw!r}")
    return path.as_posix()


def _https_url(raw: str, *, field: str, line_number: int, required: bool) -> str:
    value = raw.strip()
    if not value and not required:
        return ""
    if not value.startswith("https://"):
        raise ValueError(f"{field} must use HTTPS at line {line_number}: {value!r}")
    return value


def _parse_page_bound(raw: str, *, field: str, line_number: int, allow_blank: bool) -> int | None:
    if allow_blank and not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"Invalid {field} at line {line_number}: {raw!r}") from error
    if value < 1:
        raise ValueError(f"{field} must be positive at line {line_number}")
    return value


def canonical_source_date(raw: str, *, context: str) -> str:
    """Return a blank or canonical ISO source date suitable for year selection."""

    value = raw.strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"Invalid source_date at {context}: {value!r}; expected YYYY-MM-DD or blank")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"Invalid source_date at {context}: {value!r}; expected YYYY-MM-DD or blank") from error


def _parse_source(row: dict[str, str], line_number: int) -> SourceRecord:
    try:
        source_order = int(row["source_order"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"Invalid source_order at line {line_number}") from error
    source_id = row["source_id"].strip()
    method = row["acquisition_method"].strip().casefold()
    expected_sha256 = row["expected_sha256"].strip().casefold()
    min_pages = _parse_page_bound(row["min_pages"], field="min_pages", line_number=line_number, allow_blank=False)
    max_pages = _parse_page_bound(row["max_pages"], field="max_pages", line_number=line_number, allow_blank=True)
    assert min_pages is not None

    if source_order < 1:
        raise ValueError(f"source_order must be positive at line {line_number}")
    if SOURCE_ID_RE.fullmatch(source_id) is None:
        raise ValueError(f"Invalid source_id at line {line_number}: {source_id!r}")
    if method not in ACQUISITION_METHODS:
        raise ValueError(f"Unknown acquisition_method at line {line_number}: {method!r}")
    for field in ("provider", "title"):
        if not row[field].strip():
            raise ValueError(f"{field} is required at line {line_number}")
    if expected_sha256 and SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError(f"Invalid expected_sha256 at line {line_number}")
    if max_pages is not None and max_pages < min_pages:
        raise ValueError(f"max_pages precedes min_pages at line {line_number}")

    return SourceRecord(
        source_order=source_order,
        source_id=source_id,
        provider=row["provider"].strip(),
        provider_id=row["provider_id"].strip(),
        title=row["title"].strip(),
        source_date=canonical_source_date(row["source_date"], context=f"line {line_number}"),
        item_url=_https_url(row["item_url"], field="item_url", line_number=line_number, required=False),
        download_url=_https_url(
            row["download_url"],
            field="download_url",
            line_number=line_number,
            required=method == "direct",
        ),
        acquisition_method=method,
        filename=_safe_relative_pdf(row["filename"].strip(), line_number=line_number),
        expected_sha256=expected_sha256,
        min_pages=min_pages,
        max_pages=max_pages,
        notes=row["notes"].strip(),
    )


def load_sources(path: Path, *, allow_empty: bool = False) -> list[SourceRecord]:
    """Load the canonical source manifest in its declared stable order."""

    fields, rows = _read_tsv_snapshot(path)
    if fields != SOURCE_FIELDS:
        raise ValueError(f"Expected source columns {SOURCE_FIELDS}, got {fields}")
    records = [_parse_source(row, line_number) for line_number, row in enumerate(rows, 2)]
    if not records and not allow_empty:
        raise ValueError(f"Source manifest is empty: {path}")

    validate_source_records(records)
    return records


def validate_source_records(records: list[SourceRecord]) -> None:
    """Validate an in-memory manifest before atomically replacing its durable file."""

    orders = [record.source_order for record in records]
    identities = [record.source_id for record in records]
    filenames = [record.filename.casefold() for record in records]
    if orders != sorted(orders) or len(orders) != len(set(orders)):
        raise ValueError("source_order values must be unique and ascending")
    if len(identities) != len(set(identities)):
        raise ValueError("Source manifest contains duplicate source_id values")
    if len(filenames) != len(set(filenames)):
        raise ValueError("Source manifest contains colliding filenames")
    for line_number, record in enumerate(records, 2):
        raw = {key: str(value) for key, value in asdict(record).items()}
        raw["max_pages"] = "" if record.max_pages is None else str(record.max_pages)
        _parse_source(raw, line_number)


def select_sources(
    records: list[SourceRecord],
    *,
    all_sources: bool = False,
    limit: int = 1,
    source_ids: Iterable[str] = (),
) -> list[SourceRecord]:
    """Choose a bounded cohort unless the caller explicitly supplies ``--all``."""

    requested = tuple(source_ids)
    if all_sources and requested:
        raise ValueError("--all and explicit source IDs are mutually exclusive")
    if limit < 1:
        raise ValueError("limit must be positive")
    if requested:
        by_id = {record.source_id: record for record in records}
        unknown = sorted(set(requested) - set(by_id))
        if unknown:
            raise ValueError(f"Unknown source IDs: {unknown}")
        return [record for record in records if record.source_id in set(requested)]
    return records if all_sources else records[:limit]


def validate_pdf_root(
    pdf_root: Path,
    *,
    project_root: Path,
    external_data_root: Path | None,
    external_pdf_root: Path | None = None,
) -> Path:
    """Allow only the project small-corpus folder or the exact external PDF folder."""

    resolved = pdf_root.resolve()
    allowed = {(project_root.resolve() / "sources" / "pdfs").resolve()}
    if external_pdf_root is not None:
        allowed.add(external_pdf_root.resolve())
    elif external_data_root is not None:
        allowed.add((external_data_root.resolve() / "pdfs").resolve())
    if resolved not in allowed:
        expected = ", ".join(str(path) for path in sorted(allowed))
        raise ValueError(f"PDF root must be exactly one of [{expected}], received {resolved}")
    return resolved


def safe_destination(pdf_root: Path, relative_filename: str) -> Path:
    """Resolve one manifest filename while refusing a path escape or symlink escape."""

    root = pdf_root.resolve()
    destination = (root / PurePosixPath(relative_filename)).resolve()
    if not destination.is_relative_to(root):
        raise ValueError(f"PDF destination escapes {root}: {relative_filename}")
    return destination


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_pdf(path: Path, record: SourceRecord) -> PdfAudit:
    """Validate PDF structure, page constraints, and the optional expected hash."""

    if not path.is_file():
        raise ValueError(f"PDF is missing: {path}")
    size = path.stat().st_size
    if size < 12:
        raise ValueError(f"PDF is implausibly small: {path} ({size} bytes)")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"File does not have a PDF header: {path}")
    try:
        pages = len(PdfReader(path, strict=False).pages)
    except Exception as error:
        raise ValueError(f"PDF cannot be parsed: {path}: {error}") from error
    if pages < record.min_pages:
        raise ValueError(f"PDF has {pages} pages; {record.source_id} requires at least {record.min_pages}")
    if record.max_pages is not None and pages > record.max_pages:
        raise ValueError(f"PDF has {pages} pages; {record.source_id} permits at most {record.max_pages}")
    digest = sha256_file(path)
    if record.expected_sha256 and digest != record.expected_sha256:
        raise ValueError(f"SHA-256 mismatch for {record.source_id}: expected {record.expected_sha256}, got {digest}")
    return PdfAudit(size_bytes=size, physical_pages=pages, actual_sha256=digest)


def _response_total(response: ResponseLike, *, offset: int) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
    if match:
        if int(match.group(1)) != offset:
            raise ValueError(f"Server resumed at byte {match.group(1)}, expected {offset}")
        return None if match.group(3) == "*" else int(match.group(3))
    raw_length = response.headers.get("Content-Length")
    return offset + int(raw_length) if raw_length else None


def partial_contract(record: SourceRecord) -> dict[str, str]:
    """Bind a resumable byte stream to the exact manifest identity and URL."""

    return {
        "download_url": record.download_url,
        "expected_sha256": record.expected_sha256,
        "source_id": record.source_id,
    }


def _write_partial_contract(path: Path, payload: dict[str, str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def download_pdf(record: SourceRecord, pdf_root: Path, session: SessionLike) -> PdfAudit:
    """Resume a direct download into ``.part`` and atomically publish a valid PDF."""

    if record.acquisition_method != "direct":
        raise ValueError(f"Automated download is not allowed for {record.source_id}")
    destination = safe_destination(pdf_root, record.filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    part_metadata = part.with_suffix(part.suffix + ".json")
    expected_contract = partial_contract(record)
    if part.exists():
        if not part_metadata.is_file():
            raise ValueError(f"Refusing an unbound partial download without {part_metadata.name}")
        try:
            actual_contract = json.loads(part_metadata.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid partial-download contract: {part_metadata}") from error
        if actual_contract != expected_contract:
            raise ValueError(f"Partial download belongs to a different source contract: {part_metadata}")
    else:
        _write_partial_contract(part_metadata, expected_contract)
    offset = part.stat().st_size if part.is_file() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}

    try:
        with session.get(record.download_url, stream=True, timeout=(20, 300), headers=headers) as response:
            response.raise_for_status()
            status_code = int(getattr(response, "status_code", 200))
            if offset and status_code == 206:
                mode = "ab"
                write_offset = offset
            elif status_code == 200:
                mode = "wb"
                write_offset = 0
            else:
                raise ValueError(f"Unexpected HTTP status {status_code} while resuming {record.source_id}")
            expected_total = _response_total(response, offset=write_offset)
            with part.open(mode) as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if expected_total is not None and part.stat().st_size != expected_total:
            raise ValueError(f"Expected {expected_total} bytes, received {part.stat().st_size}")
        audit = audit_pdf(part, record)
        if destination.exists():
            raise FileExistsError(f"Refusing to replace immutable source PDF: {destination}")
        part.replace(destination)
        part_metadata.unlink(missing_ok=True)
        return audit
    except requests.RequestException:
        # Keep the partial file so the next bounded run can resume it.
        raise
    except Exception:
        part.unlink(missing_ok=True)
        part_metadata.unlink(missing_ok=True)
        raise


def acquire_record(record: SourceRecord, pdf_root: Path, session: SessionLike | None = None) -> AcquisitionResult:
    """Validate an existing immutable source, download a direct source, or flag manual work."""

    destination = safe_destination(pdf_root, record.filename)
    if destination.exists():
        audit = audit_pdf(destination, record)
        return AcquisitionResult(record, "valid_existing", audit)
    if record.acquisition_method == "manual":
        return AcquisitionResult(record, "manual_required", None, f"Save the source as {destination}")
    if session is None:
        raise ValueError("A session is required to download a direct source")
    audit = download_pdf(record, pdf_root, session)
    return AcquisitionResult(record, "downloaded", audit)


def inventory_row(result: AcquisitionResult, pdf_root: Path, *, checked_at: str) -> dict[str, str | int]:
    """Flatten one successful acquisition result into the public inventory contract."""

    if result.audit is None:
        raise ValueError(f"Cannot inventory unresolved source {result.record.source_id}")
    record = asdict(result.record)
    record["max_pages"] = "" if result.record.max_pages is None else result.record.max_pages
    return {
        **record,
        "status": result.status,
        "pdf_relative_path": f"pdfs/{result.record.filename}",
        "size_bytes": result.audit.size_bytes,
        "physical_pages": result.audit.physical_pages,
        "actual_sha256": result.audit.actual_sha256,
        "checked_at": checked_at,
    }


def atomic_write_tsv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_event(path: Path, payload: dict[str, object]) -> None:
    """Append and fsync one progress event so interruptions retain prior successes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", buffering=1) as output:
        output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def create_run_snapshot(manifest_path: Path, run_root: Path, *, run_id: str | None = None) -> Path:
    """Create an immutable startup snapshot before any acquisition is attempted."""

    identity = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = (run_root / identity).resolve()
    root = run_root.resolve()
    if not run_dir.is_relative_to(root):
        raise ValueError(f"Run snapshot escapes {root}: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest_path, run_dir / "source_manifest.tsv")
    return run_dir


def write_run_summary(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
