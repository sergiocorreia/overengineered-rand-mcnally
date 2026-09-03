from pathlib import Path

import pytest
from apply_corrections import checked_output_paths as checked_correction_outputs
from apply_record_reviews import checked_output_paths as checked_review_outputs

from histdata_pipeline.config import ProjectConfig


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        root=tmp_path / "project",
        values={
            "restoration": {
                "legacy_root": str(tmp_path / "legacy"),
                "legacy_root_read_only": True,
                "recovered_v1_root": str(tmp_path / "recovered-v1"),
                "recovered_v1_root_read_only": True,
            },
            "storage": {"external_data_root": str(tmp_path / "external")},
        },
    )


def test_record_review_validates_all_outputs_before_writing(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="immutable restoration.legacy_root"):
        checked_review_outputs(
            config,
            config.root / "temp/reviewed.tsv",
            config.root / "output/diff.tsv",
            tmp_path / "legacy" / "flags.tsv",
        )


def test_corrections_validate_all_outputs_before_writing(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="outside the V2"):
        checked_correction_outputs(
            config,
            config.root / "temp/corrected.tsv",
            config.root / "output/diff.tsv",
            tmp_path / "unrelated" / "receipt.json",
        )
