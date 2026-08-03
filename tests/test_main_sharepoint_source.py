"""Tests for the SharePoint source input mode in main.py."""

import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from config import Config, load_config


def _make_cfg() -> Config:
    return Config(
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
    )


def test_load_config_accepts_sharepoint_mode(monkeypatch):
    monkeypatch.setenv("INPUT_MODE", "sharepoint")
    monkeypatch.setenv("OUTPUT_MODE", "sharepoint")
    monkeypatch.setenv("SHAREPOINT_SOURCE_FILE_NAME", "incidents_itop_clean.xlsx")
    monkeypatch.setenv("SHAREPOINT_TENANT_ID", "tenant")
    monkeypatch.setenv("SHAREPOINT_CLIENT_ID", "client")
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SHAREPOINT_DRIVE_ID", "drive")
    monkeypatch.setenv("SHAREPOINT_FOLDER_PATH", "Rapports/iTop")
    monkeypatch.delenv("LOCAL_XLSX_PATH", raising=False)

    cfg = load_config()

    assert cfg.input_mode == "sharepoint"
    assert cfg.local_xlsx_path == ""
    assert cfg.sharepoint_source_file_name == "incidents_itop_clean.xlsx"


def test_sharepoint_pipeline_downloads_before_etl(monkeypatch, tmp_path):
    cfg = _make_cfg()
    args = Namespace(full_refresh=False)
    download_calls = {}

    def fake_download(remote_filename, local_filepath, loaded_cfg):
        download_calls["remote_filename"] = remote_filename
        download_calls["local_filepath"] = local_filepath
        download_calls["cfg"] = loaded_cfg
        path = Path(local_filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-xlsx")
        return str(path)

    def fake_xlsx_pipeline_from_path(file_path, sheet_name, pii_salt, include_agent_name):
        download_calls["pipeline_path"] = file_path
        download_calls["sheet_name"] = sheet_name
        download_calls["pii_salt"] = pii_salt
        download_calls["include_agent_name"] = include_agent_name
        return ("df", 0, 1)

    monkeypatch.setattr("sharepoint_loader.download_from_sharepoint", fake_download)
    monkeypatch.setattr(main, "_run_xlsx_pipeline_from_path", fake_xlsx_pipeline_from_path)

    result = main._run_sharepoint_pipeline(cfg, args)

    assert result == ("df", 0, 1)
    assert download_calls["remote_filename"] == "incidents_itop_clean.xlsx"
    assert download_calls["cfg"] is cfg
    assert download_calls["pipeline_path"] == download_calls["local_filepath"]
