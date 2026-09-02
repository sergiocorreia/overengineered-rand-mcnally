"""Deterministic quality checks for provenance-rich historical datasets.

Detection and adjudication are deliberately separate.  This module emits stable
case identifiers and evidence hashes; it never changes an extracted value.
"""

import json
import math
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

ALLOWED_DISPOSITIONS = {
    "corrected",
    "resolved",
    "excluded",
    "source_verified",
    "expected_gap",
    "open",
}
RESOLVED_DISPOSITIONS = ALLOWED_DISPOSITIONS - {"open"}
FLAG_FIELDS = (
    "case_id",
    "check_type",
    "severity",
    "dataset_shape",
    "key_json",
    "entity_id",
    "period",
    "record_id",
    "page_id",
    "source_sha256",
    "render_sha256",
    "contract_signature",
    "field",
    "current_value",
    "observed",
    "expected",
    "message",
    "evidence_hash",
    "disposition",
    "decision_status",
    "decision_reason",
    "decision_evidence_page",
    "correction_id",
)


@dataclass(frozen=True)
class AccountingRule:
    name: str
    total: str
    components: tuple[str, ...]
    tolerance: float = 0.0


@dataclass(frozen=True)
class QCConfig:
    dataset_shape: str
    key_fields: tuple[str, ...]
    entity_fields: tuple[str, ...]
    time_field: str | None
    value_fields: tuple[str, ...]
    record_id_field: str
    source_page_field: str
    provenance_fields: tuple[str, ...]
    bounds: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)
    identity_valid_fields: tuple[str, ...] = ()
    expected_times: tuple[str, ...] = ()
    expected_frequency: float = 1.0
    isolated_ratio: float = 3.0
    return_ratio: float = 1.5
    one_sided_ratio: float = 5.0
    persistent_ratio: float = 3.0
    persistent_window: int = 3
    coverage_ratio: float = 0.8
    cross_group_fields: tuple[str, ...] = ()
    robust_z: float = 8.0
    cross_outlier_ratio: float = 10.0
    heaping_base: int = 10
    heaping_share: float = 0.8
    heaping_minimum: int = 20
    cluster_minimum: int = 3
    accounting_rules: tuple[AccountingRule, ...] = ()
    blocking_severities: tuple[str, ...] = ("blocking",)

    def __post_init__(self) -> None:
        if self.dataset_shape not in {"panel", "cross-section"}:
            raise ValueError("dataset shape must be 'panel' or 'cross-section'")
        if not self.key_fields:
            raise ValueError("dataset keys must not be empty")
        if self.dataset_shape == "panel" and (not self.entity_fields or not self.time_field):
            raise ValueError("panel datasets require entity_keys and time_key")
        if self.dataset_shape == "panel" and not set((*self.entity_fields, self.time_field)) <= set(self.key_fields):
            raise ValueError("panel dataset keys must include every entity key and the time key")
        if not self.value_fields:
            raise ValueError("dataset value_fields must not be empty")
        if self.expected_frequency <= 0 or not math.isfinite(self.expected_frequency):
            raise ValueError("quality.panel.expected_frequency must be positive")
        if self.persistent_window < 2:
            raise ValueError("persistent_window must be at least 2")
        ratio_fields = {
            "isolated_ratio": self.isolated_ratio,
            "return_ratio": self.return_ratio,
            "one_sided_ratio": self.one_sided_ratio,
            "persistent_ratio": self.persistent_ratio,
            "cross_outlier_ratio": self.cross_outlier_ratio,
        }
        invalid_ratios = [name for name, value in ratio_fields.items() if value < 1]
        invalid_ratios.extend(name for name, value in ratio_fields.items() if not math.isfinite(value))
        if invalid_ratios:
            raise ValueError(f"quality ratios must be at least one: {', '.join(invalid_ratios)}")
        if not 0 <= self.coverage_ratio <= 1:
            raise ValueError("coverage_ratio must lie between zero and one")
        if self.robust_z < 0 or not math.isfinite(self.robust_z):
            raise ValueError("robust_z must be nonnegative")
        if self.heaping_base < 1 or self.heaping_minimum < 1 or self.cluster_minimum < 1:
            raise ValueError("heaping and clustering sizes must be positive")
        if not 0 <= self.heaping_share <= 1:
            raise ValueError("heaping_share must lie between zero and one")
        invalid_bounds = [field for field, (lower, upper) in self.bounds.items() if lower is not None and upper is not None and lower > upper]
        invalid_bounds.extend(
            field
            for field, limits in self.bounds.items()
            if any(value is not None and not math.isfinite(value) for value in limits)
        )
        if invalid_bounds:
            raise ValueError(f"quality bounds are nonfinite or have min greater than max: {', '.join(invalid_bounds)}")
        if any(rule.tolerance < 0 or not rule.components for rule in self.accounting_rules):
            raise ValueError("accounting rules require components and a nonnegative tolerance")
        if "blocking" not in self.blocking_severities:
            raise ValueError("quality.release.blocking_severities must include 'blocking'")
        duplicate_lists = {
            "keys": self.key_fields,
            "entity_keys": self.entity_fields,
            "value_fields": self.value_fields,
            "provenance_fields": self.provenance_fields,
            "expected_times": self.expected_times,
        }
        repeated = [name for name, values in duplicate_lists.items() if len(values) != len(set(values))]
        if repeated:
            raise ValueError(f"quality configuration repeats values in: {', '.join(repeated)}")


