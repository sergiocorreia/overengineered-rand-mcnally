"""Verify immutable extraction run artifacts and the optional current pointer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import sha256_file, stable_hash

HASHED_ARTIFACTS = {
    "manifest_sha256": "manifest.tsv",
    "nested_sha256": "nested.jsonl",
    "flat_sha256": "flat.tsv",
    "tokens_sha256": "tokens.tsv",
    "errors_sha256": "errors.tsv",
    "review_queue_sha256": "review_queue.tsv",
    "contract_sha256": "contract.json",
    "selection_evidence_sha256": "selection_evidence.json",
    "production_evidence_sha256": "production_evidence.json",
    "preflight_sha256": "preflight.json",
}


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Run artifact must contain a JSON object: {path}")
    return value


def _verify_signed_payload(
    payload: dict[str, Any],
    *,
    artifact_name: str,
    signature_field: str,
    expected_signature: str,
) -> None:
    if not expected_signature:
        if payload:
            raise ValueError(f"run.json is missing the signature for nonempty {artifact_name}")
        return
    embedded = str(payload.get(signature_field, ""))
    if embedded != expected_signature:
        raise ValueError(f"run.json and {artifact_name} signatures disagree")
    unsigned = dict(payload)
    unsigned.pop(signature_field, None)
    if stable_hash(unsigned) != embedded:
        raise ValueError(f"Embedded signature is invalid for {artifact_name}")


def _require_matching_field(payload: dict[str, Any], run: dict[str, Any], field: str, artifact_name: str) -> None:
    if payload.get(field) != run.get(field):
        raise ValueError(f"run.json and {artifact_name} disagree on {field}")


def verify_run(run_directory: Path) -> dict[str, Any]:
    run_directory = run_directory.resolve(strict=True)
    run_path = run_directory / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    for hash_field, filename in HASHED_ARTIFACTS.items():
        expected = str(run.get(hash_field, ""))
        artifact = run_directory / filename
        if not expected or not artifact.is_file():
            raise ValueError(f"Run is missing the integrity contract for {filename}")
        actual = sha256_file(artifact)
        if actual != expected:
            raise ValueError(f"Immutable run artifact changed: {artifact}")
    contract = _load_object(run_directory / "contract.json")
    if contract.get("signature") != run.get("contract_signature"):
        raise ValueError("run.json and contract.json signatures disagree")

    selection = _load_object(run_directory / "selection_evidence.json")
    production = _load_object(run_directory / "production_evidence.json")
    preflight = _load_object(run_directory / "preflight.json")
    _verify_signed_payload(
        selection,
        artifact_name="selection_evidence.json",
        signature_field="signature",
        expected_signature=str(run.get("selection_signature", "")),
    )
    _verify_signed_payload(
        production,
        artifact_name="production_evidence.json",
        signature_field="signature",
        expected_signature=str(run.get("evidence_signature", "")),
    )
    _verify_signed_payload(
        preflight,
        artifact_name="preflight.json",
        signature_field="preflight_signature",
        expected_signature=str(run.get("preflight_signature", "")),
    )
    if production:
        _require_matching_field(production, run, "contract_signature", "production_evidence.json")
        _require_matching_field(production, run, "selection_signature", "production_evidence.json")
    if preflight:
        for field in ("contract_signature", "selection_signature", "evidence_signature", "render_signature"):
            _require_matching_field(preflight, run, field, "preflight.json")
    return run


def verify_current(config: ProjectConfig, run_directory: Path) -> dict[str, Any]:
    run_directory = run_directory.resolve(strict=True)
    export_root = config.external_path("export_subdirectory", "data-extraction/exports")
    if not run_directory.is_relative_to(export_root):
        raise ValueError("Extraction run is outside the configured export root")
    run = verify_run(run_directory)
    pointer_path = config.root / "data" / "extraction_current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if Path(str(pointer.get("run_directory", ""))).resolve() != run_directory:
        raise ValueError("Project current-extraction pointer names a different run")
    if pointer.get("flat_tsv") != str(run_directory / "flat.tsv"):
        raise ValueError("Project current-extraction flat path is stale")
    if pointer.get("contract_signature") != run.get("contract_signature"):
        raise ValueError("Project current-extraction contract signature is stale")
    if pointer.get("run_sha256") != sha256_file(run_directory / "run.json"):
        raise ValueError("Project current-extraction run receipt changed")
    return run
