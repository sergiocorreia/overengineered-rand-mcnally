"""Offline tests for the read-only V1 recovery audit."""

import csv
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import recovery_audit


def _row(
    order: int,
    edition_id: str,
    *,
    source: str = "fraser",
    start: int | None = 1,
    end: int | None = 1,
    range_kind: str = "multipart_pdf_position",
    status: str = "configured",
    output_pages: int = 1,
) -> dict[str, object]:
    year, edition = edition_id.split("-")
    return {
        "manifest_order": order,
        "year": year,
        "edition": edition,
        "configured_source": source,
        "range_start": "" if start is None else start,
        "range_end": "" if end is None else end,
        "configured_range_kind": range_kind,
        "manifest_row_status": status,
        "output_page_count": output_pages,
    }


def _write_inventory(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=recovery_audit.MIGRATION_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[str, int]]:
    project = tmp_path / "project"
    legacy = tmp_path / "legacy"
    recovery = tmp_path / "recovery"
    for path in (project / "output", legacy, recovery / "downloads", recovery / "cache", recovery / "extracted_images"):
        path.mkdir(parents=True)

    rows = [
        _row(1, "1881-1", source="hathi", start=2, end=3, range_kind="physical_pdf_page", output_pages=2),
        _row(2, "1930-1", start=2, end=3, output_pages=3),
        _row(3, "1803-1", source="hathi", start=1, end=2, range_kind="physical_pdf_page", status="invalid", output_pages=0),
    ]
    page_counts = {"1881-1-hathi.pdf": 4}
    (recovery / "downloads" / "1881-1-hathi.pdf").write_bytes(b"fixture")
    multipart = recovery / "downloads" / "1930-1-fraser"
    multipart.mkdir()
    for name, count in (("a.pdf", 1), ("b.pdf", 2), ("c.pdf", 1)):
        (multipart / name).write_bytes(name.encode())
        page_counts[f"1930-1-fraser/{name}"] = count

    for offset, edition_id in enumerate(sorted(recovery_audit.KNOWN_UNPROCESSED_EDITIONS), 4):
        rows.append(_row(offset, edition_id, output_pages=0))
        folder = recovery / "downloads" / f"{edition_id}-fraser"
        folder.mkdir()
        (folder / "part.pdf").write_bytes(edition_id.encode())
        page_counts[f"{edition_id}-fraser/part.pdf"] = 1

    inventory = project / "legacy_migration_inventory.tsv"
    _write_inventory(inventory, rows)
    return project, legacy, recovery, inventory, page_counts


def _counter(recovery: Path, legacy: Path, page_counts: dict[str, int]):
    def count(path: Path) -> int:
        if path.is_relative_to(recovery / "downloads"):
            key = path.relative_to(recovery / "downloads").as_posix()
        else:
            key = f"legacy/{path.relative_to(legacy / 'sources').as_posix()}"
        return page_counts[key]

    return count


def _audit(
    project: Path,
    legacy: Path,
    recovery: Path,
    inventory: Path,
    page_counts: dict[str, int],
    *,
    previous: Path | None = None,
    scanned_at: datetime | None = None,
    raw_crosswalk: Path | None = None,
    expected_metadata_counts: tuple[int, int, int] = (7, 6, 0),
) -> recovery_audit.AuditBundle:
    return recovery_audit.audit_recovery(
        project_root=project,
        legacy_root=legacy,
        recovery_root=recovery,
        migration_inventory=inventory,
        raw_crosswalk=raw_crosswalk,
        previous_snapshot=previous,
        minimum_quiet_seconds=60,
        scanned_at=scanned_at,
        page_counter=_counter(recovery, legacy, page_counts),
        expected_metadata_counts=expected_metadata_counts,
    )