@dataclass(frozen=True)
class Decision:
    case_id: str
    expected_evidence_hash: str
    disposition: str
    reason: str
    evidence_page: str = ""
    correction_id: str = ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, Sequence):
        return tuple(str(part) for part in value if str(part))
    raise ValueError(f"expected a string or list, got {type(value).__name__}")


def _number(value: Any, default: float) -> float:
    return default if value is None or str(value).strip() == "" else float(value)


def load_config(path: Path) -> QCConfig:
    """Load the public project configuration without inventing project facts."""

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    dataset = _mapping(raw.get("dataset"))
    quality = _mapping(raw.get("quality"))
    panel = _mapping(quality.get("panel"))
    cross = _mapping(quality.get("cross_section"))
    release = _mapping(quality.get("release"))

    shape = str(dataset.get("shape", "cross-section"))
    entity_fields = _tuple_of_strings(dataset.get("entity_keys"))
    time_field_raw = dataset.get("time_key")
    time_field = str(time_field_raw) if time_field_raw else None
    key_fields = _tuple_of_strings(dataset.get("keys"))
    if not key_fields:
        key_fields = entity_fields + ((time_field,) if time_field else ())
    record_id_field = str(dataset.get("record_id_field", "record_id"))
    source_page_field = str(dataset.get("source_page_field", "page_id"))
    value_fields = _tuple_of_strings(dataset.get("value_fields")) or ("value",)

    provenance_fields = _tuple_of_strings(quality.get("provenance_fields"))
    if not provenance_fields:
        provenance_fields = (
            record_id_field,
            source_page_field,
            "source_sha256",
            "render_sha256",
            "contract_signature",
        )

    bounds: dict[str, tuple[float | None, float | None]] = {}
    for field_name, rule in _mapping(quality.get("bounds")).items():
        if isinstance(rule, Mapping):
            lower = rule.get("min")
            upper = rule.get("max")
        elif isinstance(rule, Sequence) and not isinstance(rule, str) and len(rule) == 2:
            lower, upper = rule
        else:
            raise ValueError(f"quality.bounds.{field_name} must contain min/max")
        bounds[str(field_name)] = (
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        )

    accounting_rules: list[AccountingRule] = []
    accounting_raw = quality.get("accounting_rules", ())
    if isinstance(accounting_raw, Sequence) and not isinstance(accounting_raw, str):
        for index, item in enumerate(accounting_raw):
            rule = _mapping(item)
            accounting_rules.append(
                AccountingRule(
                    name=str(rule.get("name", f"accounting_{index + 1}")),
                    total=str(rule["total"]),
                    components=_tuple_of_strings(rule["components"]),
                    tolerance=float(rule.get("tolerance", 0.0)),
                )
            )

    expected_times = _tuple_of_strings(panel.get("expected_times"))
    return QCConfig(
        dataset_shape=shape,
        key_fields=key_fields,
        entity_fields=entity_fields,
        time_field=time_field,
        value_fields=value_fields,
        record_id_field=record_id_field,
        source_page_field=source_page_field,
        provenance_fields=provenance_fields,
        bounds=bounds,
        identity_valid_fields=_tuple_of_strings(quality.get("identity_valid_fields")),
        expected_times=expected_times,
        expected_frequency=_number(panel.get("expected_frequency"), 1.0),
        isolated_ratio=_number(panel.get("isolated_ratio"), 3.0),
        return_ratio=_number(panel.get("return_ratio"), 1.5),
        one_sided_ratio=_number(panel.get("one_sided_ratio"), 5.0),
        persistent_ratio=_number(panel.get("persistent_ratio"), 3.0),
        persistent_window=int(panel.get("persistent_window", 3)),
        coverage_ratio=_number(panel.get("coverage_ratio"), 0.8),
        cross_group_fields=_tuple_of_strings(cross.get("group_fields")),
        robust_z=_number(cross.get("robust_z"), 8.0),
        cross_outlier_ratio=_number(cross.get("outlier_ratio"), 10.0),
        heaping_base=int(cross.get("heaping_base", 10)),
        heaping_share=_number(cross.get("heaping_share"), 0.8),
        heaping_minimum=int(cross.get("heaping_minimum", 20)),
        cluster_minimum=int(quality.get("cluster_minimum", 3)),
        accounting_rules=tuple(accounting_rules),
        blocking_severities=_tuple_of_strings(release.get("blocking_severities")) or ("blocking",),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _float(value: Any) -> float | None:
    if _blank(value):
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "valid", "resolved", "accepted"}


