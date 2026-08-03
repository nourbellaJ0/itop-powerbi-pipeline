"""
main.py — Point d'entrée du pipeline iTop → Power BI.

Deux modes d'entrée (INPUT_MODE) :
  api  → iTop API → transform → modeling → quality → export/SharePoint/SQL
  xlsx → Fichier Excel local → transform → modeling → quality → export/SharePoint/SQL
    sharepoint → Fichier Excel source téléchargé depuis SharePoint → transform → modeling → quality → export/SharePoint/SQL

Deux formats de sortie :
  Normal     → Classeur multi-feuilles incidents_itop_model.xlsx (modèle en étoile)
  --legacy-flat → Table plate incidents_itop_clean.xlsx (comportement avant refonte)

Utilisation :
    python main.py
    python main.py --input sharepoint --output sharepoint
    python main.py --input xlsx --output sharepoint
    python main.py --input xlsx --dry-run
    python main.py --legacy-flat
    python main.py --input api --full-refresh --dry-run
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config import load_config
from logger_config import setup_logger
from quality_checks import run_all_checks

logger = setup_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Gestion de l'état incrémental (mode API uniquement)
# ──────────────────────────────────────────────────────────────────────────────

def read_last_run(filepath: str) -> datetime | None:
    """Lit la date de la dernière exécution réussie depuis last_run.json."""
    path = Path(filepath)
    if not path.exists():
        logger.info(f"Fichier '{filepath}' introuvable — première exécution ou fichier supprimé.")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        last_run = datetime.fromisoformat(data["last_run"])
        logger.info(f"Dernière exécution réussie : {last_run.isoformat()}")
        return last_run
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"Impossible de lire '{filepath}' ({exc}). Repli sur J-30.")
        return None


def write_last_run(filepath: str, run_time: datetime) -> None:
    """Enregistre la date de l'exécution en cours dans last_run.json."""
    path = Path(filepath)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"last_run": run_time.isoformat(),
             "written_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()},
            f, indent=2, ensure_ascii=False,
        )
    logger.info(f"Etat mis à jour dans '{filepath}' : {run_time.isoformat()}")


# ──────────────────────────────────────────────────────────────────────────────
# Export Excel
# ──────────────────────────────────────────────────────────────────────────────

def export_flat_to_excel(df: pd.DataFrame, filepath: str) -> None:
    """Exporte le DataFrame plat (mode --legacy-flat) vers un fichier Excel."""
    import openpyxl  # noqa: F401
    output_path = Path(filepath)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(filepath, index=False, engine="openpyxl",
                sheet_name="Incidents", freeze_panes=(1, 0))
    size_kb = output_path.stat().st_size / 1024
    logger.info(f"Export plat : {len(df)} lignes → '{filepath}' ({size_kb:.1f} Ko)")


def _read_existing_ai_insights(filepath: str) -> pd.DataFrame | None:
    """Lit l'historique existant de la feuille ai_insights (pour l'append), si présent."""
    path = Path(filepath)
    if not path.exists():
        return None
    try:
        return pd.read_excel(path, sheet_name="ai_insights", engine="openpyxl")
    except (ValueError, KeyError):
        return None
    except Exception as exc:
        logger.warning(f"Lecture de l'historique 'ai_insights' impossible : {exc}")
        return None


def export_star_schema_to_excel(
    tables: dict[str, pd.DataFrame],
    filepath: str,
) -> None:
    """
    Exporte le modèle en étoile dans un classeur multi-feuilles.

    Ordre des feuilles : fact_incidents en premier, puis les dimensions,
    puis ai_insights (historique des synthèses IA, si présente).
    """
    import openpyxl  # noqa: F401
    output_path = Path(filepath)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sheet_order = [
        "fact_incidents", "dim_service", "dim_subcategory",
        "dim_team", "dim_ci", "dim_priority", "dim_date", "ai_insights",
    ]
    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        for sheet_name in sheet_order:
            df = tables.get(sheet_name)
            if df is None:
                continue
            df.to_excel(writer, index=False, sheet_name=sheet_name, freeze_panes=(1, 0))

        # Feuilles supplémentaires éventuelles non présentes dans l'ordre défini
        for name, df in tables.items():
            if name not in sheet_order:
                df.to_excel(writer, index=False, sheet_name=name, freeze_panes=(1, 0))

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"Export modèle en étoile : {len(tables)} feuilles → '{filepath}' ({size_mb:.2f} Mo)"
    )
    for name, tbl in tables.items():
        logger.info(f"  Feuille '{name}' : {len(tbl)} lignes × {len(tbl.columns)} colonnes.")