def test_maps_physical_pages_and_hashes_cache_as_diagnostic_only(tmp_path: Path) -> None:
    project, legacy, recovery, inventory, page_counts = _fixture(tmp_path)
    cache = recovery / "cache"
    (cache / "1881-1").mkdir()
    cached_json = b'{"model":"gemini-3-flash-preview","data":{}}'
    (cache / "1881-1" / "2.json").write_bytes(cached_json)
    (cache / "1881-1" / "3.error").write_text("legacy provider error", encoding="utf-8")
    (cache / "1930-1-part-2").mkdir()
    (cache / "1930-1-part-2" / "1.json").write_text("{}", encoding="utf-8")
    (cache / "1925-2-part-8").mkdir()
    (cache / "1925-2-part-8" / "1.json").write_text("{}", encoding="utf-8")

    bundle = _audit(project, legacy, recovery, inventory, page_counts)
    page_ids = [page.page_id for page in bundle.pages]
    assert page_ids[:5] == [
        "1881-1-hathi.pdf#page=2",
        "1881-1-hathi.pdf#page=3",
        "1930-1-fraser/b.pdf#page=1",
        "1930-1-fraser/b.pdf#page=2",
        "1930-1-fraser/c.pdf#page=1",
    ]
    cache_by_path = {row.cache_relative_path: row for row in bundle.cache_artifacts}
    assert cache_by_path["1881-1/2.json"].page_id == "1881-1-hathi.pdf#page=2"
    assert cache_by_path["1881-1/2.json"].content_sha256 == hashlib.sha256(cached_json).hexdigest()
    assert cache_by_path["1881-1/2.json"].model == "gemini-3-flash-preview"
    assert cache_by_path["1881-1/3.error"].kind == "error"
    assert cache_by_path["1925-2-part-8/1.json"].mapping_status == "unmapped_diagnostic"
    assert bundle.report["cache"]["purpose"] == "diagnostic_only_not_a_reusable_cache"
    assert bundle.report["metadata"]["invalid_editions"] == ["1803-1"]
    assert bundle.report["metadata"]["unprocessed_editions"] == ["1941-1", "1941-2", "1942-1", "1942-2"]


def test_finalize_requires_two_unchanged_snapshots_and_no_partial_files(tmp_path: Path) -> None:
    project, legacy, recovery, inventory, page_counts = _fixture(tmp_path)
    initial_time = datetime(2030, 1, 1, tzinfo=UTC)
    first = _audit(project, legacy, recovery, inventory, page_counts, scanned_at=initial_time)
    first_path = project / "output" / "first.json"
    recovery_audit.write_report(first_path, first.report)
    assert not first.report["finalizable"]

    (recovery / "cache" / "1881-1").mkdir()
    (recovery / "cache" / "1881-1" / "2.json").write_text("{}", encoding="utf-8")
    second = _audit(
        project,
        legacy,
        recovery,
        inventory,
        page_counts,
        previous=first_path,
        scanned_at=initial_time + timedelta(minutes=2),
    )
    assert not second.report["comparison"]["unchanged_since_previous"]
    second_path = project / "output" / "second.json"
    recovery_audit.write_report(second_path, second.report)

    third = _audit(
        project,
        legacy,
        recovery,
        inventory,
        page_counts,
        previous=second_path,
        scanned_at=initial_time + timedelta(minutes=4),
    )
    assert third.report["finalizable"]

    (recovery / "downloads" / "copy.pdf.i7urnz").write_bytes(b"partial")
    third_path = project / "output" / "third.json"
    recovery_audit.write_report(third_path, third.report)
    fourth = _audit(
        project,
        legacy,
        recovery,
        inventory,
        page_counts,
        previous=third_path,
        scanned_at=initial_time + timedelta(minutes=6),
    )
    assert fourth.report["partial_transfer_artifact_count"] == 1
    assert not fourth.report["gates"]["no_partial_transfer_artifacts"]
    assert not fourth.report["finalizable"]


