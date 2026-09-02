#!/usr/bin/env python3
"""Merge candidate band extractions only when repeated-header overlaps agree."""

import argparse
import json
import os
import tempfile
import tomllib
from collections.abc import Iterable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} is not a JSON object")
        rows.append(value)
    return rows


def merge_segments(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_segment_counts: Mapping[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: dict[str, dict[int, list[Mapping[str, Any]]]] = {}
    for row in rows:
        page_id = str(row.get("page_id", ""))
        anchor = str(row.get("record_anchor", ""))
        if not page_id or not anchor or not isinstance(row.get("record"), Mapping):
            raise ValueError("each segment row requires page_id, record_anchor, and an object-valued record")
        try:
            segment_index = int(row["segment_index"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("each segment row requires an integer segment_index") from error
        pages.setdefault(page_id, {}).setdefault(segment_index, []).append(row)

    conflicts: list[dict[str, Any]] = []
    if expected_segment_counts is not None:
        normalized_expected = {str(page_id): int(count) for page_id, count in expected_segment_counts.items()}
        if any(not page_id or count < 1 for page_id, count in normalized_expected.items()):
            raise ValueError("expected segment counts require nonblank pages and positive counts")
        for page_id in sorted(set(pages) | set(normalized_expected)):
            expected = set(range(normalized_expected.get(page_id, 0)))
            observed = set(pages.get(page_id, {}))
            if expected != observed:
                conflicts.append(
                    {
                        "page_id": page_id,
                        "left_segment": "",
                        "right_segment": "",
                        "record_anchor": "",
                        "conflict_type": "segment_completeness",
                        "left_record": sorted(observed),
                        "right_record": sorted(expected),
                    }
                )

    merged: list[dict[str, Any]] = []
    for page_id, segments in sorted(pages.items()):
        indices = sorted(segments)
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError(f"page {page_id} has noncontiguous segment indices {indices}")
        by_anchor: dict[str, dict[str, Any]] = {}
        source_segments: dict[str, list[int]] = {}
        previous: dict[str, Mapping[str, Any]] | None = None
        for segment_index in indices:
            current_rows = segments[segment_index]
            current: dict[str, Mapping[str, Any]] = {}
            for row in current_rows:
                anchor = str(row["record_anchor"])
                if anchor in current:
                    raise ValueError(f"page {page_id} segment {segment_index} repeats anchor {anchor}")
                current[anchor] = row
            if previous is not None:
                shared = sorted(previous.keys() & current.keys())
                if not shared:
                    conflicts.append(
                        {
                            "page_id": page_id,
                            "left_segment": segment_index - 1,
                            "right_segment": segment_index,
                            "record_anchor": "",
                            "conflict_type": "missing_overlap",
                            "left_record": "",
                            "right_record": "",
                        }
                    )
                for anchor in shared:
                    left_record = previous[anchor]["record"]
                    right_record = current[anchor]["record"]
                    if canonical_json(left_record) != canonical_json(right_record):
                        conflicts.append(
                            {
                                "page_id": page_id,
                                "left_segment": segment_index - 1,
                                "right_segment": segment_index,
                                "record_anchor": anchor,
                                "conflict_type": "overlap_disagreement",
                                "left_record": left_record,
                                "right_record": right_record,
                            }
                        )
            for anchor, row in current.items():
                record = dict(row["record"])
                if anchor in by_anchor and canonical_json(by_anchor[anchor]) != canonical_json(record):
                    conflicts.append(
                        {
                            "page_id": page_id,
                            "left_segment": source_segments[anchor][-1],
                            "right_segment": segment_index,
                            "record_anchor": anchor,
                            "conflict_type": "nonadjacent_disagreement",
                            "left_record": by_anchor[anchor],
                            "right_record": record,
                        }
                    )
                else:
                    by_anchor.setdefault(anchor, record)
                    source_segments.setdefault(anchor, []).append(segment_index)
            previous = current
        for anchor, record in by_anchor.items():
            merged.append(
                {
                    "page_id": page_id,
                    "record_anchor": anchor,
                    "record": record,
                    "source_segments": source_segments[anchor],
                    "candidate_only": True,
                }
            )
    return merged, conflicts


def _atomic_path(destination: Path) -> tuple[int, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    return descriptor, Path(name)


def write_jsonl_atomic(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    descriptor, temporary = _atomic_path(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(canonical_json(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary = _atomic_path(path)
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


def validate_candidate_destination(root: Path, output: Path) -> None:
    """Keep standalone candidate merges away from final/manual/current paths."""

    root = root.resolve()
    destination = output.expanduser().resolve()
    protected_directories = (root / "data", root / "manual")
    if any(destination.is_relative_to(directory.resolve()) for directory in protected_directories):
        raise ValueError("candidate merge output may not be written beneath data/ or manual/")
    config_path = root / "project.toml"
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    storage = raw.get("storage", {})
    extraction = raw.get("extraction", {})
    external_root = Path(str(storage.get("external_data_root", ""))).expanduser()
    if not external_root.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    current = Path(str(extraction.get("current_tsv", "data-extraction/exports/current/flat.tsv")))
    if current.is_absolute():
        raise ValueError("extraction.current_tsv must be external-root-relative")
    current_path = (external_root.resolve() / current).resolve()
    if destination == current_path or destination.is_relative_to(current_path.parent):
        raise ValueError("candidate merge output may not touch the baseline current extraction")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Candidate segment-record JSONL")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root used to protect final/current paths")
    parser.add_argument("--output", type=Path, required=True, help="Candidate merged JSONL; never a current-extraction path")
    parser.add_argument("--conflicts", type=Path, required=True, help="Overlap validation report")
    parser.add_argument("--metadata", type=Path, required=True, help="Candidate-only merge metadata")
    parser.add_argument("--plan", type=Path, required=True, help="Reviewed alternate plan defining every expected segment")
    arguments = parser.parse_args()
    validate_candidate_destination(arguments.root, arguments.output)

    input_bytes = arguments.input.read_bytes()
    plan = json.loads(arguments.plan.read_text(encoding="utf-8"))
    page_ids = [str(page_id) for page_id in plan.get("page_ids", [])]
    requests = int(plan.get("requests", 0))
    if (
        not plan.get("segmented")
        or plan.get("candidate_only") is not True
        or plan.get("automatic_promotion") is not False
        or not page_ids
        or requests < 1
        or requests % len(page_ids)
    ):
        raise ValueError("--plan must be a segmented candidate plan with a uniform positive request count")
    expected = {page_id: requests // len(page_ids) for page_id in page_ids}
    merged, conflicts = merge_segments(read_jsonl(arguments.input), expected_segment_counts=expected)
    write_jsonl_atomic(arguments.conflicts, conflicts)
    metadata = {
        "schema_version": 1,
        "candidate_only": True,
        "automatic_promotion": False,
        "input_sha256": sha256(input_bytes).hexdigest(),
        "plan_signature": str(plan.get("plan_signature", "")),
        "merged_record_count": len(merged),
        "conflict_count": len(conflicts),
        "merge_status": "blocked" if conflicts else "candidate_ready_for_human_review",
    }
    write_json_atomic(arguments.metadata, metadata)
    if conflicts:
        arguments.output.unlink(missing_ok=True)
        raise SystemExit(f"Segment merge blocked by {len(conflicts)} overlap conflict(s).")
    write_jsonl_atomic(arguments.output, merged)
    print(f"Wrote {len(merged)} candidate records. No result was promoted.")


if __name__ == "__main__":
    main()
