"""Offline tests for exact, hash-bound queue source staging."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest

from histdata_pipeline.config import ProjectConfig

SCRIPT = Path(__file__).with_name("stage_queue_sources.py")


@pytest.fixture(scope="module")
def staging() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rand_mcnally_stage_queue_sources", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_config(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "v2"
    recovered = tmp_path / "v1"
    legacy = tmp_path / "legacy"
    external = tmp_path / "external-v2"
    for directory in (root, recovered / "downloads", legacy / "sources", external):
        directory.mkdir(parents=True, exist_ok=True)
    crosswalk = root / "manual" / "raw_scan_pdf_crosswalk.tsv"
    crosswalk.parent.mkdir()
    crosswalk.write_text(
        "\t".join(
            (
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
        )
        + "\n",
        encoding="utf-8",
    )
    return ProjectConfig(
        root=root,
        values={
            "project": {"slug": "rand-mcnally-v2"},
            "restoration": {
                "recovered_v1_root": str(recovered),
                "recovered_v1_root_read_only": True,
                "legacy_root": str(legacy),
                "legacy_root_read_only": True,
            },
            "storage": {
                "external_data_root": str(external),
                "pdf_storage": "external",
                "external_pdf_subdirectory": "pdfs",
            },
        },
    )


def write_queue(staging: ModuleType, config: ProjectConfig, rows: list[dict[str, str]]) -> Path:
    queue = config.root / "output" / "rerun-ranking" / "selected_pages.tsv"
    queue.parent.mkdir(parents=True, exist_ok=True)
    fields = ["selection_rank", "page_id", "pdf_relative_path", "physical_page", "source_sha256", "year", "edition"]
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    queue.write_text(buffer.getvalue(), encoding="utf-8")
    unsigned = {
        "selected_queue_path": queue.relative_to(config.root).as_posix(),
        "selected_queue_sha256": staging.sha256_file(queue),
        "selected_queue_bytes": queue.stat().st_size,
        "selected_queue_rows": len(rows),
    }
    (queue.parent / staging.RECEIPT_NAME).write_text(
        json.dumps({**unsigned, "receipt_signature": staging.stable_hash(unsigned)}),
        encoding="utf-8",
    )
    return queue


def queue_row(rank: int, path: str, page: int, digest: str, *, year: int = 1900, edition: int = 1) -> dict[str, str]:
    return {
        "selection_rank": str(rank),
        "page_id": f"{path}#page={page}",
        "pdf_relative_path": path,
        "physical_page": str(page),
        "source_sha256": digest,
        "year": str(year),
        "edition": str(edition),
    }


def test_dry_run_selects_only_distinct_queued_pdfs_and_is_deterministic(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = b"%PDF-1.7\nrecovered\n"
    second = b"%PDF-1.7\nlegacy fallback\n"
    recovered_pdf = Path(config.table("restoration")["recovered_v1_root"]) / "downloads" / "nested" / "first.pdf"
    legacy_pdf = Path(config.table("restoration")["legacy_root"]) / "sources" / "second.pdf"
    recovered_pdf.parent.mkdir()
    recovered_pdf.write_bytes(first)
    legacy_pdf.write_bytes(second)
    first_hash = staging.sha256_file(recovered_pdf)
    second_hash = staging.sha256_file(legacy_pdf)
    queue = write_queue(
        staging,
        config,
        [
            queue_row(1, "nested/first.pdf", 8, first_hash),
            queue_row(2, "nested/first.pdf", 9, first_hash),
            queue_row(3, "second.pdf", 2, second_hash),
        ],
    )

    payload, receipt = staging.stage_queue_sources(config, queue, copy=False)
    first_receipt = receipt.read_bytes()
    repeated, repeated_receipt = staging.stage_queue_sources(config, queue, copy=False)
    assert payload == repeated
    assert first_receipt == repeated_receipt.read_bytes()
    assert payload["mode"] == "dry_run"
    assert payload["distinct_pdf_count"] == 2
    assert [row["pdf_relative_path"] for row in payload["files"]] == ["nested/first.pdf", "second.pdf"]
    assert [row["source_location"] for row in payload["files"]] == ["recovered_v1/downloads", "legacy/sources"]
    assert all(row["action"] == "would_copy" for row in payload["files"])
    assert not (config.pdf_directory / "nested" / "first.pdf").exists()
    assert payload["staging_signature"] == staging.stable_hash({key: value for key, value in payload.items() if key != "staging_signature"})


def test_copy_is_atomic_hash_verified_and_never_rewrites_matching_targets(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    source = Path(config.table("restoration")["recovered_v1_root"]) / "downloads" / "volume.pdf"
    source.write_bytes(b"%PDF-1.7\nselected volume\n")
    digest = staging.sha256_file(source)
    queue = write_queue(staging, config, [queue_row(1, "volume.pdf", 1, digest)])

    _, dry_receipt = staging.stage_queue_sources(config, queue, copy=False)
    dry_receipt_bytes = dry_receipt.read_bytes()
    payload, copy_receipt = staging.stage_queue_sources(config, queue, copy=True)
    target = config.pdf_directory / "volume.pdf"
    assert target.read_bytes() == source.read_bytes()
    assert staging.sha256_file(target) == digest
    assert payload["files"][0]["action"] == "copied"
    assert copy_receipt != dry_receipt
    assert dry_receipt.read_bytes() == dry_receipt_bytes
    assert copy_receipt.is_file()
    assert not list(target.parent.glob(".*.stage-*"))

    prior_stat = target.stat()
    repeated, _ = staging.stage_queue_sources(config, queue, copy=True)
    assert repeated["files"][0]["action"] == "already_present"
    assert target.stat().st_ino == prior_stat.st_ino
    assert target.stat().st_mtime_ns == prior_stat.st_mtime_ns


def test_inconsistent_queue_hash_tampering_and_conflicting_target_fail_closed(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    source = Path(config.table("restoration")["recovered_v1_root"]) / "downloads" / "volume.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n")
    digest = staging.sha256_file(source)
    queue = write_queue(
        staging,
        config,
        [queue_row(1, "volume.pdf", 1, digest), queue_row(2, "volume.pdf", 2, "a" * 64)],
    )
    with pytest.raises(ValueError, match="Inconsistent source_sha256"):
        staging.stage_queue_sources(config, queue, copy=False)

    queue = write_queue(staging, config, [queue_row(1, "volume.pdf", 1, digest)])
    queue.write_text(queue.read_text(encoding="utf-8").replace("#page=1", "#page=2"), encoding="utf-8")
    with pytest.raises(ValueError, match="page_id|selected_queue"):
        staging.stage_queue_sources(config, queue, copy=False)

    queue = write_queue(staging, config, [queue_row(1, "volume.pdf", 1, digest)])
    target = config.pdf_directory / "volume.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"conflict")
    with pytest.raises(FileExistsError, match="conflicting target"):
        staging.stage_queue_sources(config, queue, copy=True)
    assert target.read_bytes() == b"conflict"


def test_source_symlink_escape_and_queue_traversal_are_rejected(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7\noutside\n")
    digest = staging.sha256_file(outside)
    recovered = Path(config.table("restoration")["recovered_v1_root"]) / "downloads"
    (recovered / "escape.pdf").symlink_to(outside)
    queue = write_queue(staging, config, [queue_row(1, "escape.pdf", 1, digest)])
    with pytest.raises(ValueError, match="escapes"):
        staging.stage_queue_sources(config, queue, copy=False)

    queue = write_queue(staging, config, [queue_row(1, "../outside.pdf", 1, digest)])
    with pytest.raises(ValueError, match="Unsafe PDF relative path"):
        staging.stage_queue_sources(config, queue, copy=False)


def test_archive_raw_alias_uses_only_the_explicit_sha_bound_crosswalk(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    recovered = Path(config.table("restoration")["recovered_v1_root"]) / "downloads"
    source = recovered / "1891-1-archive.pdf"
    source.write_bytes(b"%PDF-1.7\narchive alias\n")
    digest = staging.sha256_file(source)
    crosswalk = config.root / staging.CROSSWALK_PATH
    crosswalk.write_text(
        "\t".join(staging.CROSSWALK_FIELDS)
        + "\n"
        + "\t".join(
            (
                "1891",
                "1",
                "archive-raw",
                "127",
                "617",
                "1891-1-archive.pdf",
                "1891-1-archive-raw.pdf",
                "-4",
                digest,
                "760",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    queue = write_queue(
        staging,
        config,
        [queue_row(1, "1891-1-archive-raw.pdf", 123, digest, year=1891, edition=1)],
    )

    payload, _ = staging.stage_queue_sources(config, queue, copy=True)
    target = config.pdf_directory / "1891-1-archive-raw.pdf"
    assert target.read_bytes() == source.read_bytes()
    assert payload["files"][0]["source_relative_path"] == "1891-1-archive.pdf"
    assert payload["files"][0]["physical_page_offset"] == -4
    assert payload["crosswalk"]["used"] == [
        {
            "physical_page_offset": -4,
            "source_relative_path": "1891-1-archive.pdf",
            "target_relative_path": "1891-1-archive-raw.pdf",
        }
    ]

    crosswalk.write_text("\t".join(staging.CROSSWALK_FIELDS) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lacks an explicit crosswalk"):
        staging.stage_queue_sources(config, queue, copy=False)


def test_full_immutable_root_overlap_is_rejected(staging: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    recovered_root = Path(config.table("restoration")["recovered_v1_root"])
    source = recovered_root / "downloads" / "volume.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n")
    digest = staging.sha256_file(source)
    queue = write_queue(staging, config, [queue_row(1, "volume.pdf", 1, digest)])
    unsafe_external = recovered_root / "v2-output"
    unsafe_external.mkdir()
    config.values["storage"]["external_data_root"] = str(unsafe_external)

    with pytest.raises(ValueError, match="overlaps immutable restoration.recovered_v1_root"):
        staging.stage_queue_sources(config, queue, copy=True)
    assert not (unsafe_external / "pdfs" / "volume.pdf").exists()


def test_copy_revalidates_a_preexisting_target_after_preflight(
    staging: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = Path(config.table("restoration")["recovered_v1_root"]) / "downloads" / "volume.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n")
    digest = staging.sha256_file(source)
    queue = write_queue(staging, config, [queue_row(1, "volume.pdf", 1, digest)])
    target = config.pdf_directory / "volume.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    original_build_plan = staging.build_plan

    def change_after_preflight(*args: object, **kwargs: object) -> object:
        result = original_build_plan(*args, **kwargs)
        target.write_bytes(b"changed-after-preflight")
        return result

    monkeypatch.setattr(staging, "build_plan", change_after_preflight)
    with pytest.raises(RuntimeError, match="changed after preflight"):
        staging.stage_queue_sources(config, queue, copy=True)
