from pathlib import Path

import pytest

from histdata_pipeline.config import ProjectConfig


def test_external_subpaths_cannot_escape_configured_root(tmp_path: Path) -> None:
    config = ProjectConfig(
        tmp_path,
        {
            "project": {"slug": "demo"},
            "storage": {
                "external_data_root": str(tmp_path / "external"),
                "pdf_storage": "external",
                "cache_subdirectory": "../../escape",
            },
        },
    )
    with pytest.raises(ValueError, match="escapes"):
        config.external_path("cache_subdirectory", "cache")


def test_local_pdf_directory_cannot_escape_project(tmp_path: Path) -> None:
    config = ProjectConfig(
        tmp_path,
        {
            "project": {"slug": "demo"},
            "storage": {
                "external_data_root": str(tmp_path / "external"),
                "pdf_storage": "project",
                "local_pdf_directory": "../pdfs",
            },
        },
    )
    with pytest.raises(ValueError, match="escapes"):
        _ = config.pdf_directory
