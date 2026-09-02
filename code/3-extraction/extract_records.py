#!/usr/bin/env python3
"""Run bounded, contract-hashed page extraction or reconstruct exports from cache."""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contract import ExtractionContract, build_contract
from extraction_policy import (
    enforce_guards,
    estimated_incremental_cost,
    render_signature,
    resolve_service,
    status_report,
    verify_sources,
)
from extraction_provider import (
    base_envelope,
    extract_one,
    overlay_current_page,
    structural_error_envelope,
)
from pipeline import (
    SelectedPage,
    load_page_cache,
    page_cache_path,
    read_reviewed_pages,
    read_selected_pages,
    render_page,
)
from run_writer import publish_current, write_run
from selection_gate import production_evidence, validate_selection_current

from histdata_pipeline.config import ProjectConfig, load_project_config
from histdata_pipeline.provenance import stable_hash

# Preserve the runner's historical private import/monkeypatch surface while the
# implementation lives in focused sibling modules.
_base_envelope = base_envelope
_estimated_incremental_cost = estimated_incremental_cost
_extract_one = extract_one
_overlay_current_page = overlay_current_page
_publish_current = publish_current
_render_signature = render_signature
_status_report = status_report
_structural_error_envelope = structural_error_envelope
_verify_sources = verify_sources
_write_run = write_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--calibration", action="store_true", help="Use the risk-based calibration fixture.")
    selectors.add_argument("--trial", action="store_true", help="Use the bounded concurrent-trial fixture.")
    selectors.add_argument("--limit", type=int, metavar="N", help="Use the first N selected pages.")
    selectors.add_argument("--year", help="Use selected pages whose year/source_date starts with this value.")
    selectors.add_argument("--page-id", action="append", help="Use one stable page ID; repeat for multiple pages.")
    selectors.add_argument("--queue-tsv", type=Path, help="Use page IDs from a generated review/QC queue.")
    selectors.add_argument("--all", action="store_true", help="Use every selected page; production gates apply.")
    parser.add_argument("--workers", type=int, help="Concurrent page workers; does not affect cache identity.")
    service = parser.add_mutually_exclusive_group()
    service.add_argument("--flex", action="store_true", help="Use the lower-cost Flex service (default).")
    service.add_argument("--standard", action="store_true", help="Use bounded Standard service.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render/inspect caches and print an exact no-model preflight without writing a run export.",
    )
    parser.add_argument("--status", action="store_true", help="Report cache state without rendering or calling a model.")
    parser.add_argument("--cache-only", action="store_true", help="Rebuild outputs without any model calls.")
    parser.add_argument("--retry-errors", action="store_true", help="Retry pages whose latest immutable cache is an error.")
    parser.add_argument("--max-requests", type=int, help="Abort before model calls if more requests would be needed.")
    return parser


def _read_page_ids(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if "page_id" not in (reader.fieldnames or []):
            raise ValueError(f"{path} must contain a page_id column")
        return [str(row["page_id"]).strip() for row in reader if str(row.get("page_id", "")).strip()]


def select_pages(config: ProjectConfig, pages: list[SelectedPage], args: argparse.Namespace) -> list[SelectedPage]:
    """Resolve exactly one declared selector while preserving manifest order."""
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        return pages[: args.limit]
    if args.year is not None:
        selected = [
            page
            for page in pages
            if page.values.get("year") == args.year or page.values.get("source_date", "").startswith(args.year)
        ]
    elif args.page_id:
        wanted = set(args.page_id)
        if len(wanted) != len(args.page_id):
            raise ValueError("--page-id values must be unique")
        selected = [page for page in pages if page.page_id in wanted]
        missing = wanted - {page.page_id for page in selected}
        if missing:
            raise ValueError(f"Unknown --page-id values: {', '.join(sorted(missing))}")
    elif args.queue_tsv:
        wanted_list = _read_page_ids(config.project_path(args.queue_tsv))
        # A QC queue may contain several cases for one page. Request each page
        # at most once, still in canonical manifest order.
        wanted = set(dict.fromkeys(wanted_list))
        selected = [page for page in pages if page.page_id in wanted]
        missing = wanted - {page.page_id for page in selected}
        if missing:
            raise ValueError(f"Queue contains unknown page IDs: {', '.join(sorted(missing))}")
    elif args.calibration or args.trial:
        fixture = "calibration_pages.tsv" if args.calibration else "trial_pages.tsv"
        wanted_list = _read_page_ids(config.root / "code" / "3-extraction" / "fixtures" / fixture)
        if not wanted_list:
            raise ValueError(f"{fixture} is empty; transcribe a representative fixture first")
        wanted = set(wanted_list)
        selected = [page for page in pages if page.page_id in wanted]
        missing = wanted - {page.page_id for page in selected}
        if missing:
            raise ValueError(f"{fixture} contains unknown page IDs: {', '.join(sorted(missing))}")
    else:
        selected = list(pages)
    if not selected:
        raise ValueError("The selector matched no pages")
    return selected


def _prepare_pages(
    config: ProjectConfig,
    contract: ExtractionContract,
    pages: list[SelectedPage],
) -> tuple[dict[str, tuple[Path, str, dict[str, Any] | None]], dict[str, dict[str, Any]]]:
    """Render pages, inspect immutable caches, and retain structural failures."""
    verified = _verify_sources(config, pages)
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]] = {}
    structural_errors: dict[str, dict[str, Any]] = {}
    for page in pages:
        try:
            rendered = render_page(
                config,
                page,
                verified_source=verified[(page.pdf_relative_path, page.source_sha256)],
            )
            cache_path = page_cache_path(
                config,
                contract_signature=contract.signature,
                page=page,
                render_sha256=rendered.sha256,
            )
            cached = load_page_cache(
                cache_path,
                contract_signature=contract.signature,
                page=page,
                render_sha256=rendered.sha256,
            )
            prepared[page.page_id] = (rendered.path, rendered.sha256, cached)
        except Exception as error:  # noqa: BLE001 - structural failures must remain visible in exports
            structural_errors[page.page_id] = _structural_error_envelope(page, contract, error)
    return prepared, structural_errors


