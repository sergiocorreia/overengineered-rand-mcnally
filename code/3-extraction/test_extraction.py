import csv
import json
import sys
import threading
import types
from argparse import Namespace
from pathlib import Path

import extract_records
import extraction_provider
import pytest
from contract import PIPELINE_SOURCE_NAMES, ExtractionContract, load_schema, make_contract_payload, validate_schema_field_names
from extract_records import _overlay_current_page, _structural_error_envelope, _write_run, enforce_guards
from pipeline import (
    RenderedPage,
    SelectedPage,
    flatten_envelope,
    load_page_cache,
    page_cache_path,
    page_error_cache_path,
    read_selected_pages,
    write_page_cache,
)
from pydantic import BaseModel
from review_store import RecordReviewStore
from run_integrity import verify_run

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import sha256_file, stable_hash


def config(tmp_path: Path) -> ProjectConfig:
    values = {
        "project": {"slug": "demo"},
        "storage": {
            "pdf_storage": "project",
            "external_data_root": str(tmp_path / "external"),
            "local_pdf_directory": "sources/pdfs",
            "cache_subdirectory": "data-extraction/cache",
            "export_subdirectory": "data-extraction/exports",
        },
        "extraction": {"record_list_field": "records", "default_workers": 2},
        "model": {"default_service": "flex"},
        "service": {"standard_max_pages": 2, "standard_request_ceiling": 5, "flex_default_max_requests": 5},
        "pricing": {},
    }
    return ProjectConfig(tmp_path, values)


def page(index: int, *, source_hash: str = "a" * 64, source_date: str = "1900-01-01") -> SelectedPage:
    relative = "volume.pdf"
    values = {
        "manifest_index": str(index),
        "page_id": f"{relative}#page={index}",
        "pdf_relative_path": relative,
        "source_sha256": source_hash,
        "source_date": source_date,
        "page": str(index),
        "final_type": "selected",
        "classification_source": "manual_page",
        "manual_notes": "checked",
        "source_id": "volume-1",
        "provider": "archive",
        "title": "Volume One",
        "ocr_method": "embedded",
        "ocr_text_sha256": "b" * 64,
    }
    return SelectedPage(index, values["page_id"], relative, source_hash, index, "selected", values)


def args(**overrides: object) -> Namespace:
    defaults = {
        "all": False,
        "cache_only": False,
        "retry_errors": False,
        "status": False,
        "dry_run": False,
        "workers": None,
        "max_requests": None,
    }
    return Namespace(**(defaults | overrides))


