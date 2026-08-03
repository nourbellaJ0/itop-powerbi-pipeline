"""
tests/test_ai_enrichment_monetique.py — Tests unitaires pour
ai_enrichment_monetique.py (classification "Support Monétique").

Aucun appel réseau : GroqClient.chat_json est systématiquement monkeypatché.
Le cache disque est isolé dans tmp_path pour chaque test qui en a besoin
(même mécanisme que test_ai_enrichment.py — le fichier cache est partagé
avec UC1, seul le namespace "__monetique__" est spécifique).
"""

import logging
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_enrichment
import ai_enrichment_monetique as aem
from ai_enrichment import GroqClient
from ai_enrichment_monetique import (
    MONETIQUE_MACRO_CATEGORIES,
    MONETIQUE_THEMES,
    _MONETIQUE_FALLBACK_THEME,
    _validate_monetique_classification,
    classify_monetique,
)
from config import Config


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _make_cfg(**overrides) -> Config:
    defaults = dict(
        input_mode="xlsx",
        itop_base_url="", itop_api_token="", itop_api_version="1.3",
        itop_class="UserRequest", itop_verify_ssl=True,
        local_xlsx_path="dummy.xlsx", local_xlsx_sheet_name="",
        sharepoint_source_file_name="incidents_itop_clean.xlsx",
        output_mode="sql", last_run_file="last_run.json",
        export_file="out.xlsx", model_export_file="model.xlsx", log_level="INFO",
        pii_salt="salt", include_agent_name=True,
        sp_tenant_id="", sp_client_id="", sp_client_secret="",
        sp_site_id="", sp_drive_id="", sp_folder_path="/",
        sql_dialect="sqlite", sql_host="", sql_port="1433", sql_database="",
        sql_user="", sql_password="", sql_table="incidents",
        ai_enrichment_enabled=True, groq_api_key="test-key",
        groq_model="llama-3.1-8b-instant", groq_model_summary="openai/gpt-oss-120b",
        groq_base_url="https://api.groq.com/openai/v1",
        groq_max_req_per_min=6000, groq_max_tokens_per_min=6000,
        ai_confidence_threshold=0.6, ai_batch_size=10,
        monetique_ai_enabled=True, monetique_confidence_threshold=0.6,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_monetique_df(n: int = 3, subcategory: str = "Support Monétique") -> pd.DataFrame:
    return pd.DataFrame({
        "reference": [f"M-{i:03d}" for i in range(n)],
        "service_subcategory": [subcategory] * n,
        "ticket_title": [f"[MXP]-LITIGE CLIENT {i}" for i in range(n)],
        "ticket_description": [f"Détail du litige numéro {i}" for i in range(n)],
    })


def _isolate_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ai_enrichment, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ai_enrichment, "_CACHE_FILE", tmp_path / "ai_classifications.json")


def _fake_batch_response(theme: str = "Litige / Chargeback", confiance: float = 0.9):
    def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
        n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
        return {"resultats": [
            {"macro_categorie": "Réclamations Clients (Service Monétique)",
             "theme": theme, "confiance": confiance, "resume": "ok"}
            for _ in range(n_items)
        ]}
    return fake_chat_json


# ──────────────────────────────────────────────────────────────────────────────
# 1. Référentiel — chargé et cohérent
# ──────────────────────────────────────────────────────────────────────────────

class TestReferential:
    def test_counts_and_consistency(self):
        assert len(MONETIQUE_THEMES) == 17
        assert len(MONETIQUE_MACRO_CATEGORIES) == 3
        for entry in MONETIQUE_THEMES:
            assert entry["macro_category"] in MONETIQUE_MACRO_CATEGORIES
            assert entry["responsabilite"] in {"Métier", "BIAT-IT", "Les deux"}
        assert _MONETIQUE_FALLBACK_THEME not in {t["theme"] for t in MONETIQUE_THEMES}


# ──────────────────────────────────────────────────────────────────────────────
# 2. Flag désactivé / clé absente → colonnes NaN, aucun appel
# ──────────────────────────────────────────────────────────────────────────────

