"""Offline safety and compatibility checks for the bounded restoration runner."""

from __future__ import annotations

import importlib.util
import json
import sys
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
                "smoke_year": 1881,
                "smoke_edition": 1,
                "smoke_pages": [84, 143],
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


def test_write_paths_allow_only_v2_and_external_roots(runner: ModuleType, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert runner.checked_write_path(config, config.root / "manual" / "ledger.tsv") == (config.root / "manual" / "ledger.tsv").resolve()
    assert runner.checked_write_path(config, config.external_root / "cache" / "page.json") == (config.external_root / "cache" / "page.json").resolve()
    with pytest.raises(ValueError, match="immutable legacy"):
        runner.checked_write_path(config, tmp_path / "legacy" / "output.tsv")
    with pytest.raises(ValueError, match="outside the V2"):
        runner.checked_write_path(config, tmp_path / "unrelated" / "output.tsv")


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
    monkeypatch.setattr(runner, "load_source_config", lambda *_args: source)
    monkeypatch.setattr(runner, "resolve_document", lambda *_args: document)
    monkeypatch.setattr(runner, "validate_selected_pages", lambda *_args: [84, 143])
    monkeypatch.setattr(runner, "load_definition", lambda *_args: (CachePage, "prompt", "schema"))
    monkeypatch.setattr(runner, "model_settings", lambda *_args: settings)
    monkeypatch.setattr(runner, "prepare_page_plans", lambda *_args: ({"definition": {}}, plans))
    monkeypatch.setattr(runner, "require_user_adc", lambda *_args: pytest.fail("ADC preflight must not run"))
    monkeypatch.setattr(runner, "make_extractor", lambda *_args: pytest.fail("provider must not be constructed"))

    with pytest.raises(RuntimeError, match="2 uncached pages > --max-requests 1"):
        runner.run_targeted(year=1881, edition=1, pages=[84, 143], part=None, max_requests=1, dry_run=False)


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
