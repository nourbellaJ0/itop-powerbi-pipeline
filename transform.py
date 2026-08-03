"""
transform.py — Transformation, nettoyage et enrichissement analytique des données iTop.

Ce module supporte deux sources de données :
  - Mode "api"  : données brutes issues de l'API REST iTop (noms techniques anglais)
  - Mode "xlsx" : données issues d'un export Excel iTop (noms d'affichage en français)

Pipeline de traitement (E1 → E2 → E3) :
  E1 : Sélection et renommage (FIELD_MAP_XLSX / FIELD_MAP_API)
  E2 : Nettoyage (typage dates, trim, normalisation casse, valeurs vides, déduplication)
  E3 : Colonnes analytiques calculées (TTO/TTR, SLA, indicateurs binaires, catégorie cause)

╔══════════════════════════════════════════════════════════════════════════════╗
║  ZONES D'ADAPTATION                                                          ║
║                                                                              ║
║  FIELD_MAP_API       → Noms techniques iTop (API) → colonnes cibles         ║
║  FIELD_MAP_XLSX      → Noms affichés iTop (export Excel) → colonnes cibles  ║
║  ROOT_CAUSE_PATTERNS → Patterns de catégorisation des causes racines         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import hashlib
import re
import unicodedata
from typing import Any

import pandas as pd

from config import SLA_TARGET_HOURS
from logger_config import setup_logger

logger = setup_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# E1 — MAPPING MODE XLSX
# Clé   = nom de la colonne dans l'export Excel iTop (en français)
# Valeur = nom de colonne cible (snake_case anglais pour Power BI)
# Seules les 34 colonnes ci-dessous sont conservées ; toutes les autres sont ignorées.
# ══════════════════════════════════════════════════════════════════════════════

FIELD_MAP_XLSX: dict[str, str] = {
    "Référence":                                    "reference",
    "Object":                                       "ticket_title",
    "Description":                                  "ticket_description",
    "Date de début":                                "created_at",
    "Date d'assignation":                           "assigned_at",
    "Date de résolution":                           "resolved_at",
    "Date de fermeture":                            "closed_at",
    "Date de Reject":                               "rejected_at",
    "Dernière mise à jour":                         "last_update",
    "Statut":                                       "status",
    "Statut opérationnel":                          "operational_status",
    "Priorité":                                     "priority",
    "Urgence":                                      "urgency",
    "Impact":                                       "impact",
    "Origine":                                      "origin",
    "Service->Nom":                                 "service_name",
    "Sous catégorie de service->Nom":               "service_subcategory",
    "CI->Nom":                                      "ci_name",
    "CI->Sous-classe de CI":                        "ci_class",
    "Equipe->Nom":                                  "team_name",
    "Intervenant->Nom complet":                     "agent_name",
    "Affectation du demandeur->Nom organisation":   "requester_org",
    "N° bureau/Agence/local":                       "requester_site",
    "Cause Racine":                                 "root_cause_raw",
    "Solution":                                     "solution",
    "Code de résolution":                           "resolution_code",
    "Type Resolution":                              "resolution_type",
    "Délai de résolution":                          "itop_resolution_delay_min",
    "Temps cumulé de suspension":                   "suspension_cumulated",
    "Echéance TTO":                                 "tto_deadline",
    "Echéance TTR":                                 "ttr_deadline",
    "SLA TTO dépassé ?":                            "sla_tto_breached_itop",
    "SLA TTR dépassé ?":                            "sla_ttr_breached_itop",
    "Incident parent->Référence":                   "parent_incident_ref",
    "Changement parent->Référence":                 "parent_change_ref",
    "Raison de Reject":                             "reject_reason",
}


# ══════════════════════════════════════════════════════════════════════════════
# E1 — MAPPING MODE API
# Clé   = nom technique du champ dans l'API REST iTop
# Valeur = nom de colonne cible (identique au mode XLSX pour la même donnée)
# ══════════════════════════════════════════════════════════════════════════════

FIELD_MAP_API: dict[str, str] = {
    # ── Identifiants / Statut ──────────────────────────────────────────────────
    "ref":                                  "reference",
    "title":                                "ticket_title",
    "description":                          "ticket_description",
    "status":                               "status",
    "operational_status":                   "operational_status",
    "origin":                               "origin",
    "priority":                             "priority",
    "urgency":                              "urgency",
    "impact":                               "impact",

    # ── Dates ─────────────────────────────────────────────────────────────────
    "start_date":                           "created_at",
    "assignment_date":                      "assigned_at",
    "resolution_date":                      "resolved_at",
    "close_date":                           "closed_at",
    "last_update":                          "last_update",
    "tto_deadline":                         "tto_deadline",
    "ttr_deadline":                         "ttr_deadline",

    # ── Organisation demandeur ────────────────────────────────────────────────
    "caller_org_name":                      "requester_org",

    # ── Demandeur (données sensibles — seront SUPPRIMÉES) ─────────────────────
    "caller_id_friendlyname":               "requester_fullname",        # SUPPRIMÉ
    "caller_name":                          "requester_lastname",        # SUPPRIMÉ
    "caller_firstname":                     "requester_firstname",       # SUPPRIMÉ
    "caller_email":                         "requester_email",           # SUPPRIMÉ
    "caller_employee_number":               "requester_employee_number", # SUPPRIMÉ

    # ── Intervenant ───────────────────────────────────────────────────────────
    "agent_id_friendlyname":                "agent_name",               # PSEUDONYMISÉ
    "agent_email":                          "agent_email",              # SUPPRIMÉ
    "agent_employee_number":                "agent_employee_number",    # SUPPRIMÉ

    # ── Equipe ────────────────────────────────────────────────────────────────
    "team_id_friendlyname":                 "team_name",
    "team_email":                           "team_email",               # SUPPRIMÉ

    # ── Service ───────────────────────────────────────────────────────────────
    "service_name":                         "service_name",
    "servicesubcategory_name":              "service_subcategory",

    # ── CI ───────────────────────────────────────────────────────────────────
    "functionalci_name":                    "ci_name",

    # ── Résolution ────────────────────────────────────────────────────────────
    "root_cause":                           "root_cause_raw",
    "resolution_code":                      "resolution_code",
    "solution":                             "solution",
    "resolution_delay":                     "itop_resolution_delay_min",
    "total_suspend_time":                   "suspension_cumulated",

    # ── SLA ───────────────────────────────────────────────────────────────────
    "tto_passed":                           "sla_tto_breached_itop",
    "ttr_passed":                           "sla_ttr_breached_itop",

    # ── Champs personnalisés (décommenter selon votre instance) ───────────────
    # "attrib_date_rejet":                  "rejected_at",
    # "attrib_type_resolution":             "resolution_type",
    # "attrib_raison_rejet":                "reject_reason",
    # "parent_incident_id_friendlyname":    "parent_incident_ref",
    # "parent_change_id_friendlyname":      "parent_change_ref",

    # ── Données sensibles ─────────────────────────────────────────────────────
    "mobile_phone":                         "mobile_phone",             # SUPPRIMÉ
    "customer_comment":                     "customer_comment",         # SUPPRIMÉ
}


# ──────────────────────────────────────────────────────────────────────────────
# Colonnes à supprimer après renommage (données personnelles)
# ──────────────────────────────────────────────────────────────────────────────

SENSITIVE_COLUMNS_TO_DROP: set[str] = {
    "requester_fullname",
    "requester_lastname",
    "requester_firstname",
    "requester_email",
    "requester_employee_number",
    "agent_email",
    "agent_employee_number",
    "agent_lastname",
    "agent_firstname",
    "team_email",
    "provider_email",
    "mobile_phone",
    "customer_comment",
}

# Regex filet de sécurité : colonnes PII résiduelles
_PII_PATTERN = re.compile(
    r"(email|phone|mobile|employee|employ[eé]|prenom|firstname|"
    r"lastname|nom_complet|num[eé]ro_employ)",
    re.IGNORECASE,
)

# Colonnes de dates à convertir en datetime pandas (noms post-renommage)
DATE_COLUMNS: list[str] = [
    "created_at",
    "assigned_at",
    "resolved_at",
    "closed_at",
    "rejected_at",
    "last_update",
    "tto_deadline",
    "ttr_deadline",
]

# Valeurs textuelles à considérer comme vides (insensible à la casse)
_EMPTY_MARKERS: set[str] = {".", "-", "n/a", "nf", "non connue", "r.a.s", "ras"}

# Colonnes sur lesquelles appliquer _EMPTY_MARKERS
_EMPTY_MARKER_COLS: set[str] = {"root_cause_raw", "reject_reason"}


# ══════════════════════════════════════════════════════════════════════════════
# E3 — Patterns de catégorisation des causes racines (modifiable sans toucher au code)
# ══════════════════════════════════════════════════════════════════════════════

ROOT_CAUSE_PATTERNS: dict[str, list[str]] = {
    "Saturation ressources (CPU/RAM/DB)": [
        r"cpu", r"m[eé]moire", r"saturation", r"consommation.excessive",
        r"performance", r"ressource",
    ],
    "Base de données": [
        r"base.de.donn", r"oracle", r"\bsql\b", r"\bdba\b", r"tablespace", r"archiv",
    ],
    "Réseau / Sécurité": [
        r"r[eé]seau", r"vlan", r"firewall", r"connectivit", r"mpls",
        r"liaison", r"load.balancer",
    ],
    "Traitements / Flux / Interfaces": [
        r"batch", r"traitement", r"\bjob\b", r"fichier", r"int[eé]gration",
        r"flux", r"\bqueue\b", r"interface",
    ],
    "Changement / Déploiement": [
        r"patch", r"version", r"mise.a.jour", r"d[eé]ploiement", r"livraison",
        r"hotfix", r"param[eé]tr",
    ],
    "Tiers / Partenaire externe": [
        r"prestataire", r"partenaire", r"externe", r"\bsmt\b", r"\bs2m\b",
        r"orange", r"refinitiv", r"pluxee",
    ],
    "Infrastructure / Matériel": [
        r"serveur", r"disque", r"stockage", r"mat[eé]riel", r"datacenter",
        r"sparc", r"hardware",
    ],
    "Accès / Licences / Certificats": [
        r"certificat", r"licence", r"mot.de.passe", r"acc[eè]s",
        r"authent", r"identity",
    ],
}
_ROOT_CAUSE_FALLBACK = "Applicatif / Autre"

# Statuts considérés ouverts / résolus
_OPEN_STATUSES: set[str] = {"Nouveau", "Assignée", "En attente", "new", "assigned", "pending"}
_RESOLVED_STATUSES: set[str] = {"Résolue", "Fermée", "resolved", "closed"}


# ══════════════════════════════════════════════════════════════════════════════
# Fonctions utilitaires internes
# ══════════════════════════════════════════════════════════════════════════════

def _remove_accents(text: str) -> str:
    """Supprime les diacritiques pour la correspondance insensible aux accents."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _pseudonymize(value: Any, salt: str) -> Any:
    """Remplace une valeur par son hash SHA-256 (8 chars) salé."""
    if pd.isna(value) or not str(value).strip():
        return value
    combined = (salt + str(value).strip()).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()[:8]


