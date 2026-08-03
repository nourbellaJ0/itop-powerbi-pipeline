"""
sql_loader.py — Chargement des données dans une base SQL via SQLAlchemy.

Supporte :
  - Une table plate (mode legacy --legacy-flat) : load_to_sql(df, cfg)
  - Le modèle en étoile complet               : load_to_sql(tables, cfg)
    tables est alors un dict[str, pd.DataFrame] avec les clés :
    "fact_incidents", "dim_service", "dim_subcategory", "dim_team",
    "dim_ci", "dim_priority", "dim_date"

Stratégie de chargement : REPLACE (DROP + CREATE + INSERT) à chaque run.
Index ajoutés sur les colonnes clés pour les performances Power BI.
"""

from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text, Index, MetaData, Table
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from config import Config
from logger_config import setup_logger

logger = setup_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class SQLLoaderError(Exception):
    """Levée quand l'insertion SQL échoue."""


class SQLConnectionError(SQLLoaderError):
    """Levée quand la connexion à la base est impossible."""


# ──────────────────────────────────────────────────────────────────────────────
# Connexion
# ──────────────────────────────────────────────────────────────────────────────

def _build_connection_string(cfg: Config) -> str:
    """Construit l'URL SQLAlchemy. Le mot de passe est URL-encodé."""
    required = {"SQL_HOST": cfg.sql_host, "SQL_DATABASE": cfg.sql_database,
                "SQL_USER": cfg.sql_user, "SQL_PASSWORD": cfg.sql_password}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SQLLoaderError(f"Configuration SQL incomplète. Variables manquantes : {missing}.")

    dialect  = cfg.sql_dialect.lower().strip()
    password = quote_plus(cfg.sql_password)

    if "mssql" in dialect or "pyodbc" in dialect:
        driver = quote_plus("ODBC Driver 17 for SQL Server")
        return (
            f"mssql+pyodbc://{cfg.sql_user}:{password}@{cfg.sql_host}:{cfg.sql_port}"
            f"/{cfg.sql_database}?driver={driver}&TrustServerCertificate=yes&encrypt=yes"
        )
    if "postgresql" in dialect or "psycopg2" in dialect:
        return (
            f"postgresql+psycopg2://{cfg.sql_user}:{password}"
            f"@{cfg.sql_host}:{cfg.sql_port}/{cfg.sql_database}"
        )
    if dialect == "sqlite":
        logger.warning("Dialecte SQLite — tests locaux uniquement.")
        return f"sqlite:///{cfg.sql_database}"

    raise SQLLoaderError(
        f"Dialecte non supporté : '{dialect}'. "
        "Valeurs acceptées : 'mssql+pyodbc', 'postgresql+psycopg2', 'sqlite'."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Chargement d'une table unique
# ──────────────────────────────────────────────────────────────────────────────

def _load_single_table(
    df: pd.DataFrame,
    table_name: str,
    engine,
    index_cols: list[str] | None = None,
    if_exists: str = "replace",
) -> None:
    """
    Charge un DataFrame dans une table SQL.

    if_exists="replace" (défaut) : DROP + CREATE + INSERT à chaque run.
    if_exists="append" : conserve l'existant et ajoute les nouvelles lignes
      (utilisé pour 'ai_insights', qui historise les synthèses mensuelles).

    Crée un index sur les colonnes spécifiées après insertion.
    """
    df_insert = df.copy()
    # Convertir les datetime avec timezone en naive
    for col in df_insert.select_dtypes(include=["datetimetz"]).columns:
        df_insert[col] = df_insert[col].dt.tz_localize(None)

    df_insert.to_sql(
        name=table_name,
        con=engine,
        if_exists=if_exists,
        index=False,
        chunksize=500,
        method="multi",
    )
    logger.info(f"  '{table_name}' : {len(df_insert)} ligne(s) insérée(s) (if_exists='{if_exists}').")

    if index_cols:
        meta = MetaData()
        meta.reflect(bind=engine, only=[table_name])
        tbl = meta.tables.get(table_name)
        if tbl is not None:
            for col in index_cols:
                if col in tbl.c:
                    idx = Index(f"ix_{table_name}_{col}", tbl.c[col])
                    try:
                        idx.create(bind=engine, checkfirst=True)
                    except Exception:
                        pass  # L'index existe déjà


# ──────────────────────────────────────────────────────────────────────────────
# Point d'entrée public
# ──────────────────────────────────────────────────────────────────────────────

def load_to_sql(
    data: pd.DataFrame | dict[str, pd.DataFrame],
    cfg: Config,
) -> None:
    """
    Charge les données dans la base SQL.

    Accepte :
      - un DataFrame unique (mode legacy --legacy-flat) → table cfg.sql_table
      - un dict[str, pd.DataFrame] (modèle en étoile) → une table par entrée

    Args:
        data: DataFrame plat ou dict de DataFrames (modèle en étoile).
        cfg:  Configuration du pipeline.

    Raises:
        SQLConnectionError: Si la connexion échoue.
        SQLLoaderError:     Si l'insertion échoue.
    """
    logger.info(
        f"Connexion SQL — {cfg.sql_dialect} | {cfg.sql_host}:{cfg.sql_port} | "
        f"{cfg.sql_database} | user={cfg.sql_user}"
    )

    conn_str = _build_connection_string(cfg)
    engine   = None

    # Index par table pour accélérer les requêtes Power BI
    _INDEX_COLS: dict[str, list[str]] = {
        "fact_incidents":  ["reference"],
        "dim_service":     ["service_key"],
        "dim_subcategory": ["subcategory_key"],
        "dim_team":        ["team_key"],
        "dim_ci":          ["ci_key"],
        "dim_priority":    ["priority_key"],
        "dim_date":        ["date"],
        "ai_insights":     [],
    }

    # ai_insights historise les synthèses IA mensuelles : on append au lieu
    # de remplacer, pour ne pas perdre les runs précédents.
    _APPEND_TABLES: set[str] = {"ai_insights"}

    try:
        engine = create_engine(
            conn_str, pool_pre_ping=True, pool_size=1, max_overflow=0
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Connexion SQL établie avec succès.")

        if isinstance(data, pd.DataFrame):
            # Mode legacy : table plate unique
            if data.empty:
                logger.warning("DataFrame vide — chargement SQL ignoré.")
                return
            _load_single_table(
                data, cfg.sql_table, engine,
                index_cols=_INDEX_COLS.get(cfg.sql_table, ["reference"])
            )
        else:
            # Mode étoile : dict de DataFrames
            logger.info(f"Chargement du modèle en étoile : {list(data.keys())}")
            for table_name, df in data.items():
                if df is None or df.empty:
                    logger.warning(f"  '{table_name}' vide — ignorée.")
                    continue
                _load_single_table(
                    df, table_name, engine,
                    index_cols=_INDEX_COLS.get(table_name),
                    if_exists="append" if table_name in _APPEND_TABLES else "replace",
                )

    except OperationalError as exc:
        raise SQLConnectionError(
            f"Impossible de se connecter. Vérifiez SQL_HOST, SQL_PORT, SQL_USER. Détail : {exc}"
        ) from exc
    except SQLAlchemyError as exc:
        raise SQLLoaderError(f"Erreur SQL : {exc}") from exc
    finally:
        if engine is not None:
            engine.dispose()
            logger.debug("Connexion SQL fermée.")
