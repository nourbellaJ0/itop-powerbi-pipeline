"""
quality_checks.py — Contrôle qualité des données avant export.

Valide le DataFrame de faits et (optionnellement) le modèle en étoile complet.
Génère un rapport JSON horodaté dans logs/.

Niveaux de résultat :
  PASS — tout est correct
  WARN — anomalies non bloquantes (à surveiller, ne bloquent pas l'export)
  FAIL — erreur critique, le dépôt est annulé

Contrôles :
  E6.1  FAIL si références vides ou doublons résiduels
  E6.2  FAIL si resolved_at < created_at
  E6.3  WARN si % root_cause_raw renseigné < seuil (défaut 10 %)
  E6.4  WARN si % ci_name renseigné < seuil (défaut 20 %)
  E6.5  WARN si sla_tto_breached_itop / sla_ttr_breached_itop à modalité unique
  E6.6  WARN si tickets Résolue sans fermeture depuis > 30 jours
  E6.7  WARN si itop_resolution_delay_min > 1 an sur > 1 % des lignes
  E6.8  FAIL si fact_incidents ≠ lignes source (réconciliation) ou FK orphelines
  E6.9  Génère logs/quality_report_YYYYMMDD.json
"""

import json
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd

from logger_config import setup_logger

logger = setup_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Paramètres configurables
# ──────────────────────────────────────────────────────────────────────────────

VALID_STATUSES: set[str] = {
    "new", "assigned", "pending", "resolved", "closed", "rejected",
    "Nouveau", "Assignée", "En attente", "Résolue", "Fermée", "Rejetée",
}

CRITICAL_ERROR_THRESHOLD: float = 0.05
WARN_ROOT_CAUSE_MIN_PCT: float  = 0.10   # E6.3
WARN_CI_MIN_PCT: float          = 0.20   # E6.4
WARN_DELAY_MAX_MIN: float       = 365 * 24 * 60   # 1 an en minutes — E6.7
WARN_DELAY_MAX_PCT: float       = 0.01   # E6.7
WARN_UNRESOLVED_DAYS: int       = 30     # E6.6
WARN_AI_INDETERMINE_MAX_PCT: float = 0.20   # E6.10 — signal de prompt à revoir
WARN_MONETIQUE_AUTRE_MAX_PCT: float = 0.15  # E6.11 — signal référentiel/prompt Monétique à revoir

_LOGS_DIR = Path(__file__).parent / "logs"


# ──────────────────────────────────────────────────────────────────────────────
# Structure interne du rapport
# ──────────────────────────────────────────────────────────────────────────────

class _Report:
    """Accumule les résultats des contrôles et construit le rapport final."""

    def __init__(self, n_rows: int, n_cols: int) -> None:
        self._buf = StringIO()
        self._json: dict = {
            "generated_at": datetime.utcnow().isoformat(),
            "rows": n_rows,
            "cols": n_cols,
            "checks": [],
        }
        self._has_fail = False
        self._has_warn = False

    def add(self, name: str, level: str, message: str, detail: dict | None = None) -> None:
        """Enregistre un résultat de contrôle (OK / WARN / FAIL)."""
        tag = f"[{level}]".ljust(7)
        self._buf.write(f"  {tag} {message}\n")
        entry: dict = {"check": name, "level": level, "message": message}
        if detail:
            entry["detail"] = detail
        self._json["checks"].append(entry)
        if level == "FAIL":
            self._has_fail = True
        if level == "WARN":
            self._has_warn = True

    def skip(self, name: str, reason: str) -> None:
        self._buf.write(f"  [SKIP]  {reason}\n")
        self._json["checks"].append({"check": name, "level": "SKIP", "message": reason})

    def text(self) -> str:
        sep = "=" * 65
        header = (
            f"\n{sep}\n"
            f"RAPPORT CONTRÔLE QUALITÉ — "
            f"{self._json['rows']} lignes × {self._json['cols']} colonnes\n"
            f"{sep}\n"
        )
        verdict = "PASS ✓" if not self._has_fail else "FAIL ✗ — export bloqué"
        if self._has_warn and not self._has_fail:
            verdict = "PASS ✓ (avec avertissements)"
        footer = f"\n{sep}\nRésultat global : {verdict}\n{sep}\n"
        return header + self._buf.getvalue() + footer

    def as_dict(self) -> dict:
        """Retourne le rapport sous forme de dict sérialisable."""
        verdict = "PASS"
        if self._has_warn and not self._has_fail:
            verdict = "PASS_WITH_WARN"
        if self._has_fail:
            verdict = "FAIL"
        return {**self._json, "verdict": verdict}

    def is_valid(self) -> bool:
        return not self._has_fail