def _extract_scalar(value: Any) -> Any:
    """Extrait une valeur scalaire depuis les formats retournés par l'API iTop."""
    if isinstance(value, dict):
        return value.get("value") or value.get("friendlyname") or ""
    return value


def _flatten_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise les enregistrements API iTop en dictionnaires plats."""
    return [
        {key: _extract_scalar(val) for key, val in record.items()}
        for record in records
    ]


def _apply_field_map(df: pd.DataFrame, field_map: dict[str, str]) -> pd.DataFrame:
    """Conserve uniquement les colonnes présentes dans field_map et les renomme."""
    known = {col: field_map[col] for col in df.columns if col in field_map}
    unknown = [col for col in df.columns if col not in field_map]

    if unknown:
        logger.debug(
            f"{len(unknown)} colonne(s) ignorée(s) (hors mapping) : "
            f"{unknown[:10]}{'…' if len(unknown) > 10 else ''}"
        )

    return df[list(known.keys())].rename(columns=known)


# ══════════════════════════════════════════════════════════════════════════════
# E2 — Fonctions de nettoyage
# ══════════════════════════════════════════════════════════════════════════════

def _convert_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Convertit les colonnes de dates en datetime pandas (errors='coerce')."""
    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _normalize_text_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Trim + normalisation des espaces multiples sur toutes les colonnes texte."""
    text_cols = df.select_dtypes(include=["object"]).columns
    for col in text_cols:
        df[col] = (
            df[col]
            .astype(str)
            .str.strip()
            .str.replace(r"\s+", " ", regex=True)
        )
        df[col] = df[col].replace({"": pd.NA, "None": pd.NA, "nan": pd.NA, "null": pd.NA, "<NA>": pd.NA})
    return df


def _replace_empty_markers(df: pd.DataFrame) -> pd.DataFrame:
    """Remplace les valeurs placeholder (., -, N/A, NF…) par NaN dans les colonnes texte libres."""
    for col in _EMPTY_MARKER_COLS:
        if col not in df.columns:
            continue
        mask = df[col].apply(
            lambda v: str(v).strip().lower() in _EMPTY_MARKERS if pd.notna(v) else False
        )
        df.loc[mask, col] = pd.NA
    return df


def _normalize_status(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise le statut : 'rejected' (anglais) → 'Rejetée' (français)."""
    if "status" not in df.columns:
        return df
    df["status"] = df["status"].replace({"rejected": "Rejetée"})
    return df


