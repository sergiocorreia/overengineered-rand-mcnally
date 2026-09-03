from pathlib import Path

import pytest
from export_stata_config import build_globals, export_stata_config


def test_external_extraction_and_banknorm_paths_follow_storage_config(tmp_path: Path) -> None:
    external = tmp_path / "external"
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(tmp_path / "legacy"),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(tmp_path / "recovered-v1"),
            "recovered_v1_root_read_only": True,
        },
        "storage": {
            "external_data_root": str(external),
            "banknorm_cache_subdirectory": "shared/banknorm",
        },
        "extraction": {"current_tsv": "data-extraction/exports/current/flat.tsv"},
        "dataset": {"entity_keys": ["entity"], "time_key": "year", "value_fields": ["value"]},
    }

    globals_ = build_globals(tmp_path, values)

    assert globals_["extraction_flat_tsv"] == external / "data-extraction/exports/current/flat.tsv"
    assert globals_["banknorm_cache"] == external / "shared/banknorm"
    assert globals_["legacy_data"] == (tmp_path / "legacy" / "data").resolve()
    assert globals_["review_priority_external"] == external / "review-prioritization"


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("storage", "banknorm_cache_subdirectory", "../escape"),
        ("extraction", "current_tsv", "../../escape.tsv"),
    ],
)
def test_external_subpaths_cannot_escape(tmp_path: Path, table: str, field: str, value: str) -> None:
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(tmp_path / "legacy"),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(tmp_path / "recovered-v1"),
            "recovered_v1_root_read_only": True,
        },
        "storage": {"external_data_root": str(tmp_path / "external")},
        "extraction": {"current_tsv": "data-extraction/exports/current/flat.tsv"},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
    }
    values[table][field] = value

    with pytest.raises(ValueError, match="escapes"):
        build_globals(tmp_path, values)


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("storage", "external_data_root"),
        ("restoration", "legacy_root"),
        ("restoration", "recovered_v1_root"),
    ],
)
def test_boundary_roots_must_be_present_and_absolute(tmp_path: Path, table: str, field: str) -> None:
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(tmp_path / "legacy"),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(tmp_path / "recovered-v1"),
            "recovered_v1_root_read_only": True,
        },
        "storage": {"external_data_root": str(tmp_path / "external")},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
    }
    values[table][field] = "relative/path"

    with pytest.raises(ValueError, match=rf"{table}\.{field} must be absolute"):
        build_globals(tmp_path, values)

    del values[table][field]
    with pytest.raises(ValueError, match=rf"{table}\.{field} is required and must be absolute"):
        build_globals(tmp_path, values)


@pytest.mark.parametrize("immutable_field", ["legacy_root", "recovered_v1_root"])
@pytest.mark.parametrize("external_relation", ["same", "child", "parent"])
def test_external_root_must_be_disjoint_from_immutable_roots(
    tmp_path: Path,
    immutable_field: str,
    external_relation: str,
) -> None:
    immutable = tmp_path / "immutable" / "nested"
    external = {
        "same": immutable,
        "child": immutable / "v2",
        "parent": immutable.parent,
    }[external_relation]
    other_immutable = tmp_path / "other-immutable"
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(other_immutable),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(other_immutable),
            "recovered_v1_root_read_only": True,
        },
        "storage": {"external_data_root": str(external)},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
    }
    values["restoration"][immutable_field] = str(immutable)

    with pytest.raises(ValueError, match=rf"overlaps immutable restoration\.{immutable_field}"):
        build_globals(tmp_path, values)


@pytest.mark.parametrize("destination", ["immutable", "outside"])
def test_configured_quality_output_must_be_in_mutable_v2_storage(tmp_path: Path, destination: str) -> None:
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(tmp_path / "legacy"),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(tmp_path / "recovered-v1"),
            "recovered_v1_root_read_only": True,
        },
        "storage": {"external_data_root": str(tmp_path / "external")},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
        "quality": {
            "output_directory": str(
                tmp_path / "legacy" / "qc" if destination == "immutable" else tmp_path.parent / "outside-rand-mcnally-qc"
            )
        },
    }

    with pytest.raises(ValueError, match="immutable|outside the V2"):
        build_globals(tmp_path, values)


def test_export_output_is_checked_before_include_is_written(tmp_path: Path) -> None:
    values = {
        "project": {"slug": "example"},
        "restoration": {
            "legacy_root": str(tmp_path / "legacy"),
            "legacy_root_read_only": True,
            "recovered_v1_root": str(tmp_path / "recovered-v1"),
            "recovered_v1_root_read_only": True,
        },
        "storage": {"external_data_root": str(tmp_path / "external")},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
    }
    destination = tmp_path / "legacy" / "stata-project-config.do"

    with pytest.raises(ValueError, match="immutable"):
        export_stata_config(tmp_path, values, destination)
    assert not destination.exists()
