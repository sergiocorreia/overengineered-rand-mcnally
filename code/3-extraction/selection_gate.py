"""Re-run the stage-2 gate read-only before any extraction can trust its export."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import stable_hash

STAGE_TWO = Path(__file__).resolve().parents[1] / "2-inventory"
if str(STAGE_TWO) not in sys.path:
    sys.path.insert(0, str(STAGE_TWO))
import page_inventory  # noqa: E402 - script stages deliberately share this audited contract


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Exact startup bytes used for both validation and durable evidence."""

    path: Path
    content: bytes

    def evidence(self) -> dict[str, Any]:
        return _content_evidence(self.content, path=self.path)


def _content_evidence(content: bytes, *, path: Path | None = None) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    if path is not None:
        evidence["path"] = str(path)
    return evidence


def snapshot_file(path: Path) -> FileSnapshot:
    if not path.is_file():
        raise FileNotFoundError(path)
    return FileSnapshot(path=path, content=path.read_bytes())


def evidence_bytes(evidence: dict[str, Any]) -> bytes:
    """Decode and verify self-contained file evidence before using its content."""
    try:
        content = base64.b64decode(str(evidence["content_base64"]), validate=True)
        size = int(evidence["size"])
        digest = str(evidence["sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Malformed self-contained content evidence") from error
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Self-contained content evidence fails its size or SHA-256 check")
    return content


def evidence_text(evidence: dict[str, Any]) -> str:
    """Return verified UTF-8 text from a captured evidence object."""
    try:
        return evidence_bytes(evidence).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Self-contained content evidence is not valid UTF-8") from error


def _snapshot_paths(paths: dict[str, Path]) -> dict[str, FileSnapshot]:
    return {name: snapshot_file(path) for name, path in paths.items()}


def _materialize_snapshots(snapshots: dict[str, FileSnapshot], directory: Path) -> dict[str, Path]:
    materialized: dict[str, Path] = {}
    for name, snapshot in snapshots.items():
        suffix = snapshot.path.suffix or ".snapshot"
        destination = directory / f"{name}{suffix}"
        destination.write_bytes(snapshot.content)
        materialized[name] = destination
    return materialized


def validate_selection_current(config: ProjectConfig) -> dict[str, Any]:
    """Prove selected_pages.tsv still follows all current source/review/gold inputs."""
    source = config.table("source")
    extraction = config.table("extraction")
    paths = {
        "source_manifest": config.project_path(str(source.get("manifest", "sources/source_manifest.tsv"))),
        "source_inventory": config.project_path(str(source.get("inventory", "data/source_inventory.tsv"))),
        "pages": config.root / "data" / "pages.tsv",
        "source_overrides": config.root / "manual" / "source_overrides.tsv",
        "page_overrides": config.root / "manual" / "page_overrides.tsv",
        "page_selection_gold": config.root / "manual" / "gold" / "page_selection.tsv",
        "selected_pages": config.project_path(str(extraction.get("selected_pages", "data/selected_pages.tsv"))),
    }
    snapshots = _snapshot_paths(paths)
    with tempfile.TemporaryDirectory(prefix="selection-gate-") as temporary:
        snapshot_paths = _materialize_snapshots(snapshots, Path(temporary))
        identities = page_inventory.load_source_identities(snapshot_paths["source_manifest"])
        inventory = page_inventory.load_inventory(snapshot_paths["source_inventory"])
        sources = page_inventory.reconcile_sources(identities, inventory, require_all=True)
        records = page_inventory.load_page_records(snapshot_paths["pages"])
        records = page_inventory.apply_manual_overrides(
            records,
            source_overrides_path=snapshot_paths["source_overrides"],
            page_overrides_path=snapshot_paths["page_overrides"],
        )
        page_inventory.validate_sources_current(records, sources, config.pdf_directory)
        expected = page_inventory.validate_extraction_ready(
            records,
            expected_source_ids=(row.source_id for row in identities),
            gold_path=snapshot_paths["page_selection_gold"],
        )
        exported = page_inventory.load_page_records(snapshot_paths["selected_pages"])
    if [asdict(row) for row in exported] != [asdict(row) for row in expected]:
        raise ValueError("data/selected_pages.tsv is stale; rerun code/2-inventory/export_selected_pages.py")
    evidence = {name: snapshot.evidence() for name, snapshot in snapshots.items()}
    evidence["ordered_selected_pages"] = [
        {
            "manifest_index": row.manifest_index,
            "page_id": row.page_id,
            "source_sha256": row.source_sha256,
            "final_type": row.final_type,
            "classification_source": row.classification_source,
        }
        for row in exported
    ]
    evidence["signature"] = stable_hash(evidence)
    return evidence


def production_evidence(
    config: ProjectConfig,
    *,
    contract_signature: str,
    selection_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Bind gold/trial/cache/cost approvals to every durable evidence input."""
    paths = {
        "calibration_fixture": config.root / "code" / "3-extraction" / "fixtures" / "calibration_pages.tsv",
        "trial_fixture": config.root / "code" / "3-extraction" / "fixtures" / "trial_pages.tsv",
        "gold_jsonl": config.root / "manual" / "gold" / "gold.jsonl",
        "gold_expectations": config.root / "manual" / "gold" / "expectations.tsv",
    }
    snapshots = _snapshot_paths(paths)
    pricing = config.table("pricing")
    pricing_content = json.dumps(pricing, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = {
        "contract_signature": contract_signature,
        "selection_signature": selection_evidence["signature"],
        "files": {name: snapshot.evidence() for name, snapshot in snapshots.items()},
        "pricing": pricing,
        "pricing_snapshot": _content_evidence(pricing_content),
    }
    payload["signature"] = stable_hash(payload)
    return payload