# ──────────────────────────────────────────────────────────────────────────────
# Contrôles individuels
# ──────────────────────────────────────────────────────────────────────────────

def _check_required_columns(df: pd.DataFrame, rep: _Report) -> None:
    required = ["reference", "status", "created_at", "last_update"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        rep.add("colonnes_obligatoires", "FAIL",
                f"Colonne(s) manquante(s) : {missing}",
                {"missing": missing})
    else:
        rep.add("colonnes_obligatoires", "OK", "Toutes les colonnes obligatoires sont présentes.")


def _check_empty_references(df: pd.DataFrame, rep: _Report) -> None:
    """E6.1a — FAIL si références vides."""
    if "reference" not in df.columns:
        rep.skip("references_vides", "Colonne 'reference' absente.")
        return
    n = int((df["reference"].isna() | (df["reference"].astype(str).str.strip() == "")).sum())
    if n:
        rep.add("references_vides", "FAIL",
                f"{n}/{len(df)} ligne(s) avec 'reference' vide.",
                {"count": n})
    else:
        rep.add("references_vides", "OK", f"Toutes les {len(df)} références sont renseignées.")


def _check_no_duplicates(df: pd.DataFrame, rep: _Report) -> None:
    """E6.1b — FAIL si doublons résiduels sur reference."""
    if "reference" not in df.columns:
        rep.skip("doublons", "Colonne 'reference' absente.")
        return
    n = int(df["reference"].duplicated(keep=False).sum())
    if n:
        examples = df.loc[df["reference"].duplicated(keep=False), "reference"].unique()[:3].tolist()
        rep.add("doublons", "FAIL",
                f"{n} ligne(s) en doublon sur 'reference' (exemples: {examples}).",
                {"count": n, "examples": examples})
    else:
        rep.add("doublons", "OK", "Aucun doublon de référence détecté.")


def _check_date_logic(df: pd.DataFrame, rep: _Report) -> None:
    """E6.2 — FAIL si resolved_at < created_at."""
    if "resolved_at" not in df.columns or "created_at" not in df.columns:
        rep.skip("logique_dates", "'created_at' ou 'resolved_at' absente.")
        return
    both = df["created_at"].notna() & df["resolved_at"].notna()
    n = int((df.loc[both, "resolved_at"] < df.loc[both, "created_at"]).sum())
    if n:
        rep.add("logique_dates", "FAIL",
                f"{n} ligne(s) avec resolved_at < created_at.",
                {"count": n})
    else:
        rep.add("logique_dates", "OK",
                f"resolved_at >= created_at pour les {int(both.sum())} lignes concernées.")


def _check_root_cause_fill(df: pd.DataFrame, rep: _Report) -> None:
    """E6.3 — WARN si root_cause_raw renseigné < WARN_ROOT_CAUSE_MIN_PCT."""
    if "root_cause_raw" not in df.columns or len(df) == 0:
        rep.skip("complétude_cause_racine", "Colonne 'root_cause_raw' absente.")
        return
    pct = df["root_cause_raw"].notna().mean()
    msg = f"root_cause_raw renseigné : {pct:.1%} ({int(pct * len(df))}/{len(df)})."
    level = "WARN" if pct < WARN_ROOT_CAUSE_MIN_PCT else "OK"
    rep.add("complétude_cause_racine", level, msg, {"pct": round(pct, 4)})


def _check_ci_fill(df: pd.DataFrame, rep: _Report) -> None:
    """E6.4 — WARN si ci_name renseigné < WARN_CI_MIN_PCT."""
    if "ci_name" not in df.columns or len(df) == 0:
        # ci_name peut avoir été transformé en ci_key par modeling
        # On cherche has_ci comme indicateur de substitution
        if "has_ci" not in df.columns:
            rep.skip("complétude_ci", "Colonne 'ci_name'/'has_ci' absente.")
            return
        pct = df["has_ci"].fillna(0).mean()
        col = "has_ci"
    else:
        pct = df["ci_name"].notna().mean()
        col = "ci_name"

    msg = f"{col} renseigné : {pct:.1%} ({int(pct * len(df))}/{len(df)})."
    level = "WARN" if pct < WARN_CI_MIN_PCT else "OK"
    rep.add("complétude_ci", level, msg, {"pct": round(pct, 4)})


def _check_sla_flags_discriminant(df: pd.DataFrame, rep: _Report) -> None:
    """E6.5 — WARN si sla_*_breached_itop à modalité unique (indicateur non discriminant)."""
    for col in ("sla_tto_breached_itop", "sla_ttr_breached_itop"):
        if col not in df.columns:
            rep.skip(f"sla_discriminant_{col}", f"Colonne '{col}' absente.")
            continue
        unique_vals = df[col].dropna().unique()
        if len(unique_vals) <= 1:
            rep.add(f"sla_discriminant_{col}", "WARN",
                    f"'{col}' a une seule modalité ({list(unique_vals)}) — indicateur non discriminant.",
                    {"unique_values": [str(v) for v in unique_vals]})
        else:
            rep.add(f"sla_discriminant_{col}", "OK",
                    f"'{col}' est discriminant ({len(unique_vals)} modalité(s)).")


def _check_unresolved_long(df: pd.DataFrame, rep: _Report) -> None:
    """E6.6 — WARN si tickets Résolue avec > 30 jours sans fermeture."""
    if "status" not in df.columns or "resolved_at" not in df.columns:
        rep.skip("tickets_résolus_non_fermés", "'status' ou 'resolved_at' absente.")
        return
    resolved_mask = df["status"].isin({"Résolue", "resolved"}) & df["resolved_at"].notna()
    resolved_df = df[resolved_mask]
    if resolved_df.empty:
        rep.add("tickets_résolus_non_fermés", "OK", "Aucun ticket à l'état Résolue.")
        return
    threshold = pd.Timestamp.utcnow().tz_localize(None) - timedelta(days=WARN_UNRESOLVED_DAYS)
    n = int((resolved_df["resolved_at"] < threshold).sum())
    total = len(resolved_df)
    if n:
        rep.add("tickets_résolus_non_fermés", "WARN",
                f"{n}/{total} ticket(s) Résolue depuis > {WARN_UNRESOLVED_DAYS} jours sans fermeture.",
                {"count": n, "total_resolved": total})
    else:
        rep.add("tickets_résolus_non_fermés", "OK",
                f"Aucun ticket Résolue depuis plus de {WARN_UNRESOLVED_DAYS} jours.")


def _check_delay_pollution(df: pd.DataFrame, rep: _Report) -> None:
    """E6.7 — WARN si itop_resolution_delay_min > 1 an sur > 1 % des lignes."""
    col = "itop_resolution_delay_min"
    if col not in df.columns or len(df) == 0:
        rep.skip("pollution_délai_résolution", f"Colonne '{col}' absente.")
        return
    numeric = pd.to_numeric(df[col], errors="coerce")
    n = int((numeric > WARN_DELAY_MAX_MIN).sum())
    pct = n / len(df)
    msg = (
        f"'{col}' > 1 an : {n} lignes ({pct:.1%}). "
        f"Seuil : {WARN_DELAY_MAX_PCT:.0%}."
    )
    level = "WARN" if pct > WARN_DELAY_MAX_PCT else "OK"
    rep.add("pollution_délai_résolution", level, msg, {"count": n, "pct": round(pct, 4)})


def _check_reconciliation(
    df: pd.DataFrame,
    rep: _Report,
    source_count: int | None,
    tables: dict[str, pd.DataFrame] | None,
) -> None:
    """E6.8 — Réconciliation volumétrique et intégrité référentielle FK."""
    # Réconciliation volumétrique
    if source_count is not None:
        expected = source_count
        actual = len(df)
        if actual == expected:
            rep.add("réconciliation_volumes", "OK",
                    f"fact_incidents : {actual} lignes = source après dédoublonnage.")
        else:
            rep.add("réconciliation_volumes", "FAIL",
                    f"fact_incidents : {actual} lignes ≠ {expected} lignes source.",
                    {"fact_count": actual, "source_count": expected})

    # Intégrité référentielle FK
    if tables is None:
        return
    from modeling import check_referential_integrity
    errors = check_referential_integrity(tables)
    if errors:
        for err in errors:
            rep.add("intégrité_FK", "FAIL", err)
    else:
        rep.add("intégrité_FK", "OK", "Toutes les clés étrangères sont valides.")


def _check_ai_classification(rep: _Report, ai_stats: dict | None) -> None:
    """E6.10 — WARN si > 20% des réponses IA sont 'Indéterminé' (prompt à revoir).

    Reporte aussi dans le rapport JSON : nb classifiés, % depuis cache,
    % confiance >= seuil, coût estimé du run. N'effectue aucun appel réseau —
    lit uniquement les compteurs déjà calculés par ai_enrichment.enrich_dataframe().
    """
    if not ai_stats or not ai_stats.get("n_classified"):
        rep.skip("classification_ia", "Enrichissement IA désactivé ou aucun ticket classifié.")
        return

    n_classified = ai_stats["n_classified"]
    pct_indetermine = ai_stats.get("n_indetermine", 0) / n_classified
    pct_cache = ai_stats.get("n_cache_hit", 0) / n_classified
    pct_high_confidence = ai_stats.get("n_high_confidence", 0) / n_classified

    detail = {
        "n_classified": n_classified,
        "pct_cache": round(pct_cache, 4),
        "pct_high_confidence": round(pct_high_confidence, 4),
        "estimated_cost_usd": ai_stats.get("estimated_cost_usd", 0.0),
    }

    msg = (
        f"IA : {n_classified} ticket(s) classifié(s), "
        f"{pct_indetermine:.1%} 'Indéterminé', {pct_cache:.1%} depuis cache, "
        f"{pct_high_confidence:.1%} à confiance >= seuil, "
        f"coût estimé ~{detail['estimated_cost_usd']}$."
    )
    level = "WARN" if pct_indetermine > WARN_AI_INDETERMINE_MAX_PCT else "OK"
    rep.add("classification_ia", level, msg, detail)


def _check_monetique_classification(rep: _Report, monetique_stats: dict | None) -> None:
    """E6.11 — WARN si > 15% des tickets Monétique classifiés tombent en
    'Autre / à qualifier' (référentiel/prompt à revoir).

    Reporte aussi dans le rapport JSON : nb tickets Monétique classifiés,
    % depuis cache, % confiance >= seuil, % par responsabilité
    (Métier/BIAT-IT/Les deux/À qualifier). N'effectue aucun appel réseau —
    lit uniquement les compteurs déjà calculés par
    ai_enrichment_monetique.classify_monetique().
    """
    if not monetique_stats or not monetique_stats.get("n_classified"):
        rep.skip("classification_monetique", "Classification Monétique désactivée ou aucun ticket classifié.")
        return

    n_classified = monetique_stats["n_classified"]
    pct_autre = monetique_stats.get("n_autre_a_qualifier", 0) / n_classified
    pct_cache = monetique_stats.get("n_cache_hit", 0) / n_classified
    pct_high_confidence = monetique_stats.get("n_high_confidence", 0) / n_classified
    responsabilite_counts = monetique_stats.get("responsabilite_counts", {})
    pct_par_responsabilite = {
        k: round(v / n_classified, 4) for k, v in responsabilite_counts.items()
    }

    detail = {
        "n_classified": n_classified,
        "pct_cache": round(pct_cache, 4),
        "pct_high_confidence": round(pct_high_confidence, 4),
        "pct_autre_a_qualifier": round(pct_autre, 4),
        "pct_par_responsabilite": pct_par_responsabilite,
    }

    msg = (
        f"Monétique : {n_classified} ticket(s) classifié(s), "
        f"{pct_autre:.1%} 'Autre / à qualifier', {pct_cache:.1%} depuis cache, "
        f"{pct_high_confidence:.1%} à confiance >= seuil."
    )
    level = "WARN" if pct_autre > WARN_MONETIQUE_AUTRE_MAX_PCT else "OK"
    rep.add("classification_monetique", level, msg, detail)


def _check_status_values(df: pd.DataFrame, rep: _Report) -> None:
    """Vérifie que tous les statuts sont dans la liste de référence."""
    if "status" not in df.columns:
        rep.skip("valeurs_statut", "Colonne 'status' absente.")
        return
    unknown = set(df["status"].dropna().unique()) - VALID_STATUSES
    if unknown:
        rep.add("valeurs_statut", "WARN",
                f"Statut(s) inconnu(s) : {sorted(unknown)} — mettre à jour VALID_STATUSES.",
                {"unknown": sorted(str(s) for s in unknown)})
    else:
        rep.add("valeurs_statut", "OK", "Tous les statuts sont dans la liste de référence.")


def _check_critical_dates(df: pd.DataFrame, rep: _Report) -> None:
    """Vérifie que created_at et last_update ne dépassent pas le seuil de NaN."""
    for col in ("created_at", "last_update"):
        if col not in df.columns:
            rep.skip(f"validité_{col}", f"Colonne '{col}' absente.")
            continue
        n = int(df[col].isna().sum())
        pct = n / len(df) if len(df) else 0
        if pct > CRITICAL_ERROR_THRESHOLD:
            rep.add(f"validité_{col}", "FAIL",
                    f"'{col}' : {n}/{len(df)} dates invalides ({pct:.1%}) — seuil {CRITICAL_ERROR_THRESHOLD:.0%} dépassé.",
                    {"count": n, "pct": round(pct, 4)})
        elif n:
            rep.add(f"validité_{col}", "WARN",
                    f"'{col}' : {n}/{len(df)} dates manquantes ({pct:.1%}).")
        else:
            rep.add(f"validité_{col}", "OK", f"'{col}' : toutes les dates sont valides.")


# ──────────────────────────────────────────────────────────────────────────────
# Sauvegarde JSON
# ──────────────────────────────────────────────────────────────────────────────

def _save_json_report(report_dict: dict) -> None:
    """E6.9 — Sauvegarde le rapport dans logs/quality_report_YYYYMMDD.json."""
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        filename = _LOGS_DIR / f"quality_report_{datetime.utcnow().strftime('%Y%m%d')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"Rapport qualité JSON sauvegardé : '{filename}'.")
    except Exception as exc:
        logger.warning(f"Impossible d'écrire le rapport JSON : {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Fonction principale
# ──────────────────────────────────────────────────────────────────────────────

def run_all_checks(
    df: pd.DataFrame,
    tables: dict[str, pd.DataFrame] | None = None,
    source_count: int | None = None,
    ai_stats: dict | None = None,
    monetique_stats: dict | None = None,
) -> tuple[bool, str]:
    """
    Exécute tous les contrôles qualité E6 et retourne un résultat global.

    Args:
        df:              DataFrame de faits nettoyé (fact_incidents ou table plate).
        tables:          Modèle en étoile complet pour les checks FK (optionnel).
        source_count:    Nombre de lignes source avant dédoublonnage (pour E6.8).
        ai_stats:        Stats retournées par ai_enrichment.enrich_dataframe() (optionnel).
                         None (défaut) → le check IA est SKIP, rétrocompatible.
        monetique_stats: Stats retournées par
                         ai_enrichment_monetique.classify_monetique() (optionnel).
                         None (défaut) → le check Monétique est SKIP, rétrocompatible.

    Returns:
        Tuple (is_valid, report_text) :
          - is_valid   : False si au moins un contrôle FAIL.
          - report_text: Rapport textuel pour les logs.
    """
    rep = _Report(len(df), len(df.columns) if not df.empty else 0)

    if df.empty:
        rep.add("vide", "WARN", "Le DataFrame est vide — aucun contrôle effectué.")
        return True, rep.text()

    checks = [
        ("Colonnes obligatoires",           lambda: _check_required_columns(df, rep)),
        ("Références vides",                lambda: _check_empty_references(df, rep)),
        ("Doublons résiduels",              lambda: _check_no_duplicates(df, rep)),
        ("Logique des dates",               lambda: _check_date_logic(df, rep)),
        ("Validité dates critiques",        lambda: _check_critical_dates(df, rep)),
        ("Valeurs de statut",               lambda: _check_status_values(df, rep)),
        ("Complétude cause racine",         lambda: _check_root_cause_fill(df, rep)),
        ("Complétude CI",                   lambda: _check_ci_fill(df, rep)),
        ("SLA discriminant",                lambda: _check_sla_flags_discriminant(df, rep)),
        ("Tickets résolus non fermés",      lambda: _check_unresolved_long(df, rep)),
        ("Pollution délai résolution",      lambda: _check_delay_pollution(df, rep)),
        ("Réconciliation et FK",            lambda: _check_reconciliation(df, rep, source_count, tables)),
        ("Classification IA",               lambda: _check_ai_classification(rep, ai_stats)),
        ("Classification Monétique",        lambda: _check_monetique_classification(rep, monetique_stats)),
    ]

    for name, fn in checks:
        rep._buf.write(f"\n[{name}]\n")
        try:
            fn()
        except Exception as exc:
            rep.add(name, "FAIL", f"Exception dans le contrôle : {exc}")

    _save_json_report(rep.as_dict())
    return rep.is_valid(), rep.text()
