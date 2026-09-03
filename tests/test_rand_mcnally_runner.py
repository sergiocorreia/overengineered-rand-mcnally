"""Offline safety and compatibility checks for the bounded restoration runner."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yachay
from pydantic import BaseModel

from histdata_pipeline.config import ProjectConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "code" / "1-extract-data.py"


class CachePage(BaseModel):
    value: str


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    code_directory = str(RUNNER_PATH.parent)
    sys.path.insert(0, code_directory)
    try:
        spec = importlib.util.spec_from_file_location("rand_mcnally_targeted_runner", RUNNER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(code_directory)


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        root=tmp_path / "v2",
        values={
            "project": {"slug": "rand-mcnally-v2"},
            "restoration": {
                "legacy_root": str(tmp_path / "legacy"),
                "legacy_root_read_only": True,
                "recovered_v1_root": str(tmp_path / "recovered-v1"),
                "recovered_v1_root_read_only": True,
                "smoke_year": 1881,
                "smoke_edition": 1,
                "smoke_pages": [84, 143],
                "provisional_page_denominator": 106_948,
                "provisional_rerun_fraction": 0.05,
                "provisional_rerun_ceiling": 5_347,
            },
            "review_prioritization": {
                "calibration_pages": 150,
                "calibration_documented": 50,
                "calibration_candidates": 50,
                "calibration_controls": 50,
                "minimum_candidate_reviews": 20,
                "minimum_observed_precision": 0.70,
                "minimum_wilson_lower_95": 0.50,
                "trial_max_pages": 100,
                "trial_ramp_pages": 10,
                "trial_ramp_workers": 10,
                "trial_workers": 50,
            },
            "storage": {
                "external_data_root": str(tmp_path / "external"),
                "pdf_storage": "external",
                "external_pdf_subdirectory": "pdfs",
                "render_subdirectory": "rendered-pages",
                "cache_subdirectory": "data-extraction/cache",
                "export_subdirectory": "data-extraction/exports",
            },
        },
    )


def make_source(runner: ModuleType, tmp_path: Path, *, unit: int = 1) -> Any:
    return runner.SourceConfig(
        year=1881,
        edition=1,
        source="hathi",
        start=84,
        end=282,
        variant="1879",
        unit=unit,
        rotation=90,
        path=tmp_path / "1881-1-hathi.pdf",
        is_single_pdf=True,
        source_id="rand_mcnally_1881_1_hathi",
        expected_sha256="source-hash",
    )


def make_document(runner: ModuleType, tmp_path: Path) -> Any:
    return runner.Document(
        path=tmp_path / "1881-1-hathi.pdf",
        relative_path="1881-1-hathi.pdf",
        part=0,
        page_count=443,
        source_sha256="source-hash",
    )


def make_result(value: str = "cached") -> yachay.OCRResult:
    return yachay.OCRResult(
        data=CachePage(value=value),
        input_tokens=11,
        output_tokens=7,
        thoughts_tokens=3,
        model="gemini-3.7-flash",
        flex=True,
        temperature=None,
        max_output_tokens=64_000,
        think_level="medium",
        media_resolution="ultra_high",
    )


def make_plan(runner: ModuleType, tmp_path: Path, page: int, result: yachay.OCRResult | None = None) -> Any:
    render_path = tmp_path / f"page-{page}.webp"
    return runner.PagePlan(
        page=page,
        page_id=f"1881-1-hathi.pdf#page={page}",
        render_path=render_path,
        render_sha256=f"render-{page}",
        contract_signature=f"contract-{page}",
        contract_payload={
            "page_id": f"1881-1-hathi.pdf#page={page}",
            "source_sha256": "source-hash",
            "render_sha256": f"render-{page}",
        },
        cache_path=tmp_path / "cache" / f"page-{page}.json",
        cached_result=result,
    )


def write_signed_queue(runner: ModuleType, config: ProjectConfig, rows: list[dict[str, str]]) -> Path:
    queue_path = config.root / "output" / "rerun-ranking" / "selected_pages.tsv"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    input_path = config.root / "data" / "page_universe.tsv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("page_id\nsource.pdf#page=1\n", encoding="utf-8")
    fieldnames = [*runner.QUEUE_REQUIRED_FIELDS, "already_counted"]
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    queue_path.write_text(buffer.getvalue(), encoding="utf-8")
    unsigned = {
        "schema_version": runner.QUEUE_RECEIPT_SCHEMA,
        "selected_queue_path": queue_path.relative_to(config.root).as_posix(),
        "selected_queue_sha256": runner.sha256_file(queue_path),
        "selected_queue_bytes": queue_path.stat().st_size,
        "selected_queue_rows": len(rows),
        "denominator": 106_948,
        "fraction": 0.05,
        "hard_ceiling": 5_347,
        "computed_cap": 5_347,
        "calibration_policy": {
            "sample_pages": 150,
            "documented_pages": 50,
            "candidate_pages": 50,
            "control_pages": 50,
            "minimum_candidate_reviews": 20,
            "minimum_observed_precision": 0.70,
            "minimum_wilson_lower": 0.50,
            "wilson_z": 1.96,
            "trial_max_pages": 100,
        },
        "calibration_gate_passed": True,
        "calibration_results": {"candidate": {"gate_passed": True}},
        "input_sha256s": {
            input_path.relative_to(config.root).as_posix(): runner.sha256_file(input_path),
        },
    }
    (queue_path.parent / runner.QUEUE_RECEIPT_NAME).write_text(
        json.dumps({**unsigned, "receipt_signature": runner.stable_hash(unsigned)}),
        encoding="utf-8",
    )
    return queue_path


def rewrite_receipt(runner: ModuleType, queue_path: Path, mutate: Any) -> None:
    receipt_path = queue_path.parent / runner.QUEUE_RECEIPT_NAME
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutate(payload)
    unsigned = {key: value for key, value in payload.items() if key != "receipt_signature"}
    receipt_path.write_text(
        json.dumps({**unsigned, "receipt_signature": runner.stable_hash(unsigned)}),
        encoding="utf-8",
    )


def test_write_paths_allow_only_v2_and_external_roots(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert runner.checked_write_path(config, config.root / "manual" / "ledger.tsv") == (config.root / "manual" / "ledger.tsv").resolve()
    assert runner.checked_write_path(config, config.external_root / "cache" / "page.json") == (config.external_root / "cache" / "page.json").resolve()
    with pytest.raises(ValueError, match="immutable legacy"):
        runner.checked_write_path(config, tmp_path / "legacy" / "output.tsv")
    with pytest.raises(ValueError, match="immutable recovered V1"):
        runner.checked_write_path(config, tmp_path / "recovered-v1" / "output.tsv")
    with pytest.raises(ValueError, match="outside the V2"):
        runner.checked_write_path(config, tmp_path / "unrelated" / "output.tsv")


def test_write_paths_reject_external_root_misconfigured_inside_recovered_v1(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.values["storage"]["external_data_root"] = str(tmp_path / "recovered-v1" / "generated")

    with pytest.raises(ValueError, match="immutable.*recovered_v1_root"):
        runner.checked_write_path(config, config.external_root / "cache" / "page.json")


def test_export_root_is_rejected_before_targeted_run_preparation(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    config.values["storage"]["export_subdirectory"] = "../escape"  # type: ignore[index]
    monkeypatch.setattr(runner, "load_project_config", lambda *_args: config)
    monkeypatch.setattr(
        runner,
        "load_source_config",
        lambda *_args, **_kwargs: pytest.fail("source preparation must not start"),
    )

    with pytest.raises(ValueError, match="escapes external_data_root"):
        runner.run_targeted(year=1881, edition=1, pages=[84], part=None, max_requests=1, dry_run=True)


def test_page_selection_is_exactly_the_authorized_pair(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)

    assert runner.validate_selected_pages(config, source, document, [143, 84]) == [84, 143]
    with pytest.raises(ValueError, match="Duplicate"):
        runner.validate_selected_pages(config, source, document, [84, 84])
    with pytest.raises(ValueError, match="permits only"):
        runner.validate_selected_pages(config, source, document, [84])
    with pytest.raises(ValueError, match="configured extraction range"):
        runner.validate_selected_pages(config, source, document, [1, 143])


def test_output_affecting_source_metadata_is_in_the_contract(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.values["extraction"] = {"pipeline_version": "test"}
    source = make_source(runner, tmp_path, unit=1_000)
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )

    payload = runner.definition_payload(
        config,
        source,
        CachePage,
        "prompt",
        "schema source",
        settings,
        {"version": "test", "revision": {"commit": "abc", "dirty": False}},
    )
    assert payload["source_configuration"]["unit_multiplier"] == 1_000
    assert payload["source_configuration"]["schema_regime"] == "1879"


def test_adc_preflight_accepts_only_user_adc_with_matching_quota_project(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adc = tmp_path / "application_default_credentials.json"
    secret_marker = "never-print-this-refresh-token"
    adc.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "quota_project_id": "rand-mcnally-489320",
                "refresh_token": secret_marker,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    runner.require_user_adc("rand-mcnally-489320", adc)

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "legacy.json"))
    with pytest.raises(RuntimeError, match="Unset it") as credential_error:
        runner.require_user_adc("rand-mcnally-489320", adc)
    assert secret_marker not in str(credential_error.value)

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    with pytest.raises(RuntimeError, match="Unset it"):
        runner.require_user_adc("rand-mcnally-489320", adc)

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    adc.write_text(json.dumps({"type": "service_account", "quota_project_id": "rand-mcnally-489320"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not service-account"):
        runner.require_user_adc("rand-mcnally-489320", adc)
    adc.write_text(json.dumps({"type": "authorized_user", "quota_project_id": "wrong-project"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="set-quota-project rand-mcnally-489320"):
        runner.require_user_adc("rand-mcnally-489320", adc)


def test_yachay_constructor_uses_adc_without_legacy_credential_or_cache_kwargs(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    def fake_ocr(**kwargs: Any) -> object:
        received.update(kwargs)
        return object()

    monkeypatch.setattr(runner.yachay, "OCR", fake_ocr)
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )
    runner.make_extractor(settings)

    assert received == {
        "project_id": "rand-mcnally-489320",
        "location": "global",
        "model": "gemini-3.7-flash",
        "temperature": None,
        "max_output_tokens": 64_000,
        "think_level": "medium",
        "media_resolution": "ultra_high",
        "use_flex": True,
        "retry_errors": False,
        "raise_errors": True,
        "call_delay": 0.0,
        "rate_limit_retries": 1,
        "server_retries": 1,
        "transient_retries": 0,
    }
    assert "credentials_file" not in received
    assert "cache_dir" not in received
    assert "cache_file" not in received


def test_signed_cache_round_trip_and_identity_mismatch(runner: ModuleType, tmp_path: Path) -> None:
    plan = make_plan(runner, tmp_path, 84)
    fresh = make_result()
    runner.cache_result(plan, fresh)
    loaded = runner.load_cached_result(plan.cache_path, plan.contract_payload, plan.contract_signature, CachePage)

    assert loaded.cached is True
    assert loaded.to_dict() == fresh.to_dict()
    with pytest.raises(RuntimeError, match="Cache identity mismatch"):
        runner.load_cached_result(plan.cache_path, plan.contract_payload, "different-contract", CachePage)

    changed = make_result("conflict")
    with pytest.raises(RuntimeError, match="conflicting immutable cache"):
        runner.cache_result(plan, changed)

    tampered = json.loads(plan.cache_path.read_text(encoding="utf-8"))
    tampered["result"]["data"]["value"] = "edited"
    plan.cache_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cache content hash mismatch"):
        runner.load_cached_result(plan.cache_path, plan.contract_payload, plan.contract_signature, CachePage)


def test_cached_errors_are_signed_and_replayed_without_retry(runner: ModuleType, tmp_path: Path) -> None:
    plan = make_plan(runner, tmp_path, 84)
    runner.cache_error(plan, "bounded-run", RuntimeError("provider failed"))

    cached = runner.load_cached_error(plan.cache_path, plan.contract_payload, plan.contract_signature)
    assert cached == ("RuntimeError", "provider failed")

    error_path = next(plan.cache_path.parent.glob("*.error-*.json"))
    tampered = json.loads(error_path.read_text(encoding="utf-8"))
    tampered["error_message"] = "edited"
    error_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cache content hash mismatch"):
        runner.load_cached_error(plan.cache_path, plan.contract_payload, plan.contract_signature)


def test_rendering_is_single_page_and_enables_jpx_fallback(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)
    received: dict[str, Any] = {}

    def fake_process_pdf(pdf_path: Path, **kwargs: Any) -> list[Path]:
        received["pdf_path"] = pdf_path
        received.update(kwargs)
        output = Path(kwargs["output_dir"]) / f"page-{kwargs['first_page']}.webp"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * (runner.MIN_EXTRACTED_IMAGE_BYTES + 1))
        return [output]

    monkeypatch.setattr(runner.yachay, "process_pdf", fake_process_pdf)
    rendered = runner.render_page(
        config,
        source,
        document,
        143,
        {"version": "test", "revision": {"commit": "abc", "dirty": False}},
    )

    assert rendered.name == "page-143.webp"
    assert received["pdf_path"] == document.path
    assert received["first_page"] == received["last_page"] == 143
    assert received["detect_watermarks"] is False
    assert received["allow_jpx_fallback"] is True
    assert received["rotation"] == 90


def test_jpx_fallback_is_limited_to_visually_reviewed_volumes(runner: ModuleType) -> None:
    assert {(1881, 1), (1888, 2), (1916, 1)} == runner.JPX_FALLBACK_ALLOWLIST


def test_request_ceiling_fails_before_adc_or_provider_construction(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )
    plans = [make_plan(runner, tmp_path, 84), make_plan(runner, tmp_path, 143)]

    monkeypatch.setattr(runner, "load_project_config", lambda _path: config)
    monkeypatch.setattr(runner, "load_source_config", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(runner, "resolve_document", lambda *_args: document)
    monkeypatch.setattr(runner, "validate_selected_pages", lambda *_args: [84, 143])
    monkeypatch.setattr(runner, "load_definition", lambda *_args: (CachePage, "prompt", "schema"))
    monkeypatch.setattr(runner, "model_settings", lambda *_args: settings)
    monkeypatch.setattr(runner, "prepare_page_plans", lambda *_args: ({"definition": {}}, plans))
    monkeypatch.setattr(runner, "require_user_adc", lambda *_args: pytest.fail("ADC preflight must not run"))
    monkeypatch.setattr(runner, "make_extractor", lambda *_args: pytest.fail("provider must not be constructed"))

    with pytest.raises(RuntimeError, match="2 uncached pages > --max-requests 1"):
        runner.run_targeted(year=1881, edition=1, pages=[84, 143], part=None, max_requests=1, dry_run=False)


def test_client_preflight_fails_before_paid_page_reservation(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)
    settings = runner.ModelSettings("project", "global", "model", "medium", 64_000, "ultra_high", "flex")
    monkeypatch.setattr(runner, "load_project_config", lambda _path: config)
    monkeypatch.setattr(runner, "load_source_config", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(runner, "resolve_document", lambda *_args: document)
    monkeypatch.setattr(runner, "validate_selected_pages", lambda *_args: [84])
    monkeypatch.setattr(runner, "load_definition", lambda *_args: (CachePage, "prompt", "schema"))
    monkeypatch.setattr(runner, "model_settings", lambda *_args: settings)
    monkeypatch.setattr(runner, "prepare_page_plans", lambda *_args: ({"definition": {}}, [make_plan(runner, tmp_path, 84)]))
    monkeypatch.setattr(runner, "print_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "require_user_adc", lambda *_args: None)
    monkeypatch.setattr(runner, "reserve_paid_pages", lambda *_args: pytest.fail("Reservation must not be written"))

    def fail_setup(*_args: Any) -> None:
        raise RuntimeError("local setup failed")

    monkeypatch.setattr(runner, "make_extractor", fail_setup)

    with pytest.raises(RuntimeError, match="local setup failed"):
        runner.run_targeted(year=1881, edition=1, pages=[84], part=None, max_requests=1, dry_run=False)


def test_dry_run_and_cache_replay_never_construct_a_provider(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )

    monkeypatch.setattr(runner, "load_project_config", lambda _path: config)
    monkeypatch.setattr(runner, "load_source_config", lambda *_args: source)
    monkeypatch.setattr(runner, "resolve_document", lambda *_args: document)
    monkeypatch.setattr(runner, "validate_selected_pages", lambda *_args: [84, 143])
    monkeypatch.setattr(runner, "load_definition", lambda *_args: (CachePage, "prompt", "schema"))
    monkeypatch.setattr(runner, "model_settings", lambda *_args: settings)
    monkeypatch.setattr(runner, "print_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "require_user_adc", lambda *_args: pytest.fail("ADC preflight must not run"))
    monkeypatch.setattr(runner, "make_extractor", lambda *_args: pytest.fail("provider must not be constructed"))

    misses = [make_plan(runner, tmp_path, 84), make_plan(runner, tmp_path, 143)]
    monkeypatch.setattr(runner, "prepare_page_plans", lambda *_args: ({"definition": {}}, misses))
    assert runner.run_targeted(year=1881, edition=1, pages=[84, 143], part=None, max_requests=2, dry_run=True) is None

    hits = [make_plan(runner, tmp_path, 84, make_result("84")), make_plan(runner, tmp_path, 143, make_result("143"))]
    monkeypatch.setattr(runner, "prepare_page_plans", lambda *_args: ({"definition": {}}, hits))

    def fake_write_outputs(*args: Any, **_kwargs: Any) -> Path:
        assert args[-1] == 0
        return tmp_path / "replay"

    monkeypatch.setattr(runner, "write_run_outputs", fake_write_outputs)
    assert runner.run_targeted(year=1881, edition=1, pages=[84, 143], part=None, max_requests=0, dry_run=False) == (tmp_path / "replay")


def test_signed_queue_is_validated_before_limit_and_rejects_stale_evidence(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.root.mkdir(parents=True)
    rows = [
        {
            "selection_rank": str(rank),
            "page_id": f"volume.pdf#page={page}",
            "pdf_relative_path": "volume.pdf",
            "physical_page": str(page),
            "source_id": "rand_mcnally_1900_1_hathi",
            "source_sha256": "a" * 64,
            "year": "1900",
            "edition": "1",
            "pdf_part": "0",
            "page_evidence_sha256": "b" * 64,
            "already_counted": "0",
        }
        for rank, page in enumerate((10, 11), start=1)
    ]
    queue_path = write_signed_queue(runner, config, rows)

    evidence, selected = runner.load_signed_queue(config, queue_path, limit=1)
    assert evidence.queue_rows == 2
    assert evidence.computed_cap == 5_347
    assert [row.page_id for row in selected] == ["volume.pdf#page=10"]

    queue_path.write_text(queue_path.read_text(encoding="utf-8").replace("a" * 64, "c" * 64, 1), encoding="utf-8")
    with pytest.raises(ValueError, match="queue SHA-256"):
        runner.load_signed_queue(config, queue_path, limit=1)

    queue_path = write_signed_queue(runner, config, rows)
    (config.root / "data" / "page_universe.tsv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Ranking input changed"):
        runner.load_signed_queue(config, queue_path, limit=None)


def test_signed_queue_requires_exact_passing_configured_trial_policy(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.root.mkdir(parents=True)
    rows = [
        {
            "selection_rank": str(rank),
            "page_id": f"volume.pdf#page={page}",
            "pdf_relative_path": "volume.pdf",
            "physical_page": str(page),
            "source_id": "rand_mcnally_1900_1_hathi",
            "source_sha256": "a" * 64,
            "year": "1900",
            "edition": "1",
            "pdf_part": "0",
            "page_evidence_sha256": "b" * 64,
            "already_counted": "0",
        }
        for rank, page in enumerate((10, 11), start=1)
    ]

    queue_path = write_signed_queue(runner, config, rows)
    rewrite_receipt(runner, queue_path, lambda receipt: receipt.update(schema_version="wrong/v1"))
    with pytest.raises(ValueError, match="must use schema"):
        runner.load_signed_queue(config, queue_path, limit=None)

    queue_path = write_signed_queue(runner, config, rows)
    rewrite_receipt(runner, queue_path, lambda receipt: receipt.update(calibration_gate_passed=False))
    with pytest.raises(ValueError, match="calibration gate did not pass"):
        runner.load_signed_queue(config, queue_path, limit=None)

    queue_path = write_signed_queue(runner, config, rows)
    rewrite_receipt(
        runner,
        queue_path,
        lambda receipt: receipt["calibration_policy"].update(minimum_candidate_reviews=1),
    )
    with pytest.raises(ValueError, match="minimum_candidate_reviews differs"):
        runner.load_signed_queue(config, queue_path, limit=None)

    config.values["review_prioritization"]["trial_max_pages"] = 1
    queue_path = write_signed_queue(runner, config, rows)
    rewrite_receipt(runner, queue_path, lambda receipt: receipt["calibration_policy"].update(trial_max_pages=1))
    with pytest.raises(ValueError, match="exceeding configured trial_max_pages 1"):
        runner.load_signed_queue(config, queue_path, limit=1)


def test_failed_ramp_blocks_live_remainder_but_not_preview(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    failed_ramp = [
        replace(make_plan(runner, tmp_path, page), cached_error_type="OCRServerError")
        for page in range(1, 11)
    ]
    remainder = [make_plan(runner, tmp_path, 11)]

    runner.validate_queue_execution_policy(
        config,
        [*failed_ramp, *remainder],
        50,
        require_successful_ramp=False,
    )
    with pytest.raises(RuntimeError, match="10 ramp pages require successful result-cache hits"):
        runner.validate_queue_execution_policy(
            config,
            [*failed_ramp, *remainder],
            50,
            require_successful_ramp=True,
        )

    successful_ramp = [make_plan(runner, tmp_path, page, make_result(str(page))) for page in range(1, 11)]
    runner.validate_queue_execution_policy(
        config,
        [*successful_ramp, *remainder],
        50,
        require_successful_ramp=True,
    )


def test_queue_worker_ceilings_follow_ramp_and_trial_policy(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ramp = [make_plan(runner, tmp_path, page) for page in range(1, 11)]
    with pytest.raises(ValueError, match="ramp ceiling 10"):
        runner.validate_queue_execution_policy(config, ramp, 11, require_successful_ramp=False)

    trial = [*ramp, make_plan(runner, tmp_path, 11)]
    with pytest.raises(ValueError, match="trial ceiling 50"):
        runner.validate_queue_execution_policy(config, trial, 51, require_successful_ramp=False)


def test_queue_document_uses_exact_path_hash_and_identity(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    pdf_path = config.external_root / "pdfs" / "1900-1-hathi.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf = runner.pymupdf.open()
    pdf.new_page()
    pdf.save(pdf_path)
    pdf.close()
    digest = runner.sha256_file(pdf_path)
    source = runner.SourceConfig(
        year=1900,
        edition=1,
        source="hathi",
        start=1,
        end=1,
        variant="1879",
        unit=1,
        rotation=0,
        path=pdf_path,
        is_single_pdf=True,
        source_id="rand_mcnally_1900_1_hathi",
        expected_sha256=digest,
    )
    row = runner.QueuePage(
        selection_rank=1,
        year=1900,
        edition=1,
        pdf_part=0,
        page_id="1900-1-hathi.pdf#page=1",
        pdf_relative_path="1900-1-hathi.pdf",
        physical_page=1,
        source_id=source.source_id,
        source_sha256=digest,
        page_evidence_sha256="b" * 64,
    )

    document = runner.resolve_queue_document(config, source, row)
    assert document.path == pdf_path.resolve()
    assert document.source_sha256 == digest
    with pytest.raises(ValueError, match="source_id mismatch"):
        runner.resolve_queue_document(config, source, replace(row, source_id="wrong"))
    with pytest.raises(ValueError, match="stale"):
        runner.resolve_queue_document(config, source, replace(row, source_sha256="c" * 64))


def test_archive_raw_queue_uses_physical_pdf_bounds_not_legacy_index_range(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    pdf_path = config.external_root / "pdfs" / "1891-1-archive-raw.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf = runner.pymupdf.open()
    for _ in range(130):
        pdf.new_page()
    pdf.save(pdf_path)
    pdf.close()
    digest = runner.sha256_file(pdf_path)
    source = runner.SourceConfig(
        year=1891,
        edition=1,
        source="archive-raw",
        start=127,
        end=617,
        variant="1888",
        unit=1,
        rotation=90,
        path=pdf_path,
        is_single_pdf=True,
        source_id="rand_mcnally_1891_1_archive_raw",
        expected_sha256=digest,
    )
    row = runner.QueuePage(
        selection_rank=1,
        year=1891,
        edition=1,
        pdf_part=0,
        page_id="1891-1-archive-raw.pdf#page=123",
        pdf_relative_path="1891-1-archive-raw.pdf",
        physical_page=123,
        source_id=source.source_id,
        source_sha256=digest,
        page_evidence_sha256="b" * 64,
    )

    assert runner.resolve_queue_document(config, source, row).page_count == 130
    ordinary = replace(source, source="hathi", source_id="ordinary")
    with pytest.raises(ValueError, match="configured extraction range"):
        runner.resolve_queue_document(config, ordinary, replace(row, source_id="ordinary"))


def test_selection_rank_and_evidence_do_not_change_the_extraction_cache_contract(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    source = make_source(runner, tmp_path)
    document = make_document(runner, tmp_path)
    render_path = tmp_path / "page-84.webp"
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )
    base_row = runner.QueuePage(
        selection_rank=1,
        year=1881,
        edition=1,
        pdf_part=0,
        page_id="1881-1-hathi.pdf#page=84",
        pdf_relative_path="1881-1-hathi.pdf",
        physical_page=84,
        source_id=source.source_id,
        source_sha256="a" * 64,
        page_evidence_sha256="b" * 64,
    )
    monkeypatch.setattr(runner, "yachay_provenance", lambda _config: {"version": "test", "revision": {"commit": "abc"}})
    monkeypatch.setattr(runner, "load_source_config", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(runner, "load_definition", lambda *_args: (CachePage, "prompt", "schema source"))
    monkeypatch.setattr(runner, "resolve_queue_document", lambda *_args: document)
    monkeypatch.setattr(runner, "render_page", lambda *_args: render_path)
    original_sha256_file = runner.sha256_file
    monkeypatch.setattr(runner, "sha256_file", lambda path: "c" * 64 if Path(path) == render_path else original_sha256_file(path))

    first = runner.prepare_queue_plans(config, [base_row], settings)[0]
    reranked = runner.prepare_queue_plans(
        config,
        [replace(base_row, selection_rank=99, page_evidence_sha256="d" * 64)],
        settings,
    )[0]
    assert first.contract_signature == reranked.contract_signature
    assert first.cache_path == reranked.cache_path
    assert first.selection_rank != reranked.selection_rank
    assert first.page_evidence_sha256 != reranked.page_evidence_sha256


def test_queue_source_allows_an_absent_manifest_row_but_direct_mode_requires_it(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    pdf = config.pdf_directory / "1887-1-hathi.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"queue-staged-placeholder")

    with pytest.raises(ValueError, match="Expected one source-manifest row"):
        runner.load_source_config(config, 1887, 1)
    queue_source = runner.load_source_config(config, 1887, 1, require_manifest=False)
    assert queue_source.source_id == "rand_mcnally_1887_1_hathi"
    assert queue_source.expected_sha256 == ""

    manifest = tmp_path / "source_manifest.tsv"
    manifest.write_text(
        f"filename\tsource_id\texpected_sha256\tmin_pages\tmax_pages\n{pdf.name}\tstable-manifest-id\t{'a' * 64}\t1\t999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "SOURCE_MANIFEST_FILE", manifest)
    manifested = runner.load_source_config(config, 1887, 1, require_manifest=False)
    assert manifested.source_id == "stable-manifest-id"
    assert manifested.expected_sha256 == "a" * 64


def test_all_prepared_page_universe_source_ids_match_runner_defaults(runner: ModuleType) -> None:
    assert runner.default_source_id(1891, 1, "archive-raw") == "rand_mcnally_1891_1_archive_raw"
    configured_sources: dict[tuple[int, int], str] = {}
    with runner.PAGE_RANGES_FILE.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            raw_year = row["year"].strip()
            source_name = row["source"].strip()
            if raw_year.isdigit() and source_name and source_name != "missing":
                configured_sources[(int(raw_year), int(row["edition"]))] = source_name

    page_universe = PROJECT_ROOT / "data" / "rerun_priority_pages.tsv"
    if not page_universe.is_file():
        pytest.skip("generated page universe is absent on a clean clone")
    observed: dict[tuple[int, int], set[str]] = {}
    with page_universe.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            key = (int(row["year"]), int(row["edition"]))
            observed.setdefault(key, set()).add(row["source_id"])

    assert observed
    assert set(observed).issubset(configured_sources)
    for (year, edition), source_ids in observed.items():
        assert source_ids == {runner.default_source_id(year, edition, configured_sources[(year, edition)])}


def test_paid_page_reservation_is_atomic_and_cap_is_unique_page_based(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.values["restoration"]["provisional_page_denominator"] = 40
    config.root.mkdir(parents=True)
    plan = make_plan(runner, tmp_path, 84)

    def reserve(run_id: str) -> Path | Exception | None:
        try:
            return runner.reserve_paid_pages(config, run_id, [plan], None)
        except Exception as error:  # the losing concurrent reservation is the expected result
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("run-a", "run-b")))
    paths = [result for result in results if isinstance(result, Path)]
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(paths) == len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "already exists" in str(errors[0])
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    runner.verify_record_hash(payload, paths[0])
    assert payload["computed_cap"] == 2
    assert payload["new_unique_page_ids"] == [plan.page_id]

    second_plan = make_plan(runner, tmp_path, 143)
    third_plan = replace(make_plan(runner, tmp_path, 144), page_id="1881-1-hathi.pdf#page=144")
    with pytest.raises(RuntimeError, match="5% page ceiling"):
        runner.validate_paid_budget(config, [second_plan, third_plan], None)


def test_concurrent_execution_uses_thread_local_clients_and_returns_queue_order(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = runner.ModelSettings(
        project_id="rand-mcnally-489320",
        location="global",
        model="gemini-3.7-flash",
        think_level="medium",
        max_output_tokens=64_000,
        media_resolution="ultra_high",
        service="flex",
    )
    pages = [4, 1, 3, 2]
    plans = [
        replace(make_plan(runner, tmp_path, page), selection_rank=rank, schema=CachePage, prompt="prompt") for rank, page in enumerate(pages, start=1)
    ]
    constructor_threads: list[int] = []
    calls: list[tuple[int, int, int]] = []
    cached_pages: list[int] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeExtractor:
        def __init__(self) -> None:
            self.identity = len(constructor_threads) + 1
            constructor_threads.append(threading.get_ident())

        def extract(self, **kwargs: Any) -> yachay.OCRResult:
            nonlocal active, max_active
            page = int(kwargs["page"])
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append((threading.get_ident(), self.identity, page))
            time.sleep(0.01 * (5 - page))
            with lock:
                active -= 1
            return make_result(str(page))

    monkeypatch.setattr(runner, "make_extractor", lambda _settings: FakeExtractor())
    monkeypatch.setattr(runner, "cache_result", lambda plan, _result: cached_pages.append(plan.page))
    monkeypatch.setattr(runner, "cache_error", lambda *_args: pytest.fail("No cache error expected"))

    outcomes, provider_calls = runner.execute_page_plans(plans, settings, workers=2, run_id="queue-run")
    assert provider_calls == len(plans)
    assert [outcome.plan.page for outcome in outcomes] == pages
    assert [outcome.result.data.value for outcome in outcomes if outcome.result is not None] == [str(page) for page in pages]
    assert set(cached_pages) == set(pages)
    assert max_active == 2
    assert len(constructor_threads) == 2
    clients_by_thread: dict[int, set[int]] = {}
    for thread_id, client_id, _page in calls:
        clients_by_thread.setdefault(thread_id, set()).add(client_id)
    assert all(len(client_ids) == 1 for client_ids in clients_by_thread.values())
    assert len(calls) == len(plans)


def test_client_construction_failure_is_not_cached_as_a_provider_error(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = replace(make_plan(runner, tmp_path, 84), schema=CachePage, prompt="prompt")
    settings = runner.ModelSettings("project", "global", "model", "medium", 64_000, "ultra_high", "flex")

    def fail_setup(_settings: Any) -> None:
        raise RuntimeError("local setup failed")

    monkeypatch.setattr(runner, "make_extractor", fail_setup)
    monkeypatch.setattr(runner, "cache_error", lambda *_args: pytest.fail("Local setup failures must not be cached"))

    with pytest.raises(RuntimeError, match="local setup failed"):
        runner.execute_page_plans([plan], settings, workers=1, run_id="queue-run")


def test_result_cache_write_failure_is_not_relabelled_as_a_provider_error(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = replace(make_plan(runner, tmp_path, 84), schema=CachePage, prompt="prompt")
    settings = runner.ModelSettings("project", "global", "model", "medium", 64_000, "ultra_high", "flex")

    class FakeExtractor:
        def extract(self, **_kwargs: Any) -> yachay.OCRResult:
            return make_result("84")

    def fail_cache(*_args: Any) -> None:
        raise RuntimeError("cache write failed")

    monkeypatch.setattr(runner, "make_extractor", lambda _settings: FakeExtractor())
    monkeypatch.setattr(runner, "cache_result", fail_cache)
    monkeypatch.setattr(runner, "cache_error", lambda *_args: pytest.fail("Cache-write failures must not be cached as OCR errors"))

    with pytest.raises(RuntimeError, match="cache write failed"):
        runner.execute_page_plans([plan], settings, workers=1, run_id="queue-run")


def test_mixed_schema_field_union_is_first_seen_and_deterministic(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a = make_source(runner, tmp_path)
    source_b = replace(source_a, year=1900, variant="1900", source_id="source-b")
    document = make_document(runner, tmp_path)
    plans = [
        replace(make_plan(runner, tmp_path, 84), source=source_a, document=document),
        replace(make_plan(runner, tmp_path, 143), source=source_b, document=document),
    ]
    monkeypatch.setattr(
        runner,
        "output_fieldnames",
        lambda variant: (
            ["row_id", "year", "part", "pdf_page", "index", *("alpha", "shared")]
            if variant == "1879"
            else ["row_id", "year", "part", "pdf_page", "index", *("shared", "beta")],
            ["row_id", "part", "corr_index", *("corr_a",)] if variant == "1879" else ["row_id", "part", "corr_index", *("corr_b",)],
        ),
    )
    bank_fields, correspondent_fields = runner.mixed_output_fieldnames(plans)
    assert bank_fields[-3:] == ["alpha", "shared", "beta"]
    assert correspondent_fields[-2:] == ["corr_a", "corr_b"]