# ──────────────────────────────────────────────────────────────────────────────
# Arguments CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline iTop → Power BI (mode API, fichier Excel local ou SharePoint)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples :\n"
            "  python main.py --input sharepoint --output sharepoint\n"
            "  python main.py --input xlsx --output sharepoint\n"
            "  python main.py --input xlsx --dry-run\n"
            "  python main.py --legacy-flat\n"
            "  python main.py --input api --full-refresh --dry-run\n"
        ),
    )
    parser.add_argument("--input", choices=["api", "xlsx", "sharepoint"], dest="input_mode",
                        help="Mode d'entrée. Ecrase INPUT_MODE du .env.")
    parser.add_argument("--output", choices=["sharepoint", "sql"], dest="output_mode",
                        help="Mode de sortie. Ecrase OUTPUT_MODE du .env.")
    parser.add_argument("--full-refresh", action="store_true",
                        help="[Mode API] Ignore last_run.json et récupère tous les incidents.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Génère le fichier Excel localement sans dépôt distant.")
    parser.add_argument(
        "--legacy-flat", action="store_true",
        help=(
            "Produit l'ancienne table plate incidents_itop_clean.xlsx au lieu "
            "du classeur multi-feuilles incidents_itop_model.xlsx."
        ),
    )
    parser.add_argument(
        "--ai", action="store_true",
        help="Force l'enrichissement IA même si AI_ENRICHMENT_ENABLED=False (pour les tests).",
    )
    parser.add_argument(
        "--ai-dry-run", action="store_true", dest="ai_dry_run",
        help=(
            "Exécute l'enrichissement IA sur les 20 premiers tickets éligibles "
            "seulement, affiche les classifications reçues et une estimation de "
            "coût, sans toucher au cache définitif ni au reste du pipeline."
        ),
    )
    parser.add_argument(
        "--ai-monetique-dry-run", action="store_true", dest="ai_monetique_dry_run",
        help=(
            "Exécute la classification Monétique sur les 20 premiers tickets "
            "'Support Monétique' éligibles seulement, affiche les classifications "
            "reçues et un résumé du cache, sans toucher au cache définitif ni au "
            "reste du pipeline."
        ),
    )
    parser.add_argument(
        "--skip-ai", action="store_true",
        help=(
            "Ignore l'enrichissement IA UC1 et Monétique pour ce run, tout en "
            "gardant l'export Excel et le dépôt SharePoint/SQL."
        ),
    )
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────────────────────
# Pipelines d'entrée
# ──────────────────────────────────────────────────────────────────────────────

