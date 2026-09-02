"""Offline tests for all-page inventory, OCR cache, precedence, and gates."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import replace
from pathlib import Path

import page_inventory
import pytest
from pypdf import PdfWriter


def pdf_bytes(pages: int = 2) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


def write_tsv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def source_row(source: page_inventory.InventorySource) -> dict[str, object]:
    return {
        "source_order": source.source_order,
        "source_id": source.source_id,
        "provider": source.provider,
        "provider_id": "archive-1",
        "title": source.title,
        "source_date": source.source_date,
        "item_url": "https://example.test/item/1",
        "download_url": "",
        "acquisition_method": "manual",
        "filename": source.filename,
        "expected_sha256": "",
        "min_pages": 1,
        "max_pages": "",
        "notes": "fixture",
    }


def inventory_row(source: page_inventory.InventorySource) -> dict[str, object]:
    return {
        **source_row(source),
        "status": source.status,
        "pdf_relative_path": source.pdf_relative_path,
        "size_bytes": source.size_bytes,
        "physical_pages": source.physical_pages,
        "actual_sha256": source.actual_sha256,
        "checked_at": "2030-01-01T00:00:00+00:00",
    }


def source_files(tmp_path: Path, pages: int = 2) -> tuple[Path, Path, Path, page_inventory.InventorySource]:
    pdf_root = tmp_path / "external" / "pdfs"
    pdf_root.mkdir(parents=True)
    content = pdf_bytes(pages)
    pdf = pdf_root / "collection" / "source.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    source = page_inventory.InventorySource(
        source_order=1,
        source_id="source_1",
        provider="Archive",
        title="Source one",
        source_date="1930-01-02",
        filename="collection/source.pdf",
        status="valid_existing",
        pdf_relative_path="pdfs/collection/source.pdf",
        size_bytes=len(content),
        physical_pages=pages,
        actual_sha256=digest,
    )
    manifest = tmp_path / "sources.tsv"
    write_tsv(
        manifest,
        page_inventory.SOURCE_MANIFEST_FIELDS,
        [source_row(source)],
    )
    inventory = tmp_path / "inventory.tsv"
    write_tsv(inventory, page_inventory.INVENTORY_FIELDS, [inventory_row(source)])
    return manifest, inventory, pdf_root, source


class Rules:
    def classify_page(self, text: str, context: dict[str, object]) -> dict[str, object]:
        assert context["source_date"] == "1930-01-02"
        return {
            "classification": "selected" if "target" in text else "excluded",
            "score": 10 if "target" in text else -10,
            "reasons": ["fixture"],
        }

    def needs_locro(self, text: str, decision: dict[str, object], context: dict[str, object]) -> bool:
        return "needs OCR" in text


def test_source_reconciliation_refuses_missing_and_identity_drift(tmp_path: Path) -> None:
    manifest, inventory_path, _pdf_root, source = source_files(tmp_path)
    identities = page_inventory.load_source_identities(manifest)
    inventory = page_inventory.load_inventory(inventory_path)
    assert page_inventory.reconcile_sources(identities, inventory, require_all=True) == [source]
    assert identities[0].source_date == source.source_date
    with pytest.raises(ValueError, match="incomplete"):
        page_inventory.reconcile_sources(identities, [], require_all=True)

    write_tsv(manifest, tuple(field for field in page_inventory.SOURCE_MANIFEST_FIELDS if field != "source_date"), [])
    with pytest.raises(ValueError, match="Expected columns"):
        page_inventory.load_source_identities(manifest)

    legacy_inventory = tmp_path / "inventory-without-date.tsv"
    write_tsv(
        legacy_inventory,
        tuple(field for field in page_inventory.INVENTORY_FIELDS if field != "source_date"),
        [],
    )
    with pytest.raises(ValueError, match="Expected columns"):
        page_inventory.load_inventory(legacy_inventory)


def test_source_date_must_be_canonical_and_match_inventory(tmp_path: Path) -> None:
    manifest, inventory_path, _pdf_root, source = source_files(tmp_path)
    write_tsv(
        manifest,
        page_inventory.SOURCE_MANIFEST_FIELDS,
        [source_row(replace(source, source_date="January 1930"))],
    )
    with pytest.raises(ValueError, match="Invalid source_date"):
        page_inventory.load_source_identities(manifest)

    write_tsv(
        manifest,
        page_inventory.SOURCE_MANIFEST_FIELDS,
        [source_row(replace(source, source_date="1930-01-03"))],
    )
    with pytest.raises(ValueError, match="identity drift"):
        page_inventory.reconcile_sources(
            page_inventory.load_source_identities(manifest),
            page_inventory.load_inventory(inventory_path),
            require_all=True,
        )


def test_default_is_unreviewed_and_page_override_beats_source_override(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    cache = tmp_path / "cache"
    records = page_inventory.build_page_records(
        [source],
        pdf_root,
        cache,
        rules=Rules(),
        embedded_extractor=lambda _path: ["target", "not relevant"],
    )
    assert [row.page_id for row in records] == [
        "pdfs/collection/source.pdf#page=1",
        "pdfs/collection/source.pdf#page=2",
    ]
    assert [row.automatic_classification for row in records] == ["selected", "excluded"]
    assert {row.source_date for row in records} == {"1930-01-02"}
    assert {row.final_type for row in records} == {"unreviewed"}

    source_overrides = tmp_path / "source_overrides.tsv"
    page_overrides = tmp_path / "page_overrides.tsv"
    write_tsv(
        source_overrides,
        page_inventory.SOURCE_OVERRIDE_FIELDS,
        [
            {
                "source_id": source.source_id,
                "expected_source_sha256": source.actual_sha256,
                "classification": "excluded",
                "notes": "source decision",
            }
        ],
    )
    write_tsv(
        page_overrides,
        page_inventory.PAGE_OVERRIDE_FIELDS,
        [
            {
                "page_id": records[0].page_id,
                "expected_source_sha256": source.actual_sha256,
                "classification": "selected",
                "notes": "page exception",
            }
        ],
    )
    reviewed = page_inventory.build_page_records(
        [source],
        pdf_root,
        cache,
        source_overrides_path=source_overrides,
        page_overrides_path=page_overrides,
        rules=Rules(),
        embedded_extractor=lambda _path: ["target", "not relevant"],
    )
    assert reviewed[0].final_type == "selected" and reviewed[0].classification_source == "manual_page"
    assert reviewed[1].final_type == "excluded" and reviewed[1].classification_source == "manual_source"


def test_stale_override_fails_closed(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    overrides = tmp_path / "page_overrides.tsv"
    write_tsv(
        overrides,
        page_inventory.PAGE_OVERRIDE_FIELDS,
        [
            {
                "page_id": "pdfs/collection/source.pdf#page=1",
                "expected_source_sha256": "0" * 64,
                "classification": "selected",
                "notes": "stale",
            }
        ],
    )
    with pytest.raises(ValueError, match="Stale page override"):
        page_inventory.build_page_records(
            [source],
            pdf_root,
            tmp_path / "cache",
            page_overrides_path=overrides,
            embedded_extractor=lambda _path: ["one", "two"],
        )


def test_current_manual_snapshot_reapplies_without_rerunning_ocr(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    records = page_inventory.build_page_records(
        [source],
        pdf_root,
        tmp_path / "cache",
        embedded_extractor=lambda _path: ["one", "two"],
    )
    overrides = tmp_path / "page_overrides.tsv"
    write_tsv(
        overrides,
        page_inventory.PAGE_OVERRIDE_FIELDS,
        [
            {
                "page_id": record.page_id,
                "expected_source_sha256": source.actual_sha256,
                "classification": "selected" if record.page == 1 else "excluded",
                "notes": "reviewed",
            }
            for record in records
        ],
    )
    reviewed = page_inventory.apply_manual_overrides(
        records,
        source_overrides_path=None,
        page_overrides_path=overrides,
    )
    assert [record.final_type for record in reviewed] == ["selected", "excluded"]
    page_inventory.validate_sources_current(reviewed, [source], pdf_root)

    write_tsv(overrides, page_inventory.PAGE_OVERRIDE_FIELDS, [])
    reset = page_inventory.apply_manual_overrides(
        reviewed,
        source_overrides_path=None,
        page_overrides_path=overrides,
    )
    assert {record.final_type for record in reset} == {"unreviewed"}
    (pdf_root / "collection" / "source.pdf").write_bytes(b"changed source")
    with pytest.raises(ValueError, match="stale"):
        page_inventory.validate_sources_current(reviewed, [source], pdf_root)


def test_targeted_locro_cache_is_external_and_reused(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    cache = tmp_path / "external-cache"
    calls: list[list[int]] = []

    def locro(_path: Path, pages: list[int]) -> dict[int, str]:
        calls.append(pages)
        return {page: "target recovered" for page in pages}

    first = page_inventory.build_page_records(
        [source],
        pdf_root,
        cache,
        ocr_mode="targeted",
        rules=Rules(),
        embedded_extractor=lambda _path: ["needs OCR", "clear non-target"],
        locro_runner=locro,
    )
    assert calls == [[1]]
    assert first[0].ocr_method == "embedded+locro"
    assert first[0].automatic_classification == "selected"

    page_inventory.build_page_records(
        [source],
        pdf_root,
        cache,
        ocr_mode="targeted",
        rules=Rules(),
        embedded_extractor=lambda _path: ["needs OCR", "clear non-target"],
        locro_runner=lambda *_args: pytest.fail("Locro cache should be reused"),
    )


def test_full_locro_is_batched_and_each_completed_batch_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path, pages=5)
    monkeypatch.setattr(page_inventory, "LOCRO_BATCH_SIZE", 2)
    calls: list[list[int]] = []

    def locro(_path: Path, pages: list[int]) -> dict[int, str]:
        calls.append(pages)
        return {page: f"page {page}" for page in pages}

    page_inventory.build_page_records(
        [source],
        pdf_root,
        tmp_path / "cache",
        ocr_mode="full",
        embedded_extractor=lambda _path: [""] * 5,
        locro_runner=locro,
    )
    assert calls == [[1, 2], [3, 4], [5]]


def test_extraction_gate_requires_complete_resolution_and_positive_negative_gold(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    records = page_inventory.build_page_records(
        [source],
        pdf_root,
        tmp_path / "cache",
        rules=Rules(),
        embedded_extractor=lambda _path: ["target", "not relevant"],
    )
    gold = tmp_path / "gold.tsv"
    write_tsv(
        gold,
        page_inventory.GOLD_FIELDS,
        [
            {"page_id": records[0].page_id, "expected_classification": "selected", "risk_labels": "positive", "notes": ""},
            {"page_id": records[1].page_id, "expected_classification": "excluded", "risk_labels": "negative", "notes": ""},
        ],
    )
    with pytest.raises(ValueError, match="unresolved"):
        page_inventory.validate_extraction_ready(records, expected_source_ids=[source.source_id], gold_path=gold)

    resolved = [
        replace(record, final_type="selected" if record.page == 1 else "excluded")
        for record in records
    ]
    assert page_inventory.validate_extraction_ready(
        resolved,
        expected_source_ids=[source.source_id],
        gold_path=gold,
    ) == [resolved[0]]
    selected_path = tmp_path / "selected_pages.tsv"
    page_inventory.atomic_write_pages(selected_path, [resolved[0]])
    assert page_inventory.load_page_records(selected_path)[0].source_date == source.source_date

    bad_gold = tmp_path / "bad_gold.tsv"
    write_tsv(
        bad_gold,
        page_inventory.GOLD_FIELDS,
        [
            {"page_id": records[0].page_id, "expected_classification": "excluded", "risk_labels": "negative", "notes": ""},
            {"page_id": records[1].page_id, "expected_classification": "selected", "risk_labels": "positive", "notes": ""},
        ],
    )
    with pytest.raises(ValueError, match="Gold page expectations failed"):
        page_inventory.validate_extraction_ready(resolved, expected_source_ids=[source.source_id], gold_path=bad_gold)

    write_tsv(
        bad_gold,
        page_inventory.GOLD_FIELDS,
        [
            {"page_id": records[0].page_id, "expected_classification": "selected", "risk_labels": "", "notes": ""},
            {"page_id": records[1].page_id, "expected_classification": "excluded", "risk_labels": "negative", "notes": ""},
        ],
    )
    with pytest.raises(ValueError, match="risk_labels"):
        page_inventory.validate_extraction_ready(resolved, expected_source_ids=[source.source_id], gold_path=bad_gold)


def test_page_manifest_roundtrip_is_byte_deterministic(tmp_path: Path) -> None:
    _manifest, _inventory, pdf_root, source = source_files(tmp_path)
    records = page_inventory.build_page_records(
        [source],
        pdf_root,
        tmp_path / "cache",
        embedded_extractor=lambda _path: ["one", "two"],
    )
    path = tmp_path / "pages.tsv"
    page_inventory.atomic_write_pages(path, records)
    first = path.read_bytes()
    loaded = page_inventory.load_page_records(path)
    page_inventory.atomic_write_pages(path, loaded)
    assert path.read_bytes() == first

    legacy = tmp_path / "legacy-pages.tsv"
    write_tsv(legacy, tuple(field for field in page_inventory.PAGE_FIELDS if field != "source_date"), [])
    with pytest.raises(ValueError, match="Expected page columns"):
        page_inventory.load_page_records(legacy)


def test_bounded_candidate_updates_merge_without_reordering() -> None:
    digest = "a" * 64
    first = page_inventory.PageRecord(
        manifest_index=0,
        source_order=1,
        source_id="one",
        provider="Archive",
        title="One",
        source_date="1930-01-02",
        filename="one.pdf",
        pdf_relative_path="pdfs/one.pdf",
        source_sha256=digest,
        pdf_size_bytes=100,
        pdf_page_count=1,
        page=1,
        page_id="pdfs/one.pdf#page=1",
        embedded_text_sha256=digest,
        ocr_method="embedded",
        ocr_text_sha256=digest,
        ocr_cache_relative_path="embedded/one.txt",
        automatic_classification="unreviewed",
        automatic_score=0,
        automatic_reasons="old",
        source_manual_classification="",
        page_manual_classification="",
        final_type="unreviewed",
        classification_source="unreviewed",
        manual_notes="",
    )
    second = replace(
        first,
        manifest_index=1,
        source_order=2,
        source_id="two",
        filename="two.pdf",
        pdf_relative_path="pdfs/two.pdf",
        page_id="pdfs/two.pdf#page=1",
    )
    update = replace(second, manifest_index=0, ocr_method="locro", automatic_reasons="rescored")
    merged = page_inventory.merge_page_updates([first, second], [update])
    assert [record.page_id for record in merged] == [first.page_id, second.page_id]
    assert [record.manifest_index for record in merged] == [0, 1]
    assert merged[1].ocr_method == "locro"
