#!/usr/bin/env -S uv run --project .
"""Stage only PDFs named by a signed rerun queue; dry-run by default."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

from histdata_pipeline.config import ProjectConfig, load_project_config

CODE_DIR = Path(__file__).resolve().parent
RECEIPT_NAME = "ranking_receipt.json"
OUTPUT_NAME = "stage-queue-sources.json"
QUEUE_FIELDS = ("selection_rank", "page_id", "pdf_relative_path", "physical_page", "source_sha256", "year", "edition")
CROSSWALK_FIELDS = (
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
)
CROSSWALK_PATH = Path("manual/raw_scan_pdf_crosswalk.tsv")
SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class QueueEvidence:
    path: Path
    relative_path: str
    sha256: str
    byte_count: int
    row_count: int
    receipt_signature: str
    pdf_hashes: dict[str, str]
    pdf_year_editions: dict[str, tuple[int, int]]
    pdf_pages: dict[str, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class CrosswalkEntry:
    year: int
    edition: int
    source_relative_path: str
    target_relative_path: str
    physical_page_offset: int
    source_sha256: str
    physical_page_count: int


@dataclass(frozen=True, slots=True)
class StagedPDF:
    relative_path: str
    sha256: str
    source: Path
    source_root_name: str
    source_relative_path: str
    target: Path
    byte_count: int
    action: str
    physical_page_offset: int | None


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _configured_root(config: ProjectConfig, key: str) -> Path:
    value = str(config.table("restoration").get(key, "")).strip()
    if not value:
        raise ValueError(f"project.toml restoration.{key} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"restoration.{key} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Configured read-only root is absent: {path}") from error
    if not resolved.is_dir():
        raise ValueError(f"Configured read-only root is not a directory: {path}")
    return resolved


def _safe_relative_pdf(raw: str) -> str:
    if not raw or raw != raw.strip() or "\\" in raw:
        raise ValueError(f"Unsafe PDF relative path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe PDF relative path: {raw!r}")
    if path.parts[0].casefold() == "pdfs" or path.suffix.casefold() != ".pdf":
        raise ValueError(f"PDF path must be relative to the PDF root and end in .pdf: {raw!r}")
    return path.as_posix()


def _project_input(config: ProjectConfig, path: Path) -> tuple[Path, str]:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = config.root / candidate
    resolved = candidate.resolve(strict=True)
    root = config.root.resolve()
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"Input must be a regular file inside V2: {path}")
    return resolved, resolved.relative_to(root).as_posix()


def load_signed_queue(config: ProjectConfig, queue_path: Path) -> QueueEvidence:
    queue, relative_queue = _project_input(config, queue_path)
    data = queue.read_bytes()
    queue_hash = hashlib.sha256(data).hexdigest()
    try:
        reader = csv.DictReader(StringIO(data.decode("utf-8")), delimiter="\t")
        missing = [field for field in QUEUE_FIELDS if field not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"Queue is missing fields: {', '.join(missing)}")
        rows = list(reader)
    except UnicodeDecodeError as error:
        raise ValueError("Queue must be UTF-8") from error

    pdf_hashes: dict[str, str] = {}
    pdf_year_editions: dict[str, tuple[int, int]] = {}
    pdf_pages: dict[str, list[int]] = {}
    page_ids: set[str] = set()
    for rank, row in enumerate(rows, start=1):
        if row["selection_rank"].strip() != str(rank):
            raise ValueError("selection_rank must be consecutive and match queue order")
        relative_pdf = _safe_relative_pdf(row["pdf_relative_path"])
        raw_page = row["physical_page"].strip()
        if not raw_page.isdigit() or int(raw_page) < 1:
            raise ValueError(f"Invalid physical_page at queue row {rank}")
        page_id = row["page_id"].strip()
        if page_id != f"{relative_pdf}#page={int(raw_page)}" or page_id in page_ids:
            raise ValueError(f"Invalid or duplicate page_id at queue row {rank}")
        page_ids.add(page_id)
        expected_hash = row["source_sha256"].strip()
        if len(expected_hash) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in expected_hash):
            raise ValueError(f"Invalid source_sha256 at queue row {rank}")
        prior = pdf_hashes.setdefault(relative_pdf, expected_hash)
        if prior != expected_hash:
            raise ValueError(f"Inconsistent source_sha256 for queued PDF: {relative_pdf}")
        raw_year = row["year"].strip()
        raw_edition = row["edition"].strip()
        if not raw_year.isdigit() or not raw_edition.isdigit() or int(raw_year) < 1 or int(raw_edition) not in (1, 2):
            raise ValueError(f"Invalid year/edition at queue row {rank}")
        year_edition = (int(raw_year), int(raw_edition))
        prior_year_edition = pdf_year_editions.setdefault(relative_pdf, year_edition)
        if prior_year_edition != year_edition:
            raise ValueError(f"Inconsistent year/edition for queued PDF: {relative_pdf}")
        pdf_pages.setdefault(relative_pdf, []).append(int(raw_page))

    receipt, _ = _project_input(config, queue.parent / RECEIPT_NAME)
    try:
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid ranking receipt JSON: {receipt}") from error
    if not isinstance(receipt_payload, dict):
        raise ValueError("Ranking receipt must be a JSON object")
    signature = receipt_payload.get("receipt_signature")
    unsigned = {key: value for key, value in receipt_payload.items() if key != "receipt_signature"}
    if not isinstance(signature, str) or signature != stable_hash(unsigned):
        raise ValueError("Ranking receipt signature is invalid")
    checks = {
        "selected_queue_path": relative_queue,
        "selected_queue_sha256": queue_hash,
        "selected_queue_bytes": len(data),
        "selected_queue_rows": len(rows),
    }
    for field, expected in checks.items():
        if receipt_payload.get(field) != expected:
            raise ValueError(f"Ranking receipt {field} does not match the selected queue")
    return QueueEvidence(
        path=queue,
        relative_path=relative_queue,
        sha256=queue_hash,
        byte_count=len(data),
        row_count=len(rows),
        receipt_signature=signature,
        pdf_hashes=pdf_hashes,
        pdf_year_editions=pdf_year_editions,
        pdf_pages={path: tuple(pages) for path, pages in pdf_pages.items()},
    )


def _candidate(root: Path, relative_pdf: str) -> Path | None:
    lexical = root.joinpath(*PurePosixPath(relative_pdf).parts)
    if not lexical.exists():
        return None
    resolved = lexical.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"Source path escapes its configured read-only root: {relative_pdf}")
    return resolved


def _target_path(target_root: Path, relative_pdf: str) -> Path:
    target = target_root.joinpath(*PurePosixPath(relative_pdf).parts)
    resolved = target.resolve()
    if not _inside(resolved, target_root):
        raise ValueError(f"Target path escapes the configured V2 PDF root: {relative_pdf}")
    if target.is_symlink():
        raise ValueError(f"Refusing a symlink target: {target}")
    return resolved


def _integer(row: dict[str, str], field: str, line: int, *, minimum: int | None = None) -> int:
    raw = row[field].strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"Crosswalk {field} must be an integer at line {line}") from error
    if str(value) != raw or (minimum is not None and value < minimum):
        raise ValueError(f"Invalid crosswalk {field} at line {line}")
    return value


def load_crosswalk(config: ProjectConfig) -> tuple[dict[str, CrosswalkEntry], str]:
    path, _ = _project_input(config, config.root / CROSSWALK_PATH)
    data = path.read_bytes()
    try:
        reader = csv.DictReader(StringIO(data.decode("utf-8")), delimiter="\t")
        missing = [field for field in CROSSWALK_FIELDS if field not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"Raw-scan crosswalk is missing fields: {', '.join(missing)}")
        rows = list(reader)
    except UnicodeDecodeError as error:
        raise ValueError("Raw-scan crosswalk must be UTF-8") from error

    entries: dict[str, CrosswalkEntry] = {}
    sources: set[str] = set()
    for line, row in enumerate(rows, start=2):
        year = _integer(row, "year", line, minimum=1)
        edition = _integer(row, "edition", line, minimum=1)
        if edition not in (1, 2) or row["configured_source"].strip() != "archive-raw":
            raise ValueError(f"Invalid archive-raw identity in crosswalk at line {line}")
        start = _integer(row, "legacy_page_start", line, minimum=1)
        end = _integer(row, "legacy_page_end", line, minimum=1)
        if end < start:
            raise ValueError(f"Crosswalk legacy page range is reversed at line {line}")
        source = _safe_relative_pdf(row["source_relative_path"])
        target = _safe_relative_pdf(row["v2_pdf_relative_path"])
        offset = _integer(row, "physical_page_offset", line)
        page_count = _integer(row, "physical_page_count", line, minimum=1)
        digest = row["source_sha256"].strip()
        if len(digest) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"Invalid crosswalk source_sha256 at line {line}")
        if target in entries or source in sources:
            raise ValueError(f"Duplicate source or target in raw-scan crosswalk at line {line}")
        sources.add(source)
        entries[target] = CrosswalkEntry(year, edition, source, target, offset, digest, page_count)
    return entries, hashlib.sha256(data).hexdigest()


def build_plan(
    config: ProjectConfig,
    evidence: QueueEvidence,
) -> tuple[list[StagedPDF], str, list[CrosswalkEntry]]:
    recovered_root = _configured_root(config, "recovered_v1_root")
    legacy_root = _configured_root(config, "legacy_root")
    recovered = recovered_root / "downloads"
    legacy = legacy_root / "sources"
    if not recovered.is_dir():
        raise FileNotFoundError(f"Recovered V1 downloads directory is absent: {recovered}")
    if not legacy.is_dir():
        raise FileNotFoundError(f"Legacy sources directory is absent: {legacy}")
    recovered = recovered.resolve(strict=True)
    legacy = legacy.resolve(strict=True)
    target_root = config.pdf_directory.resolve()
    if not _inside(target_root, config.external_root):
        raise ValueError("Configured V2 PDF root must be inside external_data_root")
    immutable_roots = (recovered_root, legacy_root)
    if any(_inside(target_root, root) or _inside(root, target_root) for root in immutable_roots):
        raise ValueError("V2 PDF root overlaps a full immutable source root")
    crosswalk, crosswalk_sha256 = load_crosswalk(config)

    plan: list[StagedPDF] = []
    used_crosswalk: list[CrosswalkEntry] = []
    for relative_pdf, expected_hash in sorted(evidence.pdf_hashes.items()):
        alias = crosswalk.get(relative_pdf)
        if relative_pdf.endswith("-archive-raw.pdf") and alias is None:
            raise ValueError(f"Raw-scan alias lacks an explicit crosswalk row: {relative_pdf}")
        source_relative = alias.source_relative_path if alias else relative_pdf
        if alias is not None:
            if alias.source_sha256 != expected_hash:
                raise ValueError(f"Queue/crosswalk SHA-256 mismatch for {relative_pdf}")
            if evidence.pdf_year_editions[relative_pdf] != (alias.year, alias.edition):
                raise ValueError(f"Queue/crosswalk year-edition mismatch for {relative_pdf}")
            if any(page > alias.physical_page_count for page in evidence.pdf_pages[relative_pdf]):
                raise ValueError(f"Queued page exceeds crosswalk PDF page count for {relative_pdf}")
            used_crosswalk.append(alias)
        mismatches: list[str] = []
        selected: tuple[Path, str] | None = None
        for root, root_name in ((recovered, "recovered_v1/downloads"), (legacy, "legacy/sources")):
            source = _candidate(root, source_relative)
            if source is None:
                continue
            actual_hash = sha256_file(source)
            if actual_hash == expected_hash:
                selected = source, root_name
                break
            mismatches.append(f"{root_name}={actual_hash}")
        if selected is None:
            detail = f"; mismatches: {', '.join(mismatches)}" if mismatches else ""
            raise FileNotFoundError(f"No hash-matching source for {relative_pdf}{detail}")
        source, root_name = selected
        target = _target_path(target_root, relative_pdf)
        if target.exists():
            if not target.is_file() or sha256_file(target) != expected_hash:
                raise FileExistsError(f"Refusing to overwrite conflicting target: {target}")
            action = "already_present"
        else:
            action = "would_copy"
        plan.append(
            StagedPDF(
                relative_path=relative_pdf,
                sha256=expected_hash,
                source=source,
                source_root_name=root_name,
                source_relative_path=source_relative,
                target=target,
                byte_count=source.stat().st_size,
                action=action,
                physical_page_offset=alias.physical_page_offset if alias else None,
            )
        )
    return plan, crosswalk_sha256, used_crosswalk


def _copy_atomic(item: StagedPDF, target_root: Path) -> None:
    if sha256_file(item.source) != item.sha256:
        raise RuntimeError(f"Source changed after preflight: {item.relative_path}")
    item.target.parent.mkdir(parents=True, exist_ok=True)
    if not _inside(item.target.parent.resolve(strict=True), target_root):
        raise ValueError(f"Target parent escapes the configured V2 PDF root: {item.relative_path}")
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{item.target.name}.stage-", dir=item.target.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as destination, item.source.open("rb") as source:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if sha256_file(temp) != item.sha256:
            raise RuntimeError(f"Temporary copy failed SHA-256 verification: {item.relative_path}")
        try:
            os.link(temp, item.target)
        except FileExistsError as error:
            raise FileExistsError(f"Target appeared during copy; nothing was overwritten: {item.target}") from error
        if sha256_file(item.target) != item.sha256:
            raise RuntimeError(f"Staged target failed SHA-256 verification: {item.relative_path}")
    finally:
        temp.unlink(missing_ok=True)


def _receipt_payload(
    config: ProjectConfig,
    evidence: QueueEvidence,
    plan: list[StagedPDF],
    crosswalk_sha256: str,
    used_crosswalk: list[CrosswalkEntry],
    *,
    copied: bool,
) -> dict[str, Any]:
    files = [
        {
            "action": "copied" if copied and item.action == "would_copy" else item.action,
            "bytes": item.byte_count,
            "pdf_relative_path": item.relative_path,
            "source_location": item.source_root_name,
            "source_relative_path": item.source_relative_path,
            "source_sha256": item.sha256,
            "target_relative_path": item.target.relative_to(config.pdf_directory).as_posix(),
            "physical_page_offset": item.physical_page_offset,
        }
        for item in plan
    ]
    unsigned = {
        "receipt_schema": "rand-mcnally-source-staging/v1",
        "mode": "copy" if copied else "dry_run",
        "queue": {
            "bytes": evidence.byte_count,
            "path": evidence.relative_path,
            "receipt_signature": evidence.receipt_signature,
            "rows": evidence.row_count,
            "sha256": evidence.sha256,
        },
        "crosswalk": {
            "path": CROSSWALK_PATH.as_posix(),
            "sha256": crosswalk_sha256,
            "used": [
                {
                    "physical_page_offset": entry.physical_page_offset,
                    "source_relative_path": entry.source_relative_path,
                    "target_relative_path": entry.target_relative_path,
                }
                for entry in used_crosswalk
            ],
        },
        "distinct_pdf_count": len(files),
        "bytes_to_copy": sum(item.byte_count for item in plan if item.action == "would_copy"),
        "files": files,
    }
    return {**unsigned, "staging_signature": stable_hash(unsigned)}


def _write_receipt(config: ProjectConfig, payload: dict[str, Any]) -> Path:
    output_root = (config.root / "output").resolve()
    if not _inside(output_root, config.root.resolve()):
        raise ValueError("V2 output directory escapes the project")
    output_root.mkdir(parents=True, exist_ok=True)
    signature = str(payload["staging_signature"])
    if len(signature) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in signature):
        raise ValueError("Invalid staging receipt signature")
    destination = output_root / f"{Path(OUTPUT_NAME).stem}-{signature}.json"
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != data:
            raise FileExistsError(f"Refusing to replace conflicting immutable staging receipt: {destination}")
        return destination
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{OUTPUT_NAME}.stage-", dir=output_root)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temp, destination)
        except FileExistsError:
            if not destination.is_file() or destination.read_bytes() != data:
                raise FileExistsError(f"Conflicting staging receipt appeared concurrently: {destination}") from None
    finally:
        temp.unlink(missing_ok=True)
    return destination


def stage_queue_sources(config: ProjectConfig, queue_path: Path, *, copy: bool) -> tuple[dict[str, Any], Path]:
    evidence = load_signed_queue(config, queue_path)
    plan, crosswalk_sha256, used_crosswalk = build_plan(config, evidence)
    if copy:
        target_root = config.pdf_directory.resolve()
        for item in plan:
            if item.action == "would_copy":
                _copy_atomic(item, target_root)
        for item in plan:
            if not item.target.is_file() or sha256_file(item.target) != item.sha256:
                raise RuntimeError(f"Staged target changed after preflight: {item.relative_path}")
    payload = _receipt_payload(config, evidence, plan, crosswalk_sha256, used_crosswalk, copied=copy)
    return payload, _write_receipt(config, payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True, help="Signed selected_pages.tsv inside V2.")
    parser.add_argument("--copy", action="store_true", help="Copy the exact queued PDFs; default is dry-run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_project_config(CODE_DIR)
        payload, receipt = stage_queue_sources(config, args.queue, copy=args.copy)
    except (FileExistsError, FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"receipt": str(receipt), **payload}, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