def extraction_value(entity: str) -> dict[str, object]:
    return {
        "document_status": "target",
        "scan_quality": "clear",
        "records": [
            {
                "entity_raw": entity,
                "entity": entity,
                "period_raw": "1900",
                "period": None,
                "value_raw": "10",
                "value": "10",
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


def envelope(item: SelectedPage, entity: str) -> dict[str, object]:
    return {
        "manifest_index": item.manifest_index,
        "page_id": item.page_id,
        "pdf_relative_path": item.pdf_relative_path,
        "physical_page": item.page,
        "source_sha256": item.source_sha256,
        "render_sha256": str(item.page) * 64,
        "render_path": f"/render/{item.page}.jpg",
        "contract_signature": "c" * 64,
        "status": "ok",
        "error_type": "",
        "error_message": "",
        "extraction": extraction_value(entity),
        "usage": {"input_tokens": 1, "output_tokens": 2, "thoughts_tokens": 3, "total_tokens": 6},
    }


def test_selected_manifest_is_fail_closed_and_checks_stable_identity(tmp_path: Path) -> None:
    manifest = tmp_path / "selected.tsv"
    manifest.write_text(
        "manifest_index\tpage_id\tpdf_relative_path\tsource_sha256\tsource_date\tpage\tfinal_type\tclassification_source\tmanual_notes\n"
        f"0\tvolume.pdf#page=1\tvolume.pdf\t{'a' * 64}\t1900-01-01\t1\tunreviewed\tautomatic\t\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="final_type must be"):
        read_selected_pages(manifest)
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("unreviewed", "selected"), encoding="utf-8")
    assert read_selected_pages(manifest)[0].page_id == "volume.pdf#page=1"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("#page=1", "#page=2"), encoding="utf-8")
    with pytest.raises(ValueError, match="page_id must be"):
        read_selected_pages(manifest)


def test_contract_changes_for_every_material_input() -> None:
    base = make_contract_payload(
        prompt="p",
        schema_source="s",
        schema_json={"type": "object"},
        settings={"model": "m", "service": "flex", "render_dpi": 200},
        dependencies={"yachay": {"commit": "1"}},
    )
    for key, changed in (
        ("prompt", "new"),
        ("schema_source", "new"),
        ("schema_json", {"type": "array"}),
        ("settings", {"model": "m", "service": "standard", "render_dpi": 200}),
        ("dependencies", {"yachay": {"commit": "2"}}),
    ):
        variant = dict(base)
        variant[key] = changed
        assert stable_hash(variant) != stable_hash(base)


def test_contract_hashes_every_extraction_implementation_module() -> None:
    assert set(PIPELINE_SOURCE_NAMES) == {
        "contract.py",
        "pipeline.py",
        "extract_records.py",
        "extraction_policy.py",
        "extraction_provider.py",
        "run_writer.py",
    }
    assert all((Path(__file__).parent / name).is_file() for name in PIPELINE_SOURCE_NAMES)


def test_cache_identity_is_sensitive_to_source_and_render_hash(tmp_path: Path) -> None:
    project = config(tmp_path)
    original = page(1)
    changed_source = page(1, source_hash="b" * 64)
    first = page_cache_path(project, contract_signature="c" * 64, page=original, render_sha256="d" * 64)
    assert first != page_cache_path(project, contract_signature="c" * 64, page=changed_source, render_sha256="d" * 64)
    assert first != page_cache_path(project, contract_signature="c" * 64, page=original, render_sha256="e" * 64)


def test_error_cache_is_immutable_and_success_takes_precedence(tmp_path: Path) -> None:
    project = config(tmp_path)
    item = page(1)
    success_path = page_cache_path(project, contract_signature="c" * 64, page=item, render_sha256="d" * 64)
    base = {
        "contract_signature": "c" * 64,
        "page_id": item.page_id,
        "source_sha256": item.source_sha256,
        "render_sha256": "d" * 64,
    }
    error = {**base, "status": "error"}
    write_page_cache(page_error_cache_path(success_path, "001"), error)
    assert load_page_cache(success_path, contract_signature="c" * 64, page=item, render_sha256="d" * 64) == error
    success = {**base, "status": "ok", "extraction": extraction_value("A")}
    write_page_cache(success_path, success)
    assert load_page_cache(success_path, contract_signature="c" * 64, page=item, render_sha256="d" * 64) == success
    with pytest.raises(FileExistsError):
        write_page_cache(success_path, {**success, "extraction": extraction_value("B")})


def test_cli_guards_standard_and_request_ceiling(tmp_path: Path) -> None:
    project = config(tmp_path)
    keywords = {"contract_signature": "x", "evidence_signature": "e"}
    assert enforce_guards(project, args(max_requests=500), service="flex", selected_count=10, **keywords) == 500
    with pytest.raises(ValueError, match="configured ceiling"):
        enforce_guards(project, args(max_requests=6), service="standard", selected_count=1, **keywords)
    with pytest.raises(ValueError, match="restricted"):
        enforce_guards(project, args(), service="standard", selected_count=3, **keywords)
    with pytest.raises(ValueError, match="requires Flex"):
        enforce_guards(project, args(all=True, dry_run=True, max_requests=1), service="standard", selected_count=1, **keywords)
    gate = tmp_path / "manual/gold/production_gate.json"
    gate.parent.mkdir(parents=True)
    gate.write_text(
        json.dumps(
            {
                "contract_signature": "x",
                "evidence_signature": "e",
                "gold_passed": True,
                "trial_passed": True,
                "cache_reuse_passed": True,
                "cost_reviewed": True,
                "cost_reviewed_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cost review"):
        enforce_guards(project, args(all=True, max_requests=1), service="flex", selected_count=1, **keywords)


def test_manifest_order_and_cache_only_exports_are_byte_deterministic(tmp_path: Path) -> None:
    project = config(tmp_path)
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {"contract": "test"}, "prompt", schema)
    pages = [page(1), page(2)]
    values = [envelope(pages[0], "First"), envelope(pages[1], "Second")]
    first = _write_run(project, contract, pages, values, run_id="run-1", service="flex", cache_only=True, requested=0)
    second = _write_run(project, contract, pages, values, run_id="run-2", service="flex", cache_only=True, requested=0)
    assert sha256_file(first / "nested.jsonl") == sha256_file(second / "nested.jsonl")
    assert sha256_file(first / "flat.tsv") == sha256_file(second / "flat.tsv")
    assert (first / "flat.tsv").read_text(encoding="utf-8").index("First") < (first / "flat.tsv").read_text(encoding="utf-8").index("Second")
    verify_run(first)
    (first / "flat.tsv").write_text((first / "flat.tsv").read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        verify_run(first)


def test_record_reviews_are_atomic_schema_validated_and_stale_checked(tmp_path: Path) -> None:
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    project = config(tmp_path)
    contract = ExtractionContract("c" * 64, {"contract": "test"}, "prompt", schema)
    source = envelope(page(1), "Bank A")
    run = _write_run(project, contract, [page(1)], [source], run_id="r1", service="flex", cache_only=True, requested=0)
    store = RecordReviewStore(run, tmp_path / "manual", schema)
    payload = {
        "page_id": source["page_id"],
        "source_sha256": source["source_sha256"],
        "render_sha256": source["render_sha256"],
        "contract_signature": source["contract_signature"],
        "expected_model_extraction_sha256": stable_hash(source["extraction"]),
        "review_status": "accepted",
        "review_notes": "checked image",
        "extraction": source["extraction"],
    }
    saved = store.save(payload)
    assert saved["review_status"] == "accepted"
    assert not list(store.directory.glob("*.tmp"))
    broken = dict(payload)
    broken["extraction"] = {"records": []}
    with pytest.raises(ValueError):
        store.save(broken)
    saved["render_sha256"] = "z" * 64
    store.decision_path(str(source["page_id"])).write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="Stale saved review"):
        store.load(str(source["page_id"]))


def test_cached_model_result_gets_current_page_review_provenance(tmp_path: Path) -> None:
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    item = page(1)
    cached = envelope(item, "Bank A")
    cached["classification_source"] = "old"
    item.values["classification_source"] = "manual_page"
    item.values["manual_notes"] = "new evidence"
    overlaid = _overlay_current_page(cached, item, contract, tmp_path / "page.jpg", str(item.page) * 64)
    assert overlaid["classification_source"] == "manual_page"
    assert overlaid["manual_notes"] == "new evidence"
    assert overlaid["extraction"] == cached["extraction"]


def test_structural_failure_preserves_complete_reviewed_page_lineage() -> None:
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    item = page(1)
    failure = _structural_error_envelope(item, contract, ValueError("render failed"))

    for field in (
        "source_id",
        "provider",
        "title",
        "source_date",
        "final_type",
        "classification_source",
        "manual_notes",
        "ocr_method",
        "ocr_text_sha256",
        "page_manifest_sha256",
    ):
        assert failure[field]
    assert failure["page_manifest"] == item.values
    assert failure["status"] == "error"
    assert failure["provider_call_started"] is False
    assert failure["usage_known"] is True


def test_dry_run_is_a_no_provider_cache_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = config(tmp_path)
    (tmp_path / "data").mkdir()
    manifest = tmp_path / "data" / "selected_pages.tsv"
    rows = [page(1).values, page(2).values]
    with manifest.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    render_root = tmp_path / "external" / "rendered-pages"
    render_root.mkdir(parents=True)

    def fake_render(_config: ProjectConfig, item: SelectedPage, **_kwargs: object) -> RenderedPage:
        path = render_root / f"page-{item.page}.jpg"
        path.write_bytes(f"render-{item.page}".encode())
        return RenderedPage(path, str(item.page) * 64)

    monkeypatch.setattr(extract_records, "load_project_config", lambda: project)
    monkeypatch.setattr(extract_records, "validate_selection_current", lambda _config: {"signature": "s" * 64})
    monkeypatch.setattr(extract_records, "production_evidence", lambda *_args, **_kwargs: {"signature": "e" * 64})
    monkeypatch.setattr(extract_records, "build_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        extract_records,
        "_verify_sources",
        lambda _config, items: {(item.pdf_relative_path, item.source_sha256): tmp_path / "source.pdf" for item in items},
    )
    monkeypatch.setattr(extract_records, "render_page", fake_render)
    cached_item = page(1)
    cached_path = page_cache_path(
        project,
        contract_signature=contract.signature,
        page=cached_item,
        render_sha256="1" * 64,
    )
    write_page_cache(cached_path, envelope(cached_item, "Cached"))

    result = extract_records.execute(
        extract_records.build_parser().parse_args(["--limit", "2", "--dry-run", "--max-requests", "2"])
    )
    assert result["provider_calls"] == 0
    assert result["pending_requests"] == 1
    assert result["cached_successes"] == 1
    assert result["preflight_signature"]
    assert not (tmp_path / "external" / "data-extraction" / "exports").exists()

    def forbidden_request(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("cache-only/preflight paths must not call the provider")

    monkeypatch.setattr(extract_records, "_extract_one", forbidden_request)
    with pytest.raises(ValueError, match="pricing"):
        extract_records.execute(extract_records.build_parser().parse_args(["--limit", "2", "--max-requests", "2"]))
    cache_only = extract_records.execute(extract_records.build_parser().parse_args(["--limit", "2", "--cache-only"]))
    assert cache_only["model_requests"] == 0


def test_export_root_is_rejected_before_page_preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = config(tmp_path)
    project.values["storage"]["export_subdirectory"] = "../escape"  # type: ignore[index]
    (tmp_path / "data").mkdir()
    row = page(1).values
    with (tmp_path / "data" / "selected_pages.tsv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(row), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    monkeypatch.setattr(extract_records, "load_project_config", lambda: project)
    monkeypatch.setattr(extract_records, "validate_selection_current", lambda _config: {"signature": "s" * 64})
    monkeypatch.setattr(extract_records, "production_evidence", lambda *_args, **_kwargs: {"signature": "e" * 64})
    monkeypatch.setattr(extract_records, "build_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        extract_records,
        "_prepare_pages",
        lambda *_args, **_kwargs: pytest.fail("page preparation must not start"),
    )

    with pytest.raises(ValueError, match="escapes external_data_root"):
        extract_records.execute(
            extract_records.build_parser().parse_args(["--limit", "1", "--dry-run", "--max-requests", "1"])
        )


def test_qc_queue_deduplicates_repeated_cases_per_page(tmp_path: Path) -> None:
    queue = tmp_path / "queue.tsv"
    queue.write_text("case_id\tpage_id\nq1\tvolume.pdf#page=1\nq2\tvolume.pdf#page=1\nq3\tvolume.pdf#page=2\n", encoding="utf-8")
    selected = extract_records.select_pages(
        config(tmp_path),
        [page(1), page(2)],
        Namespace(
            limit=None,
            year=None,
            page_id=None,
            queue_tsv=queue,
            calibration=False,
            trial=False,
            all=False,
        ),
    )
    assert [item.page_id for item in selected] == ["volume.pdf#page=1", "volume.pdf#page=2"]


def test_year_selector_uses_canonical_stage_two_source_date(tmp_path: Path) -> None:
    selected = extract_records.select_pages(
        config(tmp_path),
        [page(1, source_date="1899-12-31"), page(2, source_date="1900-01-02")],
        Namespace(
            limit=None,
            year="1900",
            page_id=None,
            queue_tsv=None,
            calibration=False,
            trial=False,
            all=False,
        ),
    )
    assert [item.page_id for item in selected] == ["volume.pdf#page=2"]


def test_schema_rejects_runner_owned_record_fields() -> None:
    class BadRecord(BaseModel):
        record_id: str

    class BadPage(BaseModel):
        records: list[BadRecord]

    with pytest.raises(ValueError, match="runner-owned fields: record_id"):
        validate_schema_field_names(BadPage, "records")


def test_flatten_rejects_runtime_provenance_collision() -> None:
    item = envelope(page(1), "Bank A")
    item["extraction"]["records"][0]["source_sha256"] = "model-controlled"
    with pytest.raises(ValueError, match="runner-owned fields: source_sha256"):
        flatten_envelope(item)


def test_failed_provider_call_records_unknown_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingOCR:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def extract(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("provider failed after accepting the request")

    fake_yachay = types.ModuleType("yachay")
    fake_yachay.OCR = FailingOCR
    monkeypatch.setitem(sys.modules, "yachay", fake_yachay)
    monkeypatch.setattr(extraction_provider, "require_user_adc", lambda _project_id: None)
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    result = extract_records._extract_one(
        config(tmp_path),
        contract,
        page(1),
        tmp_path / "render.jpg",
        "b" * 64,
        service="flex",
        retry_errors=False,
        attempt_id="attempt",
    )
    assert result["status"] == "error"
    assert result["provider_call_started"] is True
    assert result["usage_known"] is False
    assert all(value is None for value in result["usage"].values())
    run = _write_run(
        config(tmp_path),
        contract,
        [page(1)],
        [result],
        run_id="unknown-usage",
        service="flex",
        cache_only=False,
        requested=1,
        fresh_page_ids=frozenset({page(1).page_id}),
    )
    receipt = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert receipt["request_token_usage_complete"] is False
    assert receipt["unknown_request_usage_pages"] == 1
    assert receipt["estimated_incremental_request_cost"] is None


@pytest.mark.parametrize(
    ("usage", "usage_known"),
    [
        ({"input_tokens": 1, "output_tokens": 2, "thoughts_tokens": 3, "total_tokens": None}, False),
        ({"input_tokens": None, "output_tokens": 2, "thoughts_tokens": 3, "total_tokens": 6}, True),
    ],
)
def test_preflight_cost_is_unavailable_for_incomplete_cached_usage(
    tmp_path: Path,
    usage: dict[str, int | None],
    usage_known: bool,
) -> None:
    project = config(tmp_path)
    project.values["pricing"] = {
        "as_of": "2026-08-29",
        "input_per_million": 1.0,
        "output_per_million": 2.0,
        "thinking_per_million": 3.0,
    }
    item = page(1)
    cached = envelope(item, "Bank A")
    cached["usage"] = usage
    cached["usage_known"] = usage_known
    estimate = extract_records._estimated_incremental_cost(
        project,
        {item.page_id: (tmp_path / "render.jpg", "b" * 64, cached)},
        1,
    )

    assert estimate["usage_basis_complete"] is False
    assert estimate["token_projection_available"] is False
    assert estimate["available"] is False
    assert estimate["projected_incremental_cost"] is None
    assert all(value is None for value in estimate["projected_request_tokens"].values())


def test_full_preflight_gate_and_concurrent_completion_preserve_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = config(tmp_path)
    project.values["pricing"] = {
        "as_of": "2026-08-29",
        "input_per_million": 1.0,
        "output_per_million": 2.0,
        "thinking_per_million": 3.0,
        "review_max_age_hours": 24,
    }
    (tmp_path / "data").mkdir()
    manifest = tmp_path / "data/selected_pages.tsv"
    rows = [page(1).values, page(2).values]
    with manifest.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    schema = load_schema(Path(__file__).parent / "definitions/schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)
    selection = {"ordered_selected_pages": [row["page_id"] for row in rows]}
    selection["signature"] = stable_hash(selection)
    evidence = {"contract_signature": contract.signature, "selection_signature": selection["signature"]}
    evidence["signature"] = stable_hash(evidence)
    render_root = tmp_path / "external/rendered-pages"
    render_root.mkdir(parents=True)

    def fake_render(_config: ProjectConfig, item: SelectedPage, **_kwargs: object) -> RenderedPage:
        path = render_root / f"page-{item.page}.jpg"
        path.write_bytes(f"render-{item.page}".encode())
        return RenderedPage(path, str(item.page) * 64)

    monkeypatch.setattr(extract_records, "load_project_config", lambda: project)
    monkeypatch.setattr(extract_records, "validate_selection_current", lambda _config: selection)
    monkeypatch.setattr(extract_records, "production_evidence", lambda *_args, **_kwargs: evidence)
    monkeypatch.setattr(extract_records, "build_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        extract_records,
        "_verify_sources",
        lambda _config, items: {(item.pdf_relative_path, item.source_sha256): tmp_path / "source.pdf" for item in items},
    )
    monkeypatch.setattr(extract_records, "render_page", fake_render)
    monkeypatch.setattr(
        extract_records,
        "_estimated_incremental_cost",
        lambda _config, _prepared, pending: {
            "available": True,
            "token_projection_available": True,
            "pricing_configured": True,
            "basis_cached_pages": 1,
            "projected_request_tokens": {"input_tokens": pending, "output_tokens": pending, "thoughts_tokens": 0, "total_tokens": 2 * pending},
            "pricing_as_of": "2026-08-29",
            "rates_per_million": {"input_per_million": 1.0, "output_per_million": 2.0, "thinking_per_million": 3.0},
            "projected_incremental_cost": pending * 3 / 1_000_000,
        },
    )

    preview = extract_records.execute(
        extract_records.build_parser().parse_args(["--all", "--flex", "--max-requests", "2", "--dry-run"])
    )
    gate_path = tmp_path / "manual/gold/production_gate.json"
    gate_path.parent.mkdir(parents=True)
    gate = {
        "contract_signature": contract.signature,
        "evidence_signature": evidence["signature"],
        "render_signature": preview["render_signature"],
        "preflight_signature": "stale",
        "approved_max_requests": 2,
        "gold_passed": True,
        "trial_passed": True,
        "cache_reuse_passed": True,
        "cost_reviewed": True,
        "cost_reviewed_at": extract_records.datetime.now(extract_records.UTC).isoformat(),
    }
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    completed: list[int] = []
    second_finished = threading.Event()

    def fake_extract(
        _config: ProjectConfig,
        _contract: ExtractionContract,
        item: SelectedPage,
        render_path: Path,
        render_hash: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if item.page == 1:
            assert second_finished.wait(timeout=2)
        else:
            second_finished.set()
        completed.append(item.page)
        value = envelope(item, f"Entity {item.page}")
        value["render_path"] = str(render_path)
        value["render_sha256"] = render_hash
        value["provider_call_started"] = True
        value["usage_known"] = True
        return value

    monkeypatch.setattr(extract_records, "_extract_one", fake_extract)
    with pytest.raises(ValueError, match="approved preflight"):
        extract_records.execute(
            extract_records.build_parser().parse_args(["--all", "--flex", "--max-requests", "2", "--workers", "2"])
        )
    assert completed == []

    gate["preflight_signature"] = preview["preflight_signature"]
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    result = extract_records.execute(
        extract_records.build_parser().parse_args(["--all", "--flex", "--max-requests", "2", "--workers", "2"])
    )
    assert completed[0] == 2
    run = Path(str(result["run_directory"]))
    exported = [json.loads(line) for line in (run / "nested.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["page_id"] for row in exported] == [rows[0]["page_id"], rows[1]["page_id"]]
    assert result["current_updated"] is True
    verify_run(run)
