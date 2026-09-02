"""Offline tests for generic, guarded source acquisition."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Self

import acquisition
import pytest
import requests
from pypdf import PdfWriter


def pdf_bytes(pages: int = 1) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


def record(**updates: object) -> acquisition.SourceRecord:
    base = acquisition.SourceRecord(
        source_order=1,
        source_id="source_1",
        provider="Archive",
        provider_id="A-1",
        title="Example source",
        source_date="1930-01-02",
        item_url="https://example.test/item/1",
        download_url="https://example.test/files/one.pdf",
        acquisition_method="direct",
        filename="collection/one.pdf",
        expected_sha256="",
        min_pages=1,
        max_pages=None,
        notes="",
    )
    return replace(base, **updates)


def write_manifest(path: Path, rows: list[acquisition.SourceRecord]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=acquisition.SOURCE_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            values = asdict(row)
            values["max_pages"] = "" if row.max_pages is None else row.max_pages
            writer.writerow(values)


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.failure = failure

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        if self.failure:
            raise self.failure
        yield self.content[midpoint:]


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_manifest_validation_and_bounded_default(tmp_path: Path) -> None:
    path = tmp_path / "sources.tsv"
    write_manifest(path, [record(), record(source_order=2, source_id="source_2", filename="two.pdf")])
    rows = acquisition.load_sources(path)
    assert [row.source_id for row in rows] == ["source_1", "source_2"]
    assert rows[0].source_date == "1930-01-02"
    assert [row.source_id for row in acquisition.select_sources(rows)] == ["source_1"]
    assert acquisition.select_sources(rows, all_sources=True) == rows

    write_manifest(path, [record(filename="../escape.pdf")])
    with pytest.raises(ValueError, match="Unsafe PDF filename"):
        acquisition.load_sources(path)

    write_manifest(path, [record(download_url="http://example.test/one.pdf")])
    with pytest.raises(ValueError, match="HTTPS"):
        acquisition.load_sources(path)

    with pytest.raises(ValueError, match="Invalid source_id"):
        acquisition.validate_source_records([record(source_id="INVALID PREFIX")])

    for invalid_date in ("January 1930", "19300102", "1930-02-30"):
        write_manifest(path, [record(source_date=invalid_date)])
        with pytest.raises(ValueError, match="Invalid source_date"):
            acquisition.load_sources(path)

    fields_without_date = tuple(field for field in acquisition.SOURCE_FIELDS if field != "source_date")
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields_without_date, delimiter="\t", lineterminator="\n")
        writer.writeheader()
    with pytest.raises(ValueError, match="Expected source columns"):
        acquisition.load_sources(path, allow_empty=True)


def test_path_boundary_allows_only_exact_project_or_external_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    expected = project / "sources" / "pdfs"
    assert acquisition.validate_pdf_root(expected, project_root=project, external_data_root=external) == expected.resolve()
    assert acquisition.validate_pdf_root(external / "pdfs", project_root=project, external_data_root=external) == (
        external / "pdfs"
    ).resolve()
    with pytest.raises(ValueError, match="must be exactly"):
        acquisition.validate_pdf_root(tmp_path / "wrong", project_root=project, external_data_root=external)

    outside = tmp_path / "outside"
    outside.mkdir()
    expected.mkdir(parents=True)
    (expected / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        acquisition.safe_destination(expected, "linked/escape.pdf")


def test_pdf_audit_enforces_hash_and_page_bounds(tmp_path: Path) -> None:
    path = tmp_path / "source.pdf"
    content = pdf_bytes(2)
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    audit = acquisition.audit_pdf(path, record(expected_sha256=expected, min_pages=2, max_pages=2))
    assert audit.physical_pages == 2
    assert audit.actual_sha256 == expected
    with pytest.raises(ValueError, match="at least 3"):
        acquisition.audit_pdf(path, record(min_pages=3))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        acquisition.audit_pdf(path, record(expected_sha256="0" * 64))


def test_resumable_download_uses_range_and_publishes_atomically(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    content = pdf_bytes(2)
    source = record(expected_sha256=hashlib.sha256(content).hexdigest(), min_pages=2)
    destination = pdf_root / source.filename
    destination.parent.mkdir()
    offset = len(content) // 3
    part = destination.with_suffix(".pdf.part")
    part.write_bytes(content[:offset])
    part.with_suffix(".part.json").write_text(
        json.dumps(acquisition.partial_contract(source), sort_keys=True),
        encoding="utf-8",
    )
    response = FakeResponse(
        content[offset:],
        status_code=206,
        headers={"Content-Range": f"bytes {offset}-{len(content) - 1}/{len(content)}"},
    )
    session = FakeSession([response])

    audit = acquisition.download_pdf(source, pdf_root, session)

    assert audit.physical_pages == 2
    assert destination.read_bytes() == content
    assert not part.exists()
    assert session.calls[0]["headers"] == {"Range": f"bytes={offset}-"}


def test_interruption_keeps_part_for_next_run(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    source = record()
    content = pdf_bytes()
    session = FakeSession([FakeResponse(content, failure=requests.ConnectionError("offline"))])
    with pytest.raises(requests.ConnectionError):
        acquisition.download_pdf(source, pdf_root, session)
    part = pdf_root / "collection" / "one.pdf.part"
    assert part.is_file() and part.stat().st_size > 0
    assert part.with_suffix(".part.json").is_file()
    assert not (pdf_root / source.filename).exists()


def test_unbound_or_wrong_partial_download_is_refused(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    source = record()
    part = pdf_root / "collection" / "one.pdf.part"
    part.parent.mkdir()
    part.write_bytes(b"partial")
    session = FakeSession([FakeResponse(pdf_bytes())])
    with pytest.raises(ValueError, match="unbound partial"):
        acquisition.download_pdf(source, pdf_root, session)
    assert session.calls == []


def test_manual_source_inventory_and_snapshots(tmp_path: Path) -> None:
    manifest = tmp_path / "source_manifest.tsv"
    manual = record(acquisition_method="manual", download_url="", source_date="")
    write_manifest(manifest, [manual])
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    unresolved = acquisition.acquire_record(manual, pdf_root)
    assert unresolved.status == "manual_required"

    destination = pdf_root / manual.filename
    destination.parent.mkdir()
    destination.write_bytes(pdf_bytes())
    resolved = acquisition.acquire_record(manual, pdf_root)
    assert resolved.status == "valid_existing"
    assert acquisition.inventory_row(resolved, pdf_root, checked_at="now")["source_date"] == ""

    run = acquisition.create_run_snapshot(manifest, tmp_path / "runs", run_id="run-001")
    acquisition.append_event(run / "events.jsonl", {"source_id": manual.source_id, "status": resolved.status})
    assert (run / "source_manifest.tsv").read_bytes() == manifest.read_bytes()
    assert json.loads((run / "events.jsonl").read_text(encoding="utf-8"))["status"] == "valid_existing"