def _run_api_pipeline(cfg, args, run_start: datetime):
    """Mode API : extraction iTop → transform → (df, n_dupes, source_count)."""
    from itop_client import ITopClient, ITopAPIError, ITopAuthError
    from transform import build_clean_dataframe

    logger.info("Mode d'entrée : API iTop")

    if args.full_refresh:
        since_date = None
        logger.info("Mode FULL REFRESH — tous les incidents seront récupérés.")
    else:
        since_date = read_last_run(cfg.last_run_file)
        if since_date is None:
            since_date = run_start - timedelta(days=30)
            logger.info(f"Extraction depuis : {since_date.date()} (J-30)")

    try:
        client = ITopClient(cfg)
        raw_records = client.get_incidents(since_date=since_date)
    except ITopAuthError as exc:
        logger.critical(f"Echec d'authentification iTop : {exc}")
        return 1
    except ITopAPIError as exc:
        logger.error(f"Erreur API iTop : {exc}")
        return 1
    except Exception as exc:
        logger.error(f"Erreur inattendue iTop : {exc}", exc_info=True)
        return 1

    if not raw_records:
        logger.info("iTop n'a retourné aucun incident.")
        write_last_run(cfg.last_run_file, run_start)
        return 0

    source_count = len(raw_records)
    logger.info(f"{source_count} incident(s) extraits de iTop.")

    try:
        df, n_dupes = build_clean_dataframe(
            raw_records,
            pii_salt=cfg.pii_salt,
            include_agent_name=cfg.include_agent_name,
        )
    except Exception as exc:
        logger.error(f"Echec de la transformation : {exc}", exc_info=True)
        return 1

    if df.empty:
        logger.warning("DataFrame vide après transformation.")
        write_last_run(cfg.last_run_file, run_start)
        return 0

    return df, n_dupes, source_count


def _run_xlsx_pipeline(cfg, args):
    """Mode XLSX : lecture Excel → transform → (df, n_dupes, source_count)."""
    logger.info("Mode d'entrée : fichier XLSX local")
    logger.info(f"Fichier source : '{cfg.local_xlsx_path}'")

    return _run_xlsx_pipeline_from_path(
        file_path=cfg.local_xlsx_path,
        sheet_name=cfg.local_xlsx_sheet_name or None,
        pii_salt=cfg.pii_salt,
        include_agent_name=cfg.include_agent_name,
    )


def _run_xlsx_pipeline_from_path(
    file_path: str,
    sheet_name: str | None,
    pii_salt: str,
    include_agent_name: bool,
):
    """Lecture Excel → transform → (df, n_dupes, source_count)."""
    from xlsx_loader import load_xlsx_file, XlsxLoaderError
    from transform import build_clean_dataframe_from_xlsx

    try:
        df_raw = load_xlsx_file(file_path, sheet_name=sheet_name)
    except XlsxLoaderError as exc:
        logger.error(f"Echec de la lecture du fichier Excel : {exc}")
        return 1

    source_count = len(df_raw)

    try:
        df, n_dupes = build_clean_dataframe_from_xlsx(
            df_raw,
            pii_salt=pii_salt,
            include_agent_name=include_agent_name,
        )
    except Exception as exc:
        logger.error(f"Echec de la transformation XLSX : {exc}", exc_info=True)
        return 1

    if df.empty:
        logger.warning("DataFrame vide après transformation.")
        return 1

    return df, n_dupes, source_count


