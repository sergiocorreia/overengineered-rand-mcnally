"""Offline tests for the legacy-to-ranking evidence adapter."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prepare_rerun_evidence as evidence  # noqa: E402
import rerun_priority  # noqa: E402


def _write(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8")), delimiter="\t"))


def _blank(fields: tuple[str, ...], **values: object) -> dict[str, object]:
    return {field: values.get(field, "") for field in fields}


def _policy() -> evidence.Policy:
    return evidence.Policy(
        capital_secondary_factor=Decimal("2"),
        capital_strong_factor=Decimal("10"),
        gap_minimum_support_ratio=Decimal("0.8"),
        gap_strong_weight=Decimal("5"),
        gap_corroborated_weight=Decimal("3"),
        density_neighbor_minimum=Decimal("10"),
        density_ratio=Decimal("0.25"),
        identity_cluster_minimum=2,
        identity_cluster_share=Decimal("0.1"),
        accounting_minimum_year=1934,
        accounting_error_share=Decimal("0.01"),
        accounting_cluster_minimum=2,
    )


def _fixture(tmp_path: Path) -> dict[str, Path]:
    project = tmp_path / "project"
    external = tmp_path / "external" / "legacy-inputs"
    recovered = tmp_path / "recovery" / "downloads"
    legacy = tmp_path / "legacy" / "sources"
    for path in (project / "manual", project / "sources", project / "output" / "recovery-audit", external, recovered, legacy):
        path.mkdir(parents=True, exist_ok=True)

    (recovered / "1935-1-fraser.pdf").write_bytes(b"recovered-pdf")
    (legacy / "1879-2-archive.pdf").write_bytes(b"reviewed-raw-pdf")
    (legacy / "1887-1-hathi.pdf").write_bytes(b"legacy-fallback-pdf")

    migration_fields = (
        "manifest_order",
        "year",
        "edition",
        "configured_source",
        "range_start",
        "range_end",
        "configured_range_kind",
        "manifest_row_status",
    )
    migration = project / "sources" / "legacy_migration_inventory.tsv"
    _write(
        migration,
        migration_fields,
        [
            dict(zip(migration_fields, (1, 1879, 2, "archive-raw", 79, 79, "raw_scan_index", "configured"), strict=True)),
            dict(zip(migration_fields, (2, 1887, 1, "hathi", 86, 86, "physical_pdf_page", "configured"), strict=True)),
            dict(zip(migration_fields, (3, 1935, 1, "fraser", 1, 3, "physical_pdf_page", "configured"), strict=True)),
        ],
    )

    crosswalk = project / "manual" / "raw_scan_pdf_crosswalk.tsv"
    _write(
        crosswalk,
        evidence.RAW_CROSSWALK_FIELDS,
        [
            _blank(
                evidence.RAW_CROSSWALK_FIELDS,
                year=1879,
                edition=2,
                configured_source="archive-raw",
                legacy_page_start=79,
                legacy_page_end=79,
                source_relative_path="1879-2-archive.pdf",
                v2_pdf_relative_path="1879-2-archive-raw.pdf",
                physical_page_offset=0,
                source_sha256=hashlib.sha256(b"reviewed-raw-pdf").hexdigest(),
                physical_page_count=100,
                evidence="fixture review",
                note="exact offset",
            )
        ],
    )
    crosswalk_sha256 = hashlib.sha256(crosswalk.read_bytes()).hexdigest()
    mapping = project / "output" / "2-inventory" / "v1-recovery-page-mapping.tsv"
    common = {"purpose": "recovery_mapping_not_extraction_input", "crosswalk_sha256": ""}
    _write(
        mapping,
        evidence.MAPPING_FIELDS,
        [
            {
                **common,
                "manifest_index": 1,
                "edition_id": "1879-2",
                "configured_source": "archive-raw",
                "configured_range_kind": "raw_scan_index",
                "source_origin": "reviewed_raw_crosswalk",
                "v1_copy_status": "available",
                "source_sha256": hashlib.sha256(b"reviewed-raw-pdf").hexdigest(),
                "crosswalk_sha256": crosswalk_sha256,
                "pdf_relative_path": "1879-2-archive-raw.pdf",
                "physical_page": 79,
                "page_id": "1879-2-archive-raw.pdf#page=79",
                "cache_group": "1879-2",
                "cache_page": 79,
            },
            {
                **common,
                "manifest_index": 2,
                "edition_id": "1887-1",
                "configured_source": "hathi",
                "configured_range_kind": "physical_pdf_page",
                "source_origin": "legacy_fallback",
                "v1_copy_status": "missing",
                "source_sha256": hashlib.sha256(b"legacy-fallback-pdf").hexdigest(),
                "pdf_relative_path": "1887-1-hathi.pdf",
                "physical_page": 86,
                "page_id": "1887-1-hathi.pdf#page=86",
                "cache_group": "1887-1",
                "cache_page": 86,
            },
            *[
                {
                    **common,
                    "manifest_index": 3,
                    "edition_id": "1935-1",
                    "configured_source": "fraser",
                    "configured_range_kind": "physical_pdf_page",
                    "source_origin": "v1_recovery",
                    "v1_copy_status": "available",
                    "source_sha256": "",
                    "pdf_relative_path": "1935-1-fraser.pdf",
                    "physical_page": page,
                    "page_id": f"1935-1-fraser.pdf#page={page}",
                    "cache_group": "1935-1",
                    "cache_page": page,
                }
                for page in range(1, 4)
            ],
        ],
    )

    legacy_pages = [
        _blank(
            evidence.LEGACY_PAGE_FIELDS,
            year=year,
            edition=edition,
            pdf_part=part,
            pdf_page=page,
            source=source,
            filename=f"{year}-{edition}-{source}",
            is_advertisment=0,
            input_tokens=10,
            thoughts_tokens=1,
            output_tokens=20,
            total_tokens=31,
            wave=2 * year + edition,
        )
        for year, edition, part, page, source in (
            (1879, 2, 0, 79, "archive-raw"),
            (1887, 1, 0, 86, "hathi"),
            (1935, 1, 0, 1, "fraser"),
            (1935, 1, 0, 2, "fraser"),
            (1935, 1, 0, 3, "fraser"),
        )
    ]
    legacy_pages[1]["is_advertisment"] = 1
    _write(external / "legacy_pages.tsv", evidence.LEGACY_PAGE_FIELDS, legacy_pages)

    raw_quality = []
    clean_quality = []
    for page, count in ((1, 20), (2, 2), (3, 20)):
        raw_quality.append(
            _blank(
                evidence.RAW_QUALITY_FIELDS,
                year=1935,
                edition=1,
                pdf_part=0,
                pdf_page=page,
                raw_rows=count,
                raw_missing_state=0,
                raw_missing_city=0,
                raw_missing_name=2 if page == 2 else 0,
                raw_invalid_transit=0,
                wave=3871,
            )
        )
        clean_quality.append(
            _blank(
                evidence.CLEAN_QUALITY_FIELDS,
                year=1935,
                edition=1,
                pdf_part=0,
                pdf_page=page,
                clean_rows=10 if page == 1 else count,
                invalid_city=0,
                invalid_name=2 if page == 2 else 0,
                invalid_transit=0,
                invalid_established=0,
                established_after_issue=0,
                established_before_1776=0,
                statement_after_issue=0,
                accounting_mismatch_1934=2 if page == 2 else 0,
                max_resource_error_share="0.5" if page == 2 else "",
                wave=3871,
            )
        )
    _write(external / "raw_page_quality.tsv", evidence.RAW_QUALITY_FIELDS, raw_quality)
    _write(external / "clean_page_quality.tsv", evidence.CLEAN_QUALITY_FIELDS, clean_quality)

    _write(
        external / "capital_signals.tsv",
        evidence.CAPITAL_FIELDS,
        [
            _blank(
                evidence.CAPITAL_FIELDS,
                state="AL",
                city="Alpha",
                name="First Bank",
                year=1935,
                edition=1,
                pdf_part=0,
                pdf_page=1,
                index=1,
                capital=1000,
                wave=3871,
                previous_capital=100,
                following_capital=100,
                capital_missing_middle=0,
                capital_factor=10,
                capital_factor_10=1,
                capital_factor_2=0,
            ),
            _blank(
                evidence.CAPITAL_FIELDS,
                state="AL",
                city="Gamma",
                name="Third Bank",
                year=1935,
                edition=1,
                pdf_part=0,
                pdf_page=3,
                index=3,
                capital=300,
                wave=3871,
                previous_capital=100,
                following_capital=100,
                capital_missing_middle=0,
                capital_factor=3,
                capital_factor_10=0,
                capital_factor_2=1,
            ),
        ],
    )

    gap_base = dict(
        state="AL",
        city="Beta",
        name="Second Bank",
        previous_part=0,
        previous_page=1,
        wave=3871,
        previous_charter_unique=1,
        previous_transit_unique=1,
        following_part=0,
        following_page=3,
        following_charter_unique=1,
        following_transit_unique=1,
        previous_state_rows=20,
        following_state_rows=20,
        support_ratio=1,
        previous_anchor_part=0,
        previous_anchor_page=2,
        following_anchor_part=0,
        following_anchor_page=2,
    )
    _write(
        external / "gap_signals.tsv",
        evidence.GAP_FIELDS,
        [
            _blank(
                evidence.GAP_FIELDS,
                **gap_base,
                gap_index=1,
                charter_agrees=1,
                transit_agrees=1,
                localized_part_a=0,
                localized_page_a=2,
                localized_weight_a=1,
            ),
            _blank(
                evidence.GAP_FIELDS,
                **{**gap_base, "city": "Delta", "name": "Fourth Bank"},
                gap_index=2,
                charter_agrees=1,
                transit_agrees=0,
                localized_part_a=0,
                localized_page_a=2,
                localized_weight_a="0.5",
                localized_part_b=0,
                localized_page_b=3,
                localized_weight_b="0.5",
            ),
        ],
    )

    manual = project / "manual" / "legacy_page_evidence.tsv"
    _write(
        manual,
        evidence.MANUAL_EVIDENCE_FIELDS,
        [
            _blank(
                evidence.MANUAL_EVIDENCE_FIELDS,
                year=1935,
                edition=1,
                pdf_part=0,
                pdf_page=2,
                evidence_family="documented_failure",
                strength="hard",
                evidence_source="legacy.do",
                note="known empty page",
            ),
            _blank(
                evidence.MANUAL_EVIDENCE_FIELDS,
                year=1879,
                edition=2,
                pdf_part=0,
                pdf_page=79,
                evidence_family="cache_error",
                strength="hard",
                evidence_source="cache/1879-2/79.error",
                note="provider error marker",
            ),
        ],
    )
    exclusions = project / "manual" / "legacy_scope_exclusions.tsv"
    _write(
        exclusions,
        evidence.SCOPE_EXCLUSION_FIELDS,
        [
            _blank(
                evidence.SCOPE_EXCLUSION_FIELDS,
                year=1935,
                edition=1,
                pdf_part=0,
                page_start=3,
                page_end=3,
                reason="advertisement",
                evidence_source="legacy.do",
            )
        ],
    )
    return {
        "project": project,
        "external": external,
        "recovered": recovered,
        "legacy": legacy,
        "mapping": mapping,
        "migration": migration,
        "manual": manual,
        "exclusions": exclusions,
        "crosswalk": crosswalk,
    }


def _build(paths: dict[str, Path]) -> evidence.PreparedArtifacts:
    return evidence.prepare_evidence(
        project_root=paths["project"],
        legacy_inputs_root=paths["external"],
        page_mapping_path=paths["mapping"],
        migration_inventory_path=paths["migration"],
        manual_evidence_path=paths["manual"],
        scope_exclusions_path=paths["exclusions"],
        raw_crosswalk_path=paths["crosswalk"],
        recovered_downloads_root=paths["recovered"],
        legacy_sources_root=paths["legacy"],
        policy=_policy(),
    )


def test_prepares_canonical_pages_and_tiered_signals(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    artifacts = _build(paths)
    assert evidence.PAGE_INPUT_FIELDS == rerun_priority.PAGE_INPUT_FIELDS
    assert evidence.SIGNAL_INPUT_FIELDS == rerun_priority.SIGNAL_INPUT_FIELDS

    pages = {row["page_id"]: row for row in _rows(artifacts.pages)}
    assert len(pages) == 5
    assert pages["1935-1-fraser.pdf#page=2"]["source_sha256"] == hashlib.sha256(b"recovered-pdf").hexdigest()
    assert pages["1935-1-fraser.pdf#page=3"]["eligible"] == "0"
    assert pages["1887-1-hathi.pdf#page=86"]["eligible"] == "0"
    assert pages["1887-1-hathi.pdf#page=86"]["source_sha256"] == hashlib.sha256(b"legacy-fallback-pdf").hexdigest()
    raw_page = pages["1879-2-archive-raw.pdf#page=79"]
    assert raw_page["eligible"] == "1"
    assert raw_page["source_sha256"] == hashlib.sha256(b"reviewed-raw-pdf").hexdigest()

    signals = _rows(artifacts.signals)
    by_rule = {row["rule_id"]: row for row in signals}
    assert by_rule["documented_structural_failure"]["tier"] == "1"
    assert by_rule["documented_cache_error"]["tier"] == "1"
    assert by_rule["capital_factor_10"]["tier"] == "2"
    assert by_rule["capital_factor_2_10"]["tier"] == "4"
    assert by_rule["panel_gap_same_page"]["directness"] == "same_page_bracket"
    assert by_rule["panel_gap_same_page"]["magnitude"] == "5"
    adjacent = [row for row in signals if row["rule_id"] == "panel_gap_adjacent"]
    assert {row["magnitude"] for row in adjacent} == {"1.5"}
    assert {"page_density_collapse", "raw_identity_field_loss", "clean_identity_failure_cluster", "accounting_mismatch_cluster"} <= set(by_rule)
    assert by_rule["scope_exclusion"]["page_id"] == "1935-1-fraser.pdf#page=3"
    assert artifacts.receipt["counts"] == {
        "canonical_pages": 5,
        "eligible_pages": 3,
        "ineligible_pages": 2,
        "legacy_advertisement_pages": 1,
        "legacy_fallback_pages": 1,
        "reviewed_raw_crosswalk_pages": 1,
        "scope_exclusion_rows": 1,
        "signal_rows": len(signals),
        "unlocalized_gap_rows_not_emitted": 0,
    }


def test_build_is_deterministic_and_writes_only_beneath_v2(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    first = _build(paths)
    second = _build(paths)
    assert first == second

    evidence.write_artifacts(
        first,
        project_root=paths["project"],
        pages_output=Path("data/pages.tsv"),
        signals_output=Path("data/signals.tsv"),
        receipt_output=Path("output/evidence/receipt.json"),
    )
    assert (paths["project"] / "data" / "pages.tsv").read_bytes() == first.pages
    receipt = json.loads((paths["project"] / "output" / "evidence" / "receipt.json").read_text())
    assert receipt["outputs"]["signals_sha256"] == hashlib.sha256(first.signals).hexdigest()
    with pytest.raises(ValueError, match="V2 data"):
        evidence.write_artifacts(
            first,
            project_root=paths["project"],
            pages_output=paths["legacy"] / "forbidden.tsv",
            signals_output=Path("data/signals.tsv"),
            receipt_output=Path("output/receipt.json"),
        )


def test_rejects_duplicates_and_unsafe_recovery_mapping(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    legacy_pages = (paths["external"] / "legacy_pages.tsv").read_text(encoding="utf-8").splitlines()
    (paths["external"] / "legacy_pages.tsv").write_text("\n".join([*legacy_pages, legacy_pages[-1]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate legacy page"):
        _build(paths)

    paths = _fixture(tmp_path / "unsafe")
    rows = list(csv.DictReader(paths["mapping"].open(), delimiter="\t"))
    rows[0]["pdf_relative_path"] = "../escape.pdf"
    rows[0]["page_id"] = "../escape.pdf#page=1"
    _write(paths["mapping"], evidence.MAPPING_FIELDS, rows)
    with pytest.raises(ValueError, match="Unsafe PDF relative path"):
        _build(paths)
