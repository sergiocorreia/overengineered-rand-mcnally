#!/usr/bin/env -S uv run
"""Rank prepared page-level evidence without making model requests.

The detector that prepares ``signals.tsv`` owns source-specific historical
reasoning.  This module only validates that evidence, groups it into readable
priority tiers, draws a reproducible calibration sample, applies a Wilson
precision gate, and accounts for the hard rerun cap.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

RANKING_VERSION = "2.0.0"
RECEIPT_SCHEMA = "rand-mcnally-rerun-ranking/v2"

TIER_DEFINITIONS = {
    1: "structural extraction or coverage failure",
    2: "direct record or value anomaly",
    3: "page-localized panel gap",
    4: "supporting or indirect evidence",
}
DIRECTNESS_DEFINITIONS = {
    "observed_page": "the suspect observation is assigned to this physical page",
    "same_page_bracket": "visible anchors bracket the expected record on this page",
    "adjacent_page_bracket": "visible anchors restrict the expected record to this or an adjacent page",
    "issue_only": "the evidence identifies an issue but not a defensible physical page",
}
PAGE_LOCALIZED = frozenset({"observed_page", "same_page_bracket", "adjacent_page_bracket"})
PAID_LEDGER_STATUSES = frozenset({"completed", "failed", "failed_paid", "provider_error"})
NONPAID_LEDGER_STATUSES = frozenset({"planned", "authorized", "pending", "cancelled", "skipped"})
MANUAL_EVIDENCE_CATEGORIES = ("documented", "identity_manual", "correspondent_manual", "scope_exclusion")


def _field_names(value: str) -> tuple[str, ...]:
    return tuple(value.split())


PAGE_INPUT_FIELDS = _field_names("page_id source_id source_sha256 year edition pdf_part record_count eligible")
SIGNAL_INPUT_FIELDS = _field_names("page_id rule_id signal_family tier entity_id directness magnitude evidence_json")
LABEL_INPUT_FIELDS = ("page_id", "expected_page_evidence_sha256", "outcome", "notes")
LABEL_OUTCOMES = frozenset({"confirmed_problem", "not_problem", "uncertain"})

SIGNAL_DETAIL_FIELDS = _field_names(
    """signal_id page_id pdf_relative_path physical_page source_id source_sha256 year edition pdf_part
    rule_id signal_family tier tier_name entity_id directness page_localized magnitude evidence_json
    signal_evidence_sha256 page_rule_evidence_sha256 page_evidence_sha256"""
)
PAGE_PRIORITY_FIELDS = _field_names(
    """priority_rank selection_rank selection_status page_id pdf_relative_path physical_page source_id source_sha256
    year edition pdf_part record_count eligible best_tier best_tier_name rule_ids signal_families rule_count
    independent_family_count signal_count page_localized_signal_count page_localized_rule_count
    page_localized_family_count distinct_entity_count affected_share max_magnitude qualifying_rule_ids
    qualifying_rule_count qualifying_family_count best_qualifying_tier calibration_stratum page_evidence_sha256 already_counted"""
)
CALIBRATION_SAMPLE_FIELDS = _field_names(
    """sample_order stratum stratum_order page_id pdf_relative_path physical_page source_id source_sha256 year edition
    pdf_part record_count eligible best_tier manual_evidence_categories matched_candidate_page_id match_level
    record_count_difference expected_page_evidence_sha256 already_counted"""
)
SELECTED_PAGE_FIELDS = _field_names(
    """selection_rank page_id pdf_relative_path physical_page source_id source_sha256 year edition pdf_part
    page_evidence_sha256 best_tier qualifying_rule_ids qualifying_rule_count qualifying_family_count signal_count
    distinct_entity_count affected_share max_magnitude already_counted"""
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]*$")
PAGE_ID_RE = re.compile(r"^(?P<path>.+)#page=(?P<page>[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class Page:
    page_id: str
    pdf_relative_path: str
    physical_page: int
    source_id: str
    source_sha256: str
    year: int
    edition: int
    pdf_part: int
    record_count: int
    eligible: bool

    def identity_fields(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "pdf_relative_path": self.pdf_relative_path,
            "physical_page": self.physical_page,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "year": self.year,
            "edition": self.edition,
            "pdf_part": self.pdf_part,
        }


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    page_id: str
    rule_id: str
    signal_family: str
    tier: int
    entity_id: str
    directness: str
    magnitude: Decimal | None
    evidence_json: str
    signal_evidence_sha256: str

    @property
    def page_localized(self) -> bool:
        return self.directness in PAGE_LOCALIZED


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    documented_pages: int = 50
    candidate_pages: int = 50
    control_pages: int = 50
    minimum_candidate_reviews: int = 20
    minimum_observed_precision: float = 0.70
    minimum_wilson_lower: float = 0.50
    wilson_z: float = 1.96
    trial_max_pages: int = 100

    @property
    def sample_pages(self) -> int:
        return self.documented_pages + self.candidate_pages + self.control_pages

    def validate(self) -> None:
        counts = (self.documented_pages, self.candidate_pages, self.control_pages, self.trial_max_pages)
        if any(value < 1 for value in counts):
            raise ValueError("Calibration strata and trial_max_pages must be positive")
        if self.documented_pages < len(MANUAL_EVIDENCE_CATEGORIES):
            raise ValueError("documented_pages must allow every required manual-evidence category")
        if self.control_pages != self.candidate_pages:
            raise ValueError("control_pages must equal candidate_pages for one-to-one matching")
        if not 1 <= self.minimum_candidate_reviews <= self.candidate_pages:
            raise ValueError("minimum_candidate_reviews must be in [1, candidate_pages]")
        if not 0.0 <= self.minimum_observed_precision <= 1.0:
            raise ValueError("minimum_observed_precision must be in [0, 1]")
        if not 0.0 <= self.minimum_wilson_lower <= 1.0:
            raise ValueError("minimum_wilson_lower must be in [0, 1]")
        if not math.isfinite(self.wilson_z) or self.wilson_z <= 0:
            raise ValueError("wilson_z must be a positive finite number")


@dataclass(frozen=True, slots=True)
class CapPolicy:
    denominator: int
    fraction: Decimal
    hard_ceiling: int

    def validate(self) -> None:
        if self.denominator < 1:
            raise ValueError("denominator must be positive")
        if not Decimal("0") < self.fraction <= Decimal("0.05"):
            raise ValueError("fraction must be in (0, 0.05]")
        if self.hard_ceiling < 1:
            raise ValueError("hard_ceiling must be positive")

    @property
    def computed_cap(self) -> int:
        fractional = int((Decimal(self.denominator) * self.fraction).to_integral_value(rounding=ROUND_FLOOR))
        return min(fractional, self.hard_ceiling)


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    relative_path: str
    data: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class RankingArtifacts:
    files: dict[str, bytes]
    receipt: dict[str, Any]
    project_root: Path
    output_directory: Path


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_page_id(raw: str) -> tuple[str, str, int]:
    """Return canonical ID, PDF-root-relative path, and physical page."""

    if raw != raw.strip() or "\\" in raw:
        raise ValueError(f"Unsafe page_id: {raw!r}")
    match = PAGE_ID_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"Invalid page_id: {raw!r}")
    path = PurePosixPath(match.group("path"))
    parts = path.parts
    if parts and parts[0] == "pdfs":
        parts = parts[1:]
        path = PurePosixPath(*parts)
    if not parts or path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe page_id path: {raw!r}")
    if path.suffix.casefold() != ".pdf":
        raise ValueError(f"page_id path must end in .pdf: {raw!r}")
    page = int(match.group("page"))
    relative = path.as_posix()
    return f"{relative}#page={page}", relative, page


def wilson_interval(successes: int, reviews: int, z: float = 1.96) -> tuple[float, float]:
    """Return the two-sided Wilson score interval for a binomial proportion."""

    if reviews < 0 or successes < 0 or successes > reviews:
        raise ValueError("Wilson counts must satisfy 0 <= successes <= reviews")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("Wilson z must be a positive finite number")
    if reviews == 0:
        return 0.0, 1.0
    proportion = successes / reviews
    z2 = z * z
    denominator = 1 + z2 / reviews
    center = (proportion + z2 / (2 * reviews)) / denominator
    radius = z * math.sqrt((proportion * (1 - proportion) + z2 / (4 * reviews)) / reviews) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _snapshot(path: Path, project_root: Path, *, max_bytes: int) -> Snapshot:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, project_root):
        raise ValueError(f"Input must be a regular file inside the V2 project: {path}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise ValueError(f"Input exceeds max_input_bytes={max_bytes}: {resolved.relative_to(project_root)}")
    data = resolved.read_bytes()
    return Snapshot(resolved, resolved.relative_to(project_root).as_posix(), data, hashlib.sha256(data).hexdigest())


def _read_tsv(snapshot: Snapshot, expected: tuple[str, ...] | None = None) -> list[dict[str, str]]:
    try:
        text = snapshot.data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"TSV is not UTF-8: {snapshot.relative_path}") from error
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    fields = tuple(reader.fieldnames or ())
    if expected is not None and fields != expected:
        raise ValueError(f"Expected columns {expected} in {snapshot.relative_path}, got {fields}")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"Malformed row in {snapshot.relative_path}")
    return rows


def _integer(raw: str, *, label: str, minimum: int) -> int:
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"Invalid {label}: {raw!r}")
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}: {raw!r}")
    return value


def _boolean(raw: str, *, label: str) -> bool:
    if raw not in {"0", "1"}:
        raise ValueError(f"{label} must be 0 or 1: {raw!r}")
    return raw == "1"


def _identifier(raw: str, *, label: str) -> str:
    if raw != raw.strip() or IDENTIFIER_RE.fullmatch(raw) is None:
        raise ValueError(f"Invalid {label}: {raw!r}")
    return raw


def _digest(raw: str, *, label: str) -> str:
    if SHA256_RE.fullmatch(raw) is None:
        raise ValueError(f"Invalid {label}: {raw!r}")
    return raw


def _decimal(raw: str, *, label: str, allow_blank: bool) -> Decimal | None:
    if allow_blank and raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"Invalid {label}: {raw!r}") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a nonnegative finite number: {raw!r}")
    return value


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    if value == value.to_integral():
        return str(value.to_integral())
    return format(value.normalize(), "f")


def _canonical_json(raw: str, *, label: str) -> str:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON number {value!r} in {label}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key {key!r} in {label}")
            value[key] = item
        return value

    try:
        value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Invalid evidence_json in {label}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence_json must be an object in {label}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def load_pages(snapshot: Snapshot, *, max_pages: int) -> dict[str, Page]:
    rows = _read_tsv(snapshot, PAGE_INPUT_FIELDS)
    if len(rows) > max_pages:
        raise ValueError(f"Page input exceeds max_pages={max_pages}")
    pages: dict[str, Page] = {}
    for line, row in enumerate(rows, 2):
        page_id, relative, physical_page = canonical_page_id(row["page_id"])
        page = Page(
            page_id=page_id,
            pdf_relative_path=relative,
            physical_page=physical_page,
            source_id=_identifier(row["source_id"], label=f"source_id at line {line}"),
            source_sha256=_digest(row["source_sha256"], label=f"source_sha256 at line {line}"),
            year=_integer(row["year"], label=f"year at line {line}", minimum=1),
            edition=_integer(row["edition"], label=f"edition at line {line}", minimum=1),
            pdf_part=_integer(row["pdf_part"], label=f"pdf_part at line {line}", minimum=0),
            record_count=_integer(row["record_count"], label=f"record_count at line {line}", minimum=0),
            eligible=_boolean(row["eligible"], label=f"eligible at line {line}"),
        )
        if page_id in pages:
            raise ValueError(f"Duplicate canonical page_id in page input: {page_id}")
        pages[page_id] = page
    if not pages:
        raise ValueError("Page input is empty")
    path_contracts: dict[str, tuple[Any, ...]] = {}
    for page in pages.values():
        contract = (page.source_id, page.source_sha256, page.year, page.edition, page.pdf_part)
        previous = path_contracts.setdefault(page.pdf_relative_path, contract)
        if previous != contract:
            raise ValueError(f"PDF metadata changes within {page.pdf_relative_path}")
    return pages


def load_signals(snapshot: Snapshot, pages: dict[str, Page], *, max_signals: int) -> list[Signal]:
    rows = _read_tsv(snapshot, SIGNAL_INPUT_FIELDS)
    if len(rows) > max_signals:
        raise ValueError(f"Signal input exceeds max_signals={max_signals}")
    signals: list[Signal] = []
    seen: set[str] = set()
    rule_contracts: dict[str, tuple[str, int]] = {}
    for line, row in enumerate(rows, 2):
        page_id, _relative, _page = canonical_page_id(row["page_id"])
        if page_id not in pages:
            raise ValueError(f"Signal references unknown page_id at line {line}: {page_id}")
        rule_id = _identifier(row["rule_id"], label=f"rule_id at line {line}")
        family = _identifier(row["signal_family"], label=f"signal_family at line {line}")
        tier = _integer(row["tier"], label=f"tier at line {line}", minimum=1)
        if tier not in TIER_DEFINITIONS:
            raise ValueError(f"Unknown tier at line {line}: {tier}")
        contract = (family, tier)
        if rule_id in rule_contracts and rule_contracts[rule_id] != contract:
            raise ValueError(f"Rule {rule_id!r} changes signal_family or tier")
        rule_contracts[rule_id] = contract
        directness = row["directness"]
        if directness not in DIRECTNESS_DEFINITIONS:
            raise ValueError(f"Unknown directness at line {line}: {directness!r}")
        if directness == "issue_only" and tier != 4:
            raise ValueError(f"issue_only evidence must use supporting tier 4 at line {line}")
        entity_id = row["entity_id"]
        if entity_id != entity_id.strip() or "\t" in entity_id or "\n" in entity_id or "\r" in entity_id:
            raise ValueError(f"Invalid entity_id at line {line}")
        magnitude = _decimal(row["magnitude"], label=f"magnitude at line {line}", allow_blank=True)
        evidence_json = _canonical_json(row["evidence_json"], label=f"line {line}")
        evidence_payload = {
            "page_id": page_id,
            "rule_id": rule_id,
            "signal_family": family,
            "tier": tier,
            "entity_id": entity_id,
            "directness": directness,
            "magnitude": _decimal_text(magnitude),
            "evidence": json.loads(evidence_json),
        }
        evidence_sha256 = stable_hash(evidence_payload)
        signal_id = f"signal-{evidence_sha256[:24]}"
        if signal_id in seen:
            raise ValueError(f"Duplicate signal evidence at line {line}: {signal_id}")
        seen.add(signal_id)
        signals.append(
            Signal(
                signal_id=signal_id,
                page_id=page_id,
                rule_id=rule_id,
                signal_family=family,
                tier=tier,
                entity_id=entity_id,
                directness=directness,
                magnitude=magnitude,
                evidence_json=evidence_json,
                signal_evidence_sha256=evidence_sha256,
            )
        )
    if not signals:
        raise ValueError("Signal input is empty")
    return signals


def load_paid_pages(snapshot: Snapshot) -> set[str]:
    try:
        text = snapshot.data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"TSV is not UTF-8: {snapshot.relative_path}") from error
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    fields = set(reader.fieldnames or ())
    if not {"page_id", "status"}.issubset(fields):
        raise ValueError(f"Ledger must contain page_id and status: {snapshot.relative_path}")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"Malformed row in {snapshot.relative_path}")
    paid: set[str] = set()
    for line, row in enumerate(rows, 2):
        status = row["status"].strip().casefold()
        if status in PAID_LEDGER_STATUSES:
            page_id, _relative, _page = canonical_page_id(row["page_id"])
            paid.add(page_id)
        elif status not in NONPAID_LEDGER_STATUSES:
            raise ValueError(f"Unknown ledger status at line {line}: {status!r}")
    return paid


def _rule_hashes(pages: dict[str, Page], signals: list[Signal]) -> dict[tuple[str, str], str]:
    grouped: dict[tuple[str, str], list[Signal]] = defaultdict(list)
    for signal in signals:
        grouped[(signal.page_id, signal.rule_id)].append(signal)
    hashes: dict[tuple[str, str], str] = {}
    for key, rows in grouped.items():
        page = pages[key[0]]
        hashes[key] = stable_hash(
            {
                "page_id": page.page_id,
                "source_id": page.source_id,
                "source_sha256": page.source_sha256,
                "rule_id": key[1],
                "signals": sorted(row.signal_evidence_sha256 for row in rows),
            }
        )
    return hashes


def _page_hashes(pages: dict[str, Page], signals: list[Signal]) -> dict[str, str]:
    grouped: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        grouped[signal.page_id].append(signal)
    return {
        page_id: stable_hash(
            {
                "page": {
                    "page_id": pages[page_id].page_id,
                    "source_id": pages[page_id].source_id,
                    "source_sha256": pages[page_id].source_sha256,
                    "year": pages[page_id].year,
                    "edition": pages[page_id].edition,
                    "pdf_part": pages[page_id].pdf_part,
                    "record_count": pages[page_id].record_count,
                    "eligible": pages[page_id].eligible,
                },
                "signals": sorted(row.signal_evidence_sha256 for row in grouped[page_id]),
            }
        )
        for page_id in pages
    }


def _manual_evidence_category(signal: Signal) -> str | None:
    evidence = json.loads(signal.evidence_json)
    if signal.signal_family == "documented":
        return "documented"
    if signal.rule_id == "scope_exclusion":
        return "scope_exclusion"
    if "evidence_family" not in evidence:
        return None
    if signal.signal_family == "identity":
        return "identity_manual"
    if signal.signal_family == "correspondent":
        return "correspondent_manual"
    return None


def _priority_rows(
    pages: dict[str, Page],
    signals: list[Signal],
    page_hashes: dict[str, str],
    paid_pages: set[str],
) -> tuple[list[dict[str, Any]], dict[str, list[Signal]]]:
    grouped: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        grouped[signal.page_id].append(signal)
    records: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for page_id, page_signals in grouped.items():
        page = pages[page_id]
        rules = sorted({row.rule_id for row in page_signals})
        families = sorted({row.signal_family for row in page_signals})
        localized = [row for row in page_signals if row.page_localized]
        rank_signals = [row for row in localized if row.tier <= 3] or localized or page_signals
        localized_rules = {row.rule_id for row in rank_signals}
        localized_families = {row.signal_family for row in rank_signals}
        entities = {row.entity_id for row in rank_signals if row.entity_id}
        magnitudes = [row.magnitude for row in rank_signals if row.magnitude is not None]
        best_tier = min(row.tier for row in rank_signals)
        share_denominator = max(page.record_count, len(entities))
        affected_share = Decimal(len(entities)) / Decimal(share_denominator) if share_denominator else None
        maximum = max(magnitudes) if magnitudes else None
        sort_key = (
            best_tier,
            -len(localized_families),
            -len(localized_rules),
            -len(entities),
            -(affected_share or Decimal(0)),
            -(maximum or Decimal(0)),
            page_id,
        )
        records.append(
            (
                sort_key,
                {
                    "selection_rank": "",
                    "selection_status": "",
                    **page.identity_fields(),
                    "record_count": page.record_count,
                    "eligible": int(page.eligible),
                    "best_tier": best_tier,
                    "best_tier_name": TIER_DEFINITIONS[best_tier],
                    "rule_ids": ";".join(rules),
                    "signal_families": ";".join(families),
                    "rule_count": len(rules),
                    "independent_family_count": len(families),
                    "signal_count": len(page_signals),
                    "page_localized_signal_count": len(localized),
                    "page_localized_rule_count": len(localized_rules),
                    "page_localized_family_count": len(localized_families),
                    "distinct_entity_count": len(entities),
                    "affected_share": "" if affected_share is None else f"{affected_share:.6f}",
                    "max_magnitude": _decimal_text(maximum),
                    "qualifying_rule_ids": "",
                    "qualifying_rule_count": 0,
                    "qualifying_family_count": 0,
                    "best_qualifying_tier": "",
                    "calibration_stratum": "",
                    "page_evidence_sha256": page_hashes[page_id],
                    "already_counted": int(page_id in paid_pages),
                },
            )
        )
    ordered: list[dict[str, Any]] = []
    for rank, (_key, row) in enumerate(sorted(records, key=lambda item: item[0]), 1):
        row["priority_rank"] = rank
        ordered.append(row)
    return ordered, grouped


def _calibration_sample(
    pages: dict[str, Page],
    grouped: dict[str, list[Signal]],
    priority_rows: list[dict[str, Any]],
    page_hashes: dict[str, str],
    paid_pages: set[str],
    policy: CalibrationPolicy,
) -> list[dict[str, Any]]:
    manual_categories: dict[str, set[str]] = defaultdict(set)
    for page_id, page_signals in grouped.items():
        for signal in page_signals:
            category = _manual_evidence_category(signal)
            if category is not None:
                manual_categories[page_id].add(category)
    for category in MANUAL_EVIDENCE_CATEGORIES:
        if not any(category in values for values in manual_categories.values()):
            raise ValueError(f"Manual calibration stratum lacks required category: {category}")
    if len(manual_categories) < policy.documented_pages:
        raise ValueError(f"Need {policy.documented_pages} manual-evidence pages, found {len(manual_categories)}")

    category_queues = {
        category: sorted(
            (stable_hash([RANKING_VERSION, "manual", category, page_id, page_hashes[page_id]]), page_id)
            for page_id, values in manual_categories.items()
            if category in values
        )
        for category in MANUAL_EVIDENCE_CATEGORIES
    }
    documented: list[str] = []
    chosen: set[str] = set()
    while len(documented) < policy.documented_pages:
        progress = False
        for category in MANUAL_EVIDENCE_CATEGORIES:
            queue = category_queues[category]
            while queue and queue[0][1] in chosen:
                queue.pop(0)
            if queue and len(documented) < policy.documented_pages:
                _draw, page_id = queue.pop(0)
                documented.append(page_id)
                chosen.add(page_id)
                progress = True
        if not progress:
            raise ValueError("Could not build the exact unique manual-evidence calibration stratum")

    preliminary = [
        row
        for row in priority_rows
        if bool(row["eligible"])
        and not bool(row["already_counted"])
        and row["page_id"] not in manual_categories
        and any(signal.page_localized and signal.tier <= 3 for signal in grouped[row["page_id"]])
    ]
    if len(preliminary) < policy.candidate_pages:
        raise ValueError(f"Need {policy.candidate_pages} new candidate pages, found {len(preliminary)}")
    candidates = [row["page_id"] for row in preliminary[: policy.candidate_pages]]

    excluded_controls = set(manual_categories) | paid_pages | set(candidates)
    control_pool = [
        page
        for page in pages.values()
        if page.eligible
        and page.page_id not in excluded_controls
        and not any(signal.page_localized and signal.tier <= 3 for signal in grouped.get(page.page_id, []))
    ]
    if len(control_pool) < policy.control_pages:
        raise ValueError(f"Need {policy.control_pages} matched controls, found {len(control_pool)}")

    controls: list[tuple[str, str, str, int]] = []
    unused = {page.page_id: page for page in control_pool}
    match_names = ("same_issue_part", "same_issue", "same_edition_part", "same_edition", "other")
    for candidate_id in candidates:
        candidate = pages[candidate_id]

        def match_key(
            control: Page,
            candidate: Page = candidate,
            candidate_id: str = candidate_id,
        ) -> tuple[Any, ...]:
            if (control.year, control.edition, control.pdf_part) == (candidate.year, candidate.edition, candidate.pdf_part):
                level = 0
            elif (control.year, control.edition) == (candidate.year, candidate.edition):
                level = 1
            elif (control.edition, control.pdf_part) == (candidate.edition, candidate.pdf_part):
                level = 2
            elif control.edition == candidate.edition:
                level = 3
            else:
                level = 4
            return (
                level,
                abs(control.record_count - candidate.record_count),
                abs(control.year - candidate.year),
                stable_hash([RANKING_VERSION, "control", candidate_id, control.page_id, page_hashes[control.page_id]]),
            )

        control = min(unused.values(), key=match_key)
        level = int(match_key(control)[0])
        controls.append((control.page_id, candidate_id, match_names[level], abs(control.record_count - candidate.record_count)))
        del unused[control.page_id]

    def sample_row(page_id: str, stratum: str, stratum_order: int) -> dict[str, Any]:
        page = pages[page_id]
        page_signals = grouped.get(page_id, [])
        return {
            "sample_order": 0,
            "stratum": stratum,
            "stratum_order": stratum_order,
            **page.identity_fields(),
            "record_count": page.record_count,
            "eligible": int(page.eligible),
            "best_tier": min((signal.tier for signal in page_signals), default=""),
            "manual_evidence_categories": ";".join(sorted(manual_categories.get(page_id, set()))),
            "matched_candidate_page_id": "",
            "match_level": "",
            "record_count_difference": "",
            "expected_page_evidence_sha256": page_hashes[page_id],
            "already_counted": int(page_id in paid_pages),
        }

    output = [sample_row(page_id, "documented", index) for index, page_id in enumerate(documented, 1)]
    output.extend(sample_row(page_id, "candidate", index) for index, page_id in enumerate(candidates, 1))
    for index, (page_id, candidate_id, match_level, difference) in enumerate(controls, 1):
        row = sample_row(page_id, "control", index)
        row.update(
            matched_candidate_page_id=candidate_id,
            match_level=match_level,
            record_count_difference=difference,
        )
        output.append(row)
    if len({row["page_id"] for row in output}) != policy.sample_pages:
        raise AssertionError("Calibration sample is not unique and exact")
    for order, row in enumerate(output, 1):
        row["sample_order"] = order
    return output


def _calibration_results(
    labels_snapshot: Snapshot | None,
    sample_rows: list[dict[str, Any]],
    policy: CalibrationPolicy,
) -> tuple[dict[str, dict[str, Any]], bool]:
    sample = {row["page_id"]: row for row in sample_rows}
    labels_by_stratum: dict[str, list[str]] = defaultdict(list)
    if labels_snapshot is not None:
        seen: set[str] = set()
        for line, row in enumerate(_read_tsv(labels_snapshot, LABEL_INPUT_FIELDS), 2):
            page_id, _relative, _page = canonical_page_id(row["page_id"])
            if page_id in seen:
                raise ValueError(f"Duplicate calibration label: {page_id}")
            seen.add(page_id)
            if page_id not in sample:
                raise ValueError(f"Calibration label is outside the deterministic sample: {page_id}")
            expected = _digest(row["expected_page_evidence_sha256"], label=f"expected_page_evidence_sha256 at line {line}")
            if expected != sample[page_id]["expected_page_evidence_sha256"]:
                raise ValueError(f"Stale calibration label: {page_id}")
            outcome = row["outcome"]
            if outcome not in LABEL_OUTCOMES:
                raise ValueError(f"Unknown calibration outcome at line {line}: {outcome!r}")
            labels_by_stratum[sample[page_id]["stratum"]].append(outcome)

    results: dict[str, dict[str, Any]] = {}
    for stratum in ("documented", "candidate", "control"):
        outcomes = labels_by_stratum.get(stratum, [])
        confirmed = outcomes.count("confirmed_problem")
        rejected = outcomes.count("not_problem")
        uncertain = outcomes.count("uncertain")
        determinate = confirmed + rejected
        lower, upper = wilson_interval(confirmed, determinate, policy.wilson_z)
        results[stratum] = {
            "sampled": sum(row["stratum"] == stratum for row in sample_rows),
            "labels": len(outcomes),
            "determinate_reviews": determinate,
            "confirmed_problem": confirmed,
            "not_problem": rejected,
            "uncertain": uncertain,
            "observed_precision": round(confirmed / determinate, 10) if determinate else None,
            "wilson_lower": round(lower, 10),
            "wilson_upper": round(upper, 10),
        }
    candidate = results["candidate"]
    determinate = int(candidate["determinate_reviews"])
    confirmed = int(candidate["confirmed_problem"])
    observed = confirmed / determinate if determinate else None
    candidate_lower = wilson_interval(confirmed, determinate, policy.wilson_z)[0]
    passed = (
        determinate >= policy.minimum_candidate_reviews
        and observed is not None
        and observed >= policy.minimum_observed_precision
        and candidate_lower >= policy.minimum_wilson_lower
    )
    candidate["gate_passed"] = passed
    return results, passed


def _apply_selection(
    priority_rows: list[dict[str, Any]],
    grouped: dict[str, list[Signal]],
    gate_passed: bool,
    remaining: int,
    trial_max_pages: int,
) -> list[dict[str, Any]]:
    limit = min(remaining, trial_max_pages)
    candidates: list[tuple[dict[str, Any], int]] = []
    for row in priority_rows:
        page_signals = grouped[row["page_id"]]
        trial_signals = [signal for signal in page_signals if signal.page_localized and signal.tier <= 3]
        qualifying_signals = trial_signals if gate_passed else []
        qualifying = sorted({signal.rule_id for signal in qualifying_signals})
        families = {signal.signal_family for signal in qualifying_signals}
        row["qualifying_rule_ids"] = ";".join(qualifying)
        row["qualifying_rule_count"] = len(qualifying)
        row["qualifying_family_count"] = len(families)
        row["best_qualifying_tier"] = min((signal.tier for signal in qualifying_signals), default="")
        if not bool(row["eligible"]):
            row["selection_status"] = "ineligible"
        elif bool(row["already_counted"]):
            row["selection_status"] = "already_counted"
        elif not trial_signals:
            row["selection_status"] = "tier4_only"
        elif not gate_passed:
            row["selection_status"] = "calibration_gate_failed"
        else:
            candidates.append((row, int(row["best_qualifying_tier"])))

    selected: list[dict[str, Any]] = []
    for row, best_qualifying_tier in candidates:
        if len(selected) >= limit:
            row["selection_status"] = "cap_exhausted" if remaining < trial_max_pages else "trial_limit"
        else:
            rank = len(selected) + 1
            row["selection_rank"] = rank
            row["selection_status"] = "selected"
            selected_row = {field: row[field] for field in SELECTED_PAGE_FIELDS}
            selected_row["best_tier"] = best_qualifying_tier
            selected.append(selected_row)
    return selected


def _signal_detail_rows(
    pages: dict[str, Page],
    signals: list[Signal],
    rule_hashes: dict[tuple[str, str], str],
    page_hashes: dict[str, str],
    page_ranks: dict[str, int],
) -> list[dict[str, Any]]:
    ordered = sorted(signals, key=lambda row: (page_ranks[row.page_id], row.tier, row.rule_id, row.signal_id))
    output: list[dict[str, Any]] = []
    for signal in ordered:
        page = pages[signal.page_id]
        output.append(
            {
                "signal_id": signal.signal_id,
                **page.identity_fields(),
                "rule_id": signal.rule_id,
                "signal_family": signal.signal_family,
                "tier": signal.tier,
                "tier_name": TIER_DEFINITIONS[signal.tier],
                "entity_id": signal.entity_id,
                "directness": signal.directness,
                "page_localized": int(signal.page_localized),
                "magnitude": _decimal_text(signal.magnitude),
                "evidence_json": signal.evidence_json,
                "signal_evidence_sha256": signal.signal_evidence_sha256,
                "page_rule_evidence_sha256": rule_hashes[(signal.page_id, signal.rule_id)],
                "page_evidence_sha256": page_hashes[signal.page_id],
            }
        )
    return output


def _tsv_bytes(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def build_ranking(
    *,
    project_root: Path,
    pages_path: Path,
    signals_path: Path,
    ledger_path: Path,
    labels_path: Path | None,
    output_directory: Path,
    cap_policy: CapPolicy,
    calibration_policy: CalibrationPolicy,
    max_pages: int = 200_000,
    max_signals: int = 5_000_000,
    max_input_bytes: int = 512_000_000,
) -> RankingArtifacts:
    """Build deterministic artifacts in memory; this function never writes files."""

    root = project_root.resolve(strict=True)
    cap_policy.validate()
    calibration_policy.validate()
    if max_pages < 1 or max_signals < 1 or max_input_bytes < 1:
        raise ValueError("Input ceilings must be positive")
    output = output_directory.resolve(strict=False)
    output_root = (root / "output").resolve(strict=False)
    if not _inside(output, output_root):
        raise ValueError("Ranking output must remain inside the V2 output directory")

    snapshots = [
        _snapshot(root / "project.toml", root, max_bytes=max_input_bytes),
        _snapshot(pages_path, root, max_bytes=max_input_bytes),
        _snapshot(signals_path, root, max_bytes=max_input_bytes),
        _snapshot(ledger_path, root, max_bytes=max_input_bytes),
    ]
    labels_snapshot = _snapshot(labels_path, root, max_bytes=max_input_bytes) if labels_path is not None else None
    if labels_snapshot is not None:
        snapshots.append(labels_snapshot)
    if len({row.path for row in snapshots}) != len(snapshots):
        raise ValueError("Ranking input paths must be distinct")
    destinations = {output / name for name in (*files_names(), "ranking_receipt.json")}
    if destinations.intersection(row.path for row in snapshots):
        raise ValueError("Ranking output cannot overwrite an input")

    pages_snapshot, signals_snapshot, ledger_snapshot = snapshots[1:4]
    pages = load_pages(pages_snapshot, max_pages=max_pages)
    if len(pages) != cap_policy.denominator:
        raise ValueError(
            f"Canonical page universe does not match denominator: {len(pages)} != {cap_policy.denominator}"
        )
    signals = load_signals(signals_snapshot, pages, max_signals=max_signals)
    paid_pages = load_paid_pages(ledger_snapshot)
    missing_paid = sorted(paid_pages - set(pages))
    if missing_paid:
        raise ValueError(f"Paid-page ledger contains pages outside the canonical universe: {missing_paid[:5]}")
    computed_cap = cap_policy.computed_cap
    if len(paid_pages) > computed_cap:
        raise ValueError(f"Paid-page ledger already exceeds the cap: {len(paid_pages)} > {computed_cap}")
    remaining = computed_cap - len(paid_pages)

    rule_hashes = _rule_hashes(pages, signals)
    page_hashes = _page_hashes(pages, signals)
    priority_rows, grouped = _priority_rows(pages, signals, page_hashes, paid_pages)
    sample_rows = _calibration_sample(pages, grouped, priority_rows, page_hashes, paid_pages, calibration_policy)
    sample_strata = {row["page_id"]: row["stratum"] for row in sample_rows}
    for row in priority_rows:
        row["calibration_stratum"] = sample_strata.get(row["page_id"], "")
    calibration_results, gate_passed = _calibration_results(labels_snapshot, sample_rows, calibration_policy)
    selected_rows = _apply_selection(priority_rows, grouped, gate_passed, remaining, calibration_policy.trial_max_pages)
    page_ranks = {row["page_id"]: int(row["priority_rank"]) for row in priority_rows}
    signal_rows = _signal_detail_rows(pages, signals, rule_hashes, page_hashes, page_ranks)

    files = {
        "signal_details.tsv": _tsv_bytes(SIGNAL_DETAIL_FIELDS, signal_rows),
        "page_priority.tsv": _tsv_bytes(PAGE_PRIORITY_FIELDS, priority_rows),
        "calibration_sample.tsv": _tsv_bytes(CALIBRATION_SAMPLE_FIELDS, sample_rows),
        "selected_pages.tsv": _tsv_bytes(SELECTED_PAGE_FIELDS, selected_rows),
    }
    selected_relative = (output / "selected_pages.tsv").relative_to(root).as_posix()
    selected_bytes = files["selected_pages.tsv"]
    output_sha256s = {
        (output / name).relative_to(root).as_posix(): hashlib.sha256(data).hexdigest()
        for name, data in sorted(files.items())
    }
    unsigned_receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "ranking_version": RANKING_VERSION,
        "selected_queue_path": selected_relative,
        "selected_queue_sha256": hashlib.sha256(selected_bytes).hexdigest(),
        "selected_queue_bytes": len(selected_bytes),
        "selected_queue_rows": len(selected_rows),
        "denominator": cap_policy.denominator,
        "fraction": float(cap_policy.fraction),
        "fraction_decimal": str(cap_policy.fraction),
        "hard_ceiling": cap_policy.hard_ceiling,
        "computed_cap": computed_cap,
        "prior_paid_unique_pages": len(paid_pages),
        "remaining_before_selection": remaining,
        "selected_new_pages": len(selected_rows),
        "remaining_after_selection": remaining - len(selected_rows),
        "candidate_pages": len(priority_rows),
        "signal_rows": len(signal_rows),
        "denominator_source_path": pages_snapshot.relative_path,
        "denominator_source_sha256": pages_snapshot.sha256,
        "denominator_unique_pages": len(pages),
        "input_sha256s": {row.relative_path: row.sha256 for row in sorted(snapshots, key=lambda item: item.relative_path)},
        "output_sha256s": output_sha256s,
        "paid_ledger_statuses": sorted(PAID_LEDGER_STATUSES),
        "nonpaid_ledger_statuses": sorted(NONPAID_LEDGER_STATUSES),
        "tier_definitions": {str(key): value for key, value in TIER_DEFINITIONS.items()},
        "directness_definitions": DIRECTNESS_DEFINITIONS,
        "ranking_policy": {
            "method": "lexicographic",
            "uses_only_page_localized_tier_1_to_3_signals_for_paid_selection": True,
            "order": [
                "best qualifying tier ascending",
                "independent qualifying signal families descending",
                "qualifying rules descending",
                "distinct affected entities descending",
                "affected share descending",
                "maximum magnitude descending",
                "canonical page_id ascending",
            ],
            "affected_share_denominator": "max(record_count, distinct affected entities)",
            "documented_sample": "deterministic hash round-robin across all four required manual-evidence categories",
            "candidate_sample": "highest preliminary new eligible pages with localized tier <= 3 evidence",
            "control_matching": [
                "same issue and part",
                "same issue",
                "same edition number and part",
                "same edition number",
                "other",
                "then nearest record count and year with a stable-hash tie break",
            ],
        },
        "calibration_policy": {
            "sample_pages": calibration_policy.sample_pages,
            "documented_pages": calibration_policy.documented_pages,
            "candidate_pages": calibration_policy.candidate_pages,
            "control_pages": calibration_policy.control_pages,
            "minimum_candidate_reviews": calibration_policy.minimum_candidate_reviews,
            "minimum_observed_precision": calibration_policy.minimum_observed_precision,
            "minimum_wilson_lower": calibration_policy.minimum_wilson_lower,
            "wilson_z": calibration_policy.wilson_z,
            "trial_max_pages": calibration_policy.trial_max_pages,
        },
        "calibration_results": calibration_results,
        "calibration_gate_passed": gate_passed,
        "calibration_sample_sha256": hashlib.sha256(files["calibration_sample.tsv"]).hexdigest(),
        "calibration_sample_rows": len(sample_rows),
        "calibration_labels_path": labels_snapshot.relative_path if labels_snapshot is not None else None,
        "calibration_labels_sha256": labels_snapshot.sha256 if labels_snapshot is not None else None,
        "calibration_label_rows": sum(row["labels"] for row in calibration_results.values()),
        "trial_selection_limit": min(remaining, calibration_policy.trial_max_pages),
    }
    receipt = {**unsigned_receipt, "receipt_signature": stable_hash(unsigned_receipt)}
    return RankingArtifacts(files=files, receipt=receipt, project_root=root, output_directory=output)


def files_names() -> tuple[str, ...]:
    return ("signal_details.tsv", "page_priority.tsv", "calibration_sample.tsv", "selected_pages.tsv")


def write_ranking(artifacts: RankingArtifacts) -> None:
    output_directory = artifacts.output_directory.resolve(strict=False)
    if not _inside(output_directory, (artifacts.project_root / "output").resolve(strict=False)):
        raise ValueError("Ranking output must remain inside the V2 output directory")
    expected_queue = (output_directory / "selected_pages.tsv").relative_to(artifacts.project_root).as_posix()
    if artifacts.receipt.get("selected_queue_path") != expected_queue:
        raise ValueError("Ranking output path does not match its signed receipt")
    output_directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        **artifacts.files,
        "ranking_receipt.json": (json.dumps(artifacts.receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    for name, data in payloads.items():
        destination = output_directory / name
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, required=True, help="Prepared page-universe TSV")
    parser.add_argument("--signals", type=Path, required=True, help="Prepared anomaly-signal TSV")
    parser.add_argument("--rerun-ledger", type=Path, help="Durable paid-page ledger; defaults to project.toml")
    parser.add_argument("--calibration-labels", type=Path, help="Evidence-bound manual calibration labels")
    parser.add_argument("--output-directory", type=Path, default=Path("output/rerun-ranking"))
    parser.add_argument("--denominator", type=int)
    parser.add_argument("--fraction", type=Decimal)
    parser.add_argument("--hard-ceiling", type=int)
    parser.add_argument("--documented-pages", type=int)
    parser.add_argument("--candidate-pages", type=int)
    parser.add_argument("--control-pages", type=int)
    parser.add_argument("--minimum-candidate-reviews", type=int)
    parser.add_argument("--minimum-observed-precision", type=float)
    parser.add_argument("--minimum-wilson-lower", type=float)
    parser.add_argument("--wilson-z", type=float, default=1.96)
    parser.add_argument("--trial-max-pages", type=int)
    parser.add_argument("--max-pages", type=int, default=200_000)
    parser.add_argument("--max-signals", type=int, default=5_000_000)
    parser.add_argument("--max-input-bytes", type=int, default=512_000_000)
    parser.add_argument("--write", action="store_true", help="Publish artifacts; omission is a no-write preview")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    with (project_root / "project.toml").open("rb") as source:
        config = tomllib.load(source)
    restoration = config.get("restoration")
    review = config.get("review_prioritization")
    if not isinstance(restoration, dict) or not isinstance(review, dict):
        raise ValueError("project.toml must contain restoration and review_prioritization tables")

    def configured(argument: Any, key: str, table: dict[str, Any]) -> Any:
        if argument is not None:
            return argument
        if key not in table:
            raise ValueError(f"project.toml is missing {key}")
        return table[key]

    documented_pages = int(configured(args.documented_pages, "calibration_documented", review))
    candidate_pages = int(configured(args.candidate_pages, "calibration_candidates", review))
    control_pages = int(configured(args.control_pages, "calibration_controls", review))
    configured_total = int(review.get("calibration_pages", -1))
    if (
        args.documented_pages is None
        and args.candidate_pages is None
        and args.control_pages is None
        and documented_pages + candidate_pages + control_pages != configured_total
    ):
        raise ValueError("Configured calibration strata do not sum to calibration_pages")
    output_directory = args.output_directory if args.output_directory.is_absolute() else project_root / args.output_directory
    ledger_path = args.rerun_ledger or project_root / str(review["paid_ledger"])
    configured_labels = project_root / str(review["calibration_decisions"])
    labels_path = args.calibration_labels or (configured_labels if configured_labels.is_file() else None)
    artifacts = build_ranking(
        project_root=project_root,
        pages_path=args.pages,
        signals_path=args.signals,
        ledger_path=ledger_path,
        labels_path=labels_path,
        output_directory=output_directory,
        cap_policy=CapPolicy(
            int(configured(args.denominator, "provisional_page_denominator", restoration)),
            Decimal(str(configured(args.fraction, "provisional_rerun_fraction", restoration))),
            int(configured(args.hard_ceiling, "provisional_rerun_ceiling", restoration)),
        ),
        calibration_policy=CalibrationPolicy(
            documented_pages,
            candidate_pages,
            control_pages,
            int(configured(args.minimum_candidate_reviews, "minimum_candidate_reviews", review)),
            float(configured(args.minimum_observed_precision, "minimum_observed_precision", review)),
            float(configured(args.minimum_wilson_lower, "minimum_wilson_lower_95", review)),
            args.wilson_z,
            int(configured(args.trial_max_pages, "trial_max_pages", review)),
        ),
        max_pages=args.max_pages,
        max_signals=args.max_signals,
        max_input_bytes=args.max_input_bytes,
    )
    if args.write:
        write_ranking(artifacts)
        print(f"Wrote {artifacts.receipt['selected_queue_rows']} selected pages to {artifacts.receipt['selected_queue_path']}")
    else:
        print(json.dumps(artifacts.receipt, indent=2, sort_keys=True))
        print("Preview only: no artifacts written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