def _run_sharepoint_pipeline(cfg, args):
    """Mode SharePoint : téléchargement source → lecture Excel → transformation."""
    from sharepoint_loader import download_from_sharepoint, SharePointError

    logger.info("Mode d'entrée : fichier Excel SharePoint")
    logger.info(
        f"Fichier source SharePoint : '{cfg.sharepoint_source_file_name}' "
        f"dans le dossier '{cfg.sp_folder_path}'"
    )

    temp_download_path = Path(__file__).parent / "cache" / "sharepoint_source_downloaded.xlsx"
    try:
        downloaded_path = download_from_sharepoint(
            cfg.sharepoint_source_file_name,
            str(temp_download_path),
            cfg,
        )
    except SharePointError as exc:
        logger.error(f"Echec du téléchargement SharePoint : {exc}")
        return 1

    return _run_xlsx_pipeline_from_path(
        file_path=downloaded_path,
        sheet_name=cfg.local_xlsx_sheet_name or None,
        pii_salt=cfg.pii_salt,
        include_agent_name=cfg.include_agent_name,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    global logger
    run_start = datetime.now(timezone.utc).replace(tzinfo=None)
    args      = parse_args()

    logger.info("=" * 65)
    logger.info("DEBUT DU PIPELINE iTop → Power BI")
    logger.info(f"Heure UTC de démarrage : {run_start.isoformat()}")
    logger.info("=" * 65)

    # ── 1. Configuration ──────────────────────────────────────────────────────
    try:
        cfg = load_config()
    except EnvironmentError as exc:
        logger.critical(f"Erreur de configuration : {exc}")
        return 1

    logger = setup_logger(__name__, log_level=cfg.log_level)

    input_mode  = args.input_mode  or cfg.input_mode
    output_mode = args.output_mode or cfg.output_mode
    legacy_flat = args.legacy_flat

    logger.info(f"Mode d'entrée  : {input_mode.upper()}")
    logger.info(f"Mode de sortie : {output_mode.upper()}")
    logger.info(f"Format sortie  : {'TABLE PLATE (legacy)' if legacy_flat else 'MODELE EN ETOILE'}")

    # ── 2. Extraction + Transformation ───────────────────────────────────────
    if input_mode == "api":
        result = _run_api_pipeline(cfg, args, run_start)
    elif input_mode == "xlsx":
        if args.full_refresh:
            logger.warning("--full-refresh ignoré en mode XLSX.")
        result = _run_xlsx_pipeline(cfg, args)
    elif input_mode == "sharepoint":
        if args.full_refresh:
            logger.warning("--full-refresh ignoré en mode SharePoint.")
        result = _run_sharepoint_pipeline(cfg, args)
    else:
        logger.critical(f"Mode d'entrée inconnu : '{input_mode}'.")
        return 1

    if isinstance(result, int):
        return result
    df, n_dupes, source_count = result

    if df is None or df.empty:
        logger.warning("Aucune donnée disponible après transformation.")
        return 0

    logger.info(f"DataFrame transformé : {df.shape[0]} lignes × {df.shape[1]} colonnes.")

    # ── 3. Enrichissement IA (optionnel — UC1 classification + Monétique) ────
    from ai_enrichment import enrich_dataframe
    from ai_enrichment_monetique import classify_monetique

    if args.ai_dry_run or args.ai_monetique_dry_run:
        if args.ai_dry_run:
            logger.info("[AI-DRY-RUN] Enrichissement IA limité aux 20 premiers tickets éligibles.")
            df_ai, ai_stats = enrich_dataframe(df, cfg, dry_run=True, limit=20)
            classified = df_ai.loc[
                df_ai["ai_cause_category"].notna(),
                ["reference", "ai_cause_category", "ai_cause_confidence", "ai_summary"],
            ]
            for _, row in classified.iterrows():
                logger.info(
                    f"[AI-DRY-RUN] {row['reference']} -> "
                    f"categorie='{row['ai_cause_category']}' "
                    f"confiance={row['ai_cause_confidence']} "
                    f"resume='{row['ai_summary']}'"
                )
            logger.info(
                f"[AI-DRY-RUN] {ai_stats['n_classified']}/{ai_stats['n_eligible_sampled']} classifié(s) "
                f"sur {ai_stats['n_eligible_total']} ticket(s) éligible(s) au total. "
                f"Coût estimé de l'échantillon : {ai_stats['estimated_cost_usd']}$ — "
                f"coût total projeté (tous les tickets éligibles) : "
                f"{ai_stats['estimated_cost_usd_total_projected']}$."
            )

        if args.ai_monetique_dry_run:
            logger.info("[AI-MONETIQUE-DRY-RUN] Classification Monétique limitée aux 20 premiers tickets éligibles.")
            df_mon, mon_stats = classify_monetique(df, cfg, dry_run=True, limit=20)
            classified = df_mon.loc[
                df_mon["monetique_theme"].notna(),
                ["reference", "monetique_macro_category", "monetique_theme",
                 "monetique_responsabilite", "monetique_ai_confidence"],
            ]
            for _, row in classified.iterrows():
                logger.info(
                    f"[AI-MONETIQUE-DRY-RUN] {row['reference']} -> "
                    f"macro='{row['monetique_macro_category']}' theme='{row['monetique_theme']}' "
                    f"responsabilite='{row['monetique_responsabilite']}' "
                    f"confiance={row['monetique_ai_confidence']}"
                )
            logger.info(
                f"[AI-MONETIQUE-DRY-RUN] {mon_stats['n_classified']}/{mon_stats['n_monetique_sampled']} "
                f"classifié(s) sur {mon_stats['n_monetique_total']} ticket(s) Monétique au total "
                f"({mon_stats['n_cache_hit']} depuis cache)."
            )

        return 0

    from dataclasses import replace

    cfg_ai = cfg
    if args.skip_ai:
        logger.info("[--skip-ai] Enrichissement IA UC1 et Monétique désactivé pour ce run.")
        cfg_ai = replace(
            cfg,
            ai_enrichment_enabled=False,
            monetique_ai_enabled=False,
        )
    elif args.ai and not cfg.ai_enrichment_enabled:
        logger.info("[--ai] Enrichissement IA forcé pour cette exécution.")
        cfg_ai = replace(cfg, ai_enrichment_enabled=True)

    df, ai_stats = enrich_dataframe(df, cfg_ai)
    df, monetique_stats = classify_monetique(df, cfg)

    # ── 4. Modélisation en étoile (sauf mode legacy) ─────────────────────────
    if legacy_flat:
        tables      = None
        export_file = cfg.export_file
        df_for_qc   = df
    else:
        from modeling import build_star_schema
        from ai_enrichment import build_monthly_insights_row, append_ai_insights

        tables      = build_star_schema(df)
        export_file = cfg.model_export_file
        df_for_qc   = tables["fact_incidents"]

        new_insight_row = build_monthly_insights_row(df_for_qc, cfg_ai)
        if new_insight_row is not None:
            previous_insights = _read_existing_ai_insights(export_file)
            tables["ai_insights"] = append_ai_insights(previous_insights, new_insight_row)

    # ── 5. Contrôle qualité ───────────────────────────────────────────────────
    expected_count = source_count - n_dupes
    is_valid, quality_report = run_all_checks(
        df_for_qc,
        tables=tables,
        source_count=expected_count,
        ai_stats=ai_stats,
        monetique_stats=monetique_stats,
    )
    logger.info(quality_report)

    if not is_valid:
        logger.error(
            "Erreurs critiques détectées lors du contrôle qualité. "
            "Le dépôt est annulé."
        )
        return 1

    # ── 6. Export Excel ───────────────────────────────────────────────────────
    try:
        if legacy_flat:
            export_flat_to_excel(df, export_file)
        else:
            export_star_schema_to_excel(tables, export_file)
    except ImportError:
        logger.error("Module 'openpyxl' manquant. Exécutez : pip install openpyxl")
        return 1
    except Exception as exc:
        logger.error(f"Echec de l'export Excel : {exc}", exc_info=True)
        return 1

    # ── 7. Dépôt distant (sauf --dry-run) ────────────────────────────────────
    if args.dry_run:
        logger.info(f"[DRY-RUN] Dépôt ignoré. Fichier généré : '{export_file}'")
    else:
        try:
            if output_mode == "sharepoint":
                from sharepoint_loader import upload_to_sharepoint
                upload_to_sharepoint(export_file, cfg)

            elif output_mode == "sql":
                from sql_loader import load_to_sql
                payload = df if legacy_flat else tables
                load_to_sql(payload, cfg)

            else:
                logger.error(f"Mode de sortie inconnu : '{output_mode}'.")
                return 1

        except Exception as exc:
            logger.error(f"Echec du dépôt ({output_mode}) : {exc}", exc_info=True)
            return 1

    # ── 8. Mise à jour last_run.json (mode API uniquement) ────────────────────
    if input_mode == "api":
        write_last_run(cfg.last_run_file, run_start)

    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - run_start).total_seconds()
    logger.info("=" * 65)
    logger.info(f"PIPELINE TERMINE AVEC SUCCES en {elapsed:.1f}s")
    logger.info("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
