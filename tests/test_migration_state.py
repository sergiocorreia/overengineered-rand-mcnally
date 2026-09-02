import csv
from pathlib import Path

from pypdf import PdfReader

from histdata_pipeline.provenance import sha256_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = Path("/home/sergio/data/rand-mcnally-v2/pdfs/1881-1-hathi.pdf")
EXPECTED_PDF_SHA256 = "3d4a8c7aeae90b8116381811329ea6d3bc29d92c6ed13a273d63db70a5b0c7cd"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def test_legacy_migration_inventory_summary() -> None:
    rows = read_tsv(PROJECT_ROOT / "sources" / "legacy_migration_inventory.tsv")

    assert len(rows) == 129
    assert sum(row["manifest_row_status"] == "configured" for row in rows) == 84
    assert sum(row["manifest_row_status"] == "blank_source_placeholder" for row in rows) == 44
    assert sum(row["selected_source_available"] == "yes" for row in rows) == 30
    assert sum(int(row["output_page_count"]) for row in rows) == 106_948

    invalid = [row for row in rows if row["manifest_row_status"] == "invalid"]
    assert len(invalid) == 1
    assert (invalid[0]["year"], invalid[0]["edition"]) == ("1803", "1")
    assert "outside 1879-1942" in invalid[0]["invalid_row_note"]


def test_copied_smoke_pdf_matches_manifest() -> None:
    manifest = read_tsv(PROJECT_ROOT / "sources" / "source_manifest.tsv")

    assert len(manifest) == 1
    source = manifest[0]
    assert source["source_id"] == "rand_mcnally_1881_1_hathi"
    assert source["acquisition_method"] == "manual"
    assert source["filename"] == PDF_PATH.name
    assert source["expected_sha256"] == EXPECTED_PDF_SHA256
    assert source["min_pages"] == source["max_pages"] == "443"
    assert sha256_file(PDF_PATH) == EXPECTED_PDF_SHA256
    assert len(PdfReader(PDF_PATH).pages) == 443


def test_rerun_ledger_is_exactly_the_authorized_smoke_pair() -> None:
    rows = read_tsv(PROJECT_ROOT / "manual" / "rerun_pages.tsv")

    assert [(row["physical_page"], row["status"]) for row in rows] == [
        ("84", "completed"),
        ("143", "completed"),
    ]
    assert {row["page_id"] for row in rows} == {
        "1881-1-hathi.pdf#page=84",
        "1881-1-hathi.pdf#page=143",
    }
    assert {row["run_id"] for row in rows} == {"20260902T055210.158908Z"}
    assert all(row["contract_signature"] and row["completed_at"] for row in rows)
