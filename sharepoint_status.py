"""
sharepoint_status.py — Ecrit le statut d'exécution du pipeline dans une liste
SharePoint, pour donner un retour visuel à l'utilisateur métier lorsque le
pipeline est déclenché à distance (GitHub Actions via Power Automate).

Authentification : réutilise le flux OAuth2 client credentials déjà en place
dans sharepoint_loader.py (mêmes credentials SHAREPOINT_TENANT_ID /
SHAREPOINT_CLIENT_ID / SHAREPOINT_CLIENT_SECRET). Aucune nouvelle
authentification n'est créée ici.

Colonnes attendues dans la liste SharePoint cible (noms internes de colonne) :
  - DateHeure : texte ou date/heure — horodatage UTC (ISO 8601) de l'écriture
  - Statut    : texte (ou choix) — "En cours" / "OK" / "Erreur"
  - Title (colonne "Titre" intégrée, présente sur toute liste SharePoint) —
    utilisée pour le message/détail (vide pour "En cours" / "OK"). On
    réutilise volontairement Title plutôt qu'une colonne "Message" custom :
    une colonne créée à la main peut recevoir un nom interne différent de son
    nom affiché (ex. observé : displayName "Message" -> nom interne réel
    "LinkTitle", un champ système en lecture seule) — Title, lui, est
    garanti présent et éditable sur n'importe quelle liste.

Le nom de la liste est configurable via SHAREPOINT_STATUS_LIST_NAME. Si la
variable est vide, l'écriture de statut est silencieusement ignorée : c'est
une fonctionnalité annexe, pas une dépendance dure du pipeline.

IMPORTANT : write_run_status() ne lève JAMAIS d'exception. Une panne de la
liste de statut (permissions, liste absente, réseau) ne doit ni masquer ni
aggraver un échec du pipeline lui-même — elle est journalisée en warning et
avalée.
"""

from datetime import datetime, timezone

import requests

from config import Config
from logger_config import setup_logger
from sharepoint_loader import _GRAPH_BASE, _get_access_token, SharePointAuthError

logger = setup_logger(__name__)

_REQUEST_TIMEOUT = 30
_MESSAGE_MAX_LEN = 1000  # évite d'envoyer un message d'erreur démesuré à la liste


def write_run_status(cfg: Config, statut: str, message: str = "") -> None:
    """
    Ecrit une ligne de statut ("En cours" / "OK" / "Erreur") dans la liste
    SharePoint désignée par SHAREPOINT_STATUS_LIST_NAME.

    Args:
        cfg:     Configuration du pipeline (credentials SharePoint réutilisés).
        statut:  "En cours", "OK" ou "Erreur".
        message: Détail optionnel (vide pour "En cours" / "OK", trace courte
                 de l'erreur pour "Erreur").

    Ne lève jamais d'exception : toute erreur est journalisée en warning et
    ignorée, pour que le retour de statut ne puisse jamais faire échouer (ou
    masquer l'échec réel de) le pipeline.
    """
    list_name = cfg.run_status_list_name
    if not list_name:
        logger.debug(
            "SHAREPOINT_STATUS_LIST_NAME non défini — écriture de statut ignorée."
        )
        return

    if not cfg.sp_site_id:
        logger.warning(
            "Statut d'exécution non écrit : SHAREPOINT_SITE_ID non défini dans .env."
        )
        return

    horodatage = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    message_tronque = (message or "")[:_MESSAGE_MAX_LEN]

    try:
        token = _get_access_token(cfg)
    except SharePointAuthError as exc:
        logger.warning(f"Statut d'exécution non écrit (échec d'authentification) : {exc}")
        return

    url = f"{_GRAPH_BASE}/sites/{cfg.sp_site_id}/lists/{list_name}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "fields": {
            "DateHeure": horodatage,
            "Statut": statut,
            # Title = colonne "Titre" intégrée (toujours présente, toujours
            # éditable) — voir le docstring du module pour le pourquoi.
            "Title": message_tronque or statut,
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # NB: Response.__bool__() renvoie self.ok (False si status >= 400),
        # donc "if exc.response" est systématiquement faux sur une vraie
        # erreur HTTP — comparer explicitement à None.
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:300] if exc.response is not None else ""
        logger.warning(
            f"Echec de l'écriture du statut d'exécution dans SharePoint "
            f"(liste '{list_name}', HTTP {status}) : {body}"
        )
        return
    except requests.exceptions.RequestException as exc:
        logger.warning(
            f"Erreur réseau lors de l'écriture du statut d'exécution dans "
            f"SharePoint (liste '{list_name}') : {exc}"
        )
        return

    logger.info(f"Statut d'exécution '{statut}' écrit dans la liste SharePoint '{list_name}'.")
