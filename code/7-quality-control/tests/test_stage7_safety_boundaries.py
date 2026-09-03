"""Offline authentication and write-containment regression tests for stage 7."""

import sys
import types
from pathlib import Path

import plan_alternate_extraction
import pytest
import run_alternate_extraction
from build_release_manifest import build_payload
from merge_segmented_extraction import validate_candidate_destinations
from run_alternate_extraction import _make_client
from run_quality_control import run

from histdata_pipeline.config import ProjectConfig


def write_project(root: Path, external: Path, legacy: Path, *, quality_output: Path | None = None) -> None:
    output = quality_output or root / "output" / "quality-control"
    (root / "project.toml").write_text(
        f"""
[project]
slug = "test"

[storage]
external_data_root = "{external}"

[restoration]
legacy_root = "{legacy}"
legacy_root_read_only = true

[extraction]
current_tsv = "exports/current/flat.tsv"

[quality]
output_directory = "{output}"
""".lstrip(),
        encoding="utf-8",
    )


def test_quality_control_rejects_legacy_output_before_reading_inputs(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    write_project(tmp_path, tmp_path / "external", legacy)

    with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
        run(tmp_path, tmp_path / "missing.tsv", tmp_path / "missing-decisions.tsv", legacy / "qc")


def test_alternate_plan_rejects_legacy_output_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "legacy"
    write_project(tmp_path, tmp_path / "external", legacy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan_alternate_extraction.py",
            "--root",
            str(tmp_path),
            "--page-id",
            "book.pdf#page=1",
            "--write-plan",
            "--output",
            str(legacy / "plan.json"),
        ],
    )

    with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
        plan_alternate_extraction.main()

    assert not (legacy / "plan.json").exists()


def test_segment_merge_validates_output_conflicts_and_metadata_destinations(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    external = tmp_path / "external"
    write_project(tmp_path, external, legacy)
    safe = tmp_path / "output" / "candidate.jsonl"

    for position in range(3):
        destinations = [safe, safe.with_name("conflicts.jsonl"), safe.with_name("metadata.json")]
        destinations[position] = legacy / f"blocked-{position}"
        with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
            validate_candidate_destinations(tmp_path, *destinations)


def test_release_manifest_rejects_configured_legacy_destination_before_reading_gate(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    write_project(tmp_path, tmp_path / "external", legacy, quality_output=legacy / "quality-control")

    with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
        build_payload(tmp_path)


def test_alternate_run_rejects_unsafe_export_before_preparation_or_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    external = tmp_path / "external"
    config = ProjectConfig(
        root=tmp_path,
        values={
            "project": {"slug": "test"},
            "storage": {
                "external_data_root": str(external),
                "alternate_export_subdirectory": "../legacy/alternate-exports",
            },
            "restoration": {"legacy_root": str(legacy), "legacy_root_read_only": True},
        },
    )
    events: list[str] = []

    monkeypatch.setattr(run_alternate_extraction, "load_project_config", lambda _root: config)
    monkeypatch.setattr(
        run_alternate_extraction,
        "load_stage3",
        lambda _root: events.append("stage3") or pytest.fail("stage 3 loaded before destination preflight"),
    )
    monkeypatch.setattr(
        run_alternate_extraction,
        "_prepare_requests",
        lambda *_args, **_kwargs: events.append("prepare") or pytest.fail("requests prepared before destination preflight"),
    )
    monkeypatch.setattr(
        run_alternate_extraction,
        "_make_client",
        lambda *_args, **_kwargs: events.append("client") or pytest.fail("client constructed before destination preflight"),
    )
    arguments = run_alternate_extraction.build_parser().parse_args(
        ["--root", str(tmp_path), "--page-id", "book.pdf#page=1", "--execute", "--max-requests", "1"]
    )

    with pytest.raises(ValueError, match="storage.alternate_export_subdirectory escapes external_data_root"):
        run_alternate_extraction.execute(arguments)

    assert events == []


def test_alternate_client_requires_user_adc_before_yachay_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def accept_adc(project_id: str) -> None:
        events.append(f"adc:{project_id}")

    class FakeOCR:
        def __init__(self, **kwargs: object) -> None:
            events.append(f"constructed:{kwargs['project_id']}")

    fake_yachay = types.ModuleType("yachay")
    fake_yachay.OCR = FakeOCR
    monkeypatch.setitem(sys.modules, "yachay", fake_yachay)
    monkeypatch.setattr(run_alternate_extraction, "require_user_adc", accept_adc)
    config = ProjectConfig(
        root=tmp_path,
        values={
            "model": {"project_id": "project-1", "name": "model-1"},
            "extraction": {},
        },
    )

    _make_client(config, retry_errors=False)

    assert events == ["adc:project-1", "constructed:project-1"]


def test_alternate_client_rejects_credential_environment_override_before_construction(
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
    config = ProjectConfig(
        root=tmp_path,
        values={
            "model": {"project_id": "project-1", "name": "model-1"},
            "extraction": {},
        },
    )

    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        _make_client(config, retry_errors=False)

    assert constructed is False
