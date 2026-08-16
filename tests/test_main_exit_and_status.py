"""Tests for main.py's top-level safety net:
- main() always returns 0 on success, 1 on any failure — including an
  unhandled exception escaping _execute_pipeline() (not just the explicit
  return-1 paths already covered elsewhere).
- write_run_status() is called with "En cours" at the start, then "OK" or
  "Erreur" depending on the outcome — so a business user gets feedback even
  when the pipeline crashes.
"""

import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from config import Config


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
        run_status_list_name="PipelineStatus",
    )


def _patch_common(monkeypatch, cfg):
    """Wire main() up to a fake config/args/status-writer, leaving
    _execute_pipeline as the only variable under test."""
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "parse_args", lambda: Namespace())
    statuses = []
    monkeypatch.setattr(
        "sharepoint_status.write_run_status",
        lambda cfg_, statut, message="": statuses.append((statut, message)),
    )
    return statuses


def test_main_returns_0_and_writes_ok_on_success(monkeypatch):
    cfg = _make_cfg()
    statuses = _patch_common(monkeypatch, cfg)
    monkeypatch.setattr(main, "_execute_pipeline", lambda cfg_, args, run_start: 0)

    result = main.main()

    assert result == 0
    assert statuses[0][0] == "En cours"
    assert statuses[-1][0] == "OK"


def test_main_returns_1_and_writes_erreur_on_explicit_failure(monkeypatch):
    cfg = _make_cfg()
    statuses = _patch_common(monkeypatch, cfg)
    monkeypatch.setattr(main, "_execute_pipeline", lambda cfg_, args, run_start: 1)

    result = main.main()

    assert result == 1
    assert statuses[-1][0] == "Erreur"


def test_main_returns_1_and_writes_erreur_on_unhandled_exception(monkeypatch):
    """This is the critical case: if the pipeline crashes with an unexpected
    exception, main() must still return 1 (non-zero exit -> red CI run) and
    must still report "Erreur" to SharePoint, not silently swallow it."""
    cfg = _make_cfg()
    statuses = _patch_common(monkeypatch, cfg)

    def boom(cfg_, args, run_start):
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(main, "_execute_pipeline", boom)

    result = main.main()

    assert result == 1
    assert statuses[-1][0] == "Erreur"
    assert "unexpected crash" in statuses[-1][1]


def test_main_returns_1_on_config_error_without_crashing(monkeypatch):
    def raise_env_error():
        raise EnvironmentError("ITOP_API_TOKEN manquant")

    monkeypatch.setattr(main, "load_config", raise_env_error)
    monkeypatch.setattr(main, "parse_args", lambda: Namespace())

    result = main.main()

    assert result == 1
