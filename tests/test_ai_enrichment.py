"""
tests/test_ai_enrichment.py — Tests unitaires pour ai_enrichment.py (UC1/UC4).

Aucun appel réseau : GroqClient.chat_json est systématiquement monkeypatché.
Le cache disque est isolé dans tmp_path pour chaque test qui en a besoin.
"""

import re
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_enrichment
from ai_enrichment import (
    GroqClient,
    _INDETERMINE,
    _validate_classification,
    enrich_dataframe,
    redact,
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


def _make_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "reference": [f"I-{i:03d}" for i in range(n)],
        "root_cause_raw": [f"Saturation CPU serveur {i}" for i in range(n)],
        "solution": [f"Redémarrage du service {i}" for i in range(n)],
        "root_cause_category": ["Saturation ressources (CPU/RAM/DB)"] * n,
    })


def _isolate_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ai_enrichment, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ai_enrichment, "_CACHE_FILE", tmp_path / "ai_classifications.json")


# ──────────────────────────────────────────────────────────────────────────────
# 1. redact() — cas réels
# ──────────────────────────────────────────────────────────────────────────────

class TestRedact:
    def test_account_number(self):
        assert redact("8925481301 ACCOUNT CLOSED") == "[NUM] ACCOUNT CLOSED"

    def test_rib_tn59(self):
        result = redact("Virement vers TN5910006035183598703226 refusé")
        assert "[RIB]" in result
        assert "TN59" not in result

    def test_rib_20_digits(self):
        assert redact("12345678901234567890") == "[RIB]"

    def test_email(self):
        result = redact("Contacter jean.dupont@biat.com.tn pour suite")
        assert "[EMAIL]" in result
        assert "@" not in result

    def test_phone_216(self):
        result = redact("Rappeler au +216 12 345 678 svp")
        assert "[TEL]" in result

    def test_ft_reference(self):
        assert redact("Ticket FT26054344260278 clôturé") == "Ticket [REF] clôturé"

    def test_mat_reference(self):
        result = redact("Voir MAT98765 pour le détail")
        assert "[REF]" in result

    def test_none_and_na_return_empty_string(self):
        assert redact(None) == ""
        assert redact(pd.NA) == ""
        assert redact(float("nan")) == ""

    def test_plain_text_untouched(self):
        assert redact("Panne réseau standard") == "Panne réseau standard"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Flag désactivé → colonnes NaN, aucun appel
# ──────────────────────────────────────────────────────────────────────────────