def test_unresolved_raw_identity_and_invalid_json_block_finalize(tmp_path: Path) -> None:
    project, legacy, recovery, inventory, page_counts = _fixture(tmp_path)
    with inventory.open("a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=recovery_audit.MIGRATION_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writerow(_row(8, "1879-2", source="archive-raw", start=79, end=80, range_kind="raw_scan_index"))
    raw = recovery / "downloads" / "1879-2-archive-raw"
    raw.mkdir()
    (raw / "directory_0079.jp2").write_bytes(b"image")
    (raw / "directory_0080.jp2").write_bytes(b"image")
    (recovery / "cache" / "1879-2").mkdir()
    (recovery / "cache" / "1879-2" / "79.json").write_text("{truncated", encoding="utf-8")

    bundle = _audit(project, legacy, recovery, inventory, page_counts)
    raw_source = next(source for source in bundle.report["sources"]["editions"] if source["edition_id"] == "1879-2")
    assert raw_source["status"] == "identity_unresolved"
    assert bundle.report["cache"]["invalid_json_count"] == 1
    assert not bundle.report["gates"]["all_configured_sources_mapped"]
    assert not bundle.report["gates"]["cache_artifacts_readable"]


def test_reviewed_raw_crosswalk_and_legacy_fallback_map_physical_pages(tmp_path: Path) -> None:
    project, legacy, recovery, inventory, page_counts = _fixture(tmp_path)
    extra_rows = [
        _row(8, "1879-2", source="archive-raw", start=79, end=80, range_kind="raw_scan_index"),
        _row(9, "1887-1", source="hathi", start=2, end=3, range_kind="physical_pdf_page"),
        _row(10, "1903-1", source="hathi", start=2, end=3, range_kind="physical_pdf_page"),
    ]
    with inventory.open("a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=recovery_audit.MIGRATION_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writerows(extra_rows)
    raw = recovery / "downloads" / "1879-2-archive-raw"
    raw.mkdir()
    for page in (79, 80):
        (raw / f"scan_{page:04d}.jp2").write_bytes(b"image")
    legacy_sources = legacy / "sources"
    legacy_sources.mkdir()
    raw_pdf = legacy_sources / "1879-2-archive.pdf"
    raw_pdf.write_bytes(b"reviewed raw source")
    raw_sha = hashlib.sha256(raw_pdf.read_bytes()).hexdigest()
    page_counts["legacy/1879-2-archive.pdf"] = 100
    for edition_id in ("1887-1", "1903-1"):
        fallback = legacy_sources / f"{edition_id}-hathi.pdf"
        fallback.write_bytes(edition_id.encode())
        page_counts[f"legacy/{fallback.name}"] = 4
    crosswalk = project / "raw_scan_pdf_crosswalk.tsv"
    with crosswalk.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=recovery_audit.RAW_CROSSWALK_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            {
                "year": 1879,
                "edition": 2,
                "configured_source": "archive-raw",
                "legacy_page_start": 79,
                "legacy_page_end": 80,
                "source_relative_path": raw_pdf.name,
                "v2_pdf_relative_path": "1879-2-archive-raw.pdf",
                "physical_page_offset": -4,
                "source_sha256": raw_sha,
                "physical_page_count": 100,
                "evidence": "reviewed boundary comparison",
                "note": "fixture",
            }
        )
    (recovery / "cache" / "1879-2").mkdir()
    (recovery / "cache" / "1879-2" / "79.json").write_text("{}", encoding="utf-8")

    bundle = _audit(
        project,
        legacy,
        recovery,
        inventory,
        page_counts,
        raw_crosswalk=crosswalk,
        expected_metadata_counts=(10, 9, 0),
    )
    sources = {row["edition_id"]: row for row in bundle.report["sources"]["editions"]}
    assert sources["1879-2"]["source_origin"] == "reviewed_raw_crosswalk"
    assert sources["1887-1"]["source_origin"] == sources["1903-1"]["source_origin"] == "legacy_fallback"
    assert sources["1887-1"]["v1_copy_status"] == sources["1903-1"]["v1_copy_status"] == "missing"
    assert all(sources[edition]["status"] == "mapped" for edition in ("1879-2", "1887-1", "1903-1"))
    raw_page = next(page for page in bundle.pages if page.edition_id == "1879-2" and page.cache_page == 79)
    assert (raw_page.physical_page, raw_page.page_id) == (75, "1879-2-archive-raw.pdf#page=75")
    assert raw_page.crosswalk_sha256 == hashlib.sha256(crosswalk.read_bytes()).hexdigest()
    assert bundle.report["raw_scan_pdf_crosswalk"]["sha256"] == raw_page.crosswalk_sha256
    cached = next(row for row in bundle.cache_artifacts if row.cache_relative_path == "1879-2/79.json")
    assert cached.page_id == raw_page.page_id


def test_outputs_are_confined_to_project_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "output").mkdir(parents=True)
    accepted = recovery_audit.checked_output_path(Path("output/recovery/report.json"), project)
    assert accepted == project / "output" / "recovery" / "report.json"
    with pytest.raises(ValueError, match="must remain"):
        recovery_audit.checked_output_path(project / "report.json", project)
    with pytest.raises(ValueError, match="must remain"):
        recovery_audit.checked_output_path(tmp_path / "legacy" / "report.json", project)


def test_diagnostic_exports_are_explicitly_labeled(tmp_path: Path) -> None:
    pages = [
        recovery_audit.PageMapping(
            1,
            "1881-1",
            "hathi",
            "physical_pdf_page",
            "v1_recovery",
            "available",
            "",
            "",
            "source.pdf",
            2,
            "source.pdf#page=2",
            "1881-1",
            2,
        )
    ]
    cache = [
        recovery_audit.CacheArtifact(
            "1881-1/2.json", "json", 2, "a" * 64, "1881-1", 2, "1881-1", "source.pdf#page=2", "mapped", "valid", "model"
        )
    ]
    page_path = tmp_path / "pages.tsv"
    cache_path = tmp_path / "cache.tsv"
    recovery_audit.write_page_mapping(page_path, pages)
    recovery_audit.write_cache_manifest(cache_path, cache)
    assert page_path.read_text(encoding="utf-8").splitlines()[1].startswith("recovery_mapping_not_extraction_input\t")
    assert cache_path.read_text(encoding="utf-8").splitlines()[1].startswith("diagnostic_only_not_a_reusable_cache\t")
