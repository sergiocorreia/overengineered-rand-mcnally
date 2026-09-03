"""Offline tests for durable page-review decisions and local serving."""

from __future__ import annotations

import csv
from pathlib import Path

import page_inventory
import page_review
import pytest
import review_pages


def record(source_hash: str, page: int = 1) -> page_inventory.PageRecord:
    return page_inventory.PageRecord(
        manifest_index=page - 1,
        source_order=1,
        source_id="source_1",
        provider="Archive",
        title="Source",
        source_date="1930-01-02",
        filename="source.pdf",
        pdf_relative_path="pdfs/source.pdf",
        source_sha256=source_hash,
        pdf_size_bytes=10,
        pdf_page_count=2,
        page=page,
        page_id=f"pdfs/source.pdf#page={page}",
        embedded_text_sha256="1" * 64,
        ocr_method="embedded",
        ocr_text_sha256="1" * 64,
        ocr_cache_relative_path=f"cache/page-{page}.txt",
        automatic_classification="unreviewed",
        automatic_score=0.0,
        automatic_reasons="fixture",
        source_manual_classification="",
        page_manual_classification="",
        final_type="unreviewed",
        classification_source="unreviewed",
        manual_notes="",
    )


def setup_store(tmp_path: Path) -> tuple[page_review.PageReviewStore, Path, str]:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "source.pdf"
    pdf.write_bytes(b"source bytes")
    digest = page_inventory.sha256_file(pdf)
    pages = tmp_path / "pages.tsv"
    page_inventory.atomic_write_pages(pages, [record(digest), record(digest, 2)])
    overrides = tmp_path / "manual" / "page_overrides.tsv"
    store = page_review.PageReviewStore(
        pages,
        overrides,
        pdf_root,
        tmp_path / "images",
        renderer=lambda _pdf, _page, destination: destination.write_bytes(b"\xff\xd8fixture"),
    )
    return store, overrides, digest


def write_project_config(project: Path, tmp_path: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.toml").write_text(
        f"""
[template]
initialized = true

[project]
slug = "test-project"

[restoration]
legacy_root = "{tmp_path / 'legacy'}"
legacy_root_read_only = true
recovered_v1_root = "{tmp_path / 'recovered-v1'}"
recovered_v1_root_read_only = true

[storage]
external_data_root = "{tmp_path / 'external'}"
pdf_storage = "external"
external_pdf_subdirectory = "pdfs"
page_review_image_subdirectory = "review-images"

[review]
port_pages = 0
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_save_is_atomic_ordered_and_hash_pinned(tmp_path: Path) -> None:
    store, overrides, digest = setup_store(tmp_path)
    store.save("pdfs/source.pdf#page=2", "excluded", "Not a target")
    store.save("pdfs/source.pdf#page=1", "selected", "Visible table")
    assert not overrides.with_suffix(".tsv.part").exists()
    with overrides.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    assert [row["page_id"] for row in rows] == ["pdfs/source.pdf#page=1", "pdfs/source.pdf#page=2"]
    assert {row["expected_source_sha256"] for row in rows} == {digest}
    state = store.public_state()
    assert state["progress"] == {"resolved": 2, "total": 2, "unresolved": 0}
    assert state["pages"][0]["source_date"] == "1930-01-02"


def test_reviewer_rejects_unknown_classification_and_stale_decision(tmp_path: Path) -> None:
    store, overrides, _digest = setup_store(tmp_path)
    with pytest.raises(page_review.ReviewError, match="Invalid classification"):
        store.save("pdfs/source.pdf#page=1", "maybe", "")
    store.save("pdfs/source.pdf#page=1", "selected", "")
    text = overrides.read_text(encoding="utf-8").replace(store.records[0].source_sha256, "0" * 64)
    overrides.write_text(text, encoding="utf-8")
    with pytest.raises(page_review.ReviewError, match="Stale manual override"):
        page_review.PageReviewStore(store.pages_path, overrides, store.pdf_root, store.image_root)


def test_image_verifies_source_hash_and_local_server_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _overrides, _digest = setup_store(tmp_path)
    image = store.image_path("pdfs/source.pdf#page=1")
    assert image.read_bytes().startswith(b"\xff\xd8")
    html = tmp_path / "review.html"
    html.write_text("<html>review</html>", encoding="utf-8")

    class FakeServer:
        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            self.server_address = address

    monkeypatch.setattr(page_review, "ReviewServer", FakeServer)
    server = page_review.create_server(store.pages_path, store.overrides_path, store.pdf_root, store.image_root, html, 0)
    assert server.server_address[0] == "127.0.0.1"

    (store.pdf_root / "source.pdf").write_bytes(b"changed")
    with pytest.raises(page_review.ReviewError, match="changed"):
        store.image_path("pdfs/source.pdf#page=2")


def test_html_includes_keyboard_review_controls() -> None:
    html = Path(page_review.__file__).with_name("review_pages.html").read_text(encoding="utf-8")
    for token in (
        "ArrowLeft",
        "ArrowRight",
        "selected",
        "flagged",
        "excluded",
        "beforeunload",
        "goto",
        "unresolved",
        "source_date",
    ):
        assert token in html


def test_page_reviewer_uses_configured_port_with_cli_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "project.toml").write_text(
        """
[storage]
external_data_root = "/external/project"
pdf_storage = "external"
external_pdf_subdirectory = "pdfs"
page_review_image_subdirectory = "review/images"

[review]
port_pages = 9123
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_pages, "PROJECT_ROOT", tmp_path)
    pdf_root, image_root, port = review_pages._project_defaults()
    assert pdf_root == Path("/external/project/pdfs")
    assert image_root == Path("/external/project/review/images")
    assert port == 9123
    parser = review_pages._parser(pdf_root, image_root, port)
    assert parser.parse_args([]).port == 9123
    assert parser.parse_args(["--port", "9456"]).port == 9456


@pytest.mark.parametrize("option", ["--page-overrides", "--image-root"])
@pytest.mark.parametrize("unsafe_name", ["legacy", "recovered-v1", "outside"])
def test_page_reviewer_checks_write_destinations_before_server_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    unsafe_name: str,
) -> None:
    project = tmp_path / "project"
    write_project_config(project, tmp_path)
    candidate = tmp_path / unsafe_name / ("overrides.tsv" if option == "--page-overrides" else "images")
    monkeypatch.setattr(review_pages, "PROJECT_ROOT", project)
    monkeypatch.setattr(review_pages, "create_server", lambda *_args: pytest.fail("Path validation must precede server creation"))

    with pytest.raises(SystemExit) as error:
        review_pages.main([option, str(candidate), "--no-browser"])

    assert error.value.code == 2
    assert not candidate.exists()
