#!/usr/bin/env -S uv run
"""Acquire or validate a bounded cohort from ``sources/source_manifest.tsv``."""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import acquisition

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=PROJECT_ROOT / "sources" / "source_manifest.tsv")
    result.add_argument("--pdf-root", type=Path)
    result.add_argument("--external-data-root", type=Path)
    result.add_argument("--inventory", type=Path)
    result.add_argument("--run-root", type=Path)
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Authorize every manifest record.")
    selection.add_argument("--limit", type=int, help="Bounded record count (default: project sample size).")
    selection.add_argument("--source-id", action="append", default=[], help="Select an explicit source; repeatable.")
    result.add_argument(
        "--inventory-only",
        action="store_true",
        help="Audit existing manual/direct files without making network requests; defaults to all manifest rows.",
    )
    result.add_argument("--dry-run", action="store_true", help="Validate paths and print the cohort without writing or downloading.")
    return result


def project_defaults() -> dict[str, object]:
    """Read storage and source defaults while keeping every CLI value overridable."""

    config_path = PROJECT_ROOT / "project.toml"
    payload = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    storage = payload.get("storage", {})
    source = payload.get("source", {})
    if not isinstance(storage, dict) or not isinstance(source, dict):
        raise ValueError("project.toml [storage] and [source] must be tables")
    external_root = Path(str(storage.get("external_data_root", f"/home/sergio/data/{PROJECT_ROOT.name}"))).expanduser()
    if not external_root.is_absolute():
        raise ValueError("storage.external_data_root must be absolute")
    external_root = external_root.resolve()

    def external_child(setting: str, default: str) -> Path:
        configured = Path(str(storage.get(setting, default))).expanduser()
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        path = (external_root / configured).resolve()
        if not path.is_relative_to(external_root):
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
        "external_root": external_root,
        "external_pdf_subdirectory": str(storage.get("external_pdf_subdirectory", "pdfs")),
        "inventory": inventory,
        "limit": int(source.get("sample_size", 3)),
        "pdf_root": pdf_root,
        "run_root": external_child("acquisition_run_subdirectory", "acquisition-runs"),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    defaults = project_defaults()
    records = acquisition.load_sources(args.manifest)
    all_sources = args.all or (args.inventory_only and args.limit is None and not args.source_id)
    selected = acquisition.select_sources(
        records,
        all_sources=all_sources,
        limit=args.limit or int(defaults["limit"]),
        source_ids=args.source_id,
    )
    external_data_root = args.external_data_root or cast_path(defaults["external_root"])
    requested_pdf_root = args.pdf_root or cast_path(defaults["pdf_root"])
    external_pdf_subdirectory = Path(str(defaults["external_pdf_subdirectory"]))
    if external_pdf_subdirectory.is_absolute():
        raise ValueError("storage.external_pdf_subdirectory must be relative to external_data_root")
    external_pdf_root = (external_data_root / external_pdf_subdirectory).resolve()
    if not external_pdf_root.is_relative_to(external_data_root.resolve()):
        raise ValueError("storage.external_pdf_subdirectory escapes external_data_root")
    pdf_root = acquisition.validate_pdf_root(
        requested_pdf_root,
        project_root=PROJECT_ROOT,
        external_data_root=external_data_root,
        external_pdf_root=external_pdf_root,
    )
    inventory_path = args.inventory or cast_path(defaults["inventory"])
    run_root = args.run_root or cast_path(defaults["run_root"])

    print(f"Manifest records: {len(records)}; selected: {len(selected)}")
    print(f"PDF root: {pdf_root}")
    for record in selected:
        print(f"- {record.source_id}: {record.acquisition_method} -> {record.filename}")
    if args.dry_run:
        return 0

    pdf_root.mkdir(parents=True, exist_ok=True)
    run_dir = acquisition.create_run_snapshot(args.manifest, run_root)
    if (run_dir / "source_manifest.tsv").read_bytes() != args.manifest.read_bytes():
        raise RuntimeError("Source manifest changed while the acquisition run was starting; rerun the command")
    checked_at = datetime.now(UTC).isoformat()
    event_path = run_dir / "events.jsonl"
    session = None
    results: dict[str, acquisition.AcquisitionResult] = {}
    failures: list[dict[str, str]] = []
    for position, record in enumerate(selected, 1):
        try:
            destination = acquisition.safe_destination(pdf_root, record.filename)
            if args.inventory_only:
                if not destination.exists():
                    result = acquisition.AcquisitionResult(record, "missing", None, f"Missing {destination}")
                else:
                    result = acquisition.AcquisitionResult(record, "valid_existing", acquisition.audit_pdf(destination, record))
            else:
                if record.acquisition_method == "direct" and session is None:
                    session = acquisition.build_session()
                result = acquisition.acquire_record(record, pdf_root, session)
        except Exception as error:
            result = acquisition.AcquisitionResult(record, "failed", None, str(error))
            failures.append({"source_id": record.source_id, "error": str(error)})
        results[record.source_id] = result
        acquisition.append_event(
            event_path,
            {
                "position": position,
                "source_id": record.source_id,
                "status": result.status,
                "error": result.error,
                "audit": asdict(result.audit) if result.audit else None,
            },
        )
        print(f"[{position}/{len(selected)}] {record.source_id}: {result.status}")

    # Re-audit every already present source so the inventory is complete and ordered.
    successful: list[acquisition.AcquisitionResult] = []
    for record in records:
        result = results.get(record.source_id)
        if result is None or result.audit is None:
            destination = acquisition.safe_destination(pdf_root, record.filename)
            if destination.exists():
                try:
                    result = acquisition.AcquisitionResult(record, "valid_existing", acquisition.audit_pdf(destination, record))
                except ValueError:
                    continue
        if result is not None and result.audit is not None:
            successful.append(result)
    acquisition.atomic_write_tsv(
        inventory_path,
        acquisition.INVENTORY_FIELDS,
        (acquisition.inventory_row(result, pdf_root, checked_at=checked_at) for result in successful),
    )
    unresolved = [result.record.source_id for result in results.values() if result.audit is None]
    acquisition.write_run_summary(
        run_dir / "run.json",
        {
            "all_sources_authorized": args.all,
            "inventory_only": args.inventory_only,
            "failures": failures,
            "inventory": str(inventory_path),
            "manifest_records": len(records),
            "pdf_root": str(pdf_root),
            "selected_records": len(selected),
            "unresolved_source_ids": unresolved,
            "valid_local_sources": len(successful),
        },
    )
    print(f"Inventory: {len(successful)} valid sources -> {inventory_path}")
    if unresolved:
        print(f"Unresolved: {', '.join(unresolved)}")
        return 1
    return 0


def cast_path(value: object) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"Expected Path default, got {type(value).__name__}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
