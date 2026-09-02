import json
from types import SimpleNamespace

import pytest
from apply_corrections import apply_corrections, parse_corrections
from merge_segmented_extraction import merge_segments, validate_candidate_destination
from plan_alternate_extraction import build_plan, segment_count
from run_alternate_extraction import (
    RequestSpec,
    _candidate_outputs,
    _extract_request,
    _usage_accounting,
    alternate_cache_path,
    build_alternate_config,
    compute_bands,
    resolve_execution_mode,
)
from run_quality_control import _validate_correction_rows

from histdata_pipeline.config import ProjectConfig


def correction_row(**updates: str) -> dict[str, str]:
    result = {
        "correction_id": "fix-1",
        "record_id": "r1",
        "field": "amount",
        "expected_old_value": "12",
        "replacement_value": "1.2",
        "expected_source_hash": "source-1",
        "expected_contract_signature": "contract-1",
        "evidence_page": "sources/book.pdf#page=9",
        "reason": "The decimal point is visible in the page image.",
        "review_date": "2026-08-29",
        "disposition": "corrected",
    }
    result.update(updates)
    return result


def test_correction_overlay_is_keyed_and_auditable() -> None:
    source = [{"record_id": "r1", "amount": "12", "source_sha256": "source-1", "contract_signature": "contract-1"}]
    corrections = parse_corrections([correction_row()])

    repaired, differences = apply_corrections(source, corrections, record_id_field="record_id")

    assert source[0]["amount"] == "12"
    assert repaired[0]["amount"] == "1.2"
    assert differences[0]["before"] == "12"
    assert differences[0]["after"] == "1.2"


def test_stale_correction_is_rejected() -> None:
    source = [{"record_id": "r1", "amount": "13", "source_sha256": "source-1", "contract_signature": "contract-1"}]
    corrections = parse_corrections([correction_row()])

    with pytest.raises(ValueError, match="stale correction"):
        apply_corrections(source, corrections, record_id_field="record_id")


def test_correction_cannot_rewrite_provenance() -> None:
    source = [{"record_id": "r1", "amount": "12", "source_sha256": "source-1", "contract_signature": "contract-1"}]
    corrections = parse_corrections([correction_row(field="source_sha256", expected_old_value="source-1", replacement_value="other")])

    with pytest.raises(ValueError, match="protected field"):
        apply_corrections(source, corrections, record_id_field="record_id")


def test_applied_difference_must_match_exact_before_and_after() -> None:
    correction = correction_row()
    difference = {
        "correction_id": "fix-1",
        "record_id": "r1",
        "field": "amount",
        "before": "13",
        "after": "1.2",
        "source_hash": "source-1",
        "contract_signature": "contract-1",
        "evidence_page": correction["evidence_page"],
        "reason": correction["reason"],
        "review_date": correction["review_date"],
    }

    with pytest.raises(ValueError, match="expected_old_value"):
        _validate_correction_rows([correction], [difference])


def segment(page: str, index: int, anchor: str, amount: str) -> dict[str, object]:
    return {"page_id": page, "segment_index": index, "record_anchor": anchor, "record": {"amount": amount}}


def test_segment_merge_requires_agreeing_overlap() -> None:
    merged, conflicts = merge_segments(
        [segment("p1", 0, "a", "1"), segment("p1", 0, "b", "2"), segment("p1", 1, "b", "2"), segment("p1", 1, "c", "3")]
    )

    assert not conflicts
    assert [record["record_anchor"] for record in merged] == ["a", "b", "c"]
    assert all(record["candidate_only"] for record in merged)


def test_segment_merge_blocks_overlap_disagreement() -> None:
    _, conflicts = merge_segments([segment("p1", 0, "b", "2"), segment("p1", 1, "b", "20")])

    assert conflicts[0]["conflict_type"] == "overlap_disagreement"


def test_segment_merge_blocks_an_incomplete_page() -> None:
    _, conflicts = merge_segments(
        [segment("p1", 0, "a", "1")],
        expected_segment_counts={"p1": 3},
    )

    assert any(conflict["conflict_type"] == "segment_completeness" for conflict in conflicts)


