#!/usr/bin/env python3
"""Plan a bounded alternate extraction; never call a model or promote results."""

import argparse
import csv
import json
import math
import os
import tempfile
import tomllib
from collections.abc import Iterable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def resolve_path(root: Path, configured: Any, default: str) -> Path:
    candidate = Path(str(configured or default)).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def read_queue(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle, delimiter="\t")]
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        case_id = row.get("case_id", "")
        if case_id:
            result[case_id] = row
    return result


def unique_in_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def segment_count(page_height: int, band_height: int, overlap: int) -> int:
    if page_height <= 0 or band_height <= 0:
        raise ValueError("page and band heights must be positive")
    if not 0 < overlap < band_height:
        raise ValueError("band overlap must be positive and smaller than band height")
    if page_height <= band_height:
        return 1
    return 1 + math.ceil((page_height - band_height) / (band_height - overlap))


def build_plan(
    *,
    project_slug: str,
    page_ids: list[str],
    case_ids: list[str],
    configured: Mapping[str, Any],
    segmented: bool,
    page_height: int | None,
    request_ceiling: int | None,
    header_height: int | None = None,
    anchor_fields: Iterable[str] = (),
) -> dict[str, Any]:
    hard_max_pages = int(configured.get("max_pages", 25))
    hard_max_requests = int(configured.get("max_requests", 75))
    if hard_max_pages < 1 or hard_max_requests < 1:
        raise ValueError("configured alternate page and request ceilings must be positive")
    if not page_ids:
        raise ValueError("at least one explicit --page-id or case with a page_id is required")
    if len(page_ids) > hard_max_pages:
        raise ValueError(f"alternate plan has {len(page_ids)} pages; configured hard ceiling is {hard_max_pages}")

    dpi = int(configured.get("dpi", 400))
    if dpi < 1:
        raise ValueError("alternate extraction DPI must be positive")
    band_height = int(configured.get("band_height", 1800))
    overlap = int(configured.get("band_overlap", 300))
    if isinstance(anchor_fields, str):
        anchor_fields = anchor_fields.replace(",", " ").split()
    normalized_anchors = unique_in_order(str(field).strip() for field in anchor_fields)
    if not normalized_anchors:
        raise ValueError("alternate extraction requires at least one stable record anchor field")
    effective_header_height = header_height if header_height is not None else overlap
    if segmented and not 0 < effective_header_height <= band_height:
        raise ValueError("header height must be positive and no larger than band height")
    per_page = segment_count(page_height or 0, band_height, overlap) if segmented else 1
    requests = len(page_ids) * per_page
    effective_ceiling = hard_max_requests if request_ceiling is None else request_ceiling
    if effective_ceiling < 0:
        raise ValueError("--max-requests must be nonnegative")
    if effective_ceiling > hard_max_requests:
        raise ValueError("--max-requests may lower but may not exceed the configured hard ceiling")
    if requests > effective_ceiling:
        raise ValueError(f"alternate plan needs {requests} requests; effective ceiling is {effective_ceiling}")

    contract = {
        "namespace_version": "alternate-extraction-v1",
        "project_slug": project_slug,
        "page_ids": page_ids,
        "case_ids": case_ids,
        "service_mode": "standard",
        "reasoning": "high",
        "dpi": dpi,
        "segmented": segmented,
        "page_height": page_height,
        "band_height": band_height if segmented else None,
        "band_overlap": overlap if segmented else None,
        "header_height": effective_header_height if segmented else None,
        # Anchors are also required for full-page candidate rows so that every
        # alternate result has a stable identity before a human compares it
        # with the baseline extraction.
        "overlap_anchor_fields": normalized_anchors,
        "requests": requests,
    }
    signature = sha256(canonical_json(contract).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "plan_signature": signature,
        "cache_namespace": f"alternate/{signature}",
        "candidate_only": True,
        "automatic_promotion": False,
        "promotion_policy": "human evidence review plus keyed correction overlay",
        "hard_max_pages": hard_max_pages,
        "hard_max_requests": hard_max_requests,
        "effective_request_ceiling": effective_ceiling,
        **contract,
    }


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--page-id", action="append", default=[], help="Explicit page identity; repeat as needed")
    parser.add_argument("--case-id", action="append", default=[], help="Explicit QC case identity; repeat as needed")
    parser.add_argument("--queue-tsv", type=Path, help="Generated review queue used only to resolve explicit case IDs")
    parser.add_argument("--segmented", action="store_true", help="Plan repeated-header overlapping bands")
    parser.add_argument("--page-height-px", type=int, help="Rendered page height; required with --segmented")
    parser.add_argument("--header-height-px", type=int, help="Top-of-page header context repeated with each later band")
    parser.add_argument("--max-requests", type=int, help="Optional ceiling no larger than project.toml")
    parser.add_argument("--write-plan", action="store_true", help="Write metadata only; this still does not call a model")
    parser.add_argument("--output", type=Path, help="Plan destination used with --write-plan")
    arguments = parser.parse_args()
    if arguments.segmented and arguments.page_height_px is None:
        parser.error("--page-height-px is required with --segmented")
    if not arguments.page_id and not arguments.case_id:
        parser.error("provide at least one explicit --page-id or --case-id")
    if arguments.write_plan != (arguments.output is not None):
        parser.error("--write-plan and --output must be supplied together")

    root = arguments.root.resolve()
    raw = tomllib.loads((root / "project.toml").read_text(encoding="utf-8"))
    project = raw.get("project", {})
    quality = raw.get("quality", {})
    alternate = raw.get("alternate_extraction", {})
    project_slug = str(project.get("slug", project.get("name", root.name)))
    dataset = raw.get("dataset", {})
    source_page_field = str(dataset.get("source_page_field", "page_id"))
    anchor_fields = alternate.get("overlap_anchor_fields")
    if not anchor_fields:
        anchor_fields = dataset.get("keys")
    if not anchor_fields:
        entity_keys = dataset.get("entity_keys", ())
        if isinstance(entity_keys, str):
            entity_keys = [part for part in entity_keys.replace(",", " ").split() if part]
        time_key = str(dataset.get("time_key", "")).strip()
        anchor_fields = [*entity_keys, *([time_key] if time_key else [])]
    if not anchor_fields:
        anchor_fields = ["entity_raw", "period_raw"]

    page_ids = list(arguments.page_id)
    if arguments.case_id:
        queue_path = arguments.queue_tsv or resolve_path(
            root,
            quality.get("output_directory"),
            "output/quality-control",
        ) / "review_queue.tsv"
        queue = read_queue(queue_path.resolve())
        for case_id in arguments.case_id:
            if case_id not in queue:
                raise ValueError(f"explicit case {case_id} is absent from the current generated queue")
            page_id = queue[case_id].get(source_page_field, queue[case_id].get("page_id", ""))
            if not page_id:
                raise ValueError(f"case {case_id} has no page identity and cannot authorize a page request")
            page_ids.append(page_id)
    plan = build_plan(
        project_slug=project_slug,
        page_ids=unique_in_order(page_ids),
        case_ids=unique_in_order(arguments.case_id),
        configured=alternate,
        segmented=arguments.segmented,
        page_height=arguments.page_height_px,
        request_ceiling=arguments.max_requests,
        header_height=arguments.header_height_px,
        anchor_fields=anchor_fields,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    if arguments.write_plan:
        write_json_atomic(arguments.output.resolve(), plan)
        print(f"Wrote candidate-only alternate plan {arguments.output.resolve()}")
    else:
        print("Dry run only: no metadata was written and no model request was made.")


if __name__ == "__main__":
    main()
