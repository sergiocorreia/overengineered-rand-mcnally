import shutil
import subprocess
from pathlib import Path

import pytest

from tools.initialize_project import EXTERNAL_SUBDIRECTORIES, external_directories, render_files, replace_setting, validate_external_root


def copy_template(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"),
    )
    project_path = destination / "project.toml"
    project_path.write_text(
        replace_setting(project_path.read_text(encoding="utf-8"), "template", "initialized", "false"),
        encoding="utf-8",
    )


def test_render_files_sets_project_identity(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / "copy"
    copy_template(root, copy)
    rendered = render_files(
        copy,
        name="City Population",
        slug="city-population",
        description="Historical city population panel",
        dataset_shape="panel",
        pdf_storage="external",
        external_root=tmp_path / "external" / "city-population",
    )
    assert "initialized = true" in rendered[copy / "project.toml"]
    assert 'slug = "city-population"' in rendered[copy / "project.toml"]
    assert 'name = "city-population"' in rendered[copy / "pyproject.toml"]
    assert "City Population" in rendered[copy / "README.md"]
    assert "{{" not in rendered[copy / "README.md"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_initializer_creates_external_git_pointer(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / "copy"
    copy_template(root, copy)
    command = [
        "python3",
        str(copy / "tools" / "initialize_project.py"),
        "--root",
        str(copy),
        "--git-base",
        str(tmp_path / "git"),
        "--external-base",
        str(tmp_path / "data"),
        "--name",
        "City Population",
        "--slug",
        "city-population",
        "--description",
        "Historical city population panel",
        "--dataset-shape",
        "panel",
        "--pdf-storage",
        "external",
    ]
    external_root = tmp_path / "data" / "city-population"
    external_root.mkdir(parents=True)
    subprocess.run(command, check=True, capture_output=True, text=True)
    pointer = (copy / ".git").read_text(encoding="utf-8")
    assert str(tmp_path / "git" / "city-population" / ".git") in pointer
    assert all((external_root / relative).is_dir() for relative in EXTERNAL_SUBDIRECTORIES)
    assert (external_root / "pdfs").is_dir()
    assert "initialized = true" in (copy / "project.toml").read_text(encoding="utf-8")


def test_initializer_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / "copy"
    copy_template(root, copy)
    before = (copy / "project.toml").read_text(encoding="utf-8")
    subprocess.run(
        [
            "python3",
            str(copy / "tools" / "initialize_project.py"),
            "--root",
            str(copy),
            "--git-base",
            str(tmp_path / "git"),
            "--external-base",
            str(tmp_path / "data"),
            "--name",
            "Demo",
            "--slug",
            "demo",
            "--description",
            "Demo extraction",
            "--dataset-shape",
            "cross-section",
            "--pdf-storage",
            "project",
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (copy / "project.toml").read_text(encoding="utf-8") == before
    assert not (copy / ".git").exists()


def test_initializer_refuses_nonempty_external_root_without_deleting_it(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / "copy"
    copy_template(root, copy)
    before = (copy / "project.toml").read_bytes()
    external_root = tmp_path / "data" / "demo"
    existing = external_root / "existing-cache" / "receipt.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("preserve me", encoding="utf-8")
    result = subprocess.run(
        [
            "python3",
            str(copy / "tools" / "initialize_project.py"),
            "--root",
            str(copy),
            "--git-base",
            str(tmp_path / "git"),
            "--external-base",
            str(tmp_path / "data"),
            "--name",
            "Demo",
            "--slug",
            "demo",
            "--description",
            "Demo extraction",
            "--dataset-shape",
            "cross-section",
            "--pdf-storage",
            "external",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not empty" in result.stderr
    assert existing.read_text(encoding="utf-8") == "preserve me"
    assert (copy / "project.toml").read_bytes() == before
    assert not (copy / ".git").exists()
    assert not (tmp_path / "git" / "demo").exists()


def test_project_pdf_storage_uses_local_pdf_directory_only(tmp_path: Path) -> None:
    paths = external_directories(tmp_path / "external", "project")
    assert tmp_path / "external" / "pdfs" not in paths
    assert {path.relative_to(tmp_path / "external").as_posix() for path in paths} == set(EXTERNAL_SUBDIRECTORIES)


def test_external_root_with_even_an_empty_subdirectory_is_not_adopted(tmp_path: Path) -> None:
    external_root = tmp_path / "external"
    (external_root / "empty-but-preexisting").mkdir(parents=True)
    with pytest.raises(ValueError, match="not empty"):
        validate_external_root(external_root)
    assert (external_root / "empty-but-preexisting").is_dir()


def test_external_directories_follow_project_storage_settings_and_reject_escapes(tmp_path: Path) -> None:
    external = tmp_path / "external"
    paths = external_directories(
        external,
        "external",
        {
            "acquisition_run_subdirectory": "receipts/acquisition",
            "alternate_export_subdirectory": "candidates/alternate",
            "external_pdf_subdirectory": "documents/pdfs",
        },
    )
    assert external / "receipts/acquisition" in paths
    assert external / "candidates/alternate" in paths
    assert external / "documents/pdfs" in paths
    with pytest.raises(ValueError, match="escapes"):
        external_directories(external, "project", {"selection_cache_subdirectory": "../escape"})
