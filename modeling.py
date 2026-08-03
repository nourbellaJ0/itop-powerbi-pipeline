"""
modeling.py — Construction du modèle en étoile Power BI depuis la table de faits nettoyée.

Produit 7 DataFrames :
  fact_incidents   — Table de faits (mesures + clés étrangères + colonnes analytiques)
  dim_service      — Dimension Service
  dim_subcategory  — Dimension Sous-catégorie de service
  dim_team         — Dimension Équipe
  dim_ci           — Dimension CI (Configuration Item)
  dim_priority     — Dimension Priorité (avec cibles SLA)
  dim_date         — Calendrier continu du premier incident à aujourd'hui

Convention :
  - Clé 0 + libellé "Non renseigné" pour les valeurs inconnues ou manquantes
    dans chaque dimension (intégrité référentielle garantie côté Python).
  - Les colonnes couvertes par une dimension sont remplacées dans fact_incidents
    par leur clé entière (*_key). Les colonnes descriptives portées par les faits
    (reference, status, dates…) restent dans fact_incidents.
"""

import re
from datetime import datetime, date

import pandas as pd

from config import SLA_TARGET_HOURS, SUBCATEGORY_MANAGERS
from logger_config import setup_logger

logger = setup_logger(__name__)

_UNKNOWN_LABEL = "Non renseigné"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers internes
# ══════════════════════════════════════════════════════════════════════════════

def _build_dim(
    df: pd.DataFrame,
    source_cols: list[str],
    key_name: str,
    label_col: str,
) -> pd.DataFrame:
    """
    Construit une dimension simple depuis les colonnes sources du DataFrame de faits.

    Ajoute la ligne clé 0 / "Non renseigné" en premier.

    Args:
        df:          DataFrame de faits.
        source_cols: Colonnes sources à inclure dans la dimension.
        key_name:    Nom de la colonne clé entière (ex: "service_key").
        label_col:   Colonne principale du libellé (pour les logs).

    Returns:
        DataFrame dimension avec key_name comme première colonne.
    """
    present = [c for c in source_cols if c in df.columns]
    if not present:
        unknown_row = {key_name: 0, **{c: _UNKNOWN_LABEL for c in source_cols}}
        return pd.DataFrame([unknown_row])

    dim = df[present].drop_duplicates().dropna(subset=[present[0]]).copy()
    dim = dim.sort_values(present[0]).reset_index(drop=True)
    dim.insert(0, key_name, range(1, len(dim) + 1))

    # Ligne 0 pour les valeurs inconnues
    unknown_row = pd.DataFrame([{key_name: 0, **{c: _UNKNOWN_LABEL for c in present}}])
    dim = pd.concat([unknown_row, dim], ignore_index=True)

    logger.info(f"dim → {key_name}: {len(dim) - 1} valeur(s) distincte(s) + 1 inconnue.")
    return dim


def _attach_key(
    df: pd.DataFrame,
    dim: pd.DataFrame,
    join_col: str,
    key_col: str,
) -> pd.DataFrame:
    """
    Joint la clé entière de la dimension sur le DataFrame de faits.
    Les valeurs sans correspondance reçoivent la clé 0 (Non renseigné).
    Supprime la colonne source après jointure.
    """
    if join_col not in df.columns:
        df[key_col] = 0
        return df

    # Construire le dictionnaire valeur → clé depuis la dimension (hors ligne 0)
    lookup = dim[dim[key_col] != 0].set_index(join_col)[key_col].to_dict()
    df[key_col] = df[join_col].map(lookup).fillna(0).astype(int)
    df = df.drop(columns=[join_col])
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Constructeurs de dimensions
# ══════════════════════════════════════════════════════════════════════════════

def _build_dim_service(df: pd.DataFrame) -> pd.DataFrame:
    """Dimension Service : service_key, service_name, nature."""
    return _build_dim(
        df,
        source_cols=["service_name", "nature"],
        key_name="service_key",
        label_col="service_name",
    )