class TestDisabledOrMissingKey:
    def test_disabled_flag_no_calls(self, monkeypatch):
        called = {"n": 0}

        def fake_chat_json(self, *args, **kwargs):
            called["n"] += 1
            return {}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg(monetique_ai_enabled=False)
        result, stats = classify_monetique(_make_monetique_df(), cfg)

        assert called["n"] == 0
        for col in ("monetique_macro_category", "monetique_theme", "monetique_responsabilite",
                    "monetique_action_metier", "monetique_action_it", "monetique_ai_confidence"):
            assert result[col].isna().all()
        assert stats["n_classified"] == 0

    def test_missing_api_key_no_calls(self, monkeypatch):
        called = {"n": 0}

        def fake_chat_json(self, *args, **kwargs):
            called["n"] += 1
            return {}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg(monetique_ai_enabled=True, groq_api_key="")
        result, stats = classify_monetique(_make_monetique_df(), cfg)

        assert called["n"] == 0
        assert result["monetique_theme"].isna().all()
        assert stats["n_classified"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. Filtre de sous-catégorie — seuls les tickets "Support Monétique" comptent
# ──────────────────────────────────────────────────────────────────────────────

class TestSubcategoryFilter:
    def test_non_monetique_rows_untouched_no_api_calls(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
            return {"resultats": [
                {"macro_categorie": "Réclamations Clients (Service Monétique)",
                 "theme": "Litige / Chargeback", "confiance": 0.9, "resume": "ok"}
                for _ in range(n_items)
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        df_mon = _make_monetique_df(2, subcategory="Support Monétique")
        df_other = _make_monetique_df(3, subcategory="Support Banque de détail")
        df_other["reference"] = [f"O-{i:03d}" for i in range(3)]
        df = pd.concat([df_mon, df_other], ignore_index=True)

        cfg = _make_cfg()
        result, stats = classify_monetique(df, cfg)

        assert stats["n_monetique_total"] == 2
        assert calls["n"] == 1  # un seul lot, uniquement pour les 2 tickets Monétique
        other_rows = result[result["service_subcategory"] == "Support Banque de détail"]
        assert other_rows["monetique_theme"].isna().all()


# ──────────────────────────────────────────────────────────────────────────────
# 4. Cache — les 4 cas du spec
# ──────────────────────────────────────────────────────────────────────────────

class TestCacheCases:
    def test_no_entry_calls_api(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def counting_fake(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            return _fake_batch_response()(self, system_prompt, user_prompt, temperature)

        monkeypatch.setattr(GroqClient, "chat_json", counting_fake)

        cfg = _make_cfg()
        df = _make_monetique_df(3)
        result, stats = classify_monetique(df, cfg)

        assert calls["n"] == 1
        assert stats["n_api_calls"] == 1
        assert stats["n_cache_hit"] == 0
        assert stats["n_classified"] == 3
        assert (result["monetique_theme"] == "Litige / Chargeback").all()

    def test_hash_match_hits_cache_zero_calls(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def counting_fake(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            return _fake_batch_response()(self, system_prompt, user_prompt, temperature)

        monkeypatch.setattr(GroqClient, "chat_json", counting_fake)

        cfg = _make_cfg()
        df = _make_monetique_df(3)

        _, stats1 = classify_monetique(df, cfg)
        assert calls["n"] == 1
        assert stats1["n_api_calls"] == 1
        assert stats1["n_cache_hit"] == 0

        _, stats2 = classify_monetique(df, cfg)
        assert calls["n"] == 1  # aucun appel réseau supplémentaire
        assert stats2["n_api_calls"] == 0
        assert stats2["n_cache_hit"] == 3

    def test_modified_text_same_reference_stays_cached(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def counting_fake(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            return _fake_batch_response()(self, system_prompt, user_prompt, temperature)

        monkeypatch.setattr(GroqClient, "chat_json", counting_fake)

        cfg = _make_cfg()
        df = _make_monetique_df(3)

        _, stats1 = classify_monetique(df, cfg)
        assert calls["n"] == 1
        assert stats1["n_api_calls"] == 1

        df2 = df.copy()
        df2.loc[0, "ticket_description"] = "Nouveau contenu totalement différent"
        _, stats2 = classify_monetique(df2, cfg)

        assert calls["n"] == 1  # aucune nouvelle classification pour la même référence
        assert stats2["n_api_calls"] == 0
        assert stats2["n_cache_hit"] == 3

    def test_text_emptied_preserves_stale_cache_and_warns(self, tmp_path, monkeypatch, caplog):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def counting_fake(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            return _fake_batch_response()(self, system_prompt, user_prompt, temperature)

        monkeypatch.setattr(GroqClient, "chat_json", counting_fake)

        cfg = _make_cfg()
        df = _make_monetique_df(2)

        classify_monetique(df, cfg)
        assert calls["n"] == 1

        df_emptied = df.copy()
        df_emptied.loc[0, "ticket_title"] = None
        df_emptied.loc[0, "ticket_description"] = None

        with caplog.at_level(logging.WARNING):
            result, stats = classify_monetique(df_emptied, cfg)

        assert calls["n"] == 1  # pas de nouvel appel API
        assert stats["n_stale_cache_text_emptied"] == 1
        assert pd.isna(result.loc[0, "monetique_theme"])
        assert "M-000" in caplog.text
        assert "vidés" in caplog.text.lower() or "vidé" in caplog.text.lower()

        # L'entrée cache pour M-000 doit être préservée (pas supprimée)
        cache = aem._load_monetique_cache()
        assert "M-000" in cache


# ──────────────────────────────────────────────────────────────────────────────
# 5. Thème halluciné / hors référentiel → repli automatique
# ──────────────────────────────────────────────────────────────────────────────

class TestInvalidResponses:
    def test_unknown_theme_falls_back(self):
        result = _validate_monetique_classification(
            {"theme": "Thème Totalement Inventé", "confiance": 0.9, "resume": "x"}
        )
        assert result["theme"] == _MONETIQUE_FALLBACK_THEME
        assert result["confiance"] == 0.0
        assert result["responsabilite"] == "À qualifier"

    def test_non_dict_response_falls_back(self):
        result = _validate_monetique_classification("pas un objet json")
        assert result["theme"] == _MONETIQUE_FALLBACK_THEME
        assert result["confiance"] == 0.0

    def test_unknown_theme_end_to_end(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
            return {"resultats": [
                {"macro_categorie": "?", "theme": "Thème Inconnu", "confiance": 0.8, "resume": ""}
                for _ in range(n_items)
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg()
        result, stats = classify_monetique(_make_monetique_df(2), cfg)

        assert (result["monetique_theme"] == _MONETIQUE_FALLBACK_THEME).all()
        assert stats["n_autre_a_qualifier"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# 6. Lot incomplet → retraitement en unitaire (wiring _call_groq_batch partagé)
# ──────────────────────────────────────────────────────────────────────────────

class TestBatchFallback:
    def test_incomplete_monetique_batch_falls_back_to_unitary(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"batch": 0, "unitary": 0}

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            if "resultats" in user_prompt:
                calls["batch"] += 1
                return {"resultats": [
                    {"macro_categorie": "Réclamations Clients (Service Monétique)",
                     "theme": "Litige / Chargeback", "confiance": 0.8, "resume": "r"}
                ]}
            calls["unitary"] += 1
            return {"macro_categorie": "Réclamations Clients (Service Monétique)",
                    "theme": "Litige / Chargeback", "confiance": 0.8, "resume": "r-unit"}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg()
        result, stats = classify_monetique(_make_monetique_df(2), cfg)

        assert calls["batch"] == 1
        assert calls["unitary"] == 2
        assert (result["monetique_theme"] == "Litige / Chargeback").all()
        assert stats["n_classified"] == 2
