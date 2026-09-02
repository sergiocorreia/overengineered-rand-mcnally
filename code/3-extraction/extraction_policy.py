"""Bounded-service, production-gate, source, and preflight policy helpers."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from contract import ExtractionContract
from pipeline import (
    SelectedPage,
    load_page_cache,
    page_cache_path,
    render_destination,
    resolve_source,
)

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import sha256_file, stable_hash

GATE_FIELDS = ("gold_passed", "trial_passed", "cache_reuse_passed", "cost_reviewed")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")


def validate_cost_review(config: ProjectConfig, gate: dict[str, Any]) -> None:
    """Require a recent, timezone-aware owner cost review for a live full run."""
    raw = str(gate.get("cost_reviewed_at", "")).strip()
    try:
        reviewed_at = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError("Full extraction blocked: cost_reviewed_at must be an ISO timestamp") from error
    if reviewed_at.tzinfo is None:
        raise ValueError("Full extraction blocked: cost_reviewed_at must include a timezone")
    age_hours = (datetime.now(UTC) - reviewed_at.astimezone(UTC)).total_seconds() / 3600
    max_age = float(config.table("pricing").get("review_max_age_hours", 24))
    if max_age <= 0:
        raise ValueError("pricing.review_max_age_hours must be positive")
    if age_hours < -5 / 60 or age_hours > max_age:
        raise ValueError(f"Full extraction blocked: cost review must be no more than {max_age:g} hours old")


def resolve_service(config: ProjectConfig, args: argparse.Namespace) -> str:
    """Resolve the explicit service flag or the configured safe default."""
    if args.standard:
        return "standard"
    if args.flex:
        return "flex"
    service = str(config.table("model").get("default_service", "flex"))
    if service not in {"flex", "standard"}:
        raise ValueError("model.default_service must be flex or standard")
    return service


def enforce_guards(
    config: ProjectConfig,
    args: argparse.Namespace,
    *,
    service: str,
    selected_count: int,
    contract_signature: str,
    evidence_signature: str,
) -> int:
    """Validate bounded execution policy and return the effective request ceiling."""
    service_config = config.table("service")
    standard_ceiling = int(service_config.get("standard_request_ceiling", 50))
    if args.max_requests is not None:
        max_requests = args.max_requests
    elif args.cache_only or args.status:
        max_requests = 0
    elif args.all:
        raise ValueError("--all requires an explicit --max-requests for the exact approved scope")
    else:
        max_requests = int(service_config.get("flex_default_max_requests", 50))
    if max_requests < 0:
        raise ValueError("--max-requests must be nonnegative")
    if service == "standard" and max_requests > standard_ceiling:
        raise ValueError(f"Standard --max-requests cannot exceed its configured ceiling ({standard_ceiling})")
    standard_max = int(service_config.get("standard_max_pages", 20))
    if args.all and service != "flex":
        raise ValueError("--all requires Flex service")
    if service == "standard" and (args.all or selected_count > standard_max):
        raise ValueError(f"Standard service is restricted to at most {standard_max} explicitly bounded pages; use Flex")
    if args.cache_only and args.retry_errors:
        raise ValueError("--cache-only and --retry-errors cannot be combined")
    if args.status and (args.cache_only or args.retry_errors):
        raise ValueError("--status cannot be combined with --cache-only or --retry-errors")
    if args.dry_run and (args.status or args.cache_only):
        raise ValueError("--dry-run cannot be combined with --status or --cache-only")
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be positive")
    if args.all and not (args.dry_run or args.status):
        gate_path = config.root / "manual" / "gold" / "production_gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        if gate.get("contract_signature") != contract_signature:
            raise ValueError("Full extraction blocked: production gate belongs to a different extraction contract")
        if gate.get("evidence_signature") != evidence_signature:
            raise ValueError("Full extraction blocked: fixtures, gold, pricing, or page-selection evidence changed")
        open_gates = [name for name in GATE_FIELDS if gate.get(name) is not True]
        if open_gates:
            raise ValueError(f"Full extraction blocked by production gates: {', '.join(open_gates)}")
        if not args.cache_only:
            validate_cost_review(config, gate)
    return max_requests


def render_signature(
    pages: list[SelectedPage],
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
) -> str:
    """Hash the exact ordered source/render pair used by a preflight."""
    return stable_hash(
        [
            {
                "page_id": page.page_id,
                "source_sha256": page.source_sha256,
                "render_sha256": prepared[page.page_id][1],
            }
            for page in pages
            if page.page_id in prepared
        ]
    )


def _known_usage(envelope: dict[str, Any]) -> dict[str, int] | None:
    """Return a complete nonnegative usage sample, never a partial estimate."""
    if envelope.get("usage_known") is False:
        return None
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        normalized[field] = value
    return normalized


def estimated_incremental_cost(
    config: ProjectConfig,
    prepared: dict[str, tuple[Path, str, dict[str, Any] | None]],
    pending_count: int,
) -> dict[str, Any]:
    """Project bounded request usage only from a complete trustworthy cache basis."""
    cached_successes = [
        cached
        for _, _, cached in prepared.values()
        if cached is not None and cached.get("status") == "ok"
    ]
    cached_usage = [_known_usage(cached) for cached in cached_successes]
    usage_basis_complete = all(usage is not None for usage in cached_usage)
    usable_usage = [usage for usage in cached_usage if usage is not None and usage["total_tokens"] > 0]
    if pending_count == 0:
        projected: dict[str, int | None] = {field: 0 for field in TOKEN_FIELDS}
        token_projection_available = True
    elif cached_successes and usage_basis_complete and usable_usage:
        projected = {
            field: round(sum(usage[field] for usage in usable_usage) / len(usable_usage) * pending_count)
            for field in TOKEN_FIELDS
        }
        token_projection_available = True
    else:
        projected = {field: None for field in TOKEN_FIELDS}
        token_projection_available = False

    pricing = config.table("pricing")
    rates = {
        "input_per_million": float(pricing.get("input_per_million", 0.0)),
        "output_per_million": float(pricing.get("output_per_million", 0.0)),
        "thinking_per_million": float(pricing.get("thinking_per_million", 0.0)),
    }
    if any(rate < 0 for rate in rates.values()):
        raise ValueError("Pricing rates cannot be negative")
    pricing_as_of = str(pricing.get("as_of", "")).strip()
    try:
        pricing_date_valid = bool(pricing_as_of) and date.fromisoformat(pricing_as_of).isoformat() == pricing_as_of
    except ValueError:
        pricing_date_valid = False
    pricing_configured = pricing_date_valid and any(rate > 0 for rate in rates.values())
    available = token_projection_available and (pending_count == 0 or pricing_configured)
    cost = None
    if available:
        cost = (
            int(projected["input_tokens"] or 0) * rates["input_per_million"]
            + int(projected["output_tokens"] or 0) * rates["output_per_million"]
            + int(projected["thoughts_tokens"] or 0) * rates["thinking_per_million"]
        ) / 1_000_000
    return {
        "available": available,
        "token_projection_available": token_projection_available,
        "usage_basis_complete": usage_basis_complete,
        "basis_cached_pages": len(usable_usage),
        "projected_request_tokens": projected,
        "pricing_as_of": pricing_as_of,
        "rates_per_million": rates,
        "pricing_configured": pricing_configured,
        "projected_incremental_cost": cost,
    }


def verify_sources(config: ProjectConfig, pages: list[SelectedPage]) -> dict[tuple[str, str], Path]:
    """Verify each unique source once and reject conflicting manifest hashes."""
    verified: dict[tuple[str, str], Path] = {}
    path_hashes: dict[str, str] = {}
    for page in pages:
        previous = path_hashes.setdefault(page.pdf_relative_path, page.source_sha256)
        if previous != page.source_sha256:
            raise ValueError(f"Conflicting source hashes in selected manifest for {page.pdf_relative_path}")
        key = (page.pdf_relative_path, page.source_sha256)
        if key not in verified:
            verified[key] = resolve_source(config, page)
    return verified


def status_report(
    config: ProjectConfig,
    contract: ExtractionContract,
    pages: list[SelectedPage],
) -> dict[str, Any]:
    """Report exact cache state without rendering or provider access."""
    verify_sources(config, pages)
    counts = {"ok": 0, "error": 0, "missing": 0, "unrendered": 0}
    for page in pages:
        render_path, _, _ = render_destination(config, page)
        if not render_path.is_file():
            counts["unrendered"] += 1
            continue
        render_hash = sha256_file(render_path)
        cache_path = page_cache_path(
            config,
            contract_signature=contract.signature,
            page=page,
            render_sha256=render_hash,
        )
        cached = load_page_cache(
            cache_path,
            contract_signature=contract.signature,
            page=page,
            render_sha256=render_hash,
        )
        counts[cached["status"] if cached else "missing"] += 1
    return {"contract_signature": contract.signature, "selected_pages": len(pages), "cache": counts}
