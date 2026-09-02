"""Offline FRASER adapter tests covering observed catalog variants."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import acquisition
import fraser
import fraser_adapter
import pytest


def row(item_id: str) -> dict[str, object]:
    return {
        "id": item_id,
        "name": f"Issue {item_id}",
        "url": f"/title/example/{item_id}",
        "decade": "1930",
        "sortOrder": item_id,
    }


def html(payload: object, declaration: str = "var") -> str:
    return f"<script>{declaration} browseByData = {json.dumps(payload)}; var browse = 'all';</script>"


@pytest.mark.parametrize(
    "payload",
    [
        {"1930s": {"items": [row("1"), row("2")]}},
        {"items": [row("1"), row("2")]},
        [row("1"), row("2")],
        {"1930s": [row("1")], "1940s": [row("2")]},
    ],
)
def test_catalog_variants_preserve_order(payload: object) -> None:
    assert [item.item_id for item in fraser.parse_catalog(html(payload, "const"))] == ["1", "2"]


def test_catalog_rejects_missing_and_duplicate_items() -> None:
    with pytest.raises(ValueError, match="not found"):
        fraser.parse_catalog("<html></html>")
    with pytest.raises(ValueError, match="Duplicate"):
        fraser.parse_catalog(html([row("1"), row("1")]))


def mods(item_id: str = "1", pdf_url: str = "https://fraser.stlouisfed.org/docs/example/source.pdf") -> str:
    return f"""<modsCollection xmlns="http://www.loc.gov/mods/v3"><mods>
      <titleInfo><title>Weekly Bulletin</title><subTitle>Supplement</subTitle></titleInfo>
      <originInfo><sortDate>1930-01-01</sortDate><dateIssued>January 1930</dateIssued></originInfo>
      <recordInfo><recordIdentifier>{item_id}</recordIdentifier></recordInfo>
      <physicalDescription><extent>12 pages</extent></physicalDescription>
      <location><url>https://fraser.stlouisfed.org/title/example/{item_id}</url><pdfUrl>{pdf_url}</pdfUrl></location>
    </mods></modsCollection>"""


def test_mods_to_generic_source_record_and_append_only_cache(tmp_path: Path) -> None:
    item = fraser.parse_catalog(html([row("1")]))[0]
    metadata = fraser.parse_item_metadata(mods(), item)
    source = fraser.to_source_record(metadata, source_order=4, source_id_prefix="bulletin")
    assert source.source_id == "bulletin_1"
    assert source.provider == "FRASER"
    assert source.filename == "source.pdf"
    assert source.download_url.endswith("source.pdf")
    assert source.source_date == "1930-01-01"

    path = tmp_path / "items.jsonl"
    fraser.append_metadata(path, metadata)
    replacement = fraser.parse_item_metadata(mods(pdf_url="https://fraser.stlouisfed.org/docs/example/replacement.pdf"), item)
    fraser.append_metadata(path, replacement)
    assert fraser.load_metadata(path)["1"].filename == "replacement.pdf"


def test_mods_rejects_identity_mismatch_and_unsafe_pdf_name() -> None:
    item = fraser.parse_catalog(html([row("1")]))[0]
    with pytest.raises(ValueError, match="ID mismatch"):
        fraser.parse_item_metadata(mods(item_id="2"), item)
    with pytest.raises(ValueError, match="Invalid FRASER PDF filename"):
        fraser.parse_item_metadata(mods(pdf_url="https://example.test/not-pdf.txt"), item)


def test_merge_preserves_existing_order_and_refuses_filename_collisions() -> None:
    item = fraser.parse_catalog(html([row("1")]))[0]
    generated = fraser.to_source_record(fraser.parse_item_metadata(mods(), item), source_order=1)
    manual = acquisition.SourceRecord(
        source_order=1,
        source_id="manual_1",
        provider="Archive",
        provider_id="box",
        title="Manual",
        source_date="",
        item_url="",
        download_url="",
        acquisition_method="manual",
        filename="manual.pdf",
        expected_sha256="",
        min_pages=1,
        max_pages=None,
        notes="",
    )
    merged = fraser_adapter.merge_records([manual], [generated])
    assert [(value.source_order, value.source_id) for value in merged] == [(1, "manual_1"), (2, "fraser_1")]
    with pytest.raises(ValueError, match="filename collision"):
        fraser_adapter.merge_records([manual], [replace(generated, filename="manual.pdf")])

    pinned = replace(generated, source_order=2, expected_sha256="1" * 64, min_pages=10, notes="reviewed")
    refreshed = fraser_adapter.merge_records([manual, pinned], [replace(generated, title="Updated title")])
    assert refreshed[1].expected_sha256 == "1" * 64
    assert refreshed[1].min_pages == 10
    assert refreshed[1].notes == "reviewed"
    assert refreshed[1].source_date == "1930-01-01"


def test_fraser_rejects_noncanonical_sort_date() -> None:
    item = fraser.parse_catalog(html([row("1")]))[0]
    metadata = replace(fraser.parse_item_metadata(mods(), item), sort_date="January 1930")
    with pytest.raises(ValueError, match="Invalid source_date"):
        fraser.to_source_record(metadata, source_order=1)
    with pytest.raises(ValueError, match="Invalid source_date"):
        fraser_adapter.metadata_in_date_window(metadata, None, None)


def test_fraser_date_window_uses_catalog_decade_then_exact_mods_date() -> None:
    item = fraser.parse_catalog(html([row("1")]))[0]
    metadata = fraser.parse_item_metadata(mods(), item)
    assert fraser_adapter.catalog_in_date_window(item, date(1928, 1, 1), date(1932, 12, 31))
    assert not fraser_adapter.catalog_in_date_window(item, date(1940, 1, 1), None)
    assert fraser_adapter.metadata_in_date_window(metadata, date(1930, 1, 1), date(1930, 1, 1))
    assert not fraser_adapter.metadata_in_date_window(metadata, date(1930, 1, 2), None)
