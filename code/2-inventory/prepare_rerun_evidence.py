#!/usr/bin/env -S uv run
"""Prepare deterministic page and signal inputs for rerun prioritization.

This is an offline adapter.  It reads immutable legacy summaries and recovery
metadata, but writes only compact V2 artifacts.  It never calls an OCR model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILDER_VERSION = "1.0.0"
RECEIPT_SCHEMA = "rand-mcnally-prepared-evidence/v1"

PAGE_INPUT_FIELDS = (
    "page_id",
    "source_id",
    "source_sha256",
    "year",
    "edition",
    "pdf_part",
    "record_count",
    "eligible",
)
SIGNAL_INPUT_FIELDS = (
    "page_id",
    "rule_id",
    "signal_family",
    "tier",
    "entity_id",
    "directness",
    "magnitude",
    "evidence_json",
)

LEGACY_PAGE_FIELDS = (
    "year",
    "edition",
    "pdf_part",
    "pdf_page",
    "source",
    "filename",
    "is_advertisment",
    "input_tokens",
    "thoughts_tokens",
    "output_tokens",
    "total_tokens",
    "wave",
)
RAW_QUALITY_FIELDS = (
    "year",
    "edition",
    "pdf_part",
    "pdf_page",
    "raw_rows",
    "raw_missing_state",
    "raw_missing_city",
    "raw_missing_name",
    "raw_invalid_transit",
    "wave",
)
CLEAN_QUALITY_FIELDS = (
    "year",
    "edition",
    "pdf_part",
    "pdf_page",
    "clean_rows",
    "invalid_city",
    "invalid_name",
    "invalid_transit",
    "invalid_established",
    "established_after_issue",
    "established_before_1776",
    "statement_after_issue",
    "accounting_mismatch_1934",
    "max_resource_error_share",
    "wave",
)
CAPITAL_FIELDS = (
    "state",
    "city",
    "name",
    "transit_number",
    "charter_number",
    "year",
    "edition",
    "pdf_part",
    "pdf_page",
    "index",
    "capital",
    "wave",
    "previous_capital",
    "following_capital",
    "capital_missing_middle",
    "capital_factor",
    "capital_factor_10",
    "capital_factor_2",
)
GAP_FIELDS = (
    "state",
    "city",
    "name",
    "previous_transit",
    "previous_charter",
    "previous_part",
    "previous_page",
    "wave",
    "previous_charter_unique",
    "previous_transit_unique",
    "following_transit",
    "following_charter",
    "following_part",
    "following_page",
    "following_charter_unique",
    "following_transit_unique",
    "charter_agrees",
    "transit_agrees",
    "previous_state_rows",
    "following_state_rows",
    "support_ratio",
    "gap_index",
    "previous_anchor_part",
    "previous_anchor_page",
    "following_anchor_part",
    "following_anchor_page",
    "localized_part_a",
    "localized_page_a",
    "localized_weight_a",
    "localized_part_b",
    "localized_page_b",
    "localized_weight_b",
)
MANUAL_EVIDENCE_FIELDS = (
    "year",
    "edition",
    "pdf_part",
    "pdf_page",
    "evidence_family",
    "strength",
    "affected_records",
    "evidence_source",
    "note",
)
SCOPE_EXCLUSION_FIELDS = (
    "year",
    "edition",
    "pdf_part",
    "page_start",
    "page_end",
    "reason",
    "evidence_source",
)
RAW_CROSSWALK_FIELDS = (
    "year",
    "edition",
    "configured_source",
    "legacy_page_start",
    "legacy_page_end",
    "source_relative_path",
    "v2_pdf_relative_path",
    "physical_page_offset",
    "source_sha256",
    "physical_page_count",
    "evidence",
    "note",
)
MAPPING_FIELDS = (
    "purpose",
    "manifest_index",
    "edition_id",
    "configured_source",
    "configured_range_kind",
    "source_origin",
    "v1_copy_status",
    "source_sha256",
    "crosswalk_sha256",
    "pdf_relative_path",
    "physical_page",
    "page_id",
    "cache_group",
    "cache_page",
)
MIGRATION_REQUIRED_FIELDS = {
    "manifest_order",
    "year",
    "edition",
    "configured_source",
    "range_start",
    "range_end",
    "configured_range_kind",
    "manifest_row_status",
}

MANUAL_RULES = {
    "documented_failure": ("documented_structural_failure", "documented", 1, "hard"),
    "cropped_page": ("documented_cropped_page", "documented", 1, "hard"),
    "cache_error": ("documented_cache_error", "documented", 1, "hard"),
    "documented_rerun": ("documented_rerun", "documented", 2, "strong"),
    "manual_correction": ("documented_manual_correction", "documented", 2, "strong"),
    "location_correction": ("location_correction_cluster", "identity", 4, "supporting"),
    "correspondent_parse": ("correspondent_parse_error", "correspondent", 4, "supporting"),
}
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class Policy:
    capital_secondary_factor: Decimal
    capital_strong_factor: Decimal
    gap_minimum_support_ratio: Decimal
    gap_strong_weight: Decimal
    gap_corroborated_weight: Decimal
    density_neighbor_minimum: Decimal
    density_ratio: Decimal
    identity_cluster_minimum: int
    identity_cluster_share: Decimal
    accounting_minimum_year: int
    accounting_error_share: Decimal
    accounting_cluster_minimum: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Policy:
        table = config.get("review_prioritization")
        if not isinstance(table, dict):
            raise ValueError("project.toml must contain [review_prioritization]")
        try:
            policy = cls(
                Decimal(str(table["capital_secondary_factor"])),
                Decimal(str(table["capital_strong_factor"])),
                Decimal(str(table["gap_minimum_support_ratio"])),
                Decimal(str(table["gap_strong_weight"])),
                Decimal(str(table["gap_corroborated_weight"])),
                Decimal(str(table["density_neighbor_minimum"])),
                Decimal(str(table["density_ratio"])),
                int(table["identity_cluster_minimum"]),
                Decimal(str(table["identity_cluster_share"])),
                int(table["accounting_minimum_year"]),
                Decimal(str(table["accounting_error_share"])),
                int(table["accounting_cluster_minimum"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ValueError("Invalid [review_prioritization] evidence thresholds") from error
        policy.validate()
        return policy

    def validate(self) -> None:
        decimals = (
            self.capital_secondary_factor,
            self.capital_strong_factor,
            self.gap_minimum_support_ratio,
            self.gap_strong_weight,
            self.gap_corroborated_weight,
            self.density_neighbor_minimum,
            self.density_ratio,
            self.identity_cluster_share,
            self.accounting_error_share,
        )
        if any(not value.is_finite() or value < 0 for value in decimals):
            raise ValueError("Evidence thresholds must be finite and nonnegative")
        if not Decimal("1") < self.capital_secondary_factor < self.capital_strong_factor:
            raise ValueError("Capital thresholds must satisfy 1 < secondary < strong")
        if not Decimal("0") < self.gap_minimum_support_ratio:
            raise ValueError("gap_minimum_support_ratio must be positive")
        if not Decimal("0") < self.density_ratio < Decimal("1"):
            raise ValueError("density_ratio must be in (0, 1)")
        if not Decimal("0") < self.identity_cluster_share <= Decimal("1"):
            raise ValueError("identity_cluster_share must be in (0, 1]")
        if not Decimal("0") < self.accounting_error_share <= Decimal("1"):
            raise ValueError("accounting_error_share must be in (0, 1]")
        if min(self.identity_cluster_minimum, self.accounting_minimum_year, self.accounting_cluster_minimum) < 1:
            raise ValueError("Integer evidence thresholds must be positive")


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    sha256: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MigrationRow:
    manifest_order: int
    year: int
    edition: int
    source: str
    range_start: int
    range_end: int
    range_kind: str

    @property
    def edition_id(self) -> str:
        return f"{self.year}-{self.edition}"


@dataclass(frozen=True, slots=True)
class Mapping:
    pdf_relative_path: str
    physical_page: int
    source_path: Path
    source_origin: str
    declared_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedArtifacts:
    pages: bytes
    signals: bytes
    receipt: dict[str, Any]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _snapshot(path: Path, allowed_root: Path) -> Snapshot:
    root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"Input must be a regular file under {root}: {path}")
    before = resolved.stat()
    data = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Input changed while being read: {resolved}")
    return Snapshot(resolved, hashlib.sha256(data).hexdigest(), data)


def _read_tsv(snapshot: Snapshot, expected: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    try:
        text = snapshot.data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"TSV is not UTF-8: {snapshot.path}") from error
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    fields = tuple(reader.fieldnames or ())
    if expected is not None and fields != expected:
        raise ValueError(f"Expected columns {expected} in {snapshot.path}, got {fields}")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"Malformed TSV row in {snapshot.path}")
    return rows


def _integer(raw: str, label: str, *, minimum: int = 0, blank: bool = False) -> int | None:
    if blank and raw == "":
        return None
    if not re.fullmatch(r"-?[0-9]+", raw):
        raise ValueError(f"Invalid {label}: {raw!r}")
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}: {raw!r}")
    return value


def _decimal(raw: str, label: str, *, blank: bool = False) -> Decimal | None:
    if blank and raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"Invalid {label}: {raw!r}") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative: {raw!r}")
    return value


def _flag(raw: str, label: str) -> bool:
    if raw not in {"0", "1"}:
        raise ValueError(f"{label} must be 0 or 1: {raw!r}")
    return raw == "1"


def _key(row: dict[str, str], *, page_field: str = "pdf_page") -> tuple[int, int, int, int]:
    return (
        int(_integer(row["year"], "year", minimum=1)),
        int(_integer(row["edition"], "edition", minimum=1)),
        int(_integer(row["pdf_part"], "pdf_part")),
        int(_integer(row[page_field], page_field, minimum=1)),
    )


def _canonical_relative_pdf(raw: str) -> str:
    if raw != raw.strip() or "\\" in raw:
        raise ValueError(f"Unsafe PDF relative path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or path.suffix.casefold() != ".pdf" or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe PDF relative path: {raw!r}")
    return path.as_posix()


def _source_id(year: int, edition: int, source: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    if not normalized:
        raise ValueError(f"Invalid configured source: {source!r}")
    return f"rand_mcnally_{year}_{edition}_{normalized}"


def _sha256_file(path: Path, allowed_root: Path) -> str:
    root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"Source must be a regular file under {root}: {path}")
    before = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Source changed while being hashed: {resolved}")
    return digest.hexdigest()


def _sha256_directory(path: Path, allowed_root: Path) -> str:
    root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or not _inside(resolved, root):
        raise ValueError(f"Raw source must be a directory under {root}: {path}")
    files = sorted(item for item in resolved.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"Raw source directory is empty: {resolved}")
    digest = hashlib.sha256(b"rand-mcnally-raw-source/v1\0")
    for item in files:
        relative = item.relative_to(resolved).as_posix()
        if item.is_symlink():
            raise ValueError(f"Raw source contains a symlink: {item}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as source:
            for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _migration_rows(snapshot: Snapshot) -> dict[tuple[int, int], MigrationRow]:
    rows = _read_tsv(snapshot)
    if not rows or not MIGRATION_REQUIRED_FIELDS.issubset(rows[0]):
        raise ValueError(f"Migration inventory lacks required columns: {snapshot.path}")
    result: dict[tuple[int, int], MigrationRow] = {}
    for line, row in enumerate(rows, 2):
        if row["manifest_row_status"] != "configured":
            continue
        year = int(_integer(row["year"], f"year at line {line}", minimum=1))
        edition = int(_integer(row["edition"], f"edition at line {line}", minimum=1))
        start = int(_integer(row["range_start"], f"range_start at line {line}", minimum=1))
        end = int(_integer(row["range_end"], f"range_end at line {line}", minimum=start))
        source = row["configured_source"]
        if SAFE_COMPONENT.fullmatch(source) is None:
            raise ValueError(f"Unsafe configured source at line {line}: {source!r}")
        value = MigrationRow(
            int(_integer(row["manifest_order"], f"manifest_order at line {line}", minimum=1)),
            year,
            edition,
            source,
            start,
            end,
            row["configured_range_kind"],
        )
        key = (year, edition)
        if key in result:
            raise ValueError(f"Duplicate configured migration edition: {year}-{edition}")
        result[key] = value
    return result


def _mapping_rows(
    snapshot: Snapshot,
    migrations: dict[tuple[int, int], MigrationRow],
    recovered_downloads: Path,
    legacy_sources: Path,
    raw_crosswalk: dict[tuple[int, int], dict[str, Any]],
    crosswalk_sha256: str,
) -> dict[tuple[int, int, int, int], Mapping]:
    rows = _read_tsv(snapshot, MAPPING_FIELDS)
    result: dict[tuple[int, int, int, int], Mapping] = {}
    page_ids: set[str] = set()
    for line, row in enumerate(rows, 2):
        if row["purpose"] != "recovery_mapping_not_extraction_input":
            raise ValueError(f"Unexpected recovery mapping purpose at line {line}")
        match = re.fullmatch(r"([0-9]{4})-([12])", row["edition_id"])
        if match is None:
            raise ValueError(f"Invalid edition_id at mapping line {line}")
        year, edition = int(match.group(1)), int(match.group(2))
        migration = migrations.get((year, edition))
        if migration is None:
            raise ValueError(f"Mapping references an unconfigured edition at line {line}")
        if int(row["manifest_index"]) != migration.manifest_order:
            raise ValueError(f"Mapping manifest_index disagrees with migration inventory at line {line}")
        if row["configured_source"] != migration.source or row["configured_range_kind"] != migration.range_kind:
            raise ValueError(f"Mapping disagrees with migration inventory at line {line}")
        cache_match = re.fullmatch(rf"{year}-{edition}(?:-part-([1-9][0-9]*))?", row["cache_group"])
        if cache_match is None:
            raise ValueError(f"Invalid cache_group at mapping line {line}")
        part = int(cache_match.group(1) or 0)
        cache_page = int(_integer(row["cache_page"], f"cache_page at line {line}", minimum=1))
        physical_page = int(_integer(row["physical_page"], f"physical_page at line {line}", minimum=1))
        relative = _canonical_relative_pdf(row["pdf_relative_path"])
        expected_page_id = f"{relative}#page={physical_page}"
        if row["page_id"] != expected_page_id:
            raise ValueError(f"Mapping page_id mismatch at line {line}")
        origin = row["source_origin"]
        status = row["v1_copy_status"]
        declared_sha256 = row["source_sha256"]
        if declared_sha256 and re.fullmatch(r"[0-9a-f]{64}", declared_sha256) is None:
            raise ValueError(f"Invalid mapping source_sha256 at line {line}")
        if origin in {"v1_recovery", "recovered_v1"}:
            if status != "available" or row["crosswalk_sha256"]:
                raise ValueError(f"Invalid recovered-V1 provenance at mapping line {line}")
            source_path = (recovered_downloads / PurePosixPath(relative)).resolve(strict=False)
            source_root = recovered_downloads
            origin = "v1_recovery"
        elif origin == "legacy_fallback":
            if status != "missing" or not declared_sha256 or row["crosswalk_sha256"]:
                raise ValueError(f"Invalid legacy-fallback provenance at mapping line {line}")
            source_path = (legacy_sources / PurePosixPath(relative)).resolve(strict=False)
            source_root = legacy_sources
        elif origin == "reviewed_raw_crosswalk":
            crosswalk = raw_crosswalk.get((year, edition))
            if crosswalk is None or row["crosswalk_sha256"] != crosswalk_sha256 or not declared_sha256:
                raise ValueError(f"Invalid raw-crosswalk provenance at mapping line {line}")
            if relative != crosswalk["relative"] or physical_page != cache_page + int(crosswalk["offset"]):
                raise ValueError(f"Mapping disagrees with reviewed raw crosswalk at line {line}")
            if declared_sha256 != crosswalk["sha256"]:
                raise ValueError(f"Mapping raw source hash disagrees with crosswalk at line {line}")
            source_path = Path(crosswalk["source_path"])
            source_root = legacy_sources
        else:
            raise ValueError(f"Unknown source_origin at mapping line {line}: {origin!r}")
        if not _inside(source_path, source_root.resolve(strict=True)):
            raise ValueError(f"Mapping source escapes its immutable source root at line {line}")
        key = (year, edition, part, cache_page)
        if key in result:
            raise ValueError(f"Duplicate recovery mapping key at line {line}: {key}")
        if expected_page_id in page_ids:
            raise ValueError(f"Recovery mapping page collision at line {line}: {expected_page_id}")
        page_ids.add(expected_page_id)
        result[key] = Mapping(relative, physical_page, source_path, origin, declared_sha256)
    return result


def _index_rows(rows: list[dict[str, str]], name: str) -> dict[tuple[int, int, int, int], dict[str, str]]:
    result: dict[tuple[int, int, int, int], dict[str, str]] = {}
    for line, row in enumerate(rows, 2):
        key = _key(row)
        if key in result:
            raise ValueError(f"Duplicate {name} page at line {line}: {key}")
        result[key] = row
    return result


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.to_integral())
    return format(value.normalize(), "f")


def _entity_id(*parts: str) -> str:
    payload = "\0".join(part.strip().casefold() for part in parts).encode("utf-8")
    return f"bank:{hashlib.sha256(payload).hexdigest()[:24]}"


def _signal(
    page_id: str,
    rule_id: str,
    family: str,
    tier: int,
    entity_id: str,
    directness: str,
    magnitude: Decimal,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "rule_id": rule_id,
        "signal_family": family,
        "tier": tier,
        "entity_id": entity_id,
        "directness": directness,
        "magnitude": _decimal_text(magnitude),
        "evidence_json": _json(evidence),
    }


def _tsv(fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _safe_output(path: Path, project_root: Path) -> Path:
    root = project_root.resolve(strict=True)
    resolved = (path if path.is_absolute() else root / path).resolve(strict=False)
    if not (_inside(resolved, (root / "data").resolve(strict=False)) or _inside(resolved, (root / "output").resolve(strict=False))):
        raise ValueError(f"Output must remain under V2 data/ or output/: {path}")
    return resolved


def prepare_evidence(
    *,
    project_root: Path,
    legacy_inputs_root: Path,
    page_mapping_path: Path,
    migration_inventory_path: Path,
    manual_evidence_path: Path,
    scope_exclusions_path: Path,
    raw_crosswalk_path: Path,
    recovered_downloads_root: Path,
    legacy_sources_root: Path,
    policy: Policy,
) -> PreparedArtifacts:
    """Build both prepared TSVs in memory without writing any file."""

    policy.validate()
    project = project_root.resolve(strict=True)
    external_inputs = legacy_inputs_root.resolve(strict=True)
    recovered = recovered_downloads_root.resolve(strict=True)
    legacy_sources = legacy_sources_root.resolve(strict=True)
    if any(_inside(project, root) or _inside(root, project) for root in (external_inputs, recovered, legacy_sources)):
        raise ValueError("Project and immutable input roots must be separate")
    if recovered == legacy_sources or _inside(recovered, legacy_sources) or _inside(legacy_sources, recovered):
        raise ValueError("Recovered and legacy source roots must be separate")

    input_paths = {
        "legacy_pages": external_inputs / "legacy_pages.tsv",
        "raw_quality": external_inputs / "raw_page_quality.tsv",
        "clean_quality": external_inputs / "clean_page_quality.tsv",
        "capital": external_inputs / "capital_signals.tsv",
        "gaps": external_inputs / "gap_signals.tsv",
        "mapping": page_mapping_path,
        "migration": migration_inventory_path,
        "manual_evidence": manual_evidence_path,
        "scope_exclusions": scope_exclusions_path,
        "raw_crosswalk": raw_crosswalk_path,
    }
    snapshots = {
        name: _snapshot(path, external_inputs if name in {"legacy_pages", "raw_quality", "clean_quality", "capital", "gaps"} else project)
        for name, path in input_paths.items()
    }
    if len({snapshot.path for snapshot in snapshots.values()}) != len(snapshots):
        raise ValueError("Prepared-evidence input paths must be distinct")

    migrations = _migration_rows(snapshots["migration"])
    raw_crosswalk: dict[tuple[int, int], dict[str, Any]] = {}
    for line, row in enumerate(_read_tsv(snapshots["raw_crosswalk"], RAW_CROSSWALK_FIELDS), 2):
        year = int(_integer(row["year"], f"year at raw crosswalk line {line}", minimum=1))
        edition = int(_integer(row["edition"], f"edition at raw crosswalk line {line}", minimum=1))
        migration = migrations.get((year, edition))
        if migration is None or migration.range_kind != "raw_scan_index" or row["configured_source"] != migration.source:
            raise ValueError(f"Raw crosswalk disagrees with migration inventory at line {line}")
        start = int(_integer(row["legacy_page_start"], f"legacy_page_start at line {line}", minimum=1))
        end = int(_integer(row["legacy_page_end"], f"legacy_page_end at line {line}", minimum=start))
        if (start, end) != (migration.range_start, migration.range_end):
            raise ValueError(f"Raw crosswalk range disagrees with migration inventory at line {line}")
        source_relative = _canonical_relative_pdf(row["source_relative_path"])
        target_relative = _canonical_relative_pdf(row["v2_pdf_relative_path"])
        offset = int(_integer(row["physical_page_offset"], f"physical_page_offset at line {line}", minimum=-100_000))
        page_count = int(_integer(row["physical_page_count"], f"physical_page_count at line {line}", minimum=1))
        digest = row["source_sha256"]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"Invalid source_sha256 at raw crosswalk line {line}")
        source_path = (legacy_sources / PurePosixPath(source_relative)).resolve(strict=False)
        actual = _sha256_file(source_path, legacy_sources)
        if actual != digest:
            raise ValueError(f"Raw crosswalk source hash mismatch at line {line}: {source_relative}")
        if not 1 <= start + offset <= page_count or not 1 <= end + offset <= page_count:
            raise ValueError(f"Raw crosswalk physical pages exceed declared page count at line {line}")
        key = (year, edition)
        if key in raw_crosswalk:
            raise ValueError(f"Duplicate raw crosswalk edition at line {line}: {key}")
        raw_crosswalk[key] = {
            "start": start,
            "end": end,
            "offset": offset,
            "relative": target_relative,
            "source_path": source_path,
            "sha256": digest,
        }
    expected_raw = {key for key, row in migrations.items() if row.range_kind == "raw_scan_index"}
    if set(raw_crosswalk) != expected_raw:
        difference = sorted(expected_raw ^ set(raw_crosswalk))
        raise ValueError(f"Raw crosswalk editions do not match configured raw sources: {difference}")
    mapping = _mapping_rows(
        snapshots["mapping"],
        migrations,
        recovered,
        legacy_sources,
        raw_crosswalk,
        snapshots["raw_crosswalk"].sha256,
    )
    legacy_rows = _read_tsv(snapshots["legacy_pages"], LEGACY_PAGE_FIELDS)
    raw_rows = _index_rows(_read_tsv(snapshots["raw_quality"], RAW_QUALITY_FIELDS), "raw-quality")
    clean_rows = _index_rows(_read_tsv(snapshots["clean_quality"], CLEAN_QUALITY_FIELDS), "clean-quality")
    if not legacy_rows:
        raise ValueError("Legacy page universe is empty")

    universe: dict[tuple[int, int, int, int], dict[str, str]] = {}
    for line, row in enumerate(legacy_rows, 2):
        key = _key(row)
        if key in universe:
            raise ValueError(f"Duplicate legacy page at line {line}: {key}")
        migration = migrations.get(key[:2])
        if migration is None:
            raise ValueError(f"Legacy page belongs to an unconfigured edition at line {line}: {key[:2]}")
        if row["source"] != migration.source:
            raise ValueError(f"Legacy page source disagrees with migration inventory at line {line}")
        universe[key] = row
    for name, values in (("raw-quality", raw_rows), ("clean-quality", clean_rows)):
        extra = sorted(set(values) - set(universe))
        if extra:
            raise ValueError(f"{name} contains pages outside the legacy universe: {extra[:3]}")

    exclusions: dict[tuple[int, int, int], list[tuple[int | None, int | None, str, str]]] = defaultdict(list)
    exclusion_rows = _read_tsv(snapshots["scope_exclusions"], SCOPE_EXCLUSION_FIELDS)
    seen_exclusions: set[tuple[str, ...]] = set()
    for line, row in enumerate(exclusion_rows, 2):
        signature = tuple(row[field] for field in SCOPE_EXCLUSION_FIELDS)
        if signature in seen_exclusions:
            raise ValueError(f"Duplicate scope exclusion at line {line}")
        seen_exclusions.add(signature)
        year = int(_integer(row["year"], f"year at exclusion line {line}", minimum=1))
        edition = int(_integer(row["edition"], f"edition at exclusion line {line}", minimum=1))
        part = int(_integer(row["pdf_part"], f"pdf_part at exclusion line {line}"))
        start = _integer(row["page_start"], f"page_start at exclusion line {line}", minimum=1, blank=True)
        end = _integer(row["page_end"], f"page_end at exclusion line {line}", minimum=1, blank=True)
        if (start is None) != (end is None) or (start is not None and start > end):
            raise ValueError(f"Invalid scope exclusion interval at line {line}")
        exclusions[(year, edition, part)].append((start, end, row["reason"], row["evidence_source"]))

    def excluded(key: tuple[int, int, int, int]) -> bool:
        return any(start is None or int(start) <= key[3] <= int(end) for start, end, _reason, _source in exclusions.get(key[:3], []))

    source_hashes: dict[tuple[str, Path], str] = {}
    source_receipt: dict[str, dict[str, str]] = {}
    raw_crosswalk_pages = 0
    fallback_pages = 0
    advertisement_pages = 0
    page_rows: list[dict[str, Any]] = []
    page_ids: dict[tuple[int, int, int, int], str] = {}
    seen_page_ids: set[str] = set()
    for key in sorted(universe):
        year, edition, part, legacy_page = key
        migration = migrations[(year, edition)]
        resolved_mapping = mapping.get(key)
        if resolved_mapping is None:
            raise ValueError(f"Final recovery audit has no source/page mapping for legacy page {key}")
        fallback_pages += int(resolved_mapping.source_origin == "legacy_fallback")
        raw_crosswalk_pages += int(resolved_mapping.source_origin == "reviewed_raw_crosswalk")
        relative = resolved_mapping.pdf_relative_path
        page_id = f"{relative}#page={resolved_mapping.physical_page}"
        if page_id in seen_page_ids:
            raise ValueError(f"Canonical page collision: {page_id}")
        seen_page_ids.add(page_id)
        page_ids[key] = page_id
        hash_key = (resolved_mapping.source_origin, resolved_mapping.source_path)
        if hash_key not in source_hashes:
            if resolved_mapping.source_origin == "v1_recovery":
                digest = _sha256_file(resolved_mapping.source_path, recovered)
            else:
                digest = _sha256_file(resolved_mapping.source_path, legacy_sources)
            if resolved_mapping.declared_sha256 and digest != resolved_mapping.declared_sha256:
                raise ValueError(f"Source hash disagrees with recovery mapping: {relative}")
            source_hashes[hash_key] = digest
            source_receipt[relative] = {
                "sha256": digest,
                "origin": resolved_mapping.source_origin,
                "input_path": str(resolved_mapping.source_path),
            }
        digest = source_hashes[hash_key]
        is_advertisement = _flag(universe[key]["is_advertisment"], f"is_advertisment for legacy page {key}")
        advertisement_pages += int(is_advertisement)
        raw = raw_rows.get(key)
        clean = clean_rows.get(key)
        record_count = int(raw["raw_rows"]) if raw is not None else int(clean["clean_rows"]) if clean is not None else 0
        page_rows.append(
            {
                "page_id": page_id,
                "source_id": _source_id(year, edition, migration.source),
                "source_sha256": digest,
                "year": year,
                "edition": edition,
                "pdf_part": part,
                "record_count": record_count,
                "eligible": int(not is_advertisement and not excluded(key)),
            }
        )

    signals: list[dict[str, Any]] = []
    for key in sorted(universe):
        matches = [
            {"page_start": start, "page_end": end, "reason": reason, "evidence_source": source}
            for start, end, reason, source in exclusions.get(key[:3], [])
            if start is None or int(start) <= key[3] <= int(end)
        ]
        if matches:
            signals.append(
                _signal(
                    page_ids[key],
                    "scope_exclusion",
                    "known_negative",
                    4,
                    "",
                    "observed_page",
                    Decimal(len(matches)),
                    {"exclusions": matches},
                )
            )
    manual_rows = _read_tsv(snapshots["manual_evidence"], MANUAL_EVIDENCE_FIELDS)
    seen_manual: set[tuple[str, ...]] = set()
    for line, row in enumerate(manual_rows, 2):
        signature = tuple(row[field] for field in MANUAL_EVIDENCE_FIELDS)
        if signature in seen_manual:
            raise ValueError(f"Duplicate manual evidence at line {line}")
        seen_manual.add(signature)
        key = _key(row)
        if key not in universe:
            raise ValueError(f"Manual evidence references an unknown page at line {line}: {key}")
        contract = MANUAL_RULES.get(row["evidence_family"])
        if contract is None:
            raise ValueError(f"Unknown manual evidence family at line {line}: {row['evidence_family']!r}")
        rule, family, tier, expected_strength = contract
        if row["strength"] != expected_strength:
            raise ValueError(f"Manual evidence strength disagrees with its family at line {line}")
        affected = int(_integer(row["affected_records"], "affected_records", minimum=1, blank=True) or 1)
        signals.append(
            _signal(
                page_ids[key],
                rule,
                family,
                tier,
                f"manual:{line}",
                "observed_page",
                Decimal(affected),
                {
                    "affected_records": affected,
                    "evidence_family": row["evidence_family"],
                    "evidence_source": row["evidence_source"],
                    "note": row["note"],
                    "strength": row["strength"],
                },
            )
        )

    for line, row in enumerate(_read_tsv(snapshots["capital"], CAPITAL_FIELDS), 2):
        key = _key(row)
        if key not in universe:
            raise ValueError(f"Capital evidence references an unknown page at line {line}: {key}")
        missing = _flag(row["capital_missing_middle"], f"capital_missing_middle at line {line}")
        factor_10 = _flag(row["capital_factor_10"], f"capital_factor_10 at line {line}")
        factor_2 = _flag(row["capital_factor_2"], f"capital_factor_2 at line {line}")
        if sum((missing, factor_10, factor_2)) != 1:
            raise ValueError(f"Capital evidence must have exactly one anomaly flag at line {line}")
        factor = _decimal(row["capital_factor"], f"capital_factor at line {line}", blank=True)
        if missing:
            rule, tier, magnitude = "capital_missing_middle", 2, Decimal(1)
        elif factor_10:
            if factor is None or factor < policy.capital_strong_factor:
                raise ValueError(f"Strong capital factor violates configured threshold at line {line}")
            rule, tier, magnitude = "capital_factor_10", 2, factor
        else:
            if factor is None or not policy.capital_secondary_factor <= factor < policy.capital_strong_factor:
                raise ValueError(f"Secondary capital factor violates configured thresholds at line {line}")
            rule, tier, magnitude = "capital_factor_2_10", 4, factor
        signals.append(
            _signal(
                page_ids[key],
                rule,
                "capital",
                tier,
                _entity_id(row["state"], row["city"], row["name"], row["transit_number"], row["charter_number"]),
                "observed_page",
                magnitude,
                {
                    "capital": row["capital"],
                    "charter_number": row["charter_number"],
                    "city": row["city"],
                    "following_capital": row["following_capital"],
                    "index": row["index"],
                    "name": row["name"],
                    "previous_capital": row["previous_capital"],
                    "state": row["state"],
                    "transit_number": row["transit_number"],
                },
            )
        )

    unlocalized_gaps = 0
    for line, row in enumerate(_read_tsv(snapshots["gaps"], GAP_FIELDS), 2):
        support = _decimal(row["support_ratio"], f"support_ratio at gap line {line}")
        assert support is not None
        if support < policy.gap_minimum_support_ratio:
            raise ValueError(f"Gap evidence violates configured support threshold at line {line}")
        charter = _flag(row["charter_agrees"], f"charter_agrees at gap line {line}")
        transit = _flag(row["transit_agrees"], f"transit_agrees at gap line {line}")
        corroboration = policy.gap_strong_weight if charter and transit else policy.gap_corroborated_weight if charter or transit else Decimal(1)
        entity = _entity_id(row["state"], row["city"], row["name"], row["previous_transit"], row["previous_charter"])
        localized = 0
        wave = int(_integer(row["wave"], f"wave at gap line {line}", minimum=1))
        edition = 1 if wave % 2 else 2
        year = (wave - edition) // 2
        for suffix in ("a", "b"):
            part = _integer(row[f"localized_part_{suffix}"], f"localized_part_{suffix}", blank=True)
            page = _integer(row[f"localized_page_{suffix}"], f"localized_page_{suffix}", minimum=1, blank=True)
            weight = _decimal(row[f"localized_weight_{suffix}"], f"localized_weight_{suffix}", blank=True)
            if (part is None) != (page is None) or (part is None) != (weight is None):
                raise ValueError(f"Incomplete gap localization at line {line}")
            if part is None:
                continue
            target = (year, edition, int(part), int(page))
            if target not in universe:
                raise ValueError(f"Gap localization references an unknown page at line {line}: {target}")
            if weight not in {Decimal("0.5"), Decimal("1")}:
                raise ValueError(f"Unexpected gap localization weight at line {line}: {weight}")
            directness = "same_page_bracket" if weight == 1 else "adjacent_page_bracket"
            rule = "panel_gap_same_page" if weight == 1 else "panel_gap_adjacent"
            signals.append(
                _signal(
                    page_ids[target],
                    rule,
                    "panel_gap",
                    3,
                    entity,
                    directness,
                    corroboration * weight,
                    {
                        "charter_agrees": int(charter),
                        "city": row["city"],
                        "following_anchor_part": row["following_anchor_part"],
                        "following_anchor_page": row["following_anchor_page"],
                        "following_part": row["following_part"],
                        "following_page": row["following_page"],
                        "gap_index": row["gap_index"],
                        "localization_weight": _decimal_text(weight),
                        "name": row["name"],
                        "previous_anchor_part": row["previous_anchor_part"],
                        "previous_anchor_page": row["previous_anchor_page"],
                        "previous_part": row["previous_part"],
                        "previous_page": row["previous_page"],
                        "state": row["state"],
                        "support_ratio": _decimal_text(support),
                        "transit_agrees": int(transit),
                    },
                )
            )
            localized += 1
        if localized == 0:
            unlocalized_gaps += 1

    def quality_signal(
        key: tuple[int, int, int, int],
        rule: str,
        family: str,
        tier: int,
        magnitude: int | Decimal,
        evidence: dict[str, Any],
    ) -> None:
        signals.append(
            _signal(page_ids[key], rule, family, tier, "", "observed_page", Decimal(magnitude), evidence)
        )

    for key in sorted(universe):
        raw = raw_rows.get(key)
        clean = clean_rows.get(key)
        raw_count = int(raw["raw_rows"]) if raw else 0
        clean_count = int(clean["clean_rows"]) if clean else 0
        if raw_count:
            raw_identity = max(int(raw[field]) for field in ("raw_missing_state", "raw_missing_city", "raw_missing_name"))
            raw_share = Decimal(raw_identity) / Decimal(raw_count)
            if raw_identity >= policy.identity_cluster_minimum and raw_share >= policy.identity_cluster_share:
                quality_signal(
                    key,
                    "raw_identity_field_loss",
                    "identity",
                    2,
                    raw_identity,
                    {field: raw[field] for field in RAW_QUALITY_FIELDS if field not in {"wave"}},
                )
            transit_bad = max(int(raw["raw_invalid_transit"]), int(clean["invalid_transit"]) if clean else 0)
            if transit_bad >= policy.identity_cluster_minimum and Decimal(transit_bad) / Decimal(raw_count) >= policy.identity_cluster_share:
                quality_signal(
                    key,
                    "transit_format_cluster",
                    "identity",
                    2,
                    transit_bad,
                    {"raw_invalid_transit": raw["raw_invalid_transit"], "clean_invalid_transit": clean["invalid_transit"] if clean else ""},
                )
            row_loss = max(0, raw_count - clean_count)
            if row_loss >= policy.identity_cluster_minimum and Decimal(row_loss) / Decimal(raw_count) >= policy.identity_cluster_share:
                quality_signal(
                    key,
                    "raw_clean_row_loss",
                    "field_loss",
                    4,
                    row_loss,
                    {"raw_rows": raw_count, "clean_rows": clean_count, "loss_share": _decimal_text(Decimal(row_loss) / Decimal(raw_count))},
                )
        if clean_count:
            clean_identity = max(int(clean["invalid_city"]), int(clean["invalid_name"]))
            if clean_identity >= policy.identity_cluster_minimum and Decimal(clean_identity) / Decimal(clean_count) >= policy.identity_cluster_share:
                quality_signal(
                    key,
                    "clean_identity_failure_cluster",
                    "identity",
                    2,
                    clean_identity,
                    {"clean_rows": clean_count, "invalid_city": clean["invalid_city"], "invalid_name": clean["invalid_name"]},
                )
            accounting = int(clean["accounting_mismatch_1934"])
            if (
                key[0] >= policy.accounting_minimum_year
                and accounting >= policy.accounting_cluster_minimum
                and Decimal(accounting) / Decimal(clean_count) >= policy.accounting_error_share
            ):
                quality_signal(
                    key,
                    "accounting_mismatch_cluster",
                    "accounting",
                    2,
                    accounting,
                    {
                        "accounting_mismatch_count": accounting,
                        "clean_rows": clean_count,
                        "max_resource_error_share": clean["max_resource_error_share"],
                    },
                )

    by_part: dict[tuple[int, int, int], list[tuple[int, int]]] = defaultdict(list)
    counts_by_key = {
        key: int(raw_rows[key]["raw_rows"]) if key in raw_rows else int(clean_rows[key]["clean_rows"]) if key in clean_rows else 0
        for key in universe
    }
    for key, count in counts_by_key.items():
        by_part[key[:3]].append((key[3], count))
    for prefix, rows in by_part.items():
        ordered = sorted(rows)
        for index in range(1, len(ordered) - 1):
            previous_page, previous_count = ordered[index - 1]
            page, count = ordered[index]
            following_page, following_count = ordered[index + 1]
            if previous_page + 1 != page or page + 1 != following_page:
                continue
            neighbor = min(previous_count, following_count)
            if Decimal(neighbor) < policy.density_neighbor_minimum or Decimal(count) > policy.density_ratio * Decimal(neighbor):
                continue
            key = (*prefix, page)
            magnitude = Decimal(neighbor) / Decimal(max(count, 1))
            quality_signal(
                key,
                "page_density_collapse",
                "structure",
                1,
                magnitude,
                {
                    "following_page": following_page,
                    "following_record_count": following_count,
                    "neighbor_minimum": neighbor,
                    "observed_record_count": count,
                    "previous_page": previous_page,
                    "previous_record_count": previous_count,
                    "ratio_to_neighbor_minimum": _decimal_text(Decimal(count) / Decimal(neighbor)),
                },
            )

    seen_signals: set[str] = set()
    for row in signals:
        signature = hashlib.sha256(_json(row).encode("utf-8")).hexdigest()
        if signature in seen_signals:
            raise ValueError(f"Duplicate prepared signal: {row['page_id']} {row['rule_id']}")
        seen_signals.add(signature)
    signals.sort(
        key=lambda row: (
            row["page_id"],
            int(row["tier"]),
            row["rule_id"],
            row["entity_id"],
            row["directness"],
            row["evidence_json"],
        )
    )
    pages_bytes = _tsv(PAGE_INPUT_FIELDS, page_rows)
    signals_bytes = _tsv(SIGNAL_INPUT_FIELDS, signals)
    input_hashes = {str(snapshot.path): snapshot.sha256 for snapshot in snapshots.values()}
    unsigned_receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "builder_version": BUILDER_VERSION,
        "inputs_are_read_only": True,
        "input_sha256s": dict(sorted(input_hashes.items())),
        "source_files": dict(sorted(source_receipt.items())),
        "policy": {
            field: str(getattr(policy, field))
            for field in policy.__dataclass_fields__
        },
        "counts": {
            "canonical_pages": len(page_rows),
            "eligible_pages": sum(int(row["eligible"]) for row in page_rows),
            "ineligible_pages": sum(not int(row["eligible"]) for row in page_rows),
            "legacy_advertisement_pages": advertisement_pages,
            "legacy_fallback_pages": fallback_pages,
            "reviewed_raw_crosswalk_pages": raw_crosswalk_pages,
            "scope_exclusion_rows": len(exclusion_rows),
            "signal_rows": len(signals),
            "unlocalized_gap_rows_not_emitted": unlocalized_gaps,
        },
        "outputs": {
            "pages_sha256": hashlib.sha256(pages_bytes).hexdigest(),
            "signals_sha256": hashlib.sha256(signals_bytes).hexdigest(),
        },
    }
    receipt = {
        **unsigned_receipt,
        "receipt_signature": hashlib.sha256(_json(unsigned_receipt).encode("utf-8")).hexdigest(),
    }
    return PreparedArtifacts(pages_bytes, signals_bytes, receipt)


def write_artifacts(
    artifacts: PreparedArtifacts,
    *,
    project_root: Path,
    pages_output: Path,
    signals_output: Path,
    receipt_output: Path,
) -> None:
    destinations = [
        _safe_output(pages_output, project_root),
        _safe_output(signals_output, project_root),
        _safe_output(receipt_output, project_root),
    ]
    if len(set(destinations)) != 3:
        raise ValueError("Prepared-evidence output paths must be distinct")
    payloads = (
        artifacts.pages,
        artifacts.signals,
        (json.dumps(artifacts.receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    for destination, payload in zip(destinations, payloads, strict=True):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)


def _configured_paths(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "project.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    restoration = config.get("restoration")
    storage = config.get("storage")
    review = config.get("review_prioritization")
    if not isinstance(restoration, dict) or not isinstance(storage, dict) or not isinstance(review, dict):
        raise ValueError("project.toml is missing restoration, storage, or review_prioritization settings")
    if restoration.get("legacy_root_read_only") is not True:
        raise ValueError("restoration.legacy_root_read_only must be true")
    external = Path(str(storage["external_data_root"])).expanduser()
    recovered_v1 = Path(str(restoration["recovered_v1_root"])).expanduser()
    legacy = Path(str(restoration["legacy_root"])).expanduser()
    for label, path in (("external_data_root", external), ("recovered_v1_root", recovered_v1), ("legacy_root", legacy)):
        if not path.is_absolute():
            raise ValueError(f"{label} must be absolute")
    legacy_subdirectory = Path(str(review["legacy_inputs_subdirectory"]))
    if legacy_subdirectory.is_absolute() or ".." in legacy_subdirectory.parts:
        raise ValueError("legacy_inputs_subdirectory must be safe and external-root-relative")
    return {
        "config": config,
        "legacy_inputs": external / legacy_subdirectory,
        "recovered_downloads": recovered_v1 / "downloads",
        "legacy_sources": legacy / "sources",
        "manual_evidence": project_root / str(review["known_evidence"]),
        "scope_exclusions": project_root / str(review["scope_exclusions"]),
        "raw_crosswalk": project_root / "manual" / "raw_scan_pdf_crosswalk.tsv",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-mapping", type=Path, default=Path("output/2-inventory/v1-recovery-page-mapping.tsv"))
    parser.add_argument("--migration-inventory", type=Path, default=Path("sources/legacy_migration_inventory.tsv"))
    parser.add_argument("--pages-output", type=Path, default=Path("data/rerun_priority_pages.tsv"))
    parser.add_argument("--signals-output", type=Path, default=Path("data/rerun_priority_signals.tsv"))
    parser.add_argument("--receipt-output", type=Path, default=Path("output/rerun-priority/evidence_receipt.json"))
    parser.add_argument("--write", action="store_true", help="Publish V2 artifacts; omission is a no-write preview")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = _configured_paths(PROJECT_ROOT)
    artifacts = prepare_evidence(
        project_root=PROJECT_ROOT,
        legacy_inputs_root=paths["legacy_inputs"],
        page_mapping_path=PROJECT_ROOT / args.page_mapping if not args.page_mapping.is_absolute() else args.page_mapping,
        migration_inventory_path=PROJECT_ROOT / args.migration_inventory if not args.migration_inventory.is_absolute() else args.migration_inventory,
        manual_evidence_path=paths["manual_evidence"],
        scope_exclusions_path=paths["scope_exclusions"],
        raw_crosswalk_path=paths["raw_crosswalk"],
        recovered_downloads_root=paths["recovered_downloads"],
        legacy_sources_root=paths["legacy_sources"],
        policy=Policy.from_config(paths["config"]),
    )
    if args.write:
        write_artifacts(
            artifacts,
            project_root=PROJECT_ROOT,
            pages_output=args.pages_output,
            signals_output=args.signals_output,
            receipt_output=args.receipt_output,
        )
        print(f"Prepared {artifacts.receipt['counts']['canonical_pages']:,} pages and {artifacts.receipt['counts']['signal_rows']:,} signals")
    else:
        print(json.dumps(artifacts.receipt, ensure_ascii=False, indent=2, sort_keys=True))
        print("Preview only: no artifacts written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
