"""
tests/test_transform.py — Tests unitaires pour transform.py (E1, E2, E3).
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform import (
    FIELD_MAP_XLSX,
    ROOT_CAUSE_PATTERNS,
    _apply_field_map,
    _categorize_root_cause,
    _deduplicate_by_reference,
    _normalize_status,
    _normalize_priority_urgency,
    _normalize_requester_site,
    _pseudonymize,
    _replace_empty_markers,
    _ttr_bucket,
    add_analytical_columns,
    build_clean_dataframe_from_xlsx,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _make_fact_df(**kwargs) -> pd.DataFrame:
    """Construit un DataFrame minimal avec les colonnes requises pour E3."""
    defaults = {
        "reference":         ["I-001", "I-002", "I-003"],
        "status":            ["Résolue", "Assignée", "Rejetée"],
        "priority":          ["critique", "haute", "basse"],
        "urgency":           ["haute", "haute", "basse"],
        "service_name":      ["Application métier - Fonctionnel (SN2)", "Application technique", "Réseau"],
        "service_subcategory": ["Ticket à chaud", "Support technique", "Incident réseau"],
        "team_name":         ["Equipe A", "Equipe B", "Equipe C"],
        "ci_name":           ["SERVEUR01", None, "SWITCH02"],
        "ci_class":          ["Server", None, "Switch"],
        "agent_name":        ["hash001", "hash002", "hash003"],
        "requester_org":     ["DSI", "RH", None],
        "requester_site":    ["Y114", "D001", None],
        "root_cause_raw":    ["Saturation CPU", "Problème réseau", None],
        "solution":          ["Redémarrage", None, "Rejet"],
        "resolution_code":   ["Résolu", None, "Rejeté"],
        "resolution_type":   ["Technique", None, None],
        "itop_resolution_delay_min": ["120", "0", "60"],
        "suspension_cumulated": ["0", "0", "0"],
        "reject_reason":     [None, None, "Doublon"],
        "parent_change_ref": ["C-001", None, None],
        "parent_incident_ref": [None, None, None],
        "sla_tto_breached_itop": ["non", "non", "non"],
        "sla_ttr_breached_itop": ["non", "non", "non"],
        "created_at":        pd.to_datetime(["2026-01-01 08:00", "2026-01-02 09:00", "2026-01-03 10:00"]),
        "assigned_at":       pd.to_datetime(["2026-01-01 09:00", "2026-01-02 10:00", pd.NaT]),
        "resolved_at":       pd.to_datetime(["2026-01-01 10:00", pd.NaT, pd.NaT]),
        "closed_at":         pd.to_datetime([pd.NaT, pd.NaT, pd.NaT]),
        "rejected_at":       pd.to_datetime([pd.NaT, pd.NaT, "2026-01-03 11:00"]),
        "last_update":       pd.to_datetime(["2026-01-01 10:00", "2026-01-02 10:00", "2026-01-03 11:00"]),
        "tto_deadline":      pd.to_datetime(["2026-01-01 12:00", "2026-01-02 17:00", pd.NaT]),
        "ttr_deadline":      pd.to_datetime(["2026-01-02 08:00", pd.NaT, pd.NaT]),
        "operational_status": ["open", "open", "closed"],
        "origin":            ["Interface utilisateur", "Interface utilisateur", "Téléphone"],
        "impact":            ["Utilisateur", "Département", "Utilisateur"],
    }
    defaults.update(kwargs)
    return pd.DataFrame(defaults)


# ──────────────────────────────────────────────────────────────────────────────
# E1 — Mapping
# ──────────────────────────────────────────────────────────────────────────────

class TestFieldMapXlsx:
    def test_exact_36_keys(self):
        """FIELD_MAP_XLSX doit contenir exactement 36 colonnes sources."""
        assert len(FIELD_MAP_XLSX) == 36

    def test_mandatory_columns_present(self):
        required_targets = {
            "reference", "created_at", "status", "priority",
            "service_name", "team_name", "agent_name",
        }
        assert required_targets <= set(FIELD_MAP_XLSX.values())

    def test_apply_field_map_selects_and_renames(self):
        df = pd.DataFrame({
            "Référence": ["I-001"],
            "Statut":    ["Résolue"],
            "ColonneInconnue": ["Hors mapping"],
        })
        result = _apply_field_map(df, FIELD_MAP_XLSX)
        assert "reference" in result.columns
        assert "status" in result.columns
        assert "ColonneInconnue" not in result.columns

    def test_object_description_mapped_to_ticket_fields(self):
        df = pd.DataFrame({
            "Référence":   ["I-001"],
            "Object":      ["[MXP]-CHARGEBACK NON AUTORISE"],
            "Description": ["Détail du litige"],
        })
        result = _apply_field_map(df, FIELD_MAP_XLSX)
        assert result["ticket_title"].tolist() == ["[MXP]-CHARGEBACK NON AUTORISE"]
        assert result["ticket_description"].tolist() == ["Détail du litige"]

    def test_unknown_columns_silently_dropped(self):
        df = pd.DataFrame({"ColonneInconnue": [1, 2]})
        result = _apply_field_map(df, FIELD_MAP_XLSX)
        assert result.empty or len(result.columns) == 0


# ──────────────────────────────────────────────────────────────────────────────
# E2 — Nettoyage
# ──────────────────────────────────────────────────────────────────────────────

class TestCleaning:
    def test_normalize_status_rejected(self):
        df = pd.DataFrame({"status": ["rejected", "Résolue", "Assignée", None]})
        result = _normalize_status(df)
        assert result["status"].tolist()[0] == "Rejetée"
        assert result["status"].tolist()[1] == "Résolue"
        assert result["status"].tolist()[2] == "Assignée"

    def test_normalize_priority_lowercase(self):
        df = pd.DataFrame({"priority": ["HAUTE", "Critique", "basse"], "urgency": ["HAUTE", "haute", "basse"]})
        result = _normalize_priority_urgency(df)
        assert all(v == v.lower() for v in result["priority"].dropna())
        assert all(v == v.lower() for v in result["urgency"].dropna())

    def test_normalize_requester_site_upper(self):
        df = pd.DataFrame({"requester_site": ["y114", "Y114", "d001"]})
        result = _normalize_requester_site(df)
        assert result["requester_site"].tolist() == ["Y114", "Y114", "D001"]

    def test_replace_empty_markers_root_cause(self):
        df = pd.DataFrame({"root_cause_raw": [".", "-", "N/A", "NF", "NON CONNUE", "R.A.S", "Vrai cause"]})
        result = _replace_empty_markers(df)
        assert result["root_cause_raw"].isna().sum() == 6
        assert result["root_cause_raw"].dropna().tolist() == ["Vrai cause"]

    def test_replace_empty_markers_case_insensitive(self):
        df = pd.DataFrame({"root_cause_raw": ["r.a.s", "N/a", "nf"]})
        result = _replace_empty_markers(df)
        assert result["root_cause_raw"].isna().all()

    def test_dedup_keeps_most_recent(self):
        df = pd.DataFrame({
            "reference":   ["I-001", "I-001", "I-002"],
            "last_update": pd.to_datetime(["2026-01-01", "2026-01-05", "2026-01-03"]),
        })
        result, n = _deduplicate_by_reference(df)
        assert n == 1
        assert len(result) == 2
        assert result.loc[result["reference"] == "I-001", "last_update"].iloc[0] == pd.Timestamp("2026-01-05")

    def test_dedup_no_duplicates(self):
        df = pd.DataFrame({
            "reference":   ["I-001", "I-002"],
            "last_update": pd.to_datetime(["2026-01-01", "2026-01-02"]),
        })
        result, n = _deduplicate_by_reference(df)
        assert n == 0
        assert len(result) == 2

    def test_pseudonymize_stable(self):
        salt = "test-salt"
        h1 = _pseudonymize("Jean Dupont", salt)
        h2 = _pseudonymize("Jean Dupont", salt)
        assert h1 == h2
        assert len(h1) == 8

    def test_pseudonymize_different_values(self):
        salt = "test-salt"
        h1 = _pseudonymize("Jean Dupont", salt)
        h2 = _pseudonymize("Marie Martin", salt)
        assert h1 != h2

    def test_pseudonymize_na_returns_na(self):
        result = _pseudonymize(None, "salt")
        assert pd.isna(result)


# ──────────────────────────────────────────────────────────────────────────────
# E3 — Colonnes analytiques
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyticalColumns:
    def setup_method(self):
        self.df = add_analytical_columns(_make_fact_df())

    def test_nature_fonctionnel(self):
        assert self.df.loc[0, "nature"] == "Fonctionnel"

    def test_nature_technique(self):
        assert self.df.loc[1, "nature"] == "Technique"

    def test_tto_real_min_nominal(self):
        # I-001 : assigned_at - created_at = 60 min
        assert self.df.loc[0, "tto_real_min"] == pytest.approx(60.0)

    def test_tto_real_min_nan_when_no_assignation(self):
        # I-003 : pas d'assignation → NaN
        assert pd.isna(self.df.loc[2, "tto_real_min"])

    def test_ttr_real_hours_nominal(self):
        # I-001 : resolved_at - created_at = 2h
        assert self.df.loc[0, "ttr_real_hours"] == pytest.approx(2.0)

    def test_ttr_real_hours_nan_when_no_resolution(self):
        assert pd.isna(self.df.loc[1, "ttr_real_hours"])

    def test_tto_negative_becomes_nan(self):
        df = _make_fact_df(
            assigned_at=pd.to_datetime(["2026-01-01 07:00", "2026-01-02 10:00", pd.NaT]),
            created_at=pd.to_datetime(["2026-01-01 08:00", "2026-01-02 09:00", "2026-01-03 10:00"]),
        )
        result = add_analytical_columns(df)
        assert pd.isna(result.loc[0, "tto_real_min"])  # négatif → NaN
        assert result.loc[1, "tto_real_min"] == pytest.approx(60.0)

    def test_ttr_negative_becomes_nan(self):
        df = _make_fact_df(
            resolved_at=pd.to_datetime(["2025-12-31 08:00", pd.NaT, pd.NaT]),
            created_at=pd.to_datetime(["2026-01-01 08:00", "2026-01-02 09:00", "2026-01-03 10:00"]),
        )
        result = add_analytical_columns(df)
        assert pd.isna(result.loc[0, "ttr_real_hours"])

    def test_ttr_bucket_values(self):
        assert _ttr_bucket(0.5) == "<=1h"
        assert _ttr_bucket(1.0) == "<=1h"
        assert _ttr_bucket(2.0) == "1-4h"
        assert _ttr_bucket(12.0) == "4-24h"
        assert _ttr_bucket(100.0) == "1-7j"
        assert _ttr_bucket(200.0) == ">7j"
        assert _ttr_bucket(None) == "Non résolu"

    def test_is_open(self):
        assert self.df.loc[0, "is_open"] == 0  # Résolue
        assert self.df.loc[1, "is_open"] == 1  # Assignée
        assert self.df.loc[2, "is_open"] == 0  # Rejetée

    def test_is_resolved(self):
        assert self.df.loc[0, "is_resolved"] == 1   # Résolue
        assert self.df.loc[1, "is_resolved"] == 0   # Assignée
        assert self.df.loc[2, "is_resolved"] == 0   # Rejetée

    def test_is_rejected(self):
        assert self.df.loc[0, "is_rejected"] == 0
        assert self.df.loc[2, "is_rejected"] == 1

    def test_is_critical(self):
        assert self.df.loc[0, "is_critical"] == 1  # critique
        assert self.df.loc[1, "is_critical"] == 0  # haute

    def test_sla_tto_breached_calc(self):
        # I-001 : assigned_at (09:00) < tto_deadline (12:00) → non dépassé (0)
        assert self.df.loc[0, "sla_tto_breached_calc"] == 0

    def test_sla_ttr_breached_calc(self):
        # I-001 : resolved_at (10:00) < ttr_deadline (J+1 08:00) → non dépassé (0)
        assert self.df.loc[0, "sla_ttr_breached_calc"] == 0

    def test_sla_target_hours_by_priority(self):
        from config import SLA_TARGET_HOURS
        assert self.df.loc[0, "sla_target_hours"] == SLA_TARGET_HOURS["critique"]
        assert self.df.loc[1, "sla_target_hours"] == SLA_TARGET_HOURS["haute"]

    def test_is_out_of_target(self):
        # I-001 : ttr_real_hours=2 < SLA_TARGET critique=4 → pas dépassé
        assert self.df.loc[0, "is_out_of_target"] == 0

    def test_has_root_cause(self):
        assert self.df.loc[0, "has_root_cause"] == 1
        assert self.df.loc[2, "has_root_cause"] == 0

    def test_has_ci(self):
        assert self.df.loc[0, "has_ci"] == 1
        assert self.df.loc[1, "has_ci"] == 0

    def test_has_parent_change(self):
        assert self.df.loc[0, "has_parent_change"] == 1
        assert self.df.loc[1, "has_parent_change"] == 0

    def test_created_month_format(self):
        assert self.df.loc[0, "created_month"] == "2026-01"

    def test_created_date_is_date(self):
        from datetime import date
        assert self.df.loc[0, "created_date"] == date(2026, 1, 1)

    def test_resolved_date_nan_when_unresolved(self):
        assert pd.isna(self.df.loc[1, "resolved_date"])

    def test_days_since_last_update_positive_integer(self):
        from datetime import date
        # I-001 : last_update = 2026-01-01 → (today - 2026-01-01) jours
        today = date.today()
        expected = (today - date(2026, 1, 1)).days
        assert self.df.loc[0, "days_since_last_update"] == expected

    def test_days_since_last_update_nan_when_empty(self):
        df = _make_fact_df(
            last_update=pd.to_datetime([pd.NaT, "2026-01-02 10:00", pd.NaT])
        )
        result = add_analytical_columns(df)
        assert pd.isna(result.loc[0, "days_since_last_update"])
        assert pd.notna(result.loc[1, "days_since_last_update"])
        assert pd.isna(result.loc[2, "days_since_last_update"])


# ──────────────────────────────────────────────────────────────────────────────
# E3.11 — Catégorisation des causes racines
# ──────────────────────────────────────────────────────────────────────────────

class TestRootCauseCategorization:
    @pytest.mark.parametrize("text,expected_category", [
        ("Saturation CPU du serveur",            "Saturation ressources (CPU/RAM/DB)"),
        ("Problème de mémoire insuffisante",     "Saturation ressources (CPU/RAM/DB)"),
        ("Base de données Oracle corrompue",     "Base de données"),
        ("Tablespace plein",                     "Base de données"),
        ("Coupure réseau MPLS",                  "Réseau / Sécurité"),
        ("Firewall bloquant les flux",           "Réseau / Sécurité"),
        ("Batch de traitement en erreur",        "Traitements / Flux / Interfaces"),
        ("Intégration de flux échouée",          "Traitements / Flux / Interfaces"),
        ("Mise à jour logiciel",                 "Changement / Déploiement"),
        ("Déploiement hotfix raté",              "Changement / Déploiement"),
        ("Prestataire externe non disponible",   "Tiers / Partenaire externe"),
        ("Problème avec SMT",                    "Tiers / Partenaire externe"),
        ("Disque dur du serveur défaillant",     "Infrastructure / Matériel"),
        ("Matériel datacenter hors service",     "Infrastructure / Matériel"),
        ("Certificat SSL expiré",                "Accès / Licences / Certificats"),
        ("Problème d'accès authentification",    "Accès / Licences / Certificats"),
        ("Erreur applicative inconnue",          "Applicatif / Autre"),
        ("",                                     None),  # vide → NaN
    ])
    def test_categorization(self, text, expected_category):
        result = _categorize_root_cause(text if text else None)
        if expected_category is None:
            assert pd.isna(result)
        else:
            assert result == expected_category, f"'{text}' → attendu '{expected_category}', obtenu '{result}'"

    def test_case_insensitive(self):
        assert _categorize_root_cause("SATURATION CPU") == "Saturation ressources (CPU/RAM/DB)"

    def test_accent_insensitive(self):
        # "reseau" sans accent doit correspondre au pattern "réseau"
        assert _categorize_root_cause("probleme reseau") == "Réseau / Sécurité"


# ──────────────────────────────────────────────────────────────────────────────
# Test d'intégration du pipeline complet (mode XLSX)
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildCleanDataframeFromXlsx:
    def _make_raw_xlsx_df(self) -> pd.DataFrame:
        """DataFrame simulant un export iTop (noms de colonnes français)."""
        return pd.DataFrame({
            "Référence":                                    ["I-001", "I-002", "I-001"],
            "Date de début":                               ["2026-01-01 08:00:00", "2026-01-02 09:00:00", "2026-01-01 08:00:00"],
            "Date d'assignation":                          ["2026-01-01 09:00:00", None, None],
            "Date de résolution":                          ["2026-01-01 10:00:00", None, "2026-01-01 10:00:00"],
            "Date de fermeture":                           [None, None, None],
            "Date de Reject":                              [None, None, None],
            "Dernière mise à jour":                        ["2026-01-01 10:00:00", "2026-01-02 09:00:00", "2026-01-01 11:00:00"],
            "Statut":                                      ["Résolue", "Assignée", "Résolue"],
            "Statut opérationnel":                         ["closed", "open", "closed"],
            "Priorité":                                    ["haute", "critique", "haute"],
            "Urgence":                                     ["haute", "critique", "haute"],
            "Impact":                                      ["Utilisateur", "Département", "Utilisateur"],
            "Origine":                                     ["Interface utilisateur", "Téléphone", "Interface utilisateur"],
            "Service->Nom":                                ["App métier - Fonctionnel (SN2)", "App technique", "App métier - Fonctionnel (SN2)"],
            "Sous catégorie de service->Nom":              ["Ticket à chaud", "Support", "Ticket à chaud"],
            "CI->Nom":                                     ["SERVEUR01", None, "SERVEUR01"],
            "CI->Sous-classe de CI":                       ["Server", None, "Server"],
            "Equipe->Nom":                                 ["Equipe A", "Equipe B", "Equipe A"],
            "Intervenant->Nom complet":                    ["Jean Dupont", "Marie Martin", "Jean Dupont"],
            "Affectation du demandeur->Nom organisation":  ["DSI", "RH", "DSI"],
            "Cause Racine":                                ["Saturation CPU", None, "Saturation CPU"],
            "Solution":                                    ["Redémarrage", None, "Redémarrage"],
            "Code de résolution":                          ["Résolu", None, "Résolu"],
            "Type Resolution":                             ["Technique", None, "Technique"],
            "Délai de résolution":                         ["120", None, "120"],
            "Temps cumulé de suspension":                  ["0", "0", "0"],
            "Echéance TTO":                                ["2026-01-01 12:00:00", "2026-01-02 13:00:00", "2026-01-01 12:00:00"],
            "Echéance TTR":                                ["2026-01-02 08:00:00", None, "2026-01-02 08:00:00"],
            "SLA TTO dépassé ?":                           ["non", "non", "non"],
            "SLA TTR dépassé ?":                           ["non", "non", "non"],
            "Incident parent->Référence":                  [None, None, None],
            "Changement parent->Référence":                ["C-001", None, "C-001"],
            "Raison de Reject":                            [None, None, None],
        })

    def test_pipeline_returns_correct_row_count(self):
        raw = self._make_raw_xlsx_df()
        df, n_dupes = build_clean_dataframe_from_xlsx(raw, pii_salt="test")
        assert len(df) == 2       # 3 lignes - 1 doublon I-001
        assert n_dupes == 1

    def test_agent_name_in_clear_by_default(self):
        raw = self._make_raw_xlsx_df()
        # include_agent_name=True par défaut → nom en clair
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test")
        assert "agent_name" in df.columns
        agent_vals = df["agent_name"].tolist()
        # Le nom Jean Dupont doit être présent en clair (row survivante après dédup)
        assert any("Dupont" in str(v) or "Jean" in str(v) for v in agent_vals)

    def test_agent_name_hashed_when_flag_false(self):
        raw = self._make_raw_xlsx_df()
        # include_agent_name=False → hash SHA-256 (8 chars)
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test", include_agent_name=False)
        assert "agent_name" in df.columns
        agent_vals = df["agent_name"].dropna().tolist()
        assert not any("Dupont" in str(v) or "Martin" in str(v) for v in agent_vals)
        assert all(len(str(v)) == 8 for v in agent_vals)

    def test_agent_name_empty_becomes_non_assigne(self):
        raw = self._make_raw_xlsx_df()
        # Vider le nom de l'intervenant pour la première ligne
        raw.loc[0, "Intervenant->Nom complet"] = None
        raw.loc[0, "Dernière mise à jour"] = "2026-01-01 23:00:00"
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test", include_agent_name=True)
        assert "Non assigné" in df["agent_name"].values

    def test_status_rejected_normalized(self):
        raw = self._make_raw_xlsx_df()
        raw.loc[0, "Statut"] = "rejected"
        # Rendre I-001 row 0 la plus récente pour qu'elle survive au dédoublonnage
        raw.loc[0, "Dernière mise à jour"] = "2026-01-01 23:00:00"
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test")
        assert "rejected" not in df["status"].values
        assert "Rejetée" in df["status"].values

    def test_analytical_columns_present(self):
        raw = self._make_raw_xlsx_df()
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test")
        for col in ["tto_real_min", "ttr_real_hours", "ttr_bucket", "is_open",
                    "is_resolved", "is_rejected", "is_critical", "nature",
                    "root_cause_category", "created_month", "created_date"]:
            assert col in df.columns, f"Colonne '{col}' manquante dans le résultat."

    def test_no_pii_in_output(self):
        raw = self._make_raw_xlsx_df()
        df, _ = build_clean_dataframe_from_xlsx(raw, pii_salt="test")
        # Aucune colonne sensible ne doit subsister
        forbidden = {"requester_email", "requester_firstname", "requester_lastname",
                     "agent_email", "mobile_phone"}
        assert not forbidden & set(df.columns)