def test_standalone_segment_merge_cannot_target_final_data(tmp_path) -> None:
    (tmp_path / "project.toml").write_text(
        f'[storage]\nexternal_data_root = "{tmp_path / "external"}"\n[extraction]\ncurrent_tsv = "exports/current/flat.tsv"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="data/ or manual"):
        validate_candidate_destination(tmp_path, tmp_path / "data/final.tsv")
    with pytest.raises(ValueError, match="baseline current"):
        validate_candidate_destination(tmp_path, tmp_path / "external/exports/current/candidate.tsv")


def test_alternate_plan_is_bounded_and_never_promotable() -> None:
    assert segment_count(page_height=4000, band_height=1800, overlap=300) == 3
    plan = build_plan(
        project_slug="example",
        page_ids=["p1", "p2"],
        case_ids=["qc-1"],
        configured={"max_pages": 3, "max_requests": 10, "dpi": 400, "band_height": 1800, "band_overlap": 300},
        segmented=True,
        page_height=4000,
        request_ceiling=6,
        anchor_fields=["entity_raw", "period_raw"],
    )

    assert plan["requests"] == 6
    assert plan["automatic_promotion"] is False
    assert plan["cache_namespace"].startswith("alternate/")
    assert plan["overlap_anchor_fields"] == ["entity_raw", "period_raw"]


def test_alternate_plan_rejects_request_overrun() -> None:
    with pytest.raises(ValueError, match="effective ceiling"):
        build_plan(
            project_slug="example",
            page_ids=["p1", "p2"],
            case_ids=[],
            configured={"max_pages": 3, "max_requests": 5, "band_height": 1800, "band_overlap": 300},
            segmented=True,
            page_height=4000,
            request_ceiling=5,
            anchor_fields=["entity_raw", "period_raw"],
        )


def test_alternate_execution_is_dry_run_unless_explicitly_authorized() -> None:
    assert resolve_execution_mode(execute=False, cache_only=False, retry_errors=False, max_requests=None) == "dry-run"
    assert resolve_execution_mode(execute=False, cache_only=True, retry_errors=False, max_requests=None) == "cache-only"
    with pytest.raises(ValueError, match="requires an explicit --max-requests"):
        resolve_execution_mode(execute=True, cache_only=False, retry_errors=False, max_requests=None)
    with pytest.raises(ValueError, match="only with --execute"):
        resolve_execution_mode(execute=False, cache_only=False, retry_errors=True, max_requests=1)


def test_alternate_config_and_cache_are_isolated(tmp_path) -> None:
    original = ProjectConfig(
        root=tmp_path,
        values={
            "project": {"slug": "example"},
            "storage": {"external_data_root": str(tmp_path / "external")},
            "model": {"think_level": "medium", "default_service": "flex"},
            "extraction": {"render_dpi": 220},
        },
    )

    alternate = build_alternate_config(original, dpi=400)
    cache = alternate_cache_path(
        alternate,
        plan_signature="plan-1",
        contract_signature="contract-1",
        page_cache_key="page-1",
        segment_index=2,
        request_hash="image-1",
    )

    assert original.table("model")["think_level"] == "medium"
    assert original.table("extraction")["render_dpi"] == 220
    assert alternate.table("model")["think_level"] == "high"
    assert alternate.table("model")["default_service"] == "standard"
    assert alternate.table("extraction")["render_dpi"] == 400
    assert cache == tmp_path / "external/data-extraction/cache/alternate/plan-1/contract-1/page-1/segment-0002-image-1.json"


def test_segment_geometry_covers_page_with_overlap() -> None:
    assert compute_bands(height=4000, band_height=1800, overlap=300) == [(0, 1800), (1500, 3300), (3000, 4000)]


def test_alternate_candidates_use_configured_record_and_page_status_fields() -> None:
    envelopes = [
        {
            "page_id": "page-1",
            "segment_index": 0,
            "status": "ok",
            "extraction": {
                "page_kind": "included",
                "observations": [{"entity_label": "Alpha", "amount_raw": "12"}],
            },
        }
    ]
    plan = {"segmented": False, "overlap_anchor_fields": ["entity_label"]}

    segments, candidates, conflicts = _candidate_outputs(
        envelopes,
        plan,
        record_list_field="observations",
        page_status_field="page_kind",
        target_page_status="included",
    )

    assert conflicts == []
    assert len(segments) == 1
    assert candidates[0]["record"] == {"entity_label": "Alpha", "amount_raw": "12"}
    assert candidates[0]["candidate_only"] is True


def test_failed_alternate_provider_call_records_unknown_usage(tmp_path) -> None:
    class FailingClient:
        @staticmethod
        def extract(*args, **kwargs):  # noqa: ANN002, ANN003, ARG004
            raise RuntimeError("provider unavailable")

    page = SimpleNamespace(
        page_id="page-1",
        pdf_relative_path="book.pdf",
        page=3,
        source_sha256="source-hash",
        cache_key="page-cache-key",
        values={"source_date": "1901-01-01"},
    )
    request = RequestSpec(
        page=page,
        segment_index=0,
        images=(tmp_path / "page.jpg",),
        image_hashes=("image-hash",),
        request_hash="request-hash",
        cache_path=tmp_path / "cache.json",
    )
    contract = SimpleNamespace(signature="contract-hash", prompt="prompt", schema=object())

    envelope = _extract_request(
        FailingClient(),
        request,
        contract,
        {"plan_signature": "plan-hash"},
        "attempt-1",
    )

    assert envelope["status"] == "error"
    assert envelope["provider_call_started"] is True
    assert envelope["usage_known"] is False
    assert all(value is None for value in envelope["usage"].values())
    persisted = json.loads((tmp_path / "cache.error-attempt-1.json").read_text(encoding="utf-8"))
    assert persisted["usage_known"] is False
    assert all(value is None for value in persisted["usage"].values())


def test_alternate_receipt_marks_incremental_cost_unknown_when_usage_is_incomplete() -> None:
    envelopes = [
        {
            "page_id": "page-1",
            "segment_index": 0,
            "usage": {"input_tokens": 100, "output_tokens": 20, "thoughts_tokens": 30, "total_tokens": 150},
            "usage_known": True,
        },
        {
            "page_id": "page-2",
            "segment_index": 0,
            "usage": {"input_tokens": None, "output_tokens": None, "thoughts_tokens": None, "total_tokens": None},
            "usage_known": False,
        },
    ]

    accounting = _usage_accounting(
        envelopes,
        fresh_keys=frozenset({("page-1", 0), ("page-2", 0)}),
        pricing={
            "as_of": "2026-08-29",
            "input_per_million": 1.0,
            "output_per_million": 2.0,
            "thinking_per_million": 3.0,
        },
    )

    assert accounting["request_token_usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thoughts_tokens": 30,
        "total_tokens": 150,
    }
    assert accounting["request_token_usage_complete"] is False
    assert accounting["unknown_request_usage_requests"] == 1
    assert accounting["known_minimum_incremental_request_cost"] == pytest.approx(0.00023)
    assert accounting["estimated_incremental_request_cost"] is None
