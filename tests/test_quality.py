"""
tests/test_quality.py — Tests unitaires pour quality_checks.py (E6).
"""

import sys
from datetime import date, timedelta, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from quality_checks import (
    run_all_checks,
    _check_empty_references,
    _check_no_duplicates,
    _check_date_logic,
    _check_root_cause_fill,
    _check_ci_fill,
    _check_sla_flags_discriminant,
    _check_unresolved_long,
    _check_delay_pollution,
    _Report,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _make_valid_df(n: int = 10) -> pd.DataFrame:
    """DataFrame valide minimal pour les tests."""
    base = datetime(2026, 1, 1)
    return pd.DataFrame({
        "reference":    [f"I-{i:03d}" for i in range(n)],
        "status":       ["Résolue"] * n,
        "created_at":   [base + timedelta(days=i) for i in range(n)],
        "resolved_at":  [base + timedelta(days=i, hours=2) for i in range(n)],
        "last_update":  [base + timedelta(days=i, hours=3) for i in range(n)],
        "root_cause_raw": ["Saturation CPU"] * n,
        "ci_name":      ["SERVEUR01"] * n,
        "sla_tto_breached_itop": ["non"] * n,
        "sla_ttr_breached_itop": (["non"] * (n // 2) + ["oui"] * (n - n // 2)),
        "itop_resolution_delay_min": [120] * n,
        "priority":     ["haute"] * n,
    })


def _make_report(n: int = 10) -> _Report:
    return _Report(n, 5)


# ──────────────────────────────────────────────────────────────────────────────
# E6.1a — Références vides
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckEmptyReferences:
    def test_pass_all_filled(self):
        df = _make_valid_df()
        rep = _make_report()
        _check_empty_references(df, rep)
        assert rep.is_valid()

    def test_fail_on_empty_reference(self):
        df = _make_valid_df()
        df.loc[0, "reference"] = None
        rep = _make_report()
        _check_empty_references(df, rep)
        assert not rep.is_valid()

    def test_fail_on_empty_string_reference(self):
        df = _make_valid_df()
        df.loc[0, "reference"] = ""
        rep = _make_report()
        _check_empty_references(df, rep)
        assert not rep.is_valid()

    def test_skip_when_column_absent(self):
        df = pd.DataFrame({"status": ["Résolue"]})
        rep = _make_report(1)
        _check_empty_references(df, rep)
        assert rep.is_valid()  # SKIP ne bloque pas


# ──────────────────────────────────────────────────────────────────────────────
# E6.1b — Doublons
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckNoDuplicates:
    def test_pass_no_duplicates(self):
        df = _make_valid_df()
        rep = _make_report()
        _check_no_duplicates(df, rep)
        assert rep.is_valid()

    def test_fail_on_duplicate(self):
        df = _make_valid_df()
        df.loc[1, "reference"] = "I-000"  # doublon de la première ligne
        rep = _make_report()
        _check_no_duplicates(df, rep)
        assert not rep.is_valid()


# ──────────────────────────────────────────────────────────────────────────────
# E6.2 — Logique des dates
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckDateLogic:
    def test_pass_resolved_after_created(self):
        df = _make_valid_df()
        rep = _make_report()
        _check_date_logic(df, rep)
        assert rep.is_valid()

    def test_fail_resolved_before_created(self):
        df = _make_valid_df()
        df.loc[0, "resolved_at"] = df.loc[0, "created_at"] - timedelta(hours=1)
        rep = _make_report()
        _check_date_logic(df, rep)
        assert not rep.is_valid()

    def test_skip_when_columns_absent(self):
        df = pd.DataFrame({"status": ["Résolue"]})
        rep = _make_report(1)
        _check_date_logic(df, rep)
        assert rep.is_valid()


# ──────────────────────────────────────────────────────────────────────────────
# E6.3 — Complétude cause racine
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckRootCauseFill:
    def test_pass_above_threshold(self):
        df = _make_valid_df(10)  # 100% renseigné
        rep = _make_report(10)
        _check_root_cause_fill(df, rep)
        assert rep.is_valid()
        assert not rep._has_warn

    def test_warn_below_threshold(self):
        df = _make_valid_df(100)
        df.loc[:90, "root_cause_raw"] = None  # 9% renseigné < 10%
        rep = _make_report(100)
        _check_root_cause_fill(df, rep)
        assert rep.is_valid()    # WARN ne bloque pas
        assert rep._has_warn


# ──────────────────────────────────────────────────────────────────────────────
# E6.4 — Complétude CI
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckCiFill:
    def test_pass_above_threshold(self):
        df = _make_valid_df(10)  # 100% CI renseigné
        rep = _make_report(10)
        _check_ci_fill(df, rep)
        assert not rep._has_warn

    def test_warn_below_threshold(self):
        df = _make_valid_df(100)
        df.loc[:85, "ci_name"] = None  # 15% renseigné < 20%
        rep = _make_report(100)
        _check_ci_fill(df, rep)
        assert rep._has_warn


# ──────────────────────────────────────────────────────────────────────────────
# E6.5 — SLA discriminant
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckSlaDiscriminant:
    def test_warn_when_single_modality(self):
        df = _make_valid_df(10)
        df["sla_tto_breached_itop"] = "non"  # modalité unique
        rep = _make_report(10)
        _check_sla_flags_discriminant(df, rep)
        assert rep._has_warn

    def test_ok_when_multiple_modalities(self):
        df = _make_valid_df(10)
        # sla_ttr_breached_itop a déjà 2 modalités dans _make_valid_df
        rep = _make_report(10)
        _check_sla_flags_discriminant(df, rep)
        # sla_tto_breached_itop est à modalité unique (non) → WARN
        # sla_ttr_breached_itop est discriminant → OK
        assert rep._has_warn  # à cause de TTO


# ──────────────────────────────────────────────────────────────────────────────
# E6.6 — Tickets résolus non fermés
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckUnresolvedLong:
    def test_warn_when_old_resolved(self):
        df = _make_valid_df(5)
        df["status"] = "Résolue"
        # Résolution il y a 60 jours
        df["resolved_at"] = [datetime.utcnow() - timedelta(days=60)] * 5
        rep = _make_report(5)
        _check_unresolved_long(df, rep)
        assert rep._has_warn

    def test_ok_when_recent_resolved(self):
        df = _make_valid_df(5)
        df["status"] = "Résolue"
        df["resolved_at"] = [datetime.utcnow() - timedelta(days=5)] * 5
        rep = _make_report(5)
        _check_unresolved_long(df, rep)
        assert not rep._has_warn


# ──────────────────────────────────────────────────────────────────────────────
# E6.7 — Pollution délai résolution
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckDelayPollution:
    def test_warn_when_too_many_long_delays(self):
        df = _make_valid_df(100)
        # 5% de délais > 1 an (525600 min)
        df.loc[:4, "itop_resolution_delay_min"] = 600000
        rep = _make_report(100)
        _check_delay_pollution(df, rep)
        assert rep._has_warn

    def test_ok_when_few_long_delays(self):
        df = _make_valid_df(1000)
        df.loc[0, "itop_resolution_delay_min"] = 600000  # seulement 0.1%
        rep = _make_report(1000)
        _check_delay_pollution(df, rep)
        assert not rep._has_warn


# ──────────────────────────────────────────────────────────────────────────────
# Test d'intégration run_all_checks
# ──────────────────────────────────────────────────────────────────────────────

class TestRunAllChecks:
    def test_valid_df_returns_true(self):
        df = _make_valid_df(100)
        is_valid, report = run_all_checks(df)
        assert is_valid
        assert "PASS" in report

    def test_invalid_df_returns_false(self):
        df = _make_valid_df(10)
        df.loc[0, "reference"] = None  # FAIL
        is_valid, report = run_all_checks(df)
        assert not is_valid
        assert "FAIL" in report

    def test_report_is_string(self):
        df = _make_valid_df()
        _, report = run_all_checks(df)
        assert isinstance(report, str)
        assert len(report) > 0

    def test_reconciliation_source_count(self):
        df = _make_valid_df(10)
        # source_count == len(df) → OK
        is_valid, report = run_all_checks(df, source_count=10)
        assert is_valid

    def test_reconciliation_mismatch_fails(self):
        df = _make_valid_df(8)
        # 8 lignes dans facts mais 10 en source → FAIL
        is_valid, report = run_all_checks(df, source_count=10)
        assert not is_valid
        assert "réconciliation" in report.lower() or "Réconciliation" in report

    def test_json_report_written(self, tmp_path, monkeypatch):
        import quality_checks
        monkeypatch.setattr(quality_checks, "_LOGS_DIR", tmp_path)
        df = _make_valid_df()
        run_all_checks(df)
        json_files = list(tmp_path.glob("quality_report_*.json"))
        assert len(json_files) == 1
        import json
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert "verdict" in data
        assert "checks" in data