def _pending_pages(
    pages: list[SelectedPage],
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
    *,
    retry_errors: bool,
    cache_only: bool,
    max_requests: int,
) -> list[SelectedPage]:
    pending = [
        page
        for page in pages
        if page.page_id in prepared
        and (
            prepared[page.page_id][2] is None
            or (prepared[page.page_id][2]["status"] == "error" and retry_errors)
        )
    ]
    if cache_only:
        return []
    if len(pending) > max_requests:
        raise ValueError(f"Run would make {len(pending)} model requests, exceeding --max-requests {max_requests}")
    return pending


def _preflight(
    config: ProjectConfig,
    pages: list[SelectedPage],
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
    structural_errors: dict[str, dict[str, Any]],
    pending: list[SelectedPage],
    summary: dict[str, Any],
) -> dict[str, Any]:
    cached_ok = sum(cached is not None and cached.get("status") == "ok" for _, _, cached in prepared.values())
    cached_errors = sum(cached is not None and cached.get("status") == "error" for _, _, cached in prepared.values())
    value = {
        **summary,
        "render_signature": _render_signature(pages, prepared),
        "pending_requests": len(pending),
        "cached_successes": cached_ok,
        "cached_errors": cached_errors,
        "structural_errors": len(structural_errors),
        "estimated_incremental_cost": _estimated_incremental_cost(config, prepared, len(pending)),
    }
    value["preflight_signature"] = stable_hash(value)
    return value


def _validate_live_preflight(
    config: ProjectConfig,
    args: argparse.Namespace,
    preflight: dict[str, Any],
    structural_errors: dict[str, dict[str, Any]],
    pending: list[SelectedPage],
    max_requests: int,
) -> None:
    estimate = preflight["estimated_incremental_cost"]
    if pending and not estimate["pricing_configured"]:
        raise ValueError("Model requests are blocked until [pricing].as_of is an ISO date and at least one nonzero rate is configured")
    if args.all and pending and not estimate["available"]:
        raise ValueError("Full extraction is blocked until bounded trial usage supports an incremental cost estimate")
    if args.all and structural_errors:
        first = next(iter(structural_errors.values()))
        raise ValueError(
            f"Full extraction blocked by {len(structural_errors)} structural page error(s); "
            f"first: {first['page_id']}: {first['error_message']}"
        )
    if not args.all:
        return
    gate = json.loads((config.root / "manual" / "gold" / "production_gate.json").read_text(encoding="utf-8"))
    if gate.get("render_signature") != preflight["render_signature"]:
        raise ValueError("Full extraction blocked: exact rendered corpus differs from the approved preflight")
    if not args.cache_only and gate.get("preflight_signature") != preflight["preflight_signature"]:
        raise ValueError("Full extraction blocked: rerun --dry-run and record the approved preflight signature")
    try:
        approved_requests = int(gate.get("approved_max_requests", -1))
    except (TypeError, ValueError) as error:
        raise ValueError("Full extraction blocked: approved_max_requests is invalid") from error
    if not args.cache_only and (approved_requests != max_requests or len(pending) > approved_requests):
        raise ValueError("Full extraction blocked: request ceiling differs from the approved preflight")


def _request_pending(
    config: ProjectConfig,
    contract: ExtractionContract,
    pages: list[SelectedPage],
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
    *,
    service: str,
    retry_errors: bool,
    workers: int,
    attempt_id: str,
) -> dict[str, dict[str, Any]]:
    if not pages:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(pages))) as executor:
        futures = {
            executor.submit(
                _extract_one,
                config,
                contract,
                page,
                prepared[page.page_id][0],
                prepared[page.page_id][1],
                service=service,
                retry_errors=retry_errors,
                attempt_id=f"{attempt_id}-{page.cache_key[:8]}",
            ): page.page_id
            for page in pages
        }
        return {futures[future]: future.result() for future in as_completed(futures)}


