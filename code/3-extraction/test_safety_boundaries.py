import sys
import types
from pathlib import Path

import extraction_provider
import pytest
from contract import ExtractionContract, load_schema
from extraction_provider import extract_one
from pipeline import SelectedPage
from run_writer import publish_current

from histdata_pipeline.config import ProjectConfig


def project_config(root: Path, external: Path, *, legacy: Path | None = None) -> ProjectConfig:
    values: dict[str, object] = {
        "project": {"slug": "test"},
        "storage": {"external_data_root": str(external), "cache_subdirectory": "cache"},
        "model": {"project_id": "project-1", "name": "model-1", "location": "global"},
        "extraction": {"media_resolution": "ultra_high"},
    }
    if legacy is not None:
        values["restoration"] = {"legacy_root": str(legacy), "legacy_root_read_only": True}
    return ProjectConfig(root=root, values=values)


def selected_page() -> SelectedPage:
    values = {
        "source_id": "source-1",
        "provider": "archive",
        "title": "Volume",
        "source_date": "1900-01-01",
        "classification_source": "manual",
    }
    return SelectedPage(1, "volume.pdf#page=1", "volume.pdf", "a" * 64, 1, "selected", values)


def test_adc_preflight_fails_before_yachay_construction_or_cache_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def reject_adc(project_id: str) -> None:
        events.append(f"adc:{project_id}")
        raise RuntimeError("no user ADC")

    class ForbiddenOCR:
        def __init__(self, **_kwargs: object) -> None:
            events.append("constructed")

    fake_yachay = types.ModuleType("yachay")
    fake_yachay.OCR = ForbiddenOCR
    monkeypatch.setitem(sys.modules, "yachay", fake_yachay)
    monkeypatch.setattr(extraction_provider, "require_user_adc", reject_adc)
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)

    with pytest.raises(RuntimeError, match="no user ADC"):
        extract_one(
            project_config(tmp_path, tmp_path / "external"),
            contract,
            selected_page(),
            tmp_path / "page.jpg",
            "b" * 64,
            service="flex",
            retry_errors=False,
            attempt_id="attempt-1",
        )

    assert events == ["adc:project-1"]
    assert not (tmp_path / "external").exists()


def test_credential_environment_override_prevents_yachay_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = False

    class ForbiddenOCR:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    fake_yachay = types.ModuleType("yachay")
    fake_yachay.OCR = ForbiddenOCR
    monkeypatch.setitem(sys.modules, "yachay", fake_yachay)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "legacy.json"))
    schema = load_schema(Path(__file__).parent / "definitions" / "schema.py")
    contract = ExtractionContract("c" * 64, {}, "prompt", schema)

    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        extract_one(
            project_config(tmp_path, tmp_path / "external"),
            contract,
            selected_page(),
            tmp_path / "page.jpg",
            "b" * 64,
            service="flex",
            retry_errors=False,
            attempt_id="attempt-1",
        )

    assert constructed is False
    assert not (tmp_path / "external").exists()


def test_publish_current_validates_all_destinations_before_creating_a_symlink(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    project_root = legacy / "project"
    external = tmp_path / "external"
    run_directory = external / "exports" / "run-1"
    run_directory.mkdir(parents=True)
    config = project_config(project_root, external, legacy=legacy)

    with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
        publish_current(config, run_directory, "c" * 64)

    assert not (external / "exports" / "current").exists()
    assert not list((external / "exports").glob(".current-*"))


def test_publish_current_replaces_the_pointer_without_touching_its_old_target(tmp_path: Path) -> None:
    external = tmp_path / "external"
    export_root = external / "exports"
    old_run = export_root / "old-run"
    new_run = export_root / "new-run"
    old_run.mkdir(parents=True)
    new_run.mkdir()
    (new_run / "run.json").write_text("{}\n", encoding="utf-8")
    (export_root / "current").symlink_to(old_run.name, target_is_directory=True)
    config = project_config(tmp_path / "project", external)

    publish_current(config, new_run, "c" * 64)

    assert (export_root / "current").is_symlink()
    assert (export_root / "current").resolve() == new_run
    assert old_run.is_dir()
    assert (config.root / "data" / "extraction_current.json").is_file()
