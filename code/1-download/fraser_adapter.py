#!/usr/bin/env -S uv run
"""List bounded FRASER title metadata and export source-manifest rows."""

from __future__ import annotations

import argparse
import re
import tomllib
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import acquisition
import download_sources
import fraser

from histdata_pipeline.config import load_project_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--title-slug", help="FRASER title slug, including its numeric suffix (default: project.toml).")
    result.add_argument("--metadata-jsonl", type=Path)
    result.add_argument("--output-manifest", type=Path)
    result.add_argument("--source-id-prefix", default="fraser")
    result.add_argument("--browse-decade", help="FRASER browse value used to load the title catalog.")
    result.add_argument("--start-date", type=date.fromisoformat, help="Inclusive MODS sort-date boundary (YYYY-MM-DD).")
    result.add_argument("--end-date", type=date.fromisoformat, help="Inclusive MODS sort-date boundary (YYYY-MM-DD).")
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Authorize metadata calls for every catalog item.")
    selection.add_argument("--limit", type=int, help="Bounded metadata/download count (default: project sample size).")
    result.add_argument("--dry-run", action="store_true", help="Preview catalog scope without writing metadata or downloading PDFs.")
    result.add_argument(
        "--download",
        action="store_true",
        help="After writing and inspecting the manifest, explicitly acquire the same bounded FRASER cohort.",
    )
    return result


def project_defaults() -> dict[str, object]:
    config = PROJECT_ROOT / "project.toml"
    payload = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    fraser_config = payload.get("fraser", {})
    source_config = payload.get("source", {})
    storage = payload.get("storage", {})
    if not all(isinstance(value, dict) for value in (fraser_config, source_config, storage)):
        raise ValueError("project.toml [fraser], [source], and [storage] must be tables")
    external = Path(str(storage.get("external_data_root", f"/home/sergio/data/{PROJECT_ROOT.name}")))
    return {
        "limit": int(source_config.get("sample_size", 3)),
        "metadata": external / "acquisition" / "fraser_items.jsonl",
        "output": PROJECT_ROOT / str(source_config.get("manifest", "sources/source_manifest.tsv")),
        "browse_decade": str(fraser_config.get("browse_decade", "")),
        "start_date": str(fraser_config.get("start_date", "")),
        "end_date": str(fraser_config.get("end_date", "")),
        "title_slug": str(fraser_config.get("title_slug", "")),
    }


def merge_records(
    existing: list[acquisition.SourceRecord],
    generated: list[acquisition.SourceRecord],
) -> list[acquisition.SourceRecord]:
    """Update prior FRASER identities and append new ones without disturbing other sources."""

    by_id = {record.source_id: record for record in existing}
    next_order = max((record.source_order for record in existing), default=0) + 1
    for record in generated:
        prior = by_id.get(record.source_id)
        if prior is not None:
            if (prior.provider_id, prior.filename) != (record.provider_id, record.filename):
                raise ValueError(f"FRASER identity drift for existing source {record.source_id}")
            by_id[record.source_id] = replace(
                record,
                source_order=prior.source_order,
                expected_sha256=prior.expected_sha256,
                min_pages=prior.min_pages,
                max_pages=prior.max_pages,
                notes=prior.notes or record.notes,
            )
        else:
            by_id[record.source_id] = replace(record, source_order=next_order)
            next_order += 1
    merged = sorted(by_id.values(), key=lambda record: record.source_order)
    filenames = [record.filename.casefold() for record in merged]
    if len(filenames) != len(set(filenames)):
        raise ValueError("FRASER metadata would create a source filename collision")
    return merged


def catalog_in_date_window(item: fraser.CatalogItem, start: date | None, end: date | None) -> bool:
    """Use FRASER's decade only as a broad prefilter before MODS is fetched."""

    if item.decade is None:
        return True
    return not ((start is not None and item.decade + 9 < start.year) or (end is not None and item.decade > end.year))


