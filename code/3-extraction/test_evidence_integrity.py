import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import selection_gate
from run_integrity import HASHED_ARTIFACTS, verify_run
from selection_gate import evidence_bytes, production_evidence, validate_selection_current

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.provenance import sha256_file, stable_hash


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        tmp_path,
        {
            "project": {"slug": "evidence-test"},
            "storage": {
                "pdf_storage": "project",
                "external_data_root": str(tmp_path / "external"),
                "local_pdf_directory": "sources/pdfs",
            },
            "source": {
                "manifest": "sources/source_manifest.tsv",
                "inventory": "data/source_inventory.tsv",
            },
            "extraction": {"selected_pages": "data/selected_pages.tsv"},
            "pricing": {
                "as_of": "2026-08-29",
                "input_per_million": 1.25,
                "output_per_million": 2.5,
                "thinking_per_million": 3.75,
            },
        },
    )


def _selection_paths(config: ProjectConfig) -> dict[str, Path]:
    return {
        "source_manifest": config.root / "sources" / "source_manifest.tsv",
        "source_inventory": config.root / "data" / "source_inventory.tsv",
        "pages": config.root / "data" / "pages.tsv",
        "source_overrides": config.root / "manual" / "source_overrides.tsv",
        "page_overrides": config.root / "manual" / "page_overrides.tsv",
        "page_selection_gold": config.root / "manual" / "gold" / "page_selection.tsv",
        "selected_pages": config.root / "data" / "selected_pages.tsv",
    }


@dataclass(frozen=True)
class _Identity:
    source_id: str


@dataclass(frozen=True)
class _Page:
    manifest_index: int
    page_id: str
    source_sha256: str
    final_type: str
    classification_source: str


