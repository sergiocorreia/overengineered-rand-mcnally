#!/usr/bin/env -S uv run
"""Snapshot or finalize the read-only Rand McNally V1 recovery audit."""

import argparse
import tomllib
from pathlib import Path

import recovery_audit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--recovery-root", type=Path, help="Override restoration.recovered_v1_root from project.toml.")
    result.add_argument(
        "--migration-inventory",
        type=Path,
        default=PROJECT_ROOT / "sources" / "legacy_migration_inventory.tsv",
    )
    result.add_argument("--output", type=Path, required=True, help="Compact JSON report beneath output/.")
    result.add_argument(
        "--raw-crosswalk",
        type=Path,
        default=PROJECT_ROOT / "manual" / "raw_scan_pdf_crosswalk.tsv",
        help="Optional reviewed raw-scan to physical-PDF mapping.",
    )
    result.add_argument("--previous-snapshot", type=Path, help="Earlier report used to prove a quiet transfer interval.")
    result.add_argument("--minimum-quiet-seconds", type=int, default=60)
    result.add_argument("--finalize", action="store_true", help="Fail unless every recovery gate passes.")
    result.add_argument("--page-mapping-output", type=Path, help="Write resolved physical page IDs as a diagnostic-only TSV.")
    result.add_argument("--cache-manifest-output", type=Path, help="Write per-file forensic cache hashes; never extraction input.")
    return result


def _immutable_roots() -> tuple[Path, Path]:
    config = tomllib.loads((PROJECT_ROOT / "project.toml").read_text(encoding="utf-8"))
    restoration = config.get("restoration")
    if not isinstance(restoration, dict) or restoration.get("legacy_root_read_only") is not True:
        raise ValueError("project.toml must explicitly mark restoration.legacy_root_read_only = true")
    legacy_root = Path(str(restoration.get("legacy_root", ""))).expanduser()
    recovery_root = Path(str(restoration.get("recovered_v1_root", ""))).expanduser()
    if not legacy_root.is_absolute() or not recovery_root.is_absolute():
        raise ValueError("restoration legacy_root and recovered_v1_root must be absolute")
    return legacy_root, recovery_root


def main() -> int:
    args = parser().parse_args()
    if args.finalize and args.previous_snapshot is None:
        raise ValueError("--finalize requires --previous-snapshot")
    output = recovery_audit.checked_output_path(args.output, PROJECT_ROOT)
    page_output = recovery_audit.checked_output_path(args.page_mapping_output, PROJECT_ROOT) if args.page_mapping_output else None
    cache_output = recovery_audit.checked_output_path(args.cache_manifest_output, PROJECT_ROOT) if args.cache_manifest_output else None
    legacy_root, configured_recovery_root = _immutable_roots()
    bundle = recovery_audit.audit_recovery(
        project_root=PROJECT_ROOT,
        legacy_root=legacy_root,
        recovery_root=args.recovery_root or configured_recovery_root,
        migration_inventory=args.migration_inventory,
        raw_crosswalk=args.raw_crosswalk,
        previous_snapshot=args.previous_snapshot,
        minimum_quiet_seconds=args.minimum_quiet_seconds,
    )
    recovery_audit.write_report(output, bundle.report)
    if cache_output is not None:
        recovery_audit.write_cache_manifest(cache_output, bundle.cache_artifacts)
    if page_output is not None:
        recovery_audit.write_page_mapping(page_output, bundle.pages)
    print(f"Report: {output}")
    print(f"Recovery files: {sum(tree['file_count'] for tree in bundle.report['tree'].values()):,}")
    print(
        f"Cache evidence: {bundle.report['cache']['json_count']:,} JSON, "
        f"{bundle.report['cache']['error_marker_count']:,} error markers (diagnostic only)"
    )
    if page_output is not None:
        print(f"Diagnostic physical page mapping: {len(bundle.pages):,} pages -> {page_output}")
    if args.finalize and not bundle.report["finalizable"]:
        failed = [name for name, passed in bundle.report["gates"].items() if not passed]
        print(f"Finalize blocked: {', '.join(failed)}")
        return 2
    if args.finalize:
        print("Finalize preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
