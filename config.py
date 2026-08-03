"""
config.py — Chargement et validation de la configuration.

Toutes les valeurs sensibles (token, mots de passe, secrets) sont lues
depuis des variables d'environnement ou un fichier .env.

NE JAMAIS écrire de credential en dur dans ce fichier.

Modes d'entrée supportés (INPUT_MODE) :
  api  → Extraction via l'API REST iTop (comportement original)
  xlsx → Lecture d'un fichier Excel local exporté depuis iTop (nouveau mode)
    sharepoint → Téléchargement du fichier source depuis SharePoint avant ETL

En mode "xlsx" et "sharepoint", les variables iTop (ITOP_BASE_URL,
ITOP_API_TOKEN) ne sont pas obligatoires. En mode "api", elles restent
obligatoires.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Chargement du fichier .env s'il existe.
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


# ──────────────────────────────────────────────────────────────────────────────
# Constantes métier paramétrables (modifiables ici sans toucher au code métier)
# ──────────────────────────────────────────────────────────────────────────────

# Cibles SLA internes par priorité (en heures).
# Utilisées pour calculer is_out_of_target et sla_target_hours dans fact_incidents.
SLA_TARGET_HOURS: dict[str, int] = {
    "critique": 4,
    "haute":    8,
    "moyenne":  24,
    "basse":    72,
}

# Responsables par sous-catégorie de service — page "tickets ouverts" Power BI.
# La correspondance est insensible à la casse et aux espaces multiples.
# Toute sous-catégorie absente de ce dict recevra "Non défini" dans dim_subcategory.
SUBCATEGORY_MANAGERS: dict[str, str] = {
    "Support Moyens De Paiement":                       "Mouna Laroussi Khemiri",
    "Support Banque de détail":                         "Mohamed Aziz Allagui",
    "Support Risque":                                   "Zeineb Labidi",
    "Support Internationnal":                           "Thouraya Zghal Yaiche",
    "Support Banque de Financement et d'Investissement": "Hedi Lajnef",
    "Support Monétique":                                "Sana Sellami",
    "Support Comptabilité":                             "Niazi Jedidi",
    "Support Fonction Support":                         "Safa Benltifa",
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers internes
# ──────────────────────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    """Retourne la valeur d'une variable d'environnement obligatoire.

    Raises:
        EnvironmentError: Si la variable est absente ou vide.
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"La variable d'environnement obligatoire '{name}' n'est pas définie. "
            "Vérifiez votre fichier .env ou les variables système."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Retourne la valeur d'une variable optionnelle, ou la valeur par défaut."""
    return os.getenv(name, default).strip()


# ──────────────────────────────────────────────────────────────────────────────
# Dataclass de configuration (immuable après instanciation)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    """Contient toute la configuration du pipeline. Lecture seule après init."""

    # ── Mode d'entrée ─────────────────────────────────────────────────────────
    input_mode: str          # "api" = extraction iTop | "xlsx" = fichier Excel local

    # ── iTop (requis uniquement si input_mode="api") ───────────────────────────
    itop_base_url: str
    itop_api_token: str
    itop_api_version: str
    itop_class: str
    itop_verify_ssl: bool

    # ── Fichier Excel local (requis uniquement si input_mode="xlsx") ───────────
    local_xlsx_path: str
    local_xlsx_sheet_name: str

    # ── Fichier source SharePoint (requis uniquement si input_mode="sharepoint") ─
    sharepoint_source_file_name: str

    # ── Pipeline ──────────────────────────────────────────────────────────────
    output_mode: str
    last_run_file: str
    export_file: str           # Fichier plat legacy (--legacy-flat)
    model_export_file: str     # Classeur multi-feuilles modèle en étoile
    log_level: str

    # ── Sécurité / PII ────────────────────────────────────────────────────────
    pii_salt: str              # Sel pour le hash SHA-256 de agent_name
    include_agent_name: bool   # True = nom en clair ; False = hash SHA-256

    # ── SharePoint (Graph API) ─────────────────────────────────────────────────
    sp_tenant_id: str
    sp_client_id: str
    sp_client_secret: str
    sp_site_id: str
    sp_drive_id: str
    sp_folder_path: str

    # ── SQL ───────────────────────────────────────────────────────────────────
    sql_dialect: str
    sql_host: str
    sql_port: str
    sql_database: str
    sql_user: str
    sql_password: str
    sql_table: str

    # ── Enrichissement IA (Groq) ────────────────────────────────────────────────
    ai_enrichment_enabled: bool   # Flag maître — défaut False (pipeline inchangé)
    groq_api_key: str             # Jamais loggée
    groq_model: str                # UC1 — classification (lots de 10 tickets)
    groq_model_summary: str         # UC4 — synthèse mensuelle (1 seul appel/mois)
    groq_base_url: str
    groq_max_req_per_min: int
    groq_max_tokens_per_min: int    # limite TPM du compte Groq (contrainte souvent
                                    # plus stricte que le RPM — cf. console.groq.com)
    ai_confidence_threshold: float
    ai_batch_size: int

    # ── Classification Monétique (Groq) — indépendante de l'enrichissement IA ──
    monetique_ai_enabled: bool             # Flag maître dédié — défaut False
    monetique_confidence_threshold: float  # Seuil de reporting uniquement (pas de repli regex)


