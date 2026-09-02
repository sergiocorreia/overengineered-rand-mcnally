from pathlib import Path

import pytest
from apply_record_reviews import apply_reviews
from contract import load_schema

from histdata_pipeline.provenance import stable_hash


def extraction(value: str) -> dict[str, object]:
    return {
        "document_status": "target",
        "scan_quality": "clear",
        "records": [
            {
                "entity_raw": "A",
                "entity": "A",
                "period_raw": "1900",
                "period": None,
                "value_raw": value,
                "value": value,
                "value_status": "observed",
                "correction_raw": None,
                "note": None,
                "supplemental_facts": [],
                "uncertain_fields": [],
            }
        ],
        "page_note": None,
        "unmapped_text": [],
    }


def model_envelope() -> dict[str, object]:
    return {
        "manifest_index": 0,
        "page_id": "pdfs/a.pdf#page=1",
        "pdf_relative_path": "pdfs/a.pdf",
        "physical_page": 1,
        "source_sha256": "a" * 64,
        "render_sha256": "b" * 64,
        "contract_signature": "c" * 64,
        "status": "ok",
        "error_type": "",
        "error_message": "",
        "extraction": extraction("10"),
    }


def decision(status: str, reviewed: dict[str, object]) -> dict[str, object]:
    baseline = model_envelope()
    return {
        "page_id": baseline["page_id"],
        "source_sha256": baseline["source_sha256"],
        "render_sha256": baseline["render_sha256"],
        "contract_signature": baseline["contract_signature"],
        "review_status": status,
        "review_notes": "checked source",
        "reviewed_at": "2026-08-29T00:00:00+00:00",
        "model_extraction_sha256": stable_hash(baseline["extraction"]),
        "reviewed_extraction_sha256": stable_hash(reviewed),
        "extraction": reviewed,
    }


def test_accepted_record_review_reaches_standardization_rows() -> None:
    schema = load_schema(Path(__file__).parents[1] / "3-extraction" / "definitions" / "schema.py")
    reviewed = extraction("12")
    rows, differences, flags = apply_reviews(
        [model_envelope()],
        [decision("accepted", reviewed)],
        schema=schema,
        record_list_field="records",
    )
    assert rows[0]["value"] == "12"
    assert any(row["field"] == "value" and row["operation"] == "field_changed" for row in differences)
    assert flags == []


def test_flagged_record_review_is_blocking_and_does_not_change_rows() -> None:
    schema = load_schema(Path(__file__).parents[1] / "3-extraction" / "definitions" / "schema.py")
    baseline = model_envelope()
    rows, differences, flags = apply_reviews(
        [baseline],
        [decision("flagged", baseline["extraction"])],
        schema=schema,
        record_list_field="records",
    )
    assert rows[0]["value"] == "10"
    assert differences == []
    assert flags[0]["review_status"] == "flagged"


def test_hand_edited_changed_review_still_requires_evidence_note() -> None:
    schema = load_schema(Path(__file__).parents[1] / "3-extraction" / "definitions" / "schema.py")
    changed = decision("accepted", extraction("12"))
    changed["review_notes"] = ""
    with pytest.raises(ValueError, match="requires an evidence note"):
        apply_reviews([model_envelope()], [changed], schema=schema, record_list_field="records")


def test_duplicate_flagged_decisions_are_rejected() -> None:
    schema = load_schema(Path(__file__).parents[1] / "3-extraction" / "definitions" / "schema.py")
    flagged = decision("flagged", extraction("10"))
    with pytest.raises(ValueError, match="duplicate page"):
        apply_reviews([model_envelope()], [flagged, flagged], schema=schema, record_list_field="records")


def test_review_timestamp_must_be_timezone_aware_iso() -> None:
    schema = load_schema(Path(__file__).parents[1] / "3-extraction" / "definitions" / "schema.py")
    reviewed = decision("accepted", extraction("10"))
    reviewed["reviewed_at"] = "2026-08-29T00:00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        apply_reviews([model_envelope()], [reviewed], schema=schema, record_list_field="records")
