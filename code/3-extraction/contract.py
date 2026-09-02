"""Build the immutable extraction contract and load its Pydantic schema."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

from pydantic import BaseModel

from histdata_pipeline.config import ProjectConfig
from histdata_pipeline.extraction_fields import RESERVED_MODEL_FIELDS
from histdata_pipeline.provenance import git_revision, sha256_file, stable_hash

PLACEHOLDER_PATTERN = re.compile(r"\[[A-Z][A-Z0-9 _×/,-]+\]")
PIPELINE_SOURCE_NAMES = (
    "contract.py",
    "pipeline.py",
    "extract_records.py",
    "extraction_policy.py",
    "extraction_provider.py",
    "run_writer.py",
)


@dataclass(frozen=True, slots=True)
class ExtractionContract:
    """Material model inputs and their deterministic signature."""

    signature: str
    payload: dict[str, Any]
    prompt: str
    schema: type[BaseModel]


def _load_module(path: Path) -> ModuleType:
    module_name = f"project_extraction_schema_{sha256_file(path)[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import schema from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_schema(path: Path, class_name: str = "PageExtraction") -> type[BaseModel]:
    """Load and validate the project response model without importing by package name."""
    module = _load_module(path)
    candidate = getattr(module, class_name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        raise TypeError(f"{path} must define a Pydantic model named {class_name}")
    return candidate


def _model_types(annotation: Any) -> set[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    models: set[type[BaseModel]] = set()
    for argument in get_args(annotation):
        models.update(_model_types(argument))
    return models


def validate_schema_field_names(schema: type[BaseModel], record_list_field: str) -> None:
    """Keep model-controlled values disjoint from trusted runner provenance."""
    page_fields = set(schema.model_fields)
    page_collisions = sorted(page_fields & RESERVED_MODEL_FIELDS)
    if page_collisions:
        raise ValueError(f"Schema page fields collide with runner-owned fields: {', '.join(page_collisions)}")
    record_field = schema.model_fields.get(record_list_field)
    if record_field is None:
        raise ValueError(f"Schema must define the configured record list field {record_list_field!r}")
    record_models = _model_types(record_field.annotation)
    if not record_models:
        raise TypeError(f"Schema field {record_list_field!r} must contain Pydantic record models")
    record_collisions = sorted(
        {
            field_name
            for record_model in record_models
            for field_name in record_model.model_fields
            if field_name in RESERVED_MODEL_FIELDS
        }
    )
    if record_collisions:
        raise ValueError(f"Schema record fields collide with runner-owned fields: {', '.join(record_collisions)}")


def _package_revision(package: str, source_path: Path | None = None) -> dict[str, Any]:
    """Record installed version and, for a local checkout, its Git state."""
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = None
    spec = importlib.util.find_spec(package)
    location = Path(spec.origin).resolve().parent if spec and spec.origin else None
    revision: dict[str, Any] | None = git_revision(source_path) if source_path and source_path.exists() else None
    if revision is None and location is not None:
        for candidate in (location, *location.parents):
            if (candidate / ".git").exists():
                revision = git_revision(candidate)
                break
    return {"version": version, "location": str(location) if location else None, "revision": revision}


def make_contract_payload(
    *,
    prompt: str,
    schema_source: str,
    schema_json: dict[str, Any],
    settings: dict[str, Any],
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    """Pure constructor kept separate so invalidation behavior is easy to test."""
    return {
        "prompt": prompt,
        "schema_source": schema_source,
        "schema_json": schema_json,
        "settings": settings,
        "dependencies": dependencies,
    }


def build_contract(config: ProjectConfig, *, service: str) -> ExtractionContract:
    """Read every material extraction input and compute one contract signature."""
    extraction = config.table("extraction")
    model = config.table("model")
    definitions = config.root / "code" / "3-extraction" / "definitions"
    prompt_path = definitions / "prompt.md"
    schema_path = definitions / "schema.py"
    prompt = prompt_path.read_text(encoding="utf-8")
    schema_source = schema_path.read_text(encoding="utf-8")
    placeholders = sorted(set(PLACEHOLDER_PATTERN.findall(prompt)))
    if placeholders:
        raise ValueError(f"Replace extraction prompt placeholders before calibration: {', '.join(placeholders)}")
    if "Replace the example fields" in schema_source:
        raise ValueError("Customize definitions/schema.py and remove its template marker before calibration")
    schema = load_schema(schema_path, str(extraction.get("schema_class", "PageExtraction")))
    validate_schema_field_names(schema, str(extraction.get("record_list_field", "records")))
    settings = {
        "pipeline_version": extraction.get("pipeline_version"),
        "pipeline_sources": {
            name: sha256_file(Path(__file__).with_name(name))
            for name in PIPELINE_SOURCE_NAMES
        },
        "model": model.get("name"),
        "location": model.get("location"),
        "reasoning": model.get("think_level"),
        "service": service,
        "max_output_tokens": model.get("max_output_tokens"),
        "temperature": model.get("temperature"),
        "media_resolution": extraction.get("media_resolution"),
        "render_dpi": extraction.get("render_dpi"),
        "render_format": extraction.get("render_format"),
    }
    with (config.root / "pyproject.toml").open("rb") as source:
        pyproject = tomllib.load(source)
    uv_sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    dependencies: dict[str, Any] = {}
    for package in ("yachay", "locro", "banknames", "pymupdf", "pydantic"):
        source_spec = uv_sources.get(package, {}) if isinstance(uv_sources, dict) else {}
        configured = source_spec.get("path") if isinstance(source_spec, dict) else None
        source_path = Path(str(configured)).expanduser().resolve() if configured else None
        dependencies[package] = _package_revision(package, source_path)
    payload = make_contract_payload(
        prompt=prompt,
        schema_source=schema_source,
        schema_json=schema.model_json_schema(),
        settings=settings,
        dependencies=dependencies,
    )
    return ExtractionContract(signature=stable_hash(payload), payload=payload, prompt=prompt, schema=schema)
