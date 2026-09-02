from pathlib import Path

import pytest
from export_stata_config import build_globals


def test_external_extraction_and_banknorm_paths_follow_storage_config(tmp_path: Path) -> None:
    external = tmp_path / "external"
    values = {
        "project": {"slug": "example"},
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
        "storage": {"external_data_root": str(tmp_path / "external")},
        "extraction": {"current_tsv": "data-extraction/exports/current/flat.tsv"},
        "dataset": {"keys": ["record_id"], "value_fields": ["value"]},
    }
    values[table][field] = value

    with pytest.raises(ValueError, match="escapes"):
        build_globals(tmp_path, values)
