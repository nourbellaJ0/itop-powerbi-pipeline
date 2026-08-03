"""
xlsx_loader.py — Lecture d'un fichier Excel exporté depuis iTop.

Ce module est utilisé quand INPUT_MODE=xlsx.
Il remplace entièrement l'appel à l'API iTop : au lieu d'interroger iTop,
le pipeline lit un fichier Excel déjà exporté manuellement depuis l'interface iTop.

Le fichier Excel retourné contient les colonnes avec leurs noms français
tels qu'ils apparaissent dans l'export iTop (ex: "Référence", "Date de début"…).
Ces noms seront ensuite renommés par transform.py (FIELD_MAP_XLSX).

Flux :
    Fichier .xlsx local → load_xlsx_file() → DataFrame pandas → transform.py
"""

from pathlib import Path

import pandas as pd

from logger_config import setup_logger

logger = setup_logger(__name__)

# Extensions de fichiers acceptées
_ACCEPTED_EXTENSIONS = {".xlsx", ".xls"}


class XlsxLoaderError(Exception):
    """Levée quand la lecture du fichier Excel échoue."""


def load_xlsx_file(
    file_path: str,
    sheet_name: str | None = None,
) -> pd.DataFrame:
    """
    Lit un fichier Excel exporté depuis iTop et retourne un DataFrame pandas.

    Le fichier doit contenir les colonnes iTop avec leurs noms d'affichage
    en français (ex: "Référence", "Statut", "Date de début").
    Ces colonnes seront renommées et nettoyées dans l'étape suivante (transform.py).

    Args:
        file_path:  Chemin absolu ou relatif vers le fichier Excel (.xlsx ou .xls).
        sheet_name: Nom de la feuille à lire.
                    Si None ou vide, la première feuille du classeur est utilisée.

    Returns:
        DataFrame pandas avec les colonnes brutes du fichier Excel.

    Raises:
        XlsxLoaderError: Si le fichier est introuvable, invalide, vide ou illisible.

    Exemple d'utilisation :
        df = load_xlsx_file("./data/export_itop.xlsx", sheet_name="Sheet1")
    """
    path = Path(file_path)

    # ── Vérification de l'existence du fichier ────────────────────────────────
    if not path.exists():
        raise XlsxLoaderError(
            f"Fichier Excel introuvable : '{file_path}'. "
            "Vérifiez la variable LOCAL_XLSX_PATH dans votre .env."
        )

    # ── Vérification de l'extension ───────────────────────────────────────────
    if path.suffix.lower() not in _ACCEPTED_EXTENSIONS:
        raise XlsxLoaderError(
            f"Extension non supportée : '{path.suffix}'. "
            f"Le fichier doit être au format : {', '.join(_ACCEPTED_EXTENSIONS)}."
        )

    file_size_kb = path.stat().st_size / 1024
    logger.info(
        f"Lecture du fichier Excel : '{path.name}' "
        f"({file_size_kb:.1f} Ko)"
    )

    # ── Lecture avec pandas ───────────────────────────────────────────────────
    # Si sheet_name est vide ou None, pandas lit la première feuille (index 0)
    sheet_to_read: str | int = sheet_name if sheet_name else 0

    try:
        df = pd.read_excel(
            path,
            sheet_name=sheet_to_read,
            dtype=str,          # Tout lire comme texte pour éviter les conversions automatiques
            na_values=["", "N/A", "n/a", "NULL", "null", "None", "#N/A"],
            keep_default_na=True,
            engine="openpyxl",
        )
    except ValueError as exc:
        # Feuille introuvable
        raise XlsxLoaderError(
            f"Feuille '{sheet_name}' introuvable dans '{path.name}'. "
            f"Vérifiez LOCAL_XLSX_SHEET_NAME dans votre .env. Détail : {exc}"
        ) from exc
    except Exception as exc:
        raise XlsxLoaderError(
            f"Impossible de lire le fichier Excel '{path.name}' : {exc}"
        ) from exc

    # ── Vérification que le fichier n'est pas vide ────────────────────────────
    if df.empty:
        raise XlsxLoaderError(
            f"Le fichier Excel '{path.name}' est vide "
            f"(feuille : '{sheet_to_read if sheet_name else 'première feuille'}')."
        )

    # ── Nettoyage minimal des noms de colonnes ────────────────────────────────
    # Supprimer les espaces en début/fin de nom de colonne
    # (l'export iTop peut parfois ajouter des espaces invisibles)
    df.columns = [str(col).strip() for col in df.columns]

    logger.info(
        f"Fichier lu avec succès : {len(df)} lignes × {len(df.columns)} colonnes "
        f"(feuille : '{sheet_to_read if sheet_name else 'première feuille'}')."
    )
    logger.debug(
        f"Colonnes détectées ({len(df.columns)}) : "
        f"{list(df.columns)[:10]}{'…' if len(df.columns) > 10 else ''}"
    )

    return df
