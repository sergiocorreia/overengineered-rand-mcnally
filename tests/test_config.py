from pathlib import Path

import pytest

from histdata_pipeline.config import ProjectConfig


def make_config(tmp_path: Path, **restoration_overrides: object) -> ProjectConfig:
    restoration: dict[str, object] = {
        "legacy_root": str(tmp_path / "legacy"),
        "legacy_root_read_only": True,
        "recovered_v1_root": str(tmp_path / "recovered-v1"),
        "recovered_v1_root_read_only": True,
    }
    restoration.update(restoration_overrides)
    return ProjectConfig(
        tmp_path / "project",
        {
            "project": {"slug": "demo"},
            "restoration": restoration,
            "storage": {
                "external_data_root": str(tmp_path / "external"),
                "pdf_storage": "external",
            },
        },
    )


def test_external_subpaths_cannot_escape_configured_root(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.values["storage"]["cache_subdirectory"] = "../../escape"
    with pytest.raises(ValueError, match="escapes"):
        config.external_path("cache_subdirectory", "cache")


def test_local_pdf_directory_cannot_escape_project(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.values["storage"].update(pdf_storage="project", local_pdf_directory="../pdfs")
    with pytest.raises(ValueError, match="escapes"):
        _ = config.pdf_directory


def test_checked_write_path_allows_only_v2_and_external_storage(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert config.checked_write_path(config.root / "output" / "result.tsv") == (config.root / "output" / "result.tsv").resolve()
    assert config.checked_write_path(config.external_root / "cache" / "page.json") == (config.external_root / "cache" / "page.json").resolve()
    with pytest.raises(ValueError, match="outside the V2"):
        config.checked_write_path(tmp_path / "unrelated" / "result.tsv")


@pytest.mark.parametrize("immutable_name", ["legacy", "recovered-v1"])
@pytest.mark.parametrize("relation", ["same", "child", "parent"])
def test_checked_write_path_rejects_symmetric_immutable_overlap(tmp_path: Path, immutable_name: str, relation: str) -> None:
    config = make_config(tmp_path)
    immutable_root = tmp_path / immutable_name
    candidate = {
        "same": immutable_root,
        "child": immutable_root / "result.tsv",
        "parent": immutable_root.parent,
    }[relation]

    with pytest.raises(ValueError, match="overlaps immutable restoration"):
        config.checked_write_path(candidate)


def test_checked_write_path_resolves_symlinks_before_containment_check(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.root.mkdir()
    (tmp_path / "legacy").mkdir()
    (config.root / "linked-output").symlink_to(tmp_path / "legacy", target_is_directory=True)

    with pytest.raises(ValueError, match="overlaps immutable restoration.legacy_root"):
        config.checked_write_path(config.root / "linked-output" / "result.tsv")


@pytest.mark.parametrize("flag", ["legacy_root_read_only", "recovered_v1_root_read_only"])
@pytest.mark.parametrize("value", [False, None])
def test_checked_write_path_requires_explicit_immutable_read_only_flags(tmp_path: Path, flag: str, value: object) -> None:
    config = make_config(tmp_path, **{flag: value})

    with pytest.raises(ValueError, match=rf"restoration\.{flag} = true"):
        config.checked_write_path(config.root / "output.tsv")


@pytest.mark.parametrize("immutable_name", ["legacy", "recovered-v1"])
@pytest.mark.parametrize("relation", ["same", "child", "parent"])
def test_external_root_rejects_symmetric_immutable_overlap(tmp_path: Path, immutable_name: str, relation: str) -> None:
    config = make_config(tmp_path)
    immutable_root = tmp_path / immutable_name
    config.values["storage"]["external_data_root"] = str(
        {
            "same": immutable_root,
            "child": immutable_root / "v2",
            "parent": immutable_root.parent,
        }[relation]
    )

    with pytest.raises(ValueError, match="overlaps immutable restoration"):
        _ = config.external_root
