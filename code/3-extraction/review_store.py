"""Schema-aware, atomic, separate-from-model storage for record-review decisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from run_integrity import verify_run

from histdata_pipeline.provenance import atomic_write_json, stable_hash

REVIEW_STATUSES = {"unreviewed", "accepted", "flagged", "excluded"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


class RecordReviewStore:
    """Validate review writes against one immutable extraction run."""

    def __init__(self, run_directory: Path, manual_directory: Path, schema: type[BaseModel]):
        self.run_directory = run_directory.resolve()
        verify_run(self.run_directory)
        self.schema = schema
        self.pages = read_jsonl(self.run_directory / "nested.jsonl")
        self.page_lookup = {str(page.get("page_id")): page for page in self.pages}
        if len(self.page_lookup) != len(self.pages) or "" in self.page_lookup:
            raise ValueError("Run nested.jsonl contains missing or duplicate page IDs")
        run = json.loads((self.run_directory / "run.json").read_text(encoding="utf-8"))
        self.run_id = str(run["run_id"])
        self.contract_signature = str(run["contract_signature"])
        self.directory = manual_directory.resolve() / self.contract_signature
        self.directory.mkdir(parents=True, exist_ok=True)

    def decision_path(self, page_id: str) -> Path:
        if page_id not in self.page_lookup:
            raise KeyError(f"Unknown page ID: {page_id}")
        return self.directory / f"{stable_hash(page_id)[:32]}.json"

    def load(self, page_id: str) -> dict[str, Any] | None:
        path = self.decision_path(page_id)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        source = self.page_lookup[page_id]
        for field in ("source_sha256", "render_sha256", "contract_signature"):
            if value.get(field) != source.get(field):
                raise ValueError(f"Stale saved review for {page_id}: {field} changed")
        if value.get("model_extraction_sha256") != stable_hash(source.get("extraction")):
            raise ValueError(f"Stale saved review for {page_id}: model extraction changed")
        return value

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        page_id = str(payload.get("page_id", ""))
        if page_id not in self.page_lookup:
            raise ValueError(f"Unknown page ID: {page_id}")
        source = self.page_lookup[page_id]
        status = str(payload.get("review_status", ""))
        if status not in REVIEW_STATUSES - {"unreviewed"}:
            raise ValueError("review_status must be accepted, flagged, or excluded")
        for field in ("source_sha256", "render_sha256", "contract_signature"):
            if payload.get(field) != source.get(field):
                raise ValueError(f"Stale review: {field} does not match the immutable model page")
        baseline_hash = stable_hash(source.get("extraction"))
        if payload.get("expected_model_extraction_sha256") != baseline_hash:
            raise ValueError("Stale review: expected model extraction hash changed")
        extraction = self.schema.model_validate(payload.get("extraction")).model_dump(mode="json")
        reviewed_hash = stable_hash(extraction)
        notes = str(payload.get("review_notes", "")).strip()
        if (reviewed_hash != baseline_hash or status != "accepted") and not notes:
            raise ValueError("Changed, flagged, or excluded reviews require an evidence note")
        reviewed_at = datetime.now(UTC).isoformat()
        saved = {
            "page_id": page_id,
            "source_sha256": source["source_sha256"],
            "render_sha256": source["render_sha256"],
            "contract_signature": source["contract_signature"],
            "run_id": self.run_id,
            "review_status": status,
            "review_notes": notes,
            "reviewed_at": reviewed_at,
            "model_extraction_sha256": baseline_hash,
            "reviewed_extraction_sha256": reviewed_hash,
            "extraction": extraction,
        }
        history = self.directory / "history" / stable_hash(page_id)[:32] / f"{reviewed_at.replace(':', '')}.json"
        atomic_write_json(history, saved)
        atomic_write_json(self.decision_path(page_id), saved)
        return saved

    def progress(self) -> dict[str, int]:
        counts = {status: 0 for status in REVIEW_STATUSES}
        for page_id in self.page_lookup:
            decision = self.load(page_id)
            counts[str(decision.get("review_status")) if decision else "unreviewed"] += 1
        return counts