def _time_number(value: Any) -> float | None:
    number = _float(value)
    if number is not None:
        return number
    text = str(value).strip()
    if not text:
        return None
    for parser in (date.fromisoformat, datetime.fromisoformat):
        try:
            parsed = parser(text)
            return float(parsed.toordinal())
        except ValueError:
            continue
    if len(text) == 7 and text[4] == "-":
        try:
            year, month = (int(part) for part in text.split("-"))
            return float(year * 12 + month)
        except ValueError:
            return None
    return None


def _ratio(first: float, second: float) -> float:
    first_abs, second_abs = abs(first), abs(second)
    if first_abs == second_abs:
        return 1.0
    if min(first_abs, second_abs) == 0:
        return math.inf
    return max(first_abs, second_abs) / min(first_abs, second_abs)


def _inferred_time_spine(
    ordered_times: Sequence[str],
    time_numbers: Mapping[str, float],
    expected_frequency: float,
) -> list[str]:
    """Infer a bounded numeric, monthly, or ISO-date spine without losing observed spellings."""

    if not ordered_times:
        return []
    low, high = time_numbers[ordered_times[0]], time_numbers[ordered_times[-1]]
    inferred_count = int(round((high - low) / expected_frequency)) + 1
    if not 0 < inferred_count <= 100_000:
        return list(ordered_times)
    observed_by_number = {number: text for text, number in time_numbers.items()}
    if all(_float(item) is not None for item in ordered_times):
        return [observed_by_number.get(low + index * expected_frequency, f"{low + index * expected_frequency:g}") for index in range(inferred_count)]

    if expected_frequency.is_integer() and all(re.fullmatch(r"\d{4}-\d{2}", item) for item in ordered_times):
        step = int(expected_frequency)
        first_year, first_month = (int(part) for part in ordered_times[0].split("-"))
        first_index = first_year * 12 + first_month - 1
        result: list[str] = []
        for index in range(inferred_count):
            month_index = first_index + index * step
            year, month_zero = divmod(month_index, 12)
            value = f"{year:04d}-{month_zero + 1:02d}"
            result.append(observed_by_number.get(time_numbers.get(value, math.nan), value))
        return result

    if expected_frequency.is_integer():
        try:
            parsed_dates = [date.fromisoformat(item) for item in ordered_times]
        except ValueError:
            parsed_dates = []
        if parsed_dates:
            first = parsed_dates[0]
            step = timedelta(days=int(expected_frequency))
            return [(first + index * step).isoformat() for index in range(inferred_count)]
    return list(ordered_times)


def _row_key(row: Mapping[str, Any], config: QCConfig) -> dict[str, str]:
    return {field: str(row.get(field, "")) for field in config.key_fields}


def _entity(row: Mapping[str, Any], config: QCConfig) -> str:
    return "|".join(str(row.get(field, "")) for field in config.entity_fields)


