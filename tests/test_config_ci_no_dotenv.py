"""Verifies the pipeline can be configured entirely from process environment
variables, with no ``.env`` file present — the situation in GitHub Actions,
where secrets are injected as real environment variables and no ``.env``
file is ever written to disk.
"""

import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config


def test_load_dotenv_is_a_noop_when_file_is_absent(tmp_path):
    """load_dotenv() must not raise when the target .env file does not exist —
    this is exactly config.py's call pattern (override=False), and it's what
    makes the pipeline runnable in CI where no .env is ever written."""
    missing_path = tmp_path / "does-not-exist" / ".env"
    assert not missing_path.exists()

    result = load_dotenv(dotenv_path=missing_path, override=False)

    assert result is False  # no-op signal from python-dotenv, no exception raised


def test_load_config_succeeds_from_env_vars_only(monkeypatch):
    """Simulates a GitHub Actions run: every required variable is present in
    os.environ (injected from secrets.*), no .env file is involved."""
    monkeypatch.setenv("INPUT_MODE", "sharepoint")
    monkeypatch.setenv("OUTPUT_MODE", "sharepoint")
    monkeypatch.setenv("SHAREPOINT_SOURCE_FILE_NAME", "incidents_itop_clean.xlsx")
    monkeypatch.setenv("SHAREPOINT_TENANT_ID", "tenant")
    monkeypatch.setenv("SHAREPOINT_CLIENT_ID", "client")
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SHAREPOINT_SITE_ID", "site")
    monkeypatch.setenv("SHAREPOINT_DRIVE_ID", "drive")
    monkeypatch.setenv("SHAREPOINT_FOLDER_PATH", "Rapports/iTop")
    monkeypatch.delenv("LOCAL_XLSX_PATH", raising=False)
    monkeypatch.delenv("ITOP_BASE_URL", raising=False)
    monkeypatch.delenv("ITOP_API_TOKEN", raising=False)

    cfg = load_config()

    assert cfg.input_mode == "sharepoint"
    assert cfg.output_mode == "sharepoint"
    assert cfg.sp_tenant_id == "tenant"


def test_load_config_fails_clearly_when_a_required_var_is_missing(monkeypatch):
    """In API mode, ITOP_BASE_URL/ITOP_API_TOKEN are required. A missing var
    must raise EnvironmentError with an actionable message, not crash with a
    KeyError or silently proceed — this is what lets main.py exit non-zero
    with a clear log line instead of a bare traceback."""
    monkeypatch.setenv("INPUT_MODE", "api")
    monkeypatch.delenv("ITOP_BASE_URL", raising=False)
    monkeypatch.delenv("ITOP_API_TOKEN", raising=False)

    with pytest.raises(EnvironmentError, match="ITOP_BASE_URL"):
        load_config()
