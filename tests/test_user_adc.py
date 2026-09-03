import json
from pathlib import Path

import pytest

from histdata_pipeline.user_adc import require_user_adc


def write_adc(path: Path, *, credential_type: str = "authorized_user", quota_project: str = "project-1") -> None:
    path.write_text(
        json.dumps({"type": credential_type, "quota_project_id": quota_project, "client_secret": "not-inspected"}),
        encoding="utf-8",
    )


def test_user_adc_preflight_accepts_only_matching_authorized_user_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adc = tmp_path / "application_default_credentials.json"
    write_adc(adc)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    assert require_user_adc("project-1", adc_path=adc) == adc

    write_adc(adc, credential_type="service_account")
    with pytest.raises(RuntimeError, match="authorized_user|user ADC"):
        require_user_adc("project-1", adc_path=adc)

    write_adc(adc, quota_project="other-project")
    with pytest.raises(RuntimeError, match="set-quota-project project-1"):
        require_user_adc("project-1", adc_path=adc)


def test_user_adc_preflight_rejects_any_credential_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adc = tmp_path / "application_default_credentials.json"
    write_adc(adc)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")

    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        require_user_adc("project-1", adc_path=adc)


def test_user_adc_preflight_reports_missing_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(RuntimeError, match="application-default login"):
        require_user_adc("project-1", adc_path=tmp_path / "missing.json")
