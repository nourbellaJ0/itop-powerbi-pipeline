"""Tests for sharepoint_status.py — run-status feedback written to a
SharePoint list, so the business user sees En cours / OK / Erreur even when
the pipeline is triggered remotely (GitHub Actions / Power Automate)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sharepoint_status
from config import Config
from sharepoint_loader import SharePointAuthError


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


class _FakeResponse:
    def __init__(self, status_code=201):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)


def test_write_run_status_skips_when_list_name_empty(monkeypatch):
    cfg = _make_cfg(run_status_list_name="")
    calls = []
    monkeypatch.setattr(
        "sharepoint_status.requests.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )

    sharepoint_status.write_run_status(cfg, "En cours")

    assert calls == []


def test_write_run_status_skips_when_site_id_missing(monkeypatch):
    cfg = _make_cfg(sp_site_id="")
    calls = []
    monkeypatch.setattr(
        "sharepoint_status.requests.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )

    sharepoint_status.write_run_status(cfg, "En cours")

    assert calls == []


def test_write_run_status_posts_expected_fields(monkeypatch):
    cfg = _make_cfg()
    captured = {}

    monkeypatch.setattr("sharepoint_status._get_access_token", lambda c: "fake-token")

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(201)

    monkeypatch.setattr("sharepoint_status.requests.post", fake_post)

    sharepoint_status.write_run_status(cfg, "Erreur", message="boom")

    assert captured["url"] == (
        "https://graph.microsoft.com/v1.0/sites/site/lists/PipelineStatus/items"
    )
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    fields = captured["json"]["fields"]
    assert fields["Statut"] == "Erreur"
    assert fields["Title"] == "boom"
    assert "DateHeure" in fields


def test_write_run_status_truncates_long_message(monkeypatch):
    cfg = _make_cfg()
    captured = {}

    monkeypatch.setattr("sharepoint_status._get_access_token", lambda c: "fake-token")

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("sharepoint_status.requests.post", fake_post)

    sharepoint_status.write_run_status(cfg, "Erreur", message="x" * 5000)

    assert len(captured["json"]["fields"]["Title"]) == sharepoint_status._MESSAGE_MAX_LEN


def test_write_run_status_never_raises_on_auth_failure(monkeypatch):
    cfg = _make_cfg()

    def fake_token(c):
        raise SharePointAuthError("no token")

    monkeypatch.setattr("sharepoint_status._get_access_token", fake_token)

    # Must not raise.
    sharepoint_status.write_run_status(cfg, "OK")


def test_write_run_status_never_raises_on_network_error(monkeypatch):
    cfg = _make_cfg()
    monkeypatch.setattr("sharepoint_status._get_access_token", lambda c: "fake-token")

    def fake_post(*a, **k):
        import requests
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr("sharepoint_status.requests.post", fake_post)

    # Must not raise.
    sharepoint_status.write_run_status(cfg, "OK")
