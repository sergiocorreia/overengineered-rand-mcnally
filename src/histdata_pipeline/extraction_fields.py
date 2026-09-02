"""Runner-owned extraction fields that a model response may never redefine."""

FLAT_PROVENANCE_FIELDS = (
    "manifest_index",
    "page_id",
    "pdf_relative_path",
    "physical_page",
    "source_sha256",
    "render_sha256",
    "contract_signature",
    "source_id",
    "provider",
    "title",
    "source_date",
    "final_type",
    "classification_source",
    "manual_notes",
    "ocr_method",
    "ocr_text_sha256",
    "page_manifest_sha256",
    "status",
    "error_type",
    "error_message",
)

RUNNER_PROVENANCE_FIELDS = (
    *FLAT_PROVENANCE_FIELDS,
    "render_path",
    "page_manifest",
    "usage",
    "cache_path",
    "request_started_at",
    "request_completed_at",
    "request_duration_seconds",
    "provider_call_started",
    "usage_known",
    "actual_model_settings",
)

GENERATED_FLAT_FIELDS = ("record_index", "record_id")
RESERVED_MODEL_FIELDS = frozenset((*RUNNER_PROVENANCE_FIELDS, *GENERATED_FLAT_FIELDS, "extraction"))
