"""Forensic, diagnostic-only inventory of the recovered Yachay cache."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CacheArtifact:
    cache_relative_path: str
    kind: str
    size_bytes: int
    content_sha256: str
    cache_group: str
    page: int | None
    edition_id: str
    page_id: str
    mapping_status: str
    json_status: str
    model: str


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_stable(path: Path) -> tuple[bytes, int, str, bool]:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    stable = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    return payload, after.st_size, hashlib.sha256(payload).hexdigest(), stable


def scan_cache(
    cache_root: Path,
    page_lookup: dict[tuple[str, int], tuple[str, str]],
) -> tuple[dict[str, Any], list[CacheArtifact]]:
    """Hash every JSON/error marker without treating its contents as extraction truth."""

    paths = sorted(path for path in cache_root.rglob("*") if path.is_file()) if cache_root.is_dir() else []
    artifacts: list[CacheArtifact] = []
    unreadable: list[str] = []
    changed: list[str] = []
    other_file_count = 0
    for path in paths:
        kind = path.suffix.casefold().removeprefix(".")
        if kind not in {"json", "error"}:
            other_file_count += 1
            continue
        relative = path.relative_to(cache_root).as_posix()
        cache_group = path.parent.relative_to(cache_root).as_posix()
        try:
            page = int(path.stem)
        except ValueError:
            page = None
        try:
            payload, size_bytes, digest, stable = _read_stable(path)
        except OSError as error:
            unreadable.append(f"{relative}:{type(error).__name__}")
            artifacts.append(CacheArtifact(relative, kind, 0, "", cache_group, page, "", "", "unreadable", "unreadable", ""))
            continue
        if not stable:
            changed.append(relative)
        json_status = "not_applicable"
        model = ""
        if kind == "json":
            try:
                decoded = json.loads(payload)
                json_status = "valid" if isinstance(decoded, dict) else "invalid_root"
                if isinstance(decoded, dict):
                    settings = decoded.get("settings")
                    model_value = decoded.get("model") or (settings.get("model") if isinstance(settings, dict) else "")
                    model = str(model_value or "")
            except (UnicodeDecodeError, json.JSONDecodeError):
                json_status = "invalid_json"
        identity = page_lookup.get((cache_group, page)) if page is not None else None
        edition_id, page_id = identity if identity else ("", "")
        mapping_status = "mapped" if identity else "unmapped_diagnostic"
        artifacts.append(
            CacheArtifact(relative, kind, size_bytes, digest, cache_group, page, edition_id, page_id, mapping_status, json_status, model)
        )

    groups: dict[str, Counter[str]] = defaultdict(Counter)
    models: dict[str, Counter[str]] = defaultdict(Counter)
    keys: Counter[tuple[str, int | None]] = Counter()
    for artifact in artifacts:
        groups[artifact.cache_group][artifact.kind] += 1
        groups[artifact.cache_group][artifact.mapping_status] += 1
        models[artifact.cache_group][artifact.model or "not_recorded"] += artifact.kind == "json"
        keys[(artifact.cache_group, artifact.page)] += 1
    group_rows = [
        {
            "cache_group": group,
            "json_count": counts["json"],
            "error_count": counts["error"],
            "mapped_count": counts["mapped"],
            "unmapped_count": counts["unmapped_diagnostic"],
            "models": dict(sorted(models[group].items())),
        }
        for group, counts in sorted(groups.items())
    ]
    invalid_json = [artifact.cache_relative_path for artifact in artifacts if artifact.json_status not in {"valid", "not_applicable"}]
    error_markers = [artifact.cache_relative_path for artifact in artifacts if artifact.kind == "error"]
    manifest_material = [
        (artifact.cache_relative_path, artifact.kind, artifact.size_bytes, artifact.content_sha256) for artifact in artifacts
    ]
    return (
        {
            "purpose": "diagnostic_only_not_a_reusable_cache",
            "json_count": sum(artifact.kind == "json" for artifact in artifacts),
            "error_marker_count": len(error_markers),
            "other_file_count": other_file_count,
            "mapped_artifact_count": sum(artifact.mapping_status == "mapped" for artifact in artifacts),
            "unmapped_artifact_count": sum(artifact.mapping_status != "mapped" for artifact in artifacts),
            "invalid_json_count": len(invalid_json),
            "invalid_json_samples": invalid_json[:20],
            "unreadable_count": len(unreadable),
            "unreadable_samples": unreadable[:20],
            "changed_during_hash_count": len(changed),
            "changed_during_hash_samples": changed[:20],
            "json_and_error_collision_count": sum(count > 1 for count in keys.values()),
            "error_marker_paths": error_markers,
            "content_manifest_sha256": _sha256_payload(manifest_material),
            "groups": group_rows,
        },
        artifacts,
    )