def _normalize_priority_urgency(df: pd.DataFrame) -> pd.DataFrame:
    """Met priority et urgency en minuscules."""
    for col in ("priority", "urgency"):
        if col in df.columns:
            df[col] = df[col].str.lower() if df[col].dtype == object else df[col]
    return df


def _normalize_requester_site(df: pd.DataFrame) -> pd.DataFrame:
    """Met requester_site en majuscules (homogénéisation Y114 / y114)."""
    if "requester_site" in df.columns:
        df["requester_site"] = df["requester_site"].str.upper()
    return df


def _drop_sensitive_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Supprime les colonnes contenant des données personnelles sensibles."""
    to_drop = SENSITIVE_COLUMNS_TO_DROP & set(df.columns)
    if to_drop:
        df = df.drop(columns=list(to_drop))
        logger.info(f"Suppression de {len(to_drop)} colonne(s) sensible(s) : {sorted(to_drop)}")
    return df


def _warn_residual_pii(df: pd.DataFrame) -> None:
    """Avertit si des colonnes PII semblent encore présentes après masquage."""
    suspicious = [col for col in df.columns if _PII_PATTERN.search(col)]
    if suspicious:
        logger.warning(
            f"Colonnes potentiellement sensibles encore présentes : {suspicious}. "
            "Vérifiez SENSITIVE_COLUMNS_TO_DROP dans transform.py."
        )


def _handle_agent_name(df: pd.DataFrame, pii_salt: str, include_agent_name: bool) -> pd.DataFrame:
    """
    Gère la colonne agent_name selon le flag INCLUDE_AGENT_NAME.

    Si include_agent_name=True  : nom conservé en clair (déjà normalisé par
      _normalize_text_columns) ; les valeurs vides → "Non assigné".
    Si include_agent_name=False : remplacement par un hash SHA-256 (8 chars).
    Les autres colonnes PII ont déjà été supprimées par _drop_sensitive_columns.
    """
    if "agent_name" not in df.columns:
        return df
    if include_agent_name:
        df["agent_name"] = df["agent_name"].fillna("Non assigné")
        logger.info("agent_name conservé en clair (INCLUDE_AGENT_NAME=True).")
    else:
        df["agent_name"] = df["agent_name"].apply(lambda v: _pseudonymize(v, pii_salt))
        logger.info("agent_name pseudonymisé (SHA-256 / 8 chars).")
    return df


def _deduplicate_by_reference(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Supprime les doublons sur 'reference' en gardant la ligne avec last_update
    la plus récente. Retourne le DataFrame dédupliqué et le nombre de doublons supprimés.
    """
    if "reference" not in df.columns:
        return df, 0

    n_before = len(df)
    if "last_update" in df.columns:
        df = df.sort_values("last_update", ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["reference"], keep="first")
    n_after = len(df)
    n_dupes = n_before - n_after
    if n_dupes:
        logger.info(f"Dédoublonnage : {n_dupes} doublon(s) supprimé(s) sur 'reference'.")
    return df.reset_index(drop=True), n_dupes