class TestDisabledOrMissingKey:
    def test_disabled_flag_no_calls(self, monkeypatch):
        called = {"n": 0}

        def fake_chat_json(self, *args, **kwargs):
            called["n"] += 1
            return {}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg(ai_enrichment_enabled=False)
        result, stats = enrich_dataframe(_make_df(), cfg)

        assert called["n"] == 0
        assert result["ai_cause_category"].isna().all()
        assert result["ai_cause_confidence"].isna().all()
        assert result["ai_summary"].isna().all()
        assert stats["n_classified"] == 0

    def test_missing_api_key_no_calls(self, monkeypatch):
        called = {"n": 0}

        def fake_chat_json(self, *args, **kwargs):
            called["n"] += 1
            return {}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg(ai_enrichment_enabled=True, groq_api_key="")
        result, stats = enrich_dataframe(_make_df(), cfg)

        assert called["n"] == 0
        assert result["ai_cause_category"].isna().all()
        assert stats["n_classified"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. Cache : deuxième run sur les mêmes données → 0 appel
# ──────────────────────────────────────────────────────────────────────────────

class TestCache:
    def test_second_run_hits_cache(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
            return {"resultats": [
                {"categorie": "Saturation ressources (CPU/RAM/DB)", "confiance": 0.9, "resume": "ok"}
                for _ in range(n_items)
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg()
        df = _make_df(3)

        _, stats1 = enrich_dataframe(df, cfg)
        assert calls["n"] == 1
        assert stats1["n_api_calls"] == 1
        assert stats1["n_cache_hit"] == 0

        _, stats2 = enrich_dataframe(df, cfg)
        assert calls["n"] == 1  # aucun appel réseau supplémentaire
        assert stats2["n_api_calls"] == 0
        assert stats2["n_cache_hit"] == 3

    def test_modified_text_same_reference_stays_cached(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            calls["n"] += 1
            n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
            return {"resultats": [
                {"categorie": "Saturation ressources (CPU/RAM/DB)", "confiance": 0.9, "resume": "ok"}
                for _ in range(n_items)
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg()
        df_initial = _make_df(1)
        _, stats1 = enrich_dataframe(df_initial, cfg)
        assert calls["n"] == 1
        assert stats1["n_api_calls"] == 1

        df_modified = _make_df(1)
        df_modified.loc[0, "root_cause_raw"] = "Texte modifié après première analyse"
        _, stats2 = enrich_dataframe(df_modified, cfg)

        assert calls["n"] == 1
        assert stats2["n_api_calls"] == 0
        assert stats2["n_cache_hit"] == 1

    def test_cache_persisted_incrementally_survives_mid_run_crash(self, tmp_path, monkeypatch):
        """Regression test for a real CI incident: a GitHub Actions job hit
        its 30-minute timeout mid-classification and was killed. Because the
        cache used to be saved only once, after the full loop, all batches
        already classified in that run were lost — the next run started from
        zero again. The cache must now be flushed to disk after every batch,
        so a crash after N batches still leaves those N batches' results on
        disk for the next run to pick up."""
        _isolate_cache(monkeypatch, tmp_path)

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            n_items = len(re.findall(r"^\d+\. ", user_prompt, flags=re.MULTILINE))
            return {"resultats": [
                {"categorie": "Saturation ressources (CPU/RAM/DB)", "confiance": 0.9, "resume": "ok"}
                for _ in range(n_items)
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        # ai_batch_size=1 -> chaque ticket est son propre lot ; on "tue" le
        # run juste après le 2e lot pour simuler un job coupé en plein vol.
        cfg = _make_cfg(ai_batch_size=1)
        df = _make_df(4)

        original_classify_batch = ai_enrichment._classify_batch
        call_count = {"n": 0}

        def crashing_classify_batch(client, texts):
            call_count["n"] += 1
            if call_count["n"] > 2:
                raise RuntimeError("Simulated job kill (CI timeout)")
            return original_classify_batch(client, texts)

        monkeypatch.setattr(ai_enrichment, "_classify_batch", crashing_classify_batch)

        with pytest.raises(RuntimeError, match="Simulated job kill"):
            enrich_dataframe(df, cfg)

        # Les 2 lots traités avant le crash doivent déjà être sur disque,
        # pas seulement en mémoire dans la fonction qui vient de planter.
        cache_on_disk = ai_enrichment._load_cache()
        assert len(cache_on_disk) == 2

        # Un nouveau run repart avec ces 2 entrées déjà en cache (0 appel
        # réseau les concernant) plutôt que de tout reclassifier depuis zéro.
        monkeypatch.setattr(ai_enrichment, "_classify_batch", original_classify_batch)
        _, stats2 = enrich_dataframe(df, cfg)
        assert stats2["n_cache_hit"] == 2
        assert stats2["n_api_calls"] == 2  # seulement les 2 tickets restants


# ──────────────────────────────────────────────────────────────────────────────
# 4. Réponse JSON invalide / catégorie hors liste → fallback Indéterminé
# ──────────────────────────────────────────────────────────────────────────────

class TestInvalidResponses:
    def test_category_out_of_list_falls_back(self):
        result = _validate_classification({"categorie": "Bidule Inconnu", "confiance": 0.8, "resume": "x"})
        assert result["categorie"] == _INDETERMINE
        assert result["confiance"] == 0.0

    def test_non_dict_response_falls_back(self):
        result = _validate_classification("pas un objet json")
        assert result["categorie"] == _INDETERMINE
        assert result["confiance"] == 0.0

    def test_durable_api_failure_does_not_crash(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)

        def failing_chat_json(self, *args, **kwargs):
            raise ai_enrichment.GroqAPIError("échec simulé")

        monkeypatch.setattr(GroqClient, "chat_json", failing_chat_json)

        cfg = _make_cfg()
        result, stats = enrich_dataframe(_make_df(2), cfg)  # ne doit pas lever d'exception

        assert (result["ai_cause_category"] == _INDETERMINE).all()
        assert stats["n_indetermine"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# 5. Consolidation root_cause_category_final (3 branches)
# ──────────────────────────────────────────────────────────────────────────────

class TestConsolidation:
    def test_three_branches(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            return {"resultats": [
                {"categorie": "Réseau / Sécurité", "confiance": 0.9, "resume": "r1"},
                {"categorie": "Tiers / Partenaire externe", "confiance": 0.3, "resume": "r2"},
                {"categorie": _INDETERMINE, "confiance": 0.0, "resume": ""},
            ]}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        df = pd.DataFrame({
            "reference": ["I-001", "I-002", "I-003"],
            "root_cause_raw": ["texte1", "texte2", "texte3"],
            "solution": [None, None, None],
            "root_cause_category": ["Base de données", "Infrastructure / Matériel", pd.NA],
        })
        cfg = _make_cfg(ai_confidence_threshold=0.6)
        result, _ = enrich_dataframe(df, cfg)

        final = result["root_cause_category_final"].tolist()
        # Branche 1 : confiance IA >= seuil → catégorie IA
        assert final[0] == "Réseau / Sécurité"
        # Branche 2 : confiance IA < seuil → repli sur la catégorie regex
        assert final[1] == "Infrastructure / Matériel"
        # Branche 3 : ni IA fiable, ni regex → NaN
        assert pd.isna(final[2])


# ──────────────────────────────────────────────────────────────────────────────
# 6. Batch incomplet retraité en unitaire
# ──────────────────────────────────────────────────────────────────────────────

class TestBatchFallback:
    def test_incomplete_batch_falls_back_to_unitary(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"batch": 0, "unitary": 0}

        def fake_chat_json(self, system_prompt, user_prompt, temperature=0.0):
            if "resultats" in user_prompt:
                calls["batch"] += 1
                # Un seul résultat retourné au lieu des 2 tickets envoyés → incomplet
                return {"resultats": [
                    {"categorie": "Base de données", "confiance": 0.8, "resume": "r"}
                ]}
            calls["unitary"] += 1
            return {"categorie": "Base de données", "confiance": 0.8, "resume": "r-unit"}

        monkeypatch.setattr(GroqClient, "chat_json", fake_chat_json)

        cfg = _make_cfg()
        result, stats = enrich_dataframe(_make_df(2), cfg)

        assert calls["batch"] == 1
        assert calls["unitary"] == 2  # repli unitaire pour chacun des 2 tickets du lot
        assert (result["ai_cause_category"] == "Base de données").all()
        assert stats["n_classified"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# 7. Throttle TPM (tokens/minute) — fenêtre glissante
# ──────────────────────────────────────────────────────────────────────────────

class TestTokenThrottle:
    def test_no_wait_when_under_budget(self, monkeypatch):
        cfg = _make_cfg(groq_max_tokens_per_min=6000)
        client = GroqClient(cfg)
        slept = {"called": False}
        monkeypatch.setattr(ai_enrichment.time, "sleep", lambda s: slept.__setitem__("called", True))

        client._wait_for_token_budget(100)

        assert not slept["called"]

    def test_waits_when_budget_exceeded(self, monkeypatch):
        cfg = _make_cfg(groq_max_tokens_per_min=100)
        client = GroqClient(cfg)
        client._record_tokens(90)
        slept = {"seconds": None}
        monkeypatch.setattr(ai_enrichment.time, "sleep", lambda s: slept.__setitem__("seconds", s))

        client._wait_for_token_budget(50)  # 90 + 50 > 100 → doit attendre

        assert slept["seconds"] is not None
        assert slept["seconds"] > 0

    def test_old_entries_purged_from_window(self, monkeypatch):
        cfg = _make_cfg(groq_max_tokens_per_min=100)
        client = GroqClient(cfg)
        # Entrée artificiellement vieille de 61s → doit être purgée, donc pas d'attente.
        client._token_window.append((time.monotonic() - 61, 90))
        slept = {"called": False}
        monkeypatch.setattr(ai_enrichment.time, "sleep", lambda s: slept.__setitem__("called", True))

        client._wait_for_token_budget(50)

        assert not slept["called"]


# ──────────────────────────────────────────────────────────────────────────────
# 8. Retry-After honoré sur 429/5xx (plutôt qu'un backoff exponentiel aveugle)
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryAfter:
    def test_parses_retry_after_header(self):
        resp = requests.Response()
        resp.headers["Retry-After"] = "7"
        assert ai_enrichment._parse_retry_after(resp) == 7.0

    def test_missing_header_returns_none(self):
        resp = requests.Response()
        assert ai_enrichment._parse_retry_after(resp) is None

    def test_caps_to_max_wait(self):
        resp = requests.Response()
        resp.headers["Retry-After"] = "600"
        assert ai_enrichment._parse_retry_after(resp) == ai_enrichment._MAX_RETRY_AFTER_SECONDS


# ──────────────────────────────────────────────────────────────────────────────
# 9. Echec API durable sur un lot → pas de tempête de rappels unitaires
# ──────────────────────────────────────────────────────────────────────────────

class TestDurableBatchFailureFastPath:
    def test_durable_failure_skips_unitary_retries(self, tmp_path, monkeypatch):
        _isolate_cache(monkeypatch, tmp_path)
        calls = {"n": 0}

        def always_failing(self, *args, **kwargs):
            calls["n"] += 1
            raise ai_enrichment.GroqAPIError("HTTP 429 (3 tentatives épuisées)")

        monkeypatch.setattr(GroqClient, "chat_json", always_failing)

        cfg = _make_cfg()
        result, stats = enrich_dataframe(_make_df(10), cfg)

        # Un seul appel pour le lot (pas de repli en 10 appels unitaires supplémentaires).
        assert calls["n"] == 1
        assert (result["ai_cause_category"] == _INDETERMINE).all()
        assert stats["n_indetermine"] == 10