def metadata_in_date_window(metadata: fraser.ItemMetadata, start: date | None, end: date | None) -> bool:
    source_date = acquisition.canonical_source_date(
        metadata.sort_date,
        context=f"FRASER item {metadata.item_id} sortDate",
    )
    if not source_date:
        return start is None and end is None
    value = date.fromisoformat(source_date)
    return not ((start is not None and value < start) or (end is not None and value > end))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_project_config(PROJECT_ROOT)
    defaults = project_defaults()
    limit = args.limit or int(defaults["limit"])
    if limit < 1:
        parser().error("--limit must be positive")
    title_slug = args.title_slug or str(defaults["title_slug"])
    if not title_slug or re.fullmatch(r"[a-z0-9-]+", title_slug) is None:
        parser().error("Set a valid fraser.title_slug in project.toml or pass --title-slug")
    metadata_path = config.checked_write_path(args.metadata_jsonl or cast_path(defaults["metadata"]))
    output_manifest = config.checked_write_path(args.output_manifest or cast_path(defaults["output"]))
    if args.download:
        download_args = download_sources.parser().parse_args(["--manifest", str(output_manifest)])
        download_sources.validated_write_destinations(download_args, config, download_sources.project_defaults(config.root))
    start = args.start_date or optional_date(defaults["start_date"], "fraser.start_date")
    end = args.end_date or optional_date(defaults["end_date"], "fraser.end_date")
    if start is not None and end is not None and end < start:
        parser().error("FRASER end date precedes start date")
    browse_decade = args.browse_decade or str(defaults["browse_decade"]) or (f"{start.year // 10 * 10}s" if start else "all")
    session = acquisition.build_session()
    title_url = f"{fraser.BASE_URL}/title/{title_slug}"
    response = session.get(title_url, params={"browse": browse_decade}, timeout=(15, 120))
    response.raise_for_status()
    catalog = fraser.parse_catalog(response.text)
    candidates = [item for item in catalog if catalog_in_date_window(item, start, end)]
    cached = fraser.load_metadata(metadata_path)
    missing = [item for item in candidates if item.item_id not in cached]
    selected = missing if args.all else missing[:limit]
    print(
        f"Catalog: {len(catalog)} total, {len(candidates)} near the date window; "
        f"cached metadata: {len(cached)}; selected: {len(selected)}"
    )
    if args.dry_run:
        print(f"Would cache metadata at {metadata_path} and merge sources into {output_manifest}")
        return 0
    for position, item in enumerate(selected, 1):
        response = session.get(fraser.METADATA_URL, params={"type": "item", "id": item.item_id}, timeout=(15, 60))
        response.raise_for_status()
        metadata = fraser.parse_item_metadata(response.text, item)
        fraser.append_metadata(metadata_path, metadata)
        cached[item.item_id] = metadata
        print(f"[{position}/{len(selected)}] {item.item_id}: {'downloadable' if metadata.pdf_url else 'no PDF URL'}")

    generated = [
        fraser.to_source_record(cached[item.item_id], source_order=position, source_id_prefix=args.source_id_prefix)
        for position, item in enumerate(candidates, 1)
        if item.item_id in cached
        and cached[item.item_id].pdf_url
        and metadata_in_date_window(cached[item.item_id], start, end)
    ]
    existing = acquisition.load_sources(output_manifest, allow_empty=True) if output_manifest.exists() else []
    rows = merge_records(existing, generated)
    acquisition.validate_source_records(rows)
    acquisition.atomic_write_tsv(
        output_manifest,
        acquisition.SOURCE_FIELDS,
        ({**asdict(row), "max_pages": "" if row.max_pages is None else row.max_pages} for row in rows),
    )
    # Parse the persisted file before any download so URL/path/collision guards
    # are exercised on exactly the bytes future runs will use.
    acquisition.load_sources(output_manifest)
    print(f"Manifest rows: {len(rows)} -> {output_manifest}")
    if not args.download:
        print("Metadata only: inspect the manifest, then run download_sources.py on an explicit bounded cohort.")
        return 0
    downloadable = [row for row in generated if row.source_id in {value.source_id for value in rows}]
    selection = downloadable if args.all else downloadable[:limit]
    if not selection:
        print("No downloadable FRASER records are currently available.")
        return 1
    download_args = ["--manifest", str(output_manifest)]
    for row in selection:
        download_args.extend(("--source-id", row.source_id))
    return download_sources.main(download_args)


def cast_path(value: object) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"Expected Path default, got {type(value).__name__}")
    return value


def optional_date(value: object, label: str) -> date | None:
    raw = str(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"Invalid {label}: {raw!r}") from error


if __name__ == "__main__":
    raise SystemExit(main())