def _normalize_sub(s: str) -> str:
    """Normalise une sous-catégorie pour la correspondance insensible à la casse/espaces/apostrophes."""
    text = str(s).strip()
    # Harmonise les apostrophes typographiques (U+2018/U+2019) en apostrophe standard
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).lower()


def _build_dim_subcategory(df: pd.DataFrame) -> pd.DataFrame:
    """Dimension Sous-catégorie : subcategory_key, service_subcategory, is_generic, team_manager."""
    present = [c for c in ["service_subcategory"] if c in df.columns]
    if not present:
        return pd.DataFrame([{
            "subcategory_key": 0, "service_subcategory": _UNKNOWN_LABEL,
            "is_generic": 0, "team_manager": "Non défini",
        }])

    dim = df[["service_subcategory"]].drop_duplicates().dropna().copy()
    dim = dim.sort_values("service_subcategory").reset_index(drop=True)
    dim.insert(0, "subcategory_key", range(1, len(dim) + 1))

    # is_generic = 1 pour "Ticket à chaud" (sous-catégorie générique à 74% du volume)
    dim["is_generic"] = (
        dim["service_subcategory"]
        .str.lower()
        .str.contains("ticket.*chaud", na=False)
        .astype(int)
    )

    # team_manager — correspondance case/espace-insensible depuis SUBCATEGORY_MANAGERS
    manager_map = {_normalize_sub(k): v for k, v in SUBCATEGORY_MANAGERS.items()}
    dim["team_manager"] = dim["service_subcategory"].apply(
        lambda s: manager_map.get(_normalize_sub(s), "Non défini") if pd.notna(s) else "Non défini"
    )

    # Log des clés du dict sans correspondance dans les données
    subs_in_dim = {_normalize_sub(str(s)) for s in dim["service_subcategory"] if pd.notna(s)}
    for raw_key in SUBCATEGORY_MANAGERS:
        if _normalize_sub(raw_key) not in subs_in_dim:
            logger.warning(
                f"SUBCATEGORY_MANAGERS: clé '{raw_key}' absente des données "
                "— vérifier l'orthographe de la sous-catégorie dans iTop."
            )

    unknown_row = pd.DataFrame([{
        "subcategory_key": 0,
        "service_subcategory": _UNKNOWN_LABEL,
        "is_generic": 0,
        "team_manager": "Non défini",
    }])
    dim = pd.concat([unknown_row, dim], ignore_index=True)

    logger.info(f"dim → subcategory_key: {len(dim) - 1} valeur(s) + 1 inconnue.")
    return dim


def _build_dim_team(df: pd.DataFrame) -> pd.DataFrame:
    """Dimension Équipe : team_key, team_name."""
    return _build_dim(
        df,
        source_cols=["team_name"],
        key_name="team_key",
        label_col="team_name",
    )


def _build_dim_ci(df: pd.DataFrame) -> pd.DataFrame:
    """Dimension CI : ci_key, ci_name, ci_class."""
    return _build_dim(
        df,
        source_cols=["ci_name", "ci_class"],
        key_name="ci_key",
        label_col="ci_name",
    )


def _build_dim_priority(df: pd.DataFrame) -> pd.DataFrame:
    """Dimension Priorité : priority_key, priority, sla_target_hours."""
    present = [c for c in ["priority"] if c in df.columns]
    if not present:
        return pd.DataFrame([{
            "priority_key": 0, "priority": _UNKNOWN_LABEL, "sla_target_hours": None
        }])

    dim = df[["priority"]].drop_duplicates().dropna().copy()
    dim = dim.sort_values("priority").reset_index(drop=True)
    dim.insert(0, "priority_key", range(1, len(dim) + 1))
    dim["sla_target_hours"] = dim["priority"].map(SLA_TARGET_HOURS)

    unknown_row = pd.DataFrame([{
        "priority_key": 0,
        "priority": _UNKNOWN_LABEL,
        "sla_target_hours": None,
    }])
    dim = pd.concat([unknown_row, dim], ignore_index=True)

    logger.info(f"dim → priority_key: {len(dim) - 1} valeur(s) + 1 inconnue.")
    return dim


