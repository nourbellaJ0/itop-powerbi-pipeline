"""
tests/test_modeling.py — Tests unitaires pour modeling.py (E4).
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modeling import (
    build_star_schema,
    check_referential_integrity,
    _build_dim_priority,
    _build_dim_date,
    _build_dim_subcategory,
)
from config import SLA_TARGET_HOURS


# ──────────────────────────────────────────────────────────────────────────────
# Fixture commune
# ──────────────────────────────────────────────────────────────────────────────

def _make_df() -> pd.DataFrame:
    """DataFrame minimal simulant le résultat de transform + add_analytical_columns."""
    return pd.DataFrame({
        "reference":          ["I-001", "I-002", "I-003"],
        "status":             ["Résolue", "Assignée", "Fermée"],
        "operational_status": ["closed", "open", "closed"],
        "origin":             ["Interface", "Téléphone", "Interface"],
        "urgency":            ["haute", "critique", "basse"],
        "impact":             ["Utilisateur", "Département", "Service"],
        "priority":           ["haute", "critique", "basse"],
        "service_name":       ["App - Fonctionnel", "App technique", "App - Fonctionnel"],
        "service_subcategory": ["Ticket à chaud", "Support avancé", "Ticket à chaud"],
        "team_name":          ["Equipe A", "Equipe B", "Equipe A"],
        "ci_name":            ["SERVEUR01", None, "SWITCH02"],
        "ci_class":           ["Server", None, "Switch"],
        "agent_name":         ["abc12345", "def67890", "abc12345"],
        "requester_org":      ["DSI", "RH", "DSI"],
        "requester_site":     ["Y114", "D001", "Y114"],
        "root_cause_raw":     ["Saturation CPU", None, "Réseau"],
        "root_cause_category": ["Saturation ressources (CPU/RAM/DB)", None, "Réseau / Sécurité"],
        "solution":           ["Redémarrage", None, "Fix réseau"],
        "resolution_code":    ["Résolu", None, "Résolu"],
        "resolution_type":    ["Technique", None, "Technique"],
        "itop_resolution_delay_min": [120, 0, 200],
        "suspension_cumulated": [0, 0, 0],
        "reject_reason":      [None, None, None],
        "parent_change_ref":  ["C-001", None, None],
        "parent_incident_ref": [None, None, None],
        "sla_tto_breached_itop": ["non", "non", "non"],
        "sla_ttr_breached_itop": ["non", "non", "non"],
        "created_at":         pd.to_datetime(["2026-01-01", "2026-02-15", "2026-03-10"]),
        "assigned_at":        pd.to_datetime(["2026-01-01 09:00", pd.NaT, "2026-03-10 11:00"]),
        "resolved_at":        pd.to_datetime(["2026-01-02", pd.NaT, "2026-03-11"]),
        "closed_at":          pd.to_datetime([pd.NaT, pd.NaT, "2026-03-12"]),
        "rejected_at":        pd.to_datetime([pd.NaT, pd.NaT, pd.NaT]),
        "last_update":        pd.to_datetime(["2026-01-02", "2026-02-15", "2026-03-12"]),
        "tto_deadline":       pd.to_datetime(["2026-01-01 12:00", "2026-02-15 13:00", "2026-03-10 14:00"]),
        "ttr_deadline":       pd.to_datetime(["2026-01-03", pd.NaT, "2026-03-13"]),
        "tto_real_min":       [60.0, None, 60.0],
        "ttr_real_hours":     [24.0, None, 24.0],
        "ttr_bucket":         ["4-24h", "Non résolu", "4-24h"],
        "is_open":            [0, 1, 0],
        "is_resolved":        [1, 0, 1],
        "is_rejected":        [0, 0, 0],
        "is_critical":        [0, 1, 0],
        "sla_tto_breached_calc": [0, None, 0],
        "sla_ttr_breached_calc": [0, None, 0],
        "sla_target_hours":   [8, 4, 72],
        "is_out_of_target":   [0, None, 0],
        "nature":             ["Fonctionnel", "Technique", "Fonctionnel"],
        "has_root_cause":     [1, 0, 1],
        "has_ci":             [1, 0, 1],
        "has_parent_change":  [1, 0, 0],
        "created_month":      ["2026-01", "2026-02", "2026-03"],
        "created_date":       [date(2026, 1, 1), date(2026, 2, 15), date(2026, 3, 10)],
        "resolved_date":      [date(2026, 1, 2), None, date(2026, 3, 11)],
    })


# ──────────────────────────────────────────────────────────────────────────────
# Tests dimensions
# ──────────────────────────────────────────────────────────────────────────────

class TestDimensions:
    def setup_method(self):
        self.tables = build_star_schema(_make_df())

    def test_all_tables_present(self):
        expected = {"fact_incidents", "dim_service", "dim_subcategory",
                    "dim_team", "dim_ci", "dim_priority", "dim_date"}
        assert set(self.tables.keys()) == expected

    def test_dim_service_has_unknown_row(self):
        dim = self.tables["dim_service"]
        assert 0 in dim["service_key"].values
        assert "Non renseigné" in dim["service_name"].values

    def test_dim_priority_sla_targets(self):
        dim = self.tables["dim_priority"]
        for prio, expected_hours in SLA_TARGET_HOURS.items():
            rows = dim[dim["priority"] == prio]
            if not rows.empty:
                assert rows.iloc[0]["sla_target_hours"] == expected_hours

    def test_dim_subcategory_is_generic(self):
        dim = self.tables["dim_subcategory"]
        ticket_chaud = dim[dim["service_subcategory"].str.lower().str.contains("ticket.*chaud", na=False)]
        assert (ticket_chaud["is_generic"] == 1).all()
        other = dim[~dim["service_subcategory"].str.lower().str.contains("ticket.*chaud", na=False)
                    & (dim["subcategory_key"] != 0)]
        assert (other["is_generic"] == 0).all()

    def test_dim_subcategory_has_team_manager_column(self):
        dim = self.tables["dim_subcategory"]
        assert "team_manager" in dim.columns

    def test_dim_subcategory_team_manager_known_subcategory(self):
        from config import SUBCATEGORY_MANAGERS
        first_key = list(SUBCATEGORY_MANAGERS.keys())[0]
        first_manager = SUBCATEGORY_MANAGERS[first_key]
        df = _make_df().copy()
        df["service_subcategory"] = [first_key, "Inconnue", first_key]
        tables = build_star_schema(df)
        dim = tables["dim_subcategory"]
        row = dim[dim["service_subcategory"] == first_key]
        assert not row.empty
        assert row.iloc[0]["team_manager"] == first_manager

    def test_dim_subcategory_team_manager_unknown_defaults_non_defini(self):
        df = _make_df().copy()
        df["service_subcategory"] = ["Sous-catégorie inconnue", "Autre", "Sous-catégorie inconnue"]
        tables = build_star_schema(df)
        dim = tables["dim_subcategory"]
        real_rows = dim[dim["subcategory_key"] != 0]
        assert (real_rows["team_manager"] == "Non défini").all()

    def test_dim_subcategory_unknown_row_has_non_defini(self):
        dim = self.tables["dim_subcategory"]
        unknown_row = dim[dim["subcategory_key"] == 0]
        assert unknown_row.iloc[0]["team_manager"] == "Non défini"

    def test_dim_ci_handles_nan(self):
        dim = self.tables["dim_ci"]
        # Seules les valeurs non-NaN doivent être dans la dimension (hors ligne 0)
        ci_values = dim[dim["ci_key"] != 0]["ci_name"].tolist()
        assert not any(v is None or (hasattr(pd, "isna") and pd.isna(v)) for v in ci_values)

    def test_dim_date_continuous(self):
        dim = self.tables["dim_date"]
        # Doit commencer au 2026-01-01 (min created_at) et aller jusqu'à aujourd'hui
        assert date(2026, 1, 1) in dim["date"].values
        from datetime import date as dt
        assert dt.today() in dim["date"].values

    def test_dim_date_columns(self):
        dim = self.tables["dim_date"]
        for col in ["date", "year", "month_num", "month_label", "month_name_fr",
                    "quarter", "weekday_fr", "is_weekend"]:
            assert col in dim.columns

    def test_dim_date_weekend(self):
        dim = self.tables["dim_date"]
        saturdays = dim[dim["weekday_fr"] == "Samedi"]
        assert (saturdays["is_weekend"] == 1).all()
        mondays = dim[dim["weekday_fr"] == "Lundi"]
        assert (mondays["is_weekend"] == 0).all()


# ──────────────────────────────────────────────────────────────────────────────
# Tests fact_incidents
# ──────────────────────────────────────────────────────────────────────────────

class TestFactIncidents:
    def setup_method(self):
        self.tables = build_star_schema(_make_df())
        self.facts = self.tables["fact_incidents"]

    def test_row_count_preserved(self):
        assert len(self.facts) == 3

    def test_fk_columns_present(self):
        for col in ("service_key", "subcategory_key", "team_key", "ci_key", "priority_key"):
            assert col in self.facts.columns

    def test_source_columns_replaced_by_keys(self):
        # Ces colonnes doivent être absentes de fact_incidents
        for col in ("service_name", "service_subcategory", "team_name", "ci_name", "ci_class", "priority"):
            assert col not in self.facts.columns

    def test_degenerate_dims_preserved(self):
        # Ces colonnes descriptives doivent rester dans les faits
        for col in ("reference", "status", "origin", "urgency", "impact"):
            assert col in self.facts.columns

    def test_analytical_columns_preserved(self):
        for col in ("ttr_real_hours", "ttr_bucket", "nature", "is_open",
                    "root_cause_category", "sla_target_hours", "created_date"):
            assert col in self.facts.columns

    def test_fk_values_valid(self):
        # Toutes les clés FK doivent être >= 0
        for col in ("service_key", "subcategory_key", "team_key", "ci_key", "priority_key"):
            assert (self.facts[col] >= 0).all()

    def test_unknown_ci_gets_key_0(self):
        # I-002 n'a pas de CI → ci_key doit être 0
        row_i002 = self.facts[self.facts["reference"] == "I-002"]
        assert row_i002.iloc[0]["ci_key"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Tests intégrité référentielle
# ──────────────────────────────────────────────────────────────────────────────

class TestReferentialIntegrity:
    def test_no_errors_on_valid_schema(self):
        tables = build_star_schema(_make_df())
        errors = check_referential_integrity(tables)
        assert errors == [], f"Erreurs FK inattendues : {errors}"

    def test_detects_orphan_key(self):
        tables = build_star_schema(_make_df())
        # Injecter une clé orpheline
        tables["fact_incidents"] = tables["fact_incidents"].copy()
        tables["fact_incidents"].loc[0, "service_key"] = 999
        errors = check_referential_integrity(tables)
        assert any("service_key" in e for e in errors)

    def test_missing_dimension_detected(self):
        tables = build_star_schema(_make_df())
        del tables["dim_team"]
        errors = check_referential_integrity(tables)
        assert any("dim_team" in e for e in errors)
