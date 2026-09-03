"""Validate the non-secret metadata for gcloud user ADC."""

import json
import os
from pathlib import Path


def require_user_adc(project_id: str, *, adc_path: Path | None = None) -> Path:
    """Require authorized-user ADC with the project's matching quota project."""

    project_id = project_id.strip()
    if not project_id:
        raise RuntimeError("A Google Cloud project ID is required for user ADC")
    if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is set. Unset it; this project uses gcloud user ADC.")
    if adc_path is None:
        config_root = Path(os.environ.get("CLOUDSDK_CONFIG", Path.home() / ".config" / "gcloud")).expanduser()
        adc_path = config_root / "application_default_credentials.json"
    try:
        metadata = json.loads(adc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("User ADC is unavailable. Run: gcloud auth application-default login") from error
    if not isinstance(metadata, dict) or metadata.get("type") != "authorized_user":
        raise RuntimeError("Expected gcloud user ADC, not service-account credentials.")
    if metadata.get("quota_project_id") != project_id:
        raise RuntimeError(f"Configure the ADC quota project with: gcloud auth application-default set-quota-project {project_id}")
    return adc_path
