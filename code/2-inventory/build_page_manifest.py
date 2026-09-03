#!/usr/bin/env -S uv run
"""Build the all-page inventory or gate a reviewed extraction manifest."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import page_inventory

from histdata_pipeline.config import ProjectConfig, load_project_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _slug() -> str:
    config = PROJECT_ROOT / "project.toml"
    if config.exists():
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
        project = payload.get("project", {})
        if isinstance(project, dict) and project.get("slug"):
            return str(project["slug"])
    return PROJECT_ROOT.name


def _defaults() -> dict[str, Path]:
    config_path = PROJECT_ROOT / "project.toml"
    payload = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    storage = payload.get("storage", {})
    source = payload.get("source", {})
    if not isinstance(storage, dict) or not isinstance(source, dict):
        raise ValueError("project.toml [storage] and [source] must be tables")
    external = Path(str(storage.get("external_data_root", f"/home/sergio/data/{_slug()}"))).expanduser()
    if not external.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    external = external.resolve()

    def external_child(setting: str, default: str) -> Path:
        configured = Path(str(storage.get(setting, default))).expanduser()
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        path = (external / configured).resolve()
        if not path.is_relative_to(external):
            raise ValueError(f"storage.{setting} escapes external_data_root")
        return path

    pdf_mode = str(storage.get("pdf_storage", "external"))
    if pdf_mode == "project":
        local_pdf = Path(str(storage.get("local_pdf_directory", "sources/pdfs")))
        if local_pdf.is_absolute():
            raise ValueError("storage.local_pdf_directory must be project-relative")
        pdf_root = (PROJECT_ROOT / local_pdf).resolve()
        if not pdf_root.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("storage.local_pdf_directory escapes the project root")
    elif pdf_mode == "external":
        pdf_root = external_child("external_pdf_subdirectory", "pdfs")
    else:
        raise ValueError(f"Unknown storage.pdf_storage: {pdf_mode!r}")
    inventory_relative = Path(str(source.get("inventory", "data/source_inventory.tsv")))
    if inventory_relative.is_absolute():
        raise ValueError("source.inventory must be project-relative")
    inventory = (PROJECT_ROOT / inventory_relative).resolve()
    if not inventory.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("source.inventory escapes the project root")
    return {
        "cache_root": external_child("selection_cache_subdirectory", "page-selection-cache"),
        "inventory": inventory,
        "pdf_root": pdf_root,
    }


def _paths(parser: argparse.ArgumentParser) -> None:
    defaults = _defaults()
    parser.add_argument("--source-manifest", type=Path, default=PROJECT_ROOT / "sources" / "source_manifest.tsv")
    parser.add_argument("--source-inventory", type=Path, default=defaults["inventory"])
    parser.add_argument("--pdf-root", type=Path, default=defaults["pdf_root"])
    parser.add_argument("--cache-root", type=Path, default=defaults["cache_root"])
    parser.add_argument("--source-overrides", type=Path, default=PROJECT_ROOT / "manual" / "source_overrides.tsv")
    parser.add_argument("--page-overrides", type=Path, default=PROJECT_ROOT / "manual" / "page_overrides.tsv")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build a bounded sample or the complete physical-page manifest.")
    _paths(build)
    selection = build.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Require and scan every source in the canonical manifest.")
    selection.add_argument("--limit", type=int, default=1, help="Bounded source count (default: 1).")
    selection.add_argument("--source-id", action="append", default=[])
    build.add_argument("--ocr-mode", choices=("embedded", "targeted", "full"), default="embedded")
    build.add_argument("--output", type=Path)
    build.add_argument(
        "--merge-existing",
        action="store_true",
        help="Merge this bounded rescoring cohort into an existing complete output manifest.",
    )

    gate = sub.add_parser("gate", help="Validate review completeness and write selected extraction pages.")
    _paths(gate)
    gate.add_argument("--pages", type=Path, default=PROJECT_ROOT / "data" / "pages.tsv")
    gate.add_argument("--gold", type=Path, default=PROJECT_ROOT / "manual" / "gold" / "page_selection.tsv")
    gate.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "selected_pages.tsv")
    gate.add_argument(
        "--require-complete",
        action="store_true",
        help="Compatibility flag; completeness is always required and cannot be disabled.",
    )
    return result


def build(args: argparse.Namespace, config: ProjectConfig) -> int:
    cache_root = config.checked_write_path(args.cache_root)
    if cache_root.is_relative_to(config.root.resolve()):
        raise ValueError(f"Page-selection caches must remain outside the project directory: {cache_root}")
    output = config.checked_write_path(
        args.output
        or (PROJECT_ROOT / "data" / "pages.tsv" if args.all or args.merge_existing else PROJECT_ROOT / "temp" / "pages.sample.tsv")
    )
    snapshot_inputs = [args.source_manifest, args.source_inventory, args.source_overrides, args.page_overrides]
    if args.merge_existing:
        snapshot_inputs.append(output)
    run_dir = page_inventory.create_selection_snapshot(
        snapshot_inputs,
        cache_root / "runs",
    )
    manifest_snapshot = run_dir / args.source_manifest.name
    inventory_snapshot = run_dir / args.source_inventory.name
    source_overrides_snapshot = run_dir / args.source_overrides.name
    page_overrides_snapshot = run_dir / args.page_overrides.name
    identities = page_inventory.load_source_identities(manifest_snapshot)
    inventory = page_inventory.load_inventory(inventory_snapshot)
    reconciled = page_inventory.reconcile_sources(identities, inventory, require_all=args.all)
    if args.source_id:
        requested = set(args.source_id)
        unknown = sorted(requested - {row.source_id for row in reconciled})
        if unknown:
            raise ValueError(f"Unknown or unavailable source IDs: {unknown}")
        sources = [row for row in reconciled if row.source_id in requested]
    else:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        sources = reconciled if args.all else reconciled[: args.limit]
    records = page_inventory.build_page_records(
        sources,
        args.pdf_root,
        cache_root,
        source_overrides_path=source_overrides_snapshot,
        page_overrides_path=page_overrides_snapshot,
        ocr_mode=args.ocr_mode,
        allow_out_of_scope_overrides=not args.all,
    )
    if args.merge_existing:
        existing_snapshot = run_dir / output.name
        if not existing_snapshot.exists():
            raise ValueError(f"Build the complete physical-page manifest before merging candidates: {output}")
        existing = page_inventory.load_page_records(existing_snapshot)
        records = page_inventory.merge_page_updates(existing, records)
    page_inventory.atomic_write_pages(output, records)
    print(f"Pages: {len(records)} from {len(sources)} sources -> {output}")
    print(f"Startup snapshots: {run_dir}")
    print(f"Unreviewed/flagged: {sum(row.final_type in {'unreviewed', 'flagged'} for row in records)}")
    return 0


def gate(args: argparse.Namespace, config: ProjectConfig) -> int:
    cache_root = config.checked_write_path(args.cache_root)
    if cache_root.is_relative_to(config.root.resolve()):
        raise ValueError(f"Page-selection snapshots must remain outside the project directory: {cache_root}")
    pages_path = config.checked_write_path(args.pages)
    output = config.checked_write_path(args.output)
    run_dir = page_inventory.create_selection_snapshot(
        (args.source_manifest, args.source_inventory, pages_path, args.source_overrides, args.page_overrides, args.gold),
        cache_root / "runs",
    )
    identities = page_inventory.load_source_identities(run_dir / args.source_manifest.name)
    inventory = page_inventory.load_inventory(run_dir / args.source_inventory.name)
    sources = page_inventory.reconcile_sources(identities, inventory, require_all=True)
    records = page_inventory.load_page_records(run_dir / pages_path.name)
    records = page_inventory.apply_manual_overrides(
        records,
        source_overrides_path=run_dir / args.source_overrides.name,
        page_overrides_path=run_dir / args.page_overrides.name,
    )
    page_inventory.validate_sources_current(records, sources, args.pdf_root)
    selected = page_inventory.validate_extraction_ready(
        records,
        expected_source_ids=(row.source_id for row in identities),
        gold_path=run_dir / args.gold.name,
    )
    page_inventory.atomic_write_pages(pages_path, records)
    page_inventory.atomic_write_pages(output, selected)
    print(f"Extraction gate passed: {len(selected)} selected pages -> {output}")
    print(f"Startup snapshots: {run_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_project_config(PROJECT_ROOT)
    return build(args, config) if args.command == "build" else gate(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