# ══════════════════════════════════════════════════════════════════════════════
# E3 — Colonnes analytiques calculées
# ══════════════════════════════════════════════════════════════════════════════

def _categorize_root_cause(value: Any) -> Any:
    """Catégorise root_cause_raw par correspondance de patterns."""
    if pd.isna(value) or not str(value).strip():
        return pd.NA
    normalized = _remove_accents(str(value).lower())
    for category, patterns in ROOT_CAUSE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, normalized, re.IGNORECASE):
                return category
    return _ROOT_CAUSE_FALLBACK


def _ttr_bucket(hours: Any) -> str:
    """Classe ttr_real_hours en intervalles lisibles."""
    if pd.isna(hours):
        return "Non résolu"
    h = float(hours)
    if h <= 1:
        return "<=1h"
    if h <= 4:
        return "1-4h"
    if h <= 24:
        return "4-24h"
    if h <= 168:  # 7 jours
        return "1-7j"
    return ">7j"


def add_analytical_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ajoute toutes les colonnes analytiques E3 au DataFrame nettoyé.

    Colonnes produites :
      nature, tto_real_min, ttr_real_hours, ttr_bucket, is_open, is_resolved,
      is_rejected, is_critical, sla_tto_breached_calc, sla_ttr_breached_calc,
      sla_target_hours, is_out_of_target, root_cause_category,
      has_root_cause, has_ci, has_parent_change, created_month,
      created_date, resolved_date, days_since_last_update
    """
    # E3.1 — Nature fonctionnelle / technique
    if "service_name" in df.columns:
        df["nature"] = df["service_name"].apply(
            lambda v: "Fonctionnel" if pd.notna(v) and "fonctionnel" in str(v).lower() else "Technique"
        )
    else:
        df["nature"] = "Technique"

    # E3.2 — TTO réel (minutes)
    if "assigned_at" in df.columns and "created_at" in df.columns:
        df["tto_real_min"] = (
            (df["assigned_at"] - df["created_at"]).dt.total_seconds() / 60
        )
        neg_tto = int((df["tto_real_min"] < 0).sum())
        if neg_tto:
            logger.warning(f"tto_real_min : {neg_tto} valeur(s) négative(s) → NaN.")
        df.loc[df["tto_real_min"] < 0, "tto_real_min"] = pd.NA
    else:
        df["tto_real_min"] = pd.NA

    # E3.3 — TTR réel (heures)
    if "resolved_at" in df.columns and "created_at" in df.columns:
        df["ttr_real_hours"] = (
            (df["resolved_at"] - df["created_at"]).dt.total_seconds() / 3600
        )
        neg_ttr = int((df["ttr_real_hours"] < 0).sum())
        if neg_ttr:
            logger.warning(f"ttr_real_hours : {neg_ttr} valeur(s) négative(s) → NaN.")
        df.loc[df["ttr_real_hours"] < 0, "ttr_real_hours"] = pd.NA
    else:
        df["ttr_real_hours"] = pd.NA

    # E3.4 — Bucket TTR
    df["ttr_bucket"] = df["ttr_real_hours"].apply(_ttr_bucket)

    # E3.5 — is_open
    if "status" in df.columns:
        df["is_open"] = df["status"].isin(_OPEN_STATUSES).astype("Int8")
    else:
        df["is_open"] = pd.NA

    # E3.6 — is_resolved
    if "status" in df.columns:
        df["is_resolved"] = df["status"].isin(_RESOLVED_STATUSES).astype("Int8")
    else:
        df["is_resolved"] = pd.NA

    # E3.7 — is_rejected
    if "status" in df.columns:
        df["is_rejected"] = (df["status"] == "Rejetée").astype("Int8")
    else:
        df["is_rejected"] = pd.NA

    # E3.8 — is_critical
    if "priority" in df.columns:
        df["is_critical"] = (df["priority"] == "critique").astype("Int8")
    else:
        df["is_critical"] = pd.NA

    # E3.9 — SLA recalculés (ne pas se fier aux flags iTop)
    df["sla_tto_breached_calc"] = pd.NA
    if "assigned_at" in df.columns and "tto_deadline" in df.columns:
        mask = df["assigned_at"].notna() & df["tto_deadline"].notna()
        df.loc[mask, "sla_tto_breached_calc"] = (
            df.loc[mask, "assigned_at"] > df.loc[mask, "tto_deadline"]
        ).astype("Int8")

    df["sla_ttr_breached_calc"] = pd.NA
    if "resolved_at" in df.columns and "ttr_deadline" in df.columns:
        mask = df["resolved_at"].notna() & df["ttr_deadline"].notna()
        df.loc[mask, "sla_ttr_breached_calc"] = (
            df.loc[mask, "resolved_at"] > df.loc[mask, "ttr_deadline"]
        ).astype("Int8")

    # E3.10 — SLA cibles internes
    if "priority" in df.columns:
        df["sla_target_hours"] = df["priority"].map(SLA_TARGET_HOURS)
        df["is_out_of_target"] = pd.NA
        mask = df["ttr_real_hours"].notna() & df["sla_target_hours"].notna()
        df.loc[mask, "is_out_of_target"] = (
            df.loc[mask, "ttr_real_hours"] > df.loc[mask, "sla_target_hours"]
        ).astype("Int8")
    else:
        df["sla_target_hours"] = pd.NA
        df["is_out_of_target"] = pd.NA

    # E3.11 — Catégorie de cause racine
    if "root_cause_raw" in df.columns:
        df["root_cause_category"] = df["root_cause_raw"].apply(_categorize_root_cause)
    else:
        df["root_cause_category"] = pd.NA

    # E3.12 — Indicateurs de complétude
    df["has_root_cause"] = df.get("root_cause_raw", pd.Series(dtype=object)).notna().astype("Int8")
    df["has_ci"] = df.get("ci_name", pd.Series(dtype=object)).notna().astype("Int8")
    df["has_parent_change"] = (
        df.get("parent_change_ref", pd.Series(dtype=object)).notna().astype("Int8")
    )

    # E3.13 — Mois de création (clé de secours)
    if "created_at" in df.columns:
        df["created_month"] = df["created_at"].dt.strftime("%Y-%m")
    else:
        df["created_month"] = pd.NA

    # Dates sans heure pour relation avec dim_date (ajustement utilisateur)
    if "created_at" in df.columns:
        df["created_date"] = df["created_at"].dt.date
    else:
        df["created_date"] = pd.NA

    if "resolved_at" in df.columns:
        df["resolved_date"] = df["resolved_at"].dt.date
    else:
        df["resolved_date"] = pd.NA

    # E3.14 — Ancienneté depuis la dernière mise à jour (en jours calendaires entiers)
    # Normalisation à minuit des deux membres pour éviter le décalage dû à l'heure.
    if "last_update" in df.columns:
        today = pd.Timestamp.today().normalize()
        df["days_since_last_update"] = (today - df["last_update"].dt.normalize()).dt.days
        df.loc[df["last_update"].isna(), "days_since_last_update"] = pd.NA

    logger.info(f"Colonnes analytiques ajoutées : {len(df.columns)} colonnes au total.")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline interne partagé
# ══════════════════════════════════════════════════════════════════════════════

def _shared_cleaning_pipeline(
    df: pd.DataFrame,
    pii_salt: str,
    include_agent_name: bool = True,
) -> tuple[pd.DataFrame, int]:
    """
    Pipeline de nettoyage E2 commun aux deux modes.

    Étapes :
      1. Suppression des colonnes sensibles (autres PII)
      2. Conversion des dates
      3. Normalisation texte (trim, espaces, NaN)
      4. Remplacement des marqueurs vides
      5. Normalisation statut / priorité / urgence / requester_site
      6. Gestion agent_name (clair ou hash) — après normalisation texte
      7. Dédoublonnage sur reference
      8. Alerte PII résiduelle

    Retourne (df_clean, n_duplicates_removed).
    """
    df = _drop_sensitive_columns(df)
    df = _convert_dates(df)
    df = _normalize_text_columns(df)
    df = _replace_empty_markers(df)
    df = _normalize_status(df)
    df = _normalize_priority_urgency(df)
    df = _normalize_requester_site(df)
    df = _handle_agent_name(df, pii_salt, include_agent_name)
    df, n_dupes = _deduplicate_by_reference(df)
    _warn_residual_pii(df)
    return df, n_dupes


# ══════════════════════════════════════════════════════════════════════════════
# Points d'entrée publics
# ══════════════════════════════════════════════════════════════════════════════

def build_clean_dataframe(
    records: list[dict[str, Any]],
    pii_salt: str = "",
    include_agent_name: bool = True,
) -> tuple[pd.DataFrame, int]:
    """
    MODE API — Transforme les enregistrements bruts iTop en DataFrame Power BI.

    Returns:
        Tuple (df_clean, n_duplicates_removed).
    """
    if not records:
        logger.warning("Aucun enregistrement à transformer (mode API).")
        return pd.DataFrame(), 0

    logger.info(f"[MODE API] Transformation de {len(records)} enregistrement(s)…")

    flat = _flatten_records(records)
    df = pd.DataFrame(flat)
    df = _apply_field_map(df, FIELD_MAP_API)
    df, n_dupes = _shared_cleaning_pipeline(df, pii_salt, include_agent_name)
    df = add_analytical_columns(df)

    logger.info(
        f"[MODE API] Transformation terminée : {len(df)} lignes × {len(df.columns)} colonnes."
    )
    return df, n_dupes


def build_clean_dataframe_from_xlsx(
    df_raw: pd.DataFrame,
    pii_salt: str = "",
    include_agent_name: bool = True,
) -> tuple[pd.DataFrame, int]:
    """
    MODE XLSX — Transforme un DataFrame issu d'un export Excel iTop.

    Returns:
        Tuple (df_clean, n_duplicates_removed).
    """
    if df_raw.empty:
        logger.warning("DataFrame vide reçu en mode XLSX.")
        return pd.DataFrame(), 0

    logger.info(
        f"[MODE XLSX] Transformation de {len(df_raw)} lignes "
        f"× {len(df_raw.columns)} colonnes…"
    )

    df = _apply_field_map(df_raw.copy(), FIELD_MAP_XLSX)
    df, n_dupes = _shared_cleaning_pipeline(df, pii_salt, include_agent_name)
    df = add_analytical_columns(df)

    logger.info(
        f"[MODE XLSX] Transformation terminée : {len(df)} lignes × {len(df.columns)} colonnes."
    )
    return df, n_dupes