_MONTH_NAMES_FR = {
    1: "Janvier", 2: "Février",   3: "Mars",     4: "Avril",
    5: "Mai",     6: "Juin",      7: "Juillet",   8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}
_WEEKDAY_NAMES_FR = {
    0: "Lundi", 1: "Mardi", 2: "Mercredi", 3: "Jeudi",
    4: "Vendredi", 5: "Samedi", 6: "Dimanche",
}


def _build_dim_date(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calendrier continu du min(created_at) à aujourd'hui.

    Colonnes : date, year, month_num, month_label, month_name_fr,
               quarter, weekday_fr, is_weekend.
    """
    if "created_at" not in df.columns or df["created_at"].dropna().empty:
        logger.warning("created_at absent ou vide : dim_date limitée à aujourd'hui.")
        min_date = date.today()
    else:
        min_date = df["created_at"].dropna().min().date()

    max_date = date.today()
    dates = pd.date_range(start=min_date, end=max_date, freq="D")

    dim = pd.DataFrame({
        "date":          dates.date,
        "year":          dates.year,
        "month_num":     dates.month,
        "month_label":   dates.strftime("%Y-%m"),
        "month_name_fr": dates.month.map(_MONTH_NAMES_FR),
        "quarter":       dates.quarter,
        "weekday_fr":    dates.dayofweek.map(_WEEKDAY_NAMES_FR),
        "is_weekend":    (dates.dayofweek >= 5).astype(int),
    })

    logger.info(
        f"dim_date : {len(dim)} jours du {min_date} au {max_date}."
    )
    return dim


# ══════════════════════════════════════════════════════════════════════════════
# Construction des faits (remplacement des colonnes sources par les clés FK)
# ══════════════════════════════════════════════════════════════════════════════

def _build_fact_incidents(df: pd.DataFrame, dims: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Construit fact_incidents depuis le DataFrame nettoyé en attachant les clés FK.

    Colonnes remplacées par leur clé :
      service_name  → service_key   (via dim_service)
      service_subcategory → subcategory_key (via dim_subcategory)
      team_name     → team_key      (via dim_team)
      ci_name + ci_class → ci_key   (via dim_ci)
      priority      → priority_key  (via dim_priority)

    Toutes les autres colonnes restent dans la table de faits.
    """
    facts = df.copy()

    # Service
    dim_svc = dims["dim_service"]
    lookup_svc = dim_svc[dim_svc["service_key"] != 0].set_index("service_name")["service_key"].to_dict()
    if "service_name" in facts.columns:
        facts["service_key"] = facts["service_name"].map(lookup_svc).fillna(0).astype(int)
        facts = facts.drop(columns=["service_name"])

    # Sous-catégorie
    dim_sub = dims["dim_subcategory"]
    lookup_sub = dim_sub[dim_sub["subcategory_key"] != 0].set_index("service_subcategory")["subcategory_key"].to_dict()
    if "service_subcategory" in facts.columns:
        facts["subcategory_key"] = facts["service_subcategory"].map(lookup_sub).fillna(0).astype(int)
        facts = facts.drop(columns=["service_subcategory"])

    # Équipe
    dim_team = dims["dim_team"]
    lookup_team = dim_team[dim_team["team_key"] != 0].set_index("team_name")["team_key"].to_dict()
    if "team_name" in facts.columns:
        facts["team_key"] = facts["team_name"].map(lookup_team).fillna(0).astype(int)
        facts = facts.drop(columns=["team_name"])

    # CI (jointure sur ci_name uniquement pour la clé)
    dim_ci = dims["dim_ci"]
    if "ci_name" in dim_ci.columns:
        lookup_ci = dim_ci[dim_ci["ci_key"] != 0].set_index("ci_name")["ci_key"].to_dict()
    else:
        lookup_ci = {}
    if "ci_name" in facts.columns:
        facts["ci_key"] = facts["ci_name"].map(lookup_ci).fillna(0).astype(int)
        facts = facts.drop(columns=["ci_name"])
    if "ci_class" in facts.columns:
        facts = facts.drop(columns=["ci_class"])

    # Priorité
    dim_prio = dims["dim_priority"]
    lookup_prio = dim_prio[dim_prio["priority_key"] != 0].set_index("priority")["priority_key"].to_dict()
    if "priority" in facts.columns:
        facts["priority_key"] = facts["priority"].map(lookup_prio).fillna(0).astype(int)
        facts = facts.drop(columns=["priority"])

    # Réordonner : référence en premier, clés FK groupées
    priority_cols = [c for c in [
        "reference", "status", "operational_status", "origin", "urgency",
        "impact", "requester_org", "requester_site", "agent_name",
        "created_at", "assigned_at", "resolved_at", "closed_at",
        "rejected_at", "last_update", "created_date", "resolved_date",
        "tto_deadline", "ttr_deadline",
        "service_key", "subcategory_key", "team_key", "ci_key", "priority_key",
    ] if c in facts.columns]
    remaining = [c for c in facts.columns if c not in priority_cols]
    facts = facts[priority_cols + remaining]

    return facts


# ══════════════════════════════════════════════════════════════════════════════
# Point d'entrée public
# ══════════════════════════════════════════════════════════════════════════════

def build_star_schema(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Construit le modèle en étoile complet depuis le DataFrame de faits nettoyé.

    Args:
        df: DataFrame produit par transform.build_clean_dataframe_from_xlsx()
            (ou l'équivalent mode API), après add_analytical_columns().

    Returns:
        Dict avec les clés :
          "fact_incidents", "dim_service", "dim_subcategory", "dim_team",
          "dim_ci", "dim_priority", "dim_date"
    """
    logger.info("Construction du modèle en étoile…")

    dims: dict[str, pd.DataFrame] = {
        "dim_service":     _build_dim_service(df),
        "dim_subcategory": _build_dim_subcategory(df),
        "dim_team":        _build_dim_team(df),
        "dim_ci":          _build_dim_ci(df),
        "dim_priority":    _build_dim_priority(df),
        "dim_date":        _build_dim_date(df),
    }

    facts = _build_fact_incidents(df, dims)

    tables = {"fact_incidents": facts, **dims}

    for name, tbl in tables.items():
        logger.info(f"  {name}: {len(tbl)} lignes × {len(tbl.columns)} colonnes.")

    return tables


def check_referential_integrity(tables: dict[str, pd.DataFrame]) -> list[str]:
    """
    Vérifie que toutes les clés étrangères de fact_incidents existent dans leur dimension.

    Returns:
        Liste de messages d'erreur (vide si tout est OK).
    """
    errors: list[str] = []
    facts = tables.get("fact_incidents")
    if facts is None:
        return ["fact_incidents absente du modèle."]

    fk_checks = [
        ("service_key",     "dim_service",     "service_key"),
        ("subcategory_key", "dim_subcategory", "subcategory_key"),
        ("team_key",        "dim_team",        "team_key"),
        ("ci_key",          "dim_ci",          "ci_key"),
        ("priority_key",    "dim_priority",    "priority_key"),
    ]

    for fact_col, dim_name, dim_col in fk_checks:
        if fact_col not in facts.columns:
            continue
        dim = tables.get(dim_name)
        if dim is None:
            errors.append(f"Dimension '{dim_name}' absente.")
            continue
        valid_keys = set(dim[dim_col].unique())
        orphan_keys = set(facts[fact_col].unique()) - valid_keys
        if orphan_keys:
            errors.append(
                f"fact_incidents.{fact_col} : {len(orphan_keys)} clé(s) orpheline(s) "
                f"absente(s) de {dim_name} — ex: {list(orphan_keys)[:3]}"
            )

    return errors