def _ordered_envelopes(
    pages: list[SelectedPage],
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
    structural_errors: dict[str, dict[str, Any]],
    fresh: dict[str, dict[str, Any]],
    contract: ExtractionContract,
) -> list[dict[str, Any]]:
    """Reassemble concurrent results in canonical selected-manifest order."""
    envelopes: list[dict[str, Any]] = []
    for page in pages:
        if page.page_id in structural_errors:
            envelopes.append(structural_errors[page.page_id])
        elif page.page_id in fresh:
            envelopes.append(fresh[page.page_id])
        elif (cached := prepared[page.page_id][2]) is not None:
            render_path, render_hash, _ = prepared[page.page_id]
            envelopes.append(_overlay_current_page(cached, page, contract, render_path, render_hash))
        else:
            render_path, render_hash, _ = prepared[page.page_id]
            envelopes.append(
                {
                    **_base_envelope(page, contract, render_hash, render_path),
                    "status": "error",
                    "error_type": "CacheMiss",
                    "error_message": "No successful or failed cache exists; cache-only mode made no request",
                    "extraction": None,
                    "usage": {field: 0 for field in ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")},
                    "provider_call_started": False,
                    "usage_known": True,
                }
            )
    return envelopes


def execute(args: argparse.Namespace) -> dict[str, Any]:
    config = load_project_config()
    selection_evidence = validate_selection_current(config)
    selected_path = config.project_path(str(config.table("extraction").get("selected_pages", "data/selected_pages.tsv")))
    all_pages = read_selected_pages(selected_path)
    selection_universe = read_reviewed_pages(config.root / "data" / "pages.tsv") if args.calibration else all_pages
    pages = select_pages(config, selection_universe, args)
    service = resolve_service(config, args)
    contract = build_contract(config, service=service)
    evidence = production_evidence(config, contract_signature=contract.signature, selection_evidence=selection_evidence)
    max_requests = enforce_guards(
        config,
        args,
        service=service,
        selected_count=len(pages),
        contract_signature=contract.signature,
        evidence_signature=str(evidence["signature"]),
    )
    summary = {
        "contract_signature": contract.signature,
        "selector_pages": len(pages),
        "service": service,
        "max_requests": max_requests,
        "selection_signature": selection_evidence["signature"],
        "evidence_signature": evidence["signature"],
        "model": config.table("model").get("name"),
        "reasoning": config.table("model").get("think_level"),
        "media_resolution": config.table("extraction").get("media_resolution"),
        "render_dpi": config.table("extraction").get("render_dpi"),
        "retry_errors": args.retry_errors,
    }
    if args.status:
        return {**summary, "mode": "status", **_status_report(config, contract, pages)}

    prepared, structural_errors = _prepare_pages(config, contract, pages)
    pending = _pending_pages(
        pages,
        prepared,
        retry_errors=args.retry_errors,
        cache_only=args.cache_only,
        max_requests=max_requests,
    )
    preflight = _preflight(config, pages, prepared, structural_errors, pending, summary)
    if args.dry_run:
        return {**preflight, "mode": "dry-run", "provider_calls": 0}
    _validate_live_preflight(config, args, preflight, structural_errors, pending, max_requests)

    attempt_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(3)
    workers = args.workers or int(config.table("extraction").get("default_workers", 10))
    fresh = _request_pending(
        config,
        contract,
        pending,
        prepared,
        service=service,
        retry_errors=args.retry_errors,
        workers=workers,
        attempt_id=attempt_id,
    )
    envelopes = _ordered_envelopes(pages, prepared, structural_errors, fresh, contract)
    cached_page_ids = frozenset(
        page.page_id
        for page in pages
        if page.page_id in prepared and prepared[page.page_id][2] is not None and page.page_id not in fresh
    )
    run_directory = _write_run(
        config,
        contract,
        pages,
        envelopes,
        run_id=attempt_id,
        service=service,
        cache_only=args.cache_only,
        requested=len(pending),
        fresh_page_ids=frozenset(fresh),
        cached_page_ids=cached_page_ids,
        selection_evidence=selection_evidence,
        production_evidence_payload=evidence,
        preflight=preflight,
    )
    error_count = sum(envelope["status"] == "error" for envelope in envelopes)
    current_updated = bool(args.all and len(pages) == len(all_pages) and error_count == 0)
    if current_updated:
        _publish_current(config, run_directory, contract.signature)
    return {
        **summary,
        "mode": "cache-only" if args.cache_only else "extract",
        "run_directory": str(run_directory),
        "model_requests": len(pending),
        "successful_pages": len(envelopes) - error_count,
        "error_pages": error_count,
        "current_updated": current_updated,
    }


def main() -> None:
    try:
        result = execute(build_parser().parse_args())
    except (OSError, ValueError, RuntimeError, TypeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