def make_flag(
    config: QCConfig,
    check_type: str,
    severity: str,
    *,
    row: Mapping[str, Any] | None = None,
    key: Mapping[str, Any] | None = None,
    field_name: str = "",
    observed: Any = "",
    expected: Any = "",
    message: str,
    locator: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    row = row or {}
    key_dict = {str(k): str(v) for k, v in (key or _row_key(row, config)).items()}
    locator = locator or {}
    record_id = str(row.get(config.record_id_field, "")) if row else str(locator.get("record_id", key_dict.get(config.record_id_field, "")))
    page_id = str(row.get(config.source_page_field, "")) if row else str(locator.get("page_id", key_dict.get(config.source_page_field, "")))
    entity = _entity(row, config) if row else str(locator.get("entity_id", "|".join(key_dict.get(field, "") for field in config.entity_fields)))
    period = (
        str(row.get(config.time_field, ""))
        if row and config.time_field
        else str(locator.get("period", key_dict.get(config.time_field or "", "")))
    )
    stable_locator = {
        "check_type": check_type,
        "key": key_dict,
        "record_id": record_id,
        "page_id": page_id,
        "field": field_name,
        **dict(locator),
    }
    evidence = {
        "locator": stable_locator,
        "observed": observed,
        "expected": expected,
        "message": message,
        "provenance": {field: row.get(field, "") for field in config.provenance_fields} if row else {},
    }
    case_id = f"qc-{content_hash(stable_locator)[:20]}"
    return {
        "case_id": case_id,
        "check_type": check_type,
        "severity": severity,
        "dataset_shape": config.dataset_shape,
        "key_json": canonical_json(key_dict),
        "entity_id": entity,
        "period": period,
        "record_id": record_id,
        "page_id": page_id,
        "source_sha256": str(row.get("source_sha256", "")),
        "render_sha256": str(row.get("render_sha256", "")),
        "contract_signature": str(row.get("contract_signature", "")),
        "field": field_name,
        "current_value": str(row.get(field_name, "")) if row and field_name else "",
        "observed": str(observed),
        "expected": str(expected),
        "message": message,
        "evidence_hash": content_hash(evidence),
        "disposition": "open",
        "decision_status": "open",
        "decision_reason": "",
        "decision_evidence_page": "",
        "correction_id": "",
    }


def validate_columns(rows: Sequence[Mapping[str, Any]], config: QCConfig) -> None:
    if not rows:
        return
    available = set(rows[0])
    required = (
        set(config.key_fields)
        | set(config.value_fields)
        | set(config.provenance_fields)
        | set(config.bounds)
        | set(config.identity_valid_fields)
        | set(config.cross_group_fields)
    )
    for rule in config.accounting_rules:
        required.add(rule.total)
        required.update(rule.components)
    if config.time_field:
        required.add(config.time_field)
    missing = sorted(required - available)
    if missing:
        raise ValueError(f"input is missing configured columns: {', '.join(missing)}")


def common_checks(rows: Sequence[Mapping[str, Any]], config: QCConfig) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    if not rows:
        flags.append(
            make_flag(
                config,
                "empty_dataset",
                "blocking",
                key={},
                observed=0,
                expected="> 0 rows",
                message="The analytical dataset is empty.",
            )
        )
        return flags

    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        for field_name in config.key_fields:
            if _blank(row.get(field_name)):
                flags.append(
                    make_flag(
                        config,
                        "missing_key",
                        "blocking",
                        row=row,
                        field_name=field_name,
                        observed="blank",
                        expected="nonblank analytical key",
                        message="An input row has a missing analytical key.",
                    )
                )
        for field_name in config.provenance_fields:
            if _blank(row.get(field_name)):
                flags.append(
                    make_flag(
                        config,
                        "missing_provenance",
                        "blocking",
                        row=row,
                        field_name=field_name,
                        observed="blank",
                        expected="nonblank provenance",
                        message=f"Required provenance field {field_name} is blank.",
                    )
                )
        for field_name in config.identity_valid_fields:
            if not _truthy(row.get(field_name)):
                flags.append(
                    make_flag(
                        config,
                        "invalid_identity",
                        "blocking",
                        row=row,
                        field_name=field_name,
                        observed=row.get(field_name, ""),
                        expected="valid identity",
                        message=f"Configured identity validity field {field_name} is not valid.",
                    )
                )
        extraction_status = str(row.get("extraction_status", row.get("status", ""))).strip().lower()
        if extraction_status in {"error", "failed", "missing", "unusable"}:
            flags.append(
                make_flag(
                    config,
                    "extraction_failure",
                    "blocking",
                    row=row,
                    field_name="extraction_status",
                    observed=extraction_status,
                    expected="usable extraction or reviewed disposition",
                    message="A failed extraction reached the analytical input.",
                )
            )
        final_type = str(row.get("final_type", "")).strip().lower()
        classification = str(row.get("final_classification", row.get("classification", final_type))).strip().lower()
        if classification in {"unreviewed", "unresolved", "flagged", "stale", "missing"}:
            flags.append(
                make_flag(
                    config,
                    "unresolved_page",
                    "blocking",
                    row=row,
                    field_name="final_classification",
                    observed=classification,
                    expected="accepted or excluded by reviewed decision",
                    message="An unresolved page reached the analytical input.",
                )
            )
        if final_type and final_type != "selected":
            flags.append(
                make_flag(
                    config,
                    "invalid_page_selection",
                    "blocking",
                    row=row,
                    field_name="final_type",
                    observed=final_type,
                    expected="selected",
                    message="A page not selected by the fail-closed review gate reached the analytical input.",
                )
            )
        for field_name, (lower, upper) in config.bounds.items():
            raw_value = row.get(field_name)
            value = _float(raw_value)
            if _blank(raw_value):
                continue
            if value is None:
                flags.append(
                    make_flag(
                        config,
                        "nonnumeric_bounded_value",
                        "blocking",
                        row=row,
                        field_name=field_name,
                        observed=raw_value,
                        expected="numeric value within configured bounds",
                        message=f"{field_name} cannot be checked against its numeric bounds.",
                    )
                )
            elif (lower is not None and value < lower) or (upper is not None and value > upper):
                flags.append(
                    make_flag(
                        config,
                        "out_of_bounds",
                        "blocking",
                        row=row,
                        field_name=field_name,
                        observed=raw_value,
                        expected=f"[{lower if lower is not None else '-inf'}, {upper if upper is not None else 'inf'}]",
                        message=f"{field_name} lies outside a project-configured bound.",
                    )
                )
        key = tuple(str(row.get(field_name, "")) for field_name in config.key_fields)
        groups.setdefault(key, []).append(row)

    for key_tuple, duplicate_rows in groups.items():
        if len(duplicate_rows) < 2:
            continue
        values = {
            tuple(str(row.get(field_name, "")) for field_name in config.value_fields)
            for row in duplicate_rows
        }
        check_type = "repeated_vintage_disagreement" if len(values) > 1 else "duplicate_key"
        flags.append(
            make_flag(
                config,
                check_type,
                "blocking",
                key=dict(zip(config.key_fields, key_tuple, strict=True)),
                observed=canonical_json([dict(row) for row in duplicate_rows]),
                expected="one reconciled observation per analytical key",
                message=(
                    "Repeated source vintages disagree and require deterministic, evidence-based reconciliation."
                    if len(values) > 1
                    else "The final analytical key is repeated."
                ),
            )
        )
    return flags


def panel_checks(
    rows: Sequence[Mapping[str, Any]], config: QCConfig
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if config.dataset_shape != "panel" or not config.time_field:
        return [], []
    flags: list[dict[str, str]] = []
    coverage: list[dict[str, str]] = []
    by_entity: dict[str, list[Mapping[str, Any]]] = {}
    by_time: dict[str, set[str]] = {}
    time_numbers: dict[str, float] = {}
    for row in rows:
        entity = _entity(row, config)
        time_text = str(row.get(config.time_field, ""))
        time_number = _time_number(time_text)
        if time_number is None:
            flags.append(
                make_flag(
                    config,
                    "noncanonical_time",
                    "blocking",
                    row=row,
                    field_name=config.time_field,
                    observed=time_text,
                    expected="numeric or ISO date/time value",
                    message="The configured panel time cannot be ordered deterministically.",
                )
            )
            continue
        time_numbers[time_text] = time_number
        by_entity.setdefault(entity, []).append(row)
        by_time.setdefault(time_text, set()).add(entity)

    ordered_times = sorted(by_time, key=lambda item: (time_numbers[item], item))
    expected_times = list(config.expected_times)
    if not expected_times and ordered_times:
        expected_times = _inferred_time_spine(ordered_times, time_numbers, config.expected_frequency)

    for time_text in expected_times:
        count = len(by_time.get(time_text, set()))
        coverage.append(
            {
                "period": time_text,
                "entity_count": str(count),
                "is_observed": "1" if count else "0",
            }
        )
        if not count:
            flags.append(
                make_flag(
                    config,
                    "zero_coverage_period",
                    "blocking",
                    key={config.time_field: time_text},
                    observed=0,
                    expected="> 0 entities or a reviewed expected gap",
                    message="The explicit analytical time spine has a zero-coverage period.",
                    locator={"period": time_text},
                )
            )

    for previous_time, current_time in zip(ordered_times, ordered_times[1:], strict=False):
        previous_entities, current_entities = by_time[previous_time], by_time[current_time]
        previous_count, current_count = len(previous_entities), len(current_entities)
        ratio = min(previous_count, current_count) / max(previous_count, current_count) if max(previous_count, current_count) else 1.0
        if ratio < config.coverage_ratio:
            flags.append(
                make_flag(
                    config,
                    "coverage_change",
                    "advisory",
                    key={config.time_field: current_time},
                    observed=f"{previous_count}->{current_count} ({ratio:.4f})",
                    expected=f"adjacent coverage ratio >= {config.coverage_ratio:g}",
                    message="Panel coverage changes abruptly between adjacent observed periods.",
                    locator={"period": current_time, "previous_period": previous_time},
                )
            )
        entries = sorted(current_entities - previous_entities)
        exits = sorted(previous_entities - current_entities)
        if entries or exits:
            flags.append(
                make_flag(
                    config,
                    "panel_entries_exits",
                    "advisory",
                    key={config.time_field: current_time},
                    observed=canonical_json({"entries": entries, "exits": exits}),
                    expected="review reporting-panel composition changes",
                    message="Entities enter or exit the reporting panel; this is not automatically an error.",
                    locator={"period": current_time, "previous_period": previous_time},
                )
            )

    for entity, entity_rows in by_entity.items():
        ordered = sorted(entity_rows, key=lambda row: (_time_number(row.get(config.time_field)) or -math.inf, str(row.get(config.time_field, ""))))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_time = _time_number(previous.get(config.time_field))
            current_time = _time_number(current.get(config.time_field))
            if previous_time is not None and current_time is not None and current_time - previous_time > config.expected_frequency + 1e-9:
                flags.append(
                    make_flag(
                        config,
                        "panel_gap",
                        "advisory",
                        row=current,
                        field_name=config.time_field,
                        observed=f"{previous.get(config.time_field)} -> {current.get(config.time_field)}",
                        expected=f"step of {config.expected_frequency:g}",
                        message="An entity has an interior panel gap requiring historical interpretation.",
                        locator={"entity_id": entity, "previous_period": previous.get(config.time_field)},
                    )
                )
            for field_name in config.value_fields:
                previous_value = _float(previous.get(field_name))
                current_value = _float(current.get(field_name))
                if previous_value is not None and current_value is not None and _ratio(previous_value, current_value) >= config.one_sided_ratio:
                    flags.append(
                        make_flag(
                            config,
                            "one_sided_jump",
                            "advisory",
                            row=current,
                            field_name=field_name,
                            observed=f"{previous_value:g}->{current_value:g}",
                            expected=f"adjacent ratio < {config.one_sided_ratio:g} or source verification",
                            message="Adjacent values have a large one-sided change.",
                            locator={"entity_id": entity, "previous_period": previous.get(config.time_field)},
                        )
                    )

        for index in range(1, len(ordered) - 1):
            previous, current, following = ordered[index - 1 : index + 2]
            times = [_time_number(row.get(config.time_field)) for row in (previous, current, following)]
            if any(value is None for value in times):
                continue
            if abs(times[1] - times[0] - config.expected_frequency) > 1e-9 or abs(times[2] - times[1] - config.expected_frequency) > 1e-9:
                continue
            for field_name in config.value_fields:
                values = [_float(row.get(field_name)) for row in (previous, current, following)]
                if any(value is None for value in values):
                    continue
                prior, middle, after = (float(value) for value in values)
                baseline = median((prior, after))
                if _ratio(prior, after) <= config.return_ratio and _ratio(middle, baseline) >= config.isolated_ratio:
                    flags.append(
                        make_flag(
                            config,
                            "isolated_reversal",
                            "advisory",
                            row=current,
                            field_name=field_name,
                            observed=f"{prior:g}->{middle:g}->{after:g}",
                            expected=(
                                f"middle/neighbor ratio < {config.isolated_ratio:g}, neighbor ratio > {config.return_ratio:g}, "
                                "or source verification"
                            ),
                            message="A value moves sharply away from similar neighbors and immediately returns.",
                            locator={"entity_id": entity},
                        )
                    )

        window = config.persistent_window
        for boundary in range(window, len(ordered) - window + 1):
            before = ordered[boundary - window : boundary]
            after = ordered[boundary : boundary + window]
            if len(after) < window:
                continue
            combined_times = [_time_number(row.get(config.time_field)) for row in before + after]
            if any(value is None for value in combined_times):
                continue
            if any(
                abs(second - first - config.expected_frequency) > 1e-9
                for first, second in zip(combined_times, combined_times[1:], strict=False)
            ):
                continue
            for field_name in config.value_fields:
                before_values = [_float(row.get(field_name)) for row in before]
                after_values = [_float(row.get(field_name)) for row in after]
                if any(value is None for value in before_values + after_values):
                    continue
                before_median, after_median = median(before_values), median(after_values)
                if _ratio(before_median, after_median) >= config.persistent_ratio:
                    flags.append(
                        make_flag(
                            config,
                            "persistent_shift",
                            "advisory",
                            row=after[0],
                            field_name=field_name,
                            observed=f"median {before_median:g}->{after_median:g}",
                            expected=f"window-median ratio < {config.persistent_ratio:g} or source verification",
                            message="The entity has a persistent level shift across adjacent windows.",
                            locator={"entity_id": entity, "window": window},
                        )
                    )
    return flags, coverage


def cross_section_checks(rows: Sequence[Mapping[str, Any]], config: QCConfig) -> list[dict[str, str]]:
    if config.dataset_shape != "cross-section":
        return []
    flags: list[dict[str, str]] = []
    group_fields = config.cross_group_fields
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        group = tuple(str(row.get(field_name, "")) for field_name in group_fields)
        groups.setdefault(group, []).append(row)

    for group, group_rows in groups.items():
        for field_name in config.value_fields:
            valued_rows = [(row, _float(row.get(field_name))) for row in group_rows]
            valued_rows = [(row, value) for row, value in valued_rows if value is not None]
            values = [value for _, value in valued_rows]
            if len(values) >= 5:
                center = median(values)
                absolute_deviations = [abs(value - center) for value in values]
                mad = median(absolute_deviations)
                for row, value in valued_rows:
                    robust_z = math.inf if mad == 0 and value != center else (0.0 if mad == 0 else 0.67448975 * abs(value - center) / mad)
                    ratio_signal = _ratio(value, center) if center != 0 or value != 0 else 1.0
                    if robust_z >= config.robust_z or (mad == 0 and ratio_signal >= config.cross_outlier_ratio):
                        flags.append(
                            make_flag(
                                config,
                                "robust_cross_section_outlier",
                                "advisory",
                                row=row,
                                field_name=field_name,
                                observed=f"value={value:g}; median={center:g}; robust_z={robust_z:g}",
                                expected=f"robust z < {config.robust_z:g} or source verification",
                                message="A value is extreme relative to its configured comparison group.",
                                locator={"group": group},
                            )
                        )
            integer_values = [int(round(value)) for value in values if abs(value - round(value)) < 1e-9]
            if len(integer_values) >= config.heaping_minimum:
                heaped = sum(value % config.heaping_base == 0 for value in integer_values)
                share = heaped / len(integer_values)
                if share >= config.heaping_share:
                    flags.append(
                        make_flag(
                            config,
                            "heaping",
                            "advisory",
                            key={field: value for field, value in zip(group_fields, group, strict=True)},
                            field_name=field_name,
                            observed=f"{heaped}/{len(integer_values)} ({share:.4f}) divisible by {config.heaping_base}",
                            expected=f"share < {config.heaping_share:g} or documented source convention",
                            message="Values show unusually concentrated terminal digits.",
                            locator={"group": group},
                        )
                    )

    for row in rows:
        for rule in config.accounting_rules:
            total = _float(row.get(rule.total))
            components = [_float(row.get(field_name)) for field_name in rule.components]
            if total is None or any(value is None for value in components):
                continue
            component_sum = sum(float(value) for value in components)
            if abs(total - component_sum) > rule.tolerance:
                flags.append(
                    make_flag(
                        config,
                        "accounting_inconsistency",
                        "advisory",
                        row=row,
                        field_name=rule.total,
                        observed=f"{total:g} vs component sum {component_sum:g}",
                        expected=f"absolute difference <= {rule.tolerance:g}",
                        message=f"Configured accounting identity {rule.name} does not balance.",
                        locator={"accounting_rule": rule.name},
                    )
                )
    return flags


def cluster_checks(flags: Sequence[Mapping[str, str]], config: QCConfig) -> list[dict[str, str]]:
    by_page: dict[str, list[Mapping[str, str]]] = {}
    value_checks = {
        "out_of_bounds",
        "isolated_reversal",
        "one_sided_jump",
        "persistent_shift",
        "robust_cross_section_outlier",
        "accounting_inconsistency",
    }
    for flag in flags:
        page_id = flag.get("page_id", "")
        if page_id and flag.get("check_type") in value_checks:
            by_page.setdefault(page_id, []).append(flag)
    clustered: list[dict[str, str]] = []
    for page_id, page_flags in by_page.items():
        if len(page_flags) < config.cluster_minimum:
            continue
        clustered.append(
            make_flag(
                config,
                "clustered_value_anomalies",
                "blocking",
                key={config.source_page_field: page_id},
                observed=canonical_json(sorted(flag["case_id"] for flag in page_flags)),
                expected="whole-page or whole-block review",
                message="Several value anomalies share one page and require review as a possible block-level extraction failure.",
                locator={"page_id": page_id},
            )
        )
    return clustered


def run_checks(
    rows: Sequence[Mapping[str, Any]], config: QCConfig
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    validate_columns(rows, config)
    flags = common_checks(rows, config)
    coverage: list[dict[str, str]] = []
    if config.dataset_shape == "panel":
        panel_flags, coverage = panel_checks(rows, config)
        flags.extend(panel_flags)
    else:
        flags.extend(cross_section_checks(rows, config))
    flags.extend(cluster_checks(flags, config))
    unique = {flag["case_id"]: flag for flag in flags}
    return sorted(unique.values(), key=lambda flag: (flag["severity"], flag["check_type"], flag["case_id"])), coverage


def parse_decisions(rows: Iterable[Mapping[str, Any]]) -> dict[str, Decision]:
    decisions: dict[str, Decision] = {}
    for row_number, row in enumerate(rows, start=2):
        case_id = str(row.get("case_id", "")).strip()
        disposition = str(row.get("disposition", "")).strip()
        if not case_id and not disposition:
            continue
        if not case_id:
            raise ValueError(f"decision row {row_number} has no case_id")
        if case_id in decisions:
            raise ValueError(f"decision case_id {case_id} is repeated")
        if disposition not in ALLOWED_DISPOSITIONS:
            raise ValueError(f"decision {case_id} has invalid disposition {disposition!r}")
        decisions[case_id] = Decision(
            case_id=case_id,
            expected_evidence_hash=str(row.get("expected_evidence_hash", row.get("evidence_hash", ""))).strip(),
            disposition=disposition,
            reason=str(row.get("reason", "")).strip(),
            evidence_page=str(row.get("evidence_page", "")).strip(),
            correction_id=str(row.get("correction_id", "")).strip(),
        )
    return decisions


def adjudicate_flags(
    flags: Sequence[Mapping[str, str]],
    decisions: Mapping[str, Decision],
    config: QCConfig,
    *,
    applied_corrections: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    adjudicated: list[dict[str, str]] = []
    extra_flags: list[dict[str, str]] = []
    current_ids = {flag["case_id"] for flag in flags}
    for input_flag in flags:
        flag = dict(input_flag)
        decision = decisions.get(flag["case_id"])
        if decision is None:
            adjudicated.append(flag)
            continue
        valid = True
        problem = ""
        if decision.expected_evidence_hash != flag["evidence_hash"]:
            valid = False
            problem = "The decision evidence hash no longer matches the detected case."
        elif decision.disposition != "open" and not decision.reason:
            valid = False
            problem = "A non-open decision must include a reason."
        elif decision.disposition in {"corrected", "resolved", "excluded", "source_verified"} and not decision.evidence_page:
            valid = False
            problem = "This disposition requires a primary-source evidence page."
        elif decision.disposition == "corrected" and not decision.correction_id:
            valid = False
            problem = "A corrected case must identify its keyed correction overlay."
        elif decision.disposition == "corrected" and applied_corrections is not None:
            correction = applied_corrections.get(decision.correction_id)
            if correction is None:
                valid = False
                problem = "The corrected case does not identify a correction in the current applied-differences ledger."
            else:
                expected_lineage = {
                    "record_id": flag.get("record_id", ""),
                    "field": flag.get("field", ""),
                    "source_hash": flag.get("source_sha256", ""),
                    "contract_signature": flag.get("contract_signature", ""),
                    "after": flag.get("current_value", ""),
                }
                mismatches = [
                    field_name
                    for field_name, expected_value in expected_lineage.items()
                    if expected_value and str(correction.get(field_name, "")) != expected_value
                ]
                if mismatches:
                    valid = False
                    problem = f"The corrected case points to a different correction target: {', '.join(mismatches)}."
        if not valid:
            extra_flags.append(
                make_flag(
                    config,
                    "stale_qc_decision",
                    "blocking",
                    key={"case_id": flag["case_id"]},
                    observed=canonical_json(decision.__dict__),
                    expected=flag["evidence_hash"],
                    message=problem,
                    locator={"case_id": flag["case_id"]},
                )
            )
            adjudicated.append(flag)
            continue
        flag.update(
            {
                "disposition": decision.disposition,
                "decision_status": "reviewed" if decision.disposition in RESOLVED_DISPOSITIONS else "open",
                "decision_reason": decision.reason,
                "decision_evidence_page": decision.evidence_page,
                "correction_id": decision.correction_id,
            }
        )
        adjudicated.append(flag)

    for case_id, decision in decisions.items():
        if case_id in current_ids or decision.disposition != "open":
            continue
        extra_flags.append(
            make_flag(
                config,
                "orphaned_open_decision",
                "blocking",
                key={"case_id": case_id},
                observed="open decision for a case absent from the current detection run",
                expected="durable non-open adjudication; disappearance is not resolution",
                message="A previously detected case disappeared without a durable disposition.",
                locator={"case_id": case_id},
            )
        )
    adjudicated.extend(extra_flags)
    return sorted(adjudicated, key=lambda flag: (flag["severity"], flag["check_type"], flag["case_id"])), extra_flags


def release_status(flags: Sequence[Mapping[str, str]], config: QCConfig) -> tuple[str, list[str]]:
    blockers = sorted(
        flag["case_id"]
        for flag in flags
        if flag.get("severity") in config.blocking_severities and flag.get("decision_status", "open") != "reviewed"
    )
    return ("fail" if blockers else "pass"), blockers
