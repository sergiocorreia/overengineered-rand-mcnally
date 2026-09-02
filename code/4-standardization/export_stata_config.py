#!/usr/bin/env python3
"""Render project.toml settings as a temporary Stata include file."""

import argparse
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

STATA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


def strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, Sequence):
        return [str(part) for part in value if str(part)]
    raise ValueError(f"expected string/list, got {type(value).__name__}")


def resolve_path(root: Path, configured: Any, default: Path) -> Path:
    candidate = Path(str(configured)).expanduser() if configured else default
    return candidate if candidate.is_absolute() else root / candidate


def stata_quote(value: Any) -> str:
    text = str(value)
    if '"' in text or "\n" in text or "\r" in text:
        raise ValueError("Stata configuration values may not contain quotes or newlines")
    return f'"{text}"'


def validate_names(names: list[str], label: str) -> None:
    invalid = [name for name in names if not STATA_NAME.fullmatch(name)]
    if invalid:
        raise ValueError(f"{label} contains invalid Stata names: {', '.join(invalid)}")


def build_globals(root: Path, raw: Mapping[str, Any]) -> dict[str, Any]:
    project = raw.get("project", {})
    storage = raw.get("storage", {})
    extraction = raw.get("extraction", {})
    dataset = raw.get("dataset", {})
    quality = raw.get("quality", {})
    reconciliation = raw.get("reconciliation", {})
    standardization = raw.get("standardization", {})
    banknorm = standardization.get("banknorm", {}) if isinstance(standardization, Mapping) else {}

    slug = str(project.get("slug", project.get("name", root.name)))
    external_configured = Path(
        str(storage.get("external_data_root", storage.get("external_root", Path("/home/sergio/data") / slug)))
    ).expanduser()
    if not external_configured.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    external_root = external_configured.resolve()

    def external_child(setting: str, default: str) -> Path:
        configured = Path(str(storage.get(setting, default))).expanduser()
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        result = (external_root / configured).resolve()
        if not result.is_relative_to(external_root):
            raise ValueError(f"storage.{setting} escapes external_data_root")
        return result

    extraction_configured = extraction.get("current_tsv", "data-extraction/exports/current/flat.tsv")
    extraction_candidate = Path(str(extraction_configured)).expanduser()
    if extraction_candidate.is_absolute():
        raise ValueError("extraction.current_tsv must be relative to storage.external_data_root")
    extraction_input = (external_root / extraction_candidate).absolute()
    if not extraction_input.resolve().is_relative_to(external_root):
        raise ValueError("extraction.current_tsv escapes storage.external_data_root")
    banknorm_cache = external_child("banknorm_cache_subdirectory", "banknorm-cache")
    corrections_tsv = resolve_path(root, quality.get("corrections_tsv"), root / "manual/record_corrections.tsv")
    qc_output = resolve_path(root, quality.get("output_directory"), root / "output/quality-control")
    standardization_temp = root / "temp/4-standardization"

    keys = strings(dataset.get("keys"))
    entity_keys = strings(dataset.get("entity_keys"))
    time_key = str(dataset.get("time_key", ""))
    if not keys:
        keys = entity_keys + ([time_key] if time_key else [])
    value_fields = strings(dataset.get("value_fields")) or ["value"]
    record_id = str(dataset.get("record_id_field", "record_id"))
    page_id = str(dataset.get("source_page_field", "page_id"))
    provenance = strings(quality.get("provenance_fields")) or [
        record_id,
        page_id,
        "source_sha256",
        "render_sha256",
        "contract_signature",
    ]
    raw_fields = strings(dataset.get("raw_fields")) or ["entity_raw", "period_raw", "value_raw"]
    required_fields = list(dict.fromkeys(keys + value_fields + raw_fields + provenance))
    exact_extraction_fields = strings(dataset.get("extraction_fields"))
    validate_names(required_fields + exact_extraction_fields + entity_keys + ([time_key] if time_key else []), "dataset fields")

    globals_: dict[str, Any] = {
        "project_slug": slug,
        "dataset_shape": str(dataset.get("shape", "cross-section")),
        "analysis_keys": " ".join(keys),
        "entity_keys": " ".join(entity_keys),
        "time_key": time_key,
        "value_fields": " ".join(value_fields),
        "raw_fields": " ".join(raw_fields),
        "required_extraction_fields": " ".join(required_fields),
        "exact_extraction_fields": " ".join(exact_extraction_fields),
        "record_id_field": record_id,
        "source_page_field": page_id,
        "provenance_fields": " ".join(provenance),
        "external_data": external_root,
        "banknorm_cache": banknorm_cache,
        "extraction_flat_tsv": extraction_input,
        "corrections_tsv": corrections_tsv,
        "standardization_temp": standardization_temp,
        "standardization_output": root / "output/4-standardization",
        "reconciliation_output": root / "output/5-reconciliation",
        "exploration_output": root / "output/6-exploration",
        "reviewed_extraction_tsv": standardization_temp / "reviewed-extraction.tsv",
        "record_review_diff_tsv": qc_output / "record-review-differences.tsv",
        "record_review_flags_tsv": qc_output / "record-review-blocking.tsv",
        "corrected_extraction_tsv": standardization_temp / "corrected-extraction.tsv",
        "correction_diff_tsv": qc_output / "correction-differences.tsv",
        "standardized_dta": standardization_temp / "standardized.dta",
        "standardized_tsv": standardization_temp / "standardized.tsv",
        "final_dta": root / f"data/{slug}.dta",
        "final_tsv": root / f"data/{slug}.tsv",
        "qc_output": qc_output,
        "repeated_vintages": 1 if reconciliation.get("repeated_vintages", False) else 0,
        "source_priority_field": str(reconciliation.get("source_priority_field", "source_priority")),
        "use_banknorm": 1 if banknorm.get("enabled", False) else 0,
        "banknorm_state_field": str(banknorm.get("state_field", "state_raw")),
        "banknorm_city_field": str(banknorm.get("city_field", "city_raw")),
        "banknorm_bank_field": str(banknorm.get("bank_field", "")),
        "banknorm_date_field": str(banknorm.get("date_field", time_key)),
        "banknorm_state_output": str(banknorm.get("state_output", "state_banknorm")),
        "banknorm_city_output": str(banknorm.get("city_output", "city_banknorm")),
        "banknorm_city_id_output": str(banknorm.get("city_id_output", "city_id_banknorm")),
        "banknorm_bank_output": str(banknorm.get("bank_output", "bank_banknorm")),
        "banknorm_bank_id_output": str(banknorm.get("bank_id_output", "bank_id_banknorm")),
    }
    return globals_


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    raw = tomllib.loads((root / "project.toml").read_text(encoding="utf-8"))
    globals_ = build_globals(root, raw)
    lines = ["* Generated from project.toml. Do not edit; regenerate via common.do."]
    lines.extend(f"global {name} {stata_quote(value)}" for name, value in globals_.items())
    write_atomic(arguments.output.resolve(), "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