# ──────────────────────────────────────────────────────────────────────────────
# Fonction principale de chargement
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> Config:
    """
    Charge et valide toute la configuration depuis l'environnement.

    Logique de validation conditionnelle :
    - Si INPUT_MODE=api       → ITOP_BASE_URL et ITOP_API_TOKEN sont obligatoires.
    - Si INPUT_MODE=xlsx      → LOCAL_XLSX_PATH est obligatoire ; iTop non requis.
    - Si INPUT_MODE=sharepoint → fichier source lu depuis SharePoint ; iTop non requis.

    Returns:
        Instance Config immuable prête à l'emploi.

    Raises:
        EnvironmentError: Si une variable obligatoire est manquante ou invalide.
    """
    # ── Validation du mode d'entrée ───────────────────────────────────────────
    input_mode = _optional("INPUT_MODE", "api").lower()
    if input_mode not in ("api", "xlsx", "sharepoint"):
        raise EnvironmentError(
            f"INPUT_MODE doit être 'api', 'xlsx' ou 'sharepoint', valeur reçue : '{input_mode}'."
        )

    # ── Variables iTop (conditionnelles) ──────────────────────────────────────
    if input_mode == "api":
        itop_base_url = _require("ITOP_BASE_URL").rstrip("/")
        itop_api_token = _require("ITOP_API_TOKEN")
    else:
        itop_base_url = _optional("ITOP_BASE_URL", "").rstrip("/")
        itop_api_token = _optional("ITOP_API_TOKEN", "")

    # ── Variable fichier Excel (conditionnelle) ────────────────────────────────
    if input_mode == "xlsx":
        local_xlsx_path = _require("LOCAL_XLSX_PATH")
    else:
        local_xlsx_path = _optional("LOCAL_XLSX_PATH", "")

    local_export_name = Path(_optional("LOCAL_XLSX_PATH", "")).name
    sharepoint_source_file_name = _optional(
        "SHAREPOINT_SOURCE_FILE_NAME",
        local_export_name or _optional("EXPORT_FILE", "incidents_itop_clean.xlsx"),
    )

    # ── Validation du mode SSL ────────────────────────────────────────────────
    verify_ssl_raw = _optional("ITOP_VERIFY_SSL", "True").lower()
    verify_ssl = verify_ssl_raw not in ("false", "0", "no", "off")
    if not verify_ssl and input_mode == "api":
        import warnings
        warnings.warn(
            "ITOP_VERIFY_SSL est désactivé. NE PAS utiliser en production !",
            stacklevel=2,
        )

    # ── Validation du mode de sortie ──────────────────────────────────────────
    output_mode = _optional("OUTPUT_MODE", "sharepoint").lower()
    if output_mode not in ("sharepoint", "sql"):
        raise EnvironmentError(
            f"OUTPUT_MODE doit être 'sharepoint' ou 'sql', valeur reçue : '{output_mode}'."
        )

    return Config(
        # Mode d'entrée
        input_mode=input_mode,

        # iTop
        itop_base_url=itop_base_url,
        itop_api_token=itop_api_token,
        itop_api_version=_optional("ITOP_API_VERSION", "1.3"),
        itop_class=_optional("ITOP_CLASS", "UserRequest"),
        itop_verify_ssl=verify_ssl,

        # Fichier Excel local
        local_xlsx_path=local_xlsx_path,
        local_xlsx_sheet_name=_optional("LOCAL_XLSX_SHEET_NAME", ""),
        sharepoint_source_file_name=sharepoint_source_file_name,

        # Pipeline
        output_mode=output_mode,
        last_run_file=_optional("LAST_RUN_FILE", "last_run.json"),
        export_file=_optional("EXPORT_FILE", "incidents_itop_clean.xlsx"),
        model_export_file=_optional("MODEL_EXPORT_FILE", "incidents_itop_model.xlsx"),
        log_level=_optional("LOG_LEVEL", "INFO"),

        # Sécurité / PII
        pii_salt=_optional("PII_SALT", "biat-itop-pipeline-default"),
        include_agent_name=_optional("INCLUDE_AGENT_NAME", "True").lower()
            not in ("false", "0", "no", "off"),

        # SharePoint
        sp_tenant_id=_optional("SHAREPOINT_TENANT_ID"),
        sp_client_id=_optional("SHAREPOINT_CLIENT_ID"),
        sp_client_secret=_optional("SHAREPOINT_CLIENT_SECRET"),
        sp_site_id=_optional("SHAREPOINT_SITE_ID"),
        sp_drive_id=_optional("SHAREPOINT_DRIVE_ID"),
        sp_folder_path=_optional("SHAREPOINT_FOLDER_PATH", "/"),

        # SQL
        sql_dialect=_optional("SQL_DIALECT", "mssql+pyodbc"),
        sql_host=_optional("SQL_HOST"),
        sql_port=_optional("SQL_PORT", "1433"),
        sql_database=_optional("SQL_DATABASE"),
        sql_user=_optional("SQL_USER"),
        sql_password=_optional("SQL_PASSWORD"),
        sql_table=_optional("SQL_TABLE", "incidents_itop_clean"),

        # Enrichissement IA (Groq) — désactivé par défaut, aucun impact sur le
        # pipeline si AI_ENRICHMENT_ENABLED=False ou GROQ_API_KEY absente.
        ai_enrichment_enabled=_optional("AI_ENRICHMENT_ENABLED", "False").lower()
            not in ("false", "0", "no", "off", ""),
        groq_api_key=_optional("GROQ_API_KEY", ""),
        groq_model=_optional("GROQ_MODEL", "llama-3.1-8b-instant"),
        groq_model_summary=_optional("GROQ_MODEL_SUMMARY", "openai/gpt-oss-120b"),
        groq_base_url=_optional("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        groq_max_req_per_min=int(_optional("GROQ_MAX_REQ_PER_MIN", "30") or 30),
        groq_max_tokens_per_min=int(_optional("GROQ_MAX_TOKENS_PER_MIN", "6000") or 6000),
        ai_confidence_threshold=float(_optional("AI_CONFIDENCE_THRESHOLD", "0.6") or 0.6),
        ai_batch_size=int(_optional("AI_BATCH_SIZE", "10") or 10),

        # Classification Monétique (Groq) — indépendante de AI_ENRICHMENT_ENABLED.
        monetique_ai_enabled=_optional("MONETIQUE_AI_ENABLED", "False").lower()
            not in ("false", "0", "no", "off", ""),
        monetique_confidence_threshold=float(
            _optional("MONETIQUE_CONFIDENCE_THRESHOLD", "0.6") or 0.6
        ),
    )
