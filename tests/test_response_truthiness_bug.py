"""Regression test for a requests gotcha: requests.Response.__bool__()
returns self.ok (False for any 4xx/5xx status), so `if exc.response` is
*always* falsy on a genuine HTTP error — silently discarding the real
status code and response body. Every HTTPError handler must compare
`exc.response is not None`, never truthiness.

Found via a real GitHub Actions run: sharepoint_status.write_run_status()
logged "HTTP ?" with an empty body instead of the actual error detail.
"""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import sharepoint_status
from config import Config


def _make_cfg(**overrides) -> Config:
    base = dict(
        input_mode="sharepoint",
        itop_base_url="",
        itop_api_token="",
        itop_api_version="1.3",
        itop_class="UserRequest",
        itop_verify_ssl=True,
        local_xlsx_path="",
        local_xlsx_sheet_name="Sheet1",
        sharepoint_source_file_name="incidents_itop_clean.xlsx",
        output_mode="sharepoint",
        last_run_file="last_run.json",
        export_file="incidents_itop_clean.xlsx",
        model_export_file="incidents_itop_model.xlsx",
        log_level="INFO",
        pii_salt="salt",
        include_agent_name=True,
        sp_tenant_id="tenant",
        sp_client_id="client",
        sp_client_secret="secret",
        sp_site_id="site",
        sp_drive_id="drive",
        sp_folder_path="Rapports/iTop",
        sql_dialect="sqlite",
        sql_host="",
        sql_port="1433",
        sql_database="",
        sql_user="",
        sql_password="",
        sql_table="incidents",
        ai_enrichment_enabled=False,
        groq_api_key="",
        groq_model="llama-3.1-8b-instant",
        groq_model_summary="openai/gpt-oss-120b",
        groq_base_url="https://api.groq.com/openai/v1",
        groq_max_req_per_min=30,
        groq_max_tokens_per_min=6000,
        ai_confidence_threshold=0.6,
        ai_batch_size=10,
        monetique_ai_enabled=False,
        monetique_confidence_threshold=0.6,
        run_status_list_name="PipelineStatus",
    )
    base.update(overrides)
    return Config(**base)


def test_real_response_object_is_falsy_on_error_status():
    """Sanity-check the actual requests behaviour this bug hinges on."""
    resp = requests.Response()
    resp.status_code = 404
    assert not resp  # this is exactly the trap: a real, non-None object

    resp_ok = requests.Response()
    resp_ok.status_code = 200
    assert resp_ok


def test_write_run_status_reports_real_status_code_on_4xx(monkeypatch, caplog):
    """A 404 (e.g. wrong list name) must surface as 'HTTP 404' with the
    real response body, not 'HTTP ?' with nothing."""
    cfg = _make_cfg()
    monkeypatch.setattr("sharepoint_status._get_access_token", lambda c: "fake-token")

    class _ErrorResponse:
        status_code = 404
        text = "List 'PipelineStatus' does not exist"

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(
                "404 Client Error", response=self  # type: ignore[arg-type]
            )

    monkeypatch.setattr(
        "sharepoint_status.requests.post",
        lambda *a, **k: _ErrorResponse(),
    )

    with caplog.at_level("WARNING"):
        sharepoint_status.write_run_status(cfg, "OK")

    warning_text = "\n".join(caplog.messages)
    assert "HTTP 404" in warning_text
    assert "does not exist" in warning_text
    assert "HTTP ?" not in warning_text