def test_selection_gate_validates_and_receipts_the_same_startup_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    paths = _selection_paths(config)
    original = {name: f"startup-{name}\n".encode() for name in paths}
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original[name])

    selected = _Page(0, "pdfs/source.pdf#page=1", "a" * 64, "selected", "manual_page")
    snapshot_reads: dict[str, bytes] = {}

    def load_source_identities(path: Path) -> list[_Identity]:
        snapshot_reads["source_manifest"] = path.read_bytes()
        for live_path in paths.values():
            live_path.write_bytes(b"changed-after-startup\n")
        return [_Identity("source")]

    def load_inventory(path: Path) -> list[object]:
        snapshot_reads["source_inventory"] = path.read_bytes()
        return []

    def load_page_records(path: Path) -> list[_Page]:
        key = "selected_pages" if path.name.startswith("selected_pages") else "pages"
        snapshot_reads[key] = path.read_bytes()
        return [selected]

    def apply_manual_overrides(
        records: list[_Page],
        *,
        source_overrides_path: Path,
        page_overrides_path: Path,
    ) -> list[_Page]:
        snapshot_reads["source_overrides"] = source_overrides_path.read_bytes()
        snapshot_reads["page_overrides"] = page_overrides_path.read_bytes()
        return records

    def validate_extraction_ready(
        records: list[_Page],
        *,
        expected_source_ids: object,
        gold_path: Path,
    ) -> list[_Page]:
        assert list(expected_source_ids) == ["source"]
        snapshot_reads["page_selection_gold"] = gold_path.read_bytes()
        return records

    monkeypatch.setattr(selection_gate.page_inventory, "load_source_identities", load_source_identities)
    monkeypatch.setattr(selection_gate.page_inventory, "load_inventory", load_inventory)
    monkeypatch.setattr(selection_gate.page_inventory, "reconcile_sources", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(selection_gate.page_inventory, "load_page_records", load_page_records)
    monkeypatch.setattr(selection_gate.page_inventory, "apply_manual_overrides", apply_manual_overrides)
    monkeypatch.setattr(selection_gate.page_inventory, "validate_sources_current", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(selection_gate.page_inventory, "validate_extraction_ready", validate_extraction_ready)

    evidence = validate_selection_current(config)

    assert snapshot_reads == original
    for name, content in original.items():
        assert evidence_bytes(evidence[name]) == content
        assert evidence[name]["path"] == str(paths[name])
    unsigned = dict(evidence)
    unsigned.pop("signature")
    assert evidence["signature"] == stable_hash(unsigned)


def test_production_evidence_contains_immutable_fixture_gold_and_pricing_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = {
        "calibration_fixture": tmp_path / "code" / "3-extraction" / "fixtures" / "calibration_pages.tsv",
        "trial_fixture": tmp_path / "code" / "3-extraction" / "fixtures" / "trial_pages.tsv",
        "gold_jsonl": tmp_path / "manual" / "gold" / "gold.jsonl",
        "gold_expectations": tmp_path / "manual" / "gold" / "expectations.tsv",
    }
    original = {name: f"production-{name}\n".encode() for name in paths}
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original[name])

    evidence = production_evidence(config, contract_signature="c" * 64, selection_evidence={"signature": "s" * 64})
    for path in paths.values():
        path.write_bytes(b"changed-after-snapshot\n")

    for name, content in original.items():
        assert evidence_bytes(evidence["files"][name]) == content
    expected_pricing = json.dumps(config.table("pricing"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert evidence_bytes(evidence["pricing_snapshot"]) == expected_pricing
    unsigned = dict(evidence)
    unsigned.pop("signature")
    assert evidence["signature"] == stable_hash(unsigned)


def _write_signed_run(run_directory: Path) -> dict[str, object]:
    run_directory.mkdir()
    for filename in ("manifest.tsv", "nested.jsonl", "flat.tsv", "tokens.tsv", "errors.tsv", "review_queue.tsv"):
        (run_directory / filename).write_text(f"{filename}\n", encoding="utf-8")

    contract_signature = "c" * 64
    selection: dict[str, object] = {"ordered_selected_pages": []}
    selection["signature"] = stable_hash(selection)
    production: dict[str, object] = {
        "contract_signature": contract_signature,
        "selection_signature": selection["signature"],
        "files": {},
        "pricing": {},
    }
    production["signature"] = stable_hash(production)
    preflight: dict[str, object] = {
        "contract_signature": contract_signature,
        "selection_signature": selection["signature"],
        "evidence_signature": production["signature"],
        "render_signature": "r" * 64,
    }
    preflight["preflight_signature"] = stable_hash(preflight)

    artifacts = {
        "contract.json": {"signature": contract_signature, "payload": {}},
        "selection_evidence.json": selection,
        "production_evidence.json": production,
        "preflight.json": preflight,
    }
    for filename, value in artifacts.items():
        (run_directory / filename).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    run: dict[str, object] = {
        "contract_signature": contract_signature,
        "selection_signature": selection["signature"],
        "evidence_signature": production["signature"],
        "render_signature": preflight["render_signature"],
        "preflight_signature": preflight["preflight_signature"],
    }
    for hash_field, filename in HASHED_ARTIFACTS.items():
        run[hash_field] = sha256_file(run_directory / filename)
    (run_directory / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run


@pytest.mark.parametrize(
    ("filename", "hash_field"),
    [
        ("selection_evidence.json", "selection_evidence_sha256"),
        ("production_evidence.json", "production_evidence_sha256"),
        ("preflight.json", "preflight_sha256"),
    ],
)
def test_run_integrity_hashes_every_evidence_artifact(tmp_path: Path, filename: str, hash_field: str) -> None:
    run_directory = tmp_path / "run"
    run = _write_signed_run(run_directory)
    assert verify_run(run_directory)[hash_field] == run[hash_field]
    path = run_directory / filename
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        verify_run(run_directory)


@pytest.mark.parametrize(
    ("filename", "hash_field"),
    [
        ("selection_evidence.json", "selection_evidence_sha256"),
        ("production_evidence.json", "production_evidence_sha256"),
        ("preflight.json", "preflight_sha256"),
    ],
)
def test_run_integrity_recomputes_every_embedded_evidence_signature(tmp_path: Path, filename: str, hash_field: str) -> None:
    run_directory = tmp_path / "run"
    run = _write_signed_run(run_directory)
    path = run_directory / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tampered"] = True
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run[hash_field] = sha256_file(path)
    (run_directory / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        verify_run(run_directory)
