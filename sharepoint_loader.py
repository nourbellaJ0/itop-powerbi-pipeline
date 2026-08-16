"""
sharepoint_loader.py — Dépôt du fichier Excel dans SharePoint via Microsoft Graph API.

Authentification : OAuth2 flux client credentials (app-only, sans utilisateur).

Upload :
  - Fichier ≤ 4 Mo : PUT simple (un seul appel)
  - Fichier > 4 Mo : createUploadSession + envoi par fragments de 5 Mo
    (requis pour le classeur multi-feuilles incidents_itop_model.xlsx)
"""

import math
from pathlib import Path

import requests

from config import Config
from logger_config import setup_logger

logger = setup_logger(__name__)

_GRAPH_BASE             = "https://graph.microsoft.com/v1.0"
_TOKEN_URL_TEMPLATE     = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_SIMPLE_UPLOAD_LIMIT_MB = 4
_CHUNK_SIZE_MB          = 5
_REQUEST_TIMEOUT        = 60


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class SharePointError(Exception):
    """Levée quand une opération SharePoint échoue."""


class SharePointAuthError(SharePointError):
    """Levée en cas d'échec d'authentification Azure AD."""


# ──────────────────────────────────────────────────────────────────────────────
# Authentification
# ──────────────────────────────────────────────────────────────────────────────

def _get_access_token(cfg: Config) -> str:
    """Obtient un token OAuth2 depuis Azure AD (flux client credentials)."""
    if not all([cfg.sp_tenant_id, cfg.sp_client_id, cfg.sp_client_secret]):
        raise SharePointAuthError(
            "Credentials SharePoint incomplets. "
            "Vérifiez SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID et "
            "SHAREPOINT_CLIENT_SECRET dans votre .env."
        )

    token_url = _TOKEN_URL_TEMPLATE.format(tenant_id=cfg.sp_tenant_id)
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     cfg.sp_client_id,
        "client_secret": cfg.sp_client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }

    try:
        response = requests.post(token_url, data=payload, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # NB: Response.__bool__() renvoie self.ok (False si status >= 400),
        # donc "if exc.response" est systématiquement faux sur une vraie
        # erreur HTTP — comparer explicitement à None.
        body = exc.response.text[:300] if exc.response is not None else ""
        raise SharePointAuthError(
            f"Echec de l'obtention du token (HTTP {exc.response.status_code}) : {body}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise SharePointAuthError(f"Erreur réseau lors de la demande de token : {exc}") from exc

    token_data = response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        error_desc = token_data.get("error_description", token_data.get("error", "inconnu"))
        raise SharePointAuthError(f"Réponse Azure AD sans token : {error_desc}")

    logger.info("Token Microsoft Graph obtenu avec succès.")
    return access_token


# ──────────────────────────────────────────────────────────────────────────────
# Helpers URL
# ──────────────────────────────────────────────────────────────────────────────

def _item_path_url(cfg: Config, filename: str) -> str:
    """Construit le chemin Graph API pour un fichier dans le drive SharePoint."""
    folder = cfg.sp_folder_path.strip("/")
    path = f"root:/{folder}/{filename}:" if folder else f"root:/{filename}:"
    return f"{_GRAPH_BASE}/drives/{cfg.sp_drive_id}/{path}"


# ──────────────────────────────────────────────────────────────────────────────
# Upload simple (< 4 Mo)
# ──────────────────────────────────────────────────────────────────────────────

def _upload_simple(file_path: Path, token: str, cfg: Config) -> dict:
    """Upload PUT simple pour fichiers ≤ 4 Mo."""
    url = _item_path_url(cfg, file_path.name) + "/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    }
    with open(file_path, "rb") as f:
        data = f.read()
    try:
        resp = requests.put(url, headers=headers, data=data, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # NB: Response.__bool__() renvoie self.ok (False si status >= 400),
        # donc "if exc.response" est systématiquement faux sur une vraie
        # erreur HTTP — comparer explicitement à None.
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:400] if exc.response is not None else ""
        if status in (401, 403):
            raise SharePointAuthError(
                f"Accès refusé ({status}). Vérifiez les permissions Files.ReadWrite.All."
            ) from exc
        raise SharePointError(f"Upload simple échoué (HTTP {status}) : {body}") from exc
    return resp.json()


# ──────────────────────────────────────────────────────────────────────────────
# Upload par sessions (> 4 Mo) — createUploadSession + fragments
# ──────────────────────────────────────────────────────────────────────────────

def _create_upload_session(file_path: Path, token: str, cfg: Config) -> str:
    """Crée une session d'upload et retourne l'uploadUrl."""
    url = _item_path_url(cfg, file_path.name) + "/createUploadSession"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "item": {
            "@microsoft.graph.conflictBehavior": "replace",
            "name": file_path.name,
        }
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise SharePointError(f"Impossible de créer la session d'upload : {exc}") from exc
    upload_url = resp.json().get("uploadUrl")
    if not upload_url:
        raise SharePointError("La réponse createUploadSession ne contient pas d'uploadUrl.")
    return upload_url


def _upload_chunked(file_path: Path, token: str, cfg: Config) -> dict:
    """
    Upload par fragments pour fichiers > 4 Mo via createUploadSession.

    Fragments de _CHUNK_SIZE_MB Mo envoyés en PUT séquentiels sur l'uploadUrl.
    La session Graph API n'a pas besoin du token pour les fragments (URL pré-signée).
    """
    upload_url = _create_upload_session(file_path, token, cfg)
    file_size  = file_path.stat().st_size
    chunk_size = _CHUNK_SIZE_MB * 1024 * 1024
    n_chunks   = math.ceil(file_size / chunk_size)

    logger.info(
        f"Upload fragmenté : {file_size / (1024*1024):.1f} Mo "
        f"en {n_chunks} fragment(s) de {_CHUNK_SIZE_MB} Mo."
    )

    last_response = None
    with open(file_path, "rb") as f:
        for i in range(n_chunks):
            start = i * chunk_size
            chunk = f.read(chunk_size)
            end   = start + len(chunk) - 1

            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Content-Type":   (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
            }
            try:
                resp = requests.put(
                    upload_url, headers=headers, data=chunk,
                    timeout=_REQUEST_TIMEOUT
                )
                # 202 Accepted = fragment reçu, pas encore terminé
                # 201/200 = upload terminé
                if resp.status_code not in (200, 201, 202):
                    raise SharePointError(
                        f"Fragment {i+1}/{n_chunks} rejeté (HTTP {resp.status_code}) : "
                        f"{resp.text[:300]}"
                    )
                last_response = resp
                logger.debug(f"Fragment {i+1}/{n_chunks} envoyé ({end+1}/{file_size} octets).")
            except requests.exceptions.RequestException as exc:
                raise SharePointError(
                    f"Erreur réseau au fragment {i+1}/{n_chunks} : {exc}"
                ) from exc

    return last_response.json() if last_response else {}


def download_from_sharepoint(remote_filename: str, local_filepath: str, cfg: Config) -> str:
    """Télécharge un fichier depuis SharePoint vers un chemin local.

    Args:
        remote_filename: Nom du fichier dans le dossier SharePoint cible.
        local_filepath: Chemin local où écrire le classeur téléchargé.
        cfg: Configuration du pipeline.

    Returns:
        Le chemin local du fichier téléchargé.
    """
    if not cfg.sp_drive_id:
        raise SharePointError(
            "SHAREPOINT_DRIVE_ID non renseigné dans .env. "
            "Récupérez-le via GET /sites/{site-id}/drives."
        )

    file_path = Path(local_filepath)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Téléchargement de '{remote_filename}' depuis SharePoint "
        f"(drive={cfg.sp_drive_id}, dossier='{cfg.sp_folder_path}')…"
    )

    token = _get_access_token(cfg)
    url = _item_path_url(cfg, remote_filename) + "/content"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=_REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            body = resp.text[:400]
            status = resp.status_code
            if status in (401, 403):
                raise SharePointAuthError(
                    f"Accès refusé ({status}). Vérifiez les permissions Files.ReadWrite.All."
                )
            raise SharePointError(f"Téléchargement SharePoint échoué (HTTP {status}) : {body}")
    except requests.exceptions.HTTPError as exc:
        # NB: Response.__bool__() renvoie self.ok (False si status >= 400),
        # donc "if exc.response" est systématiquement faux sur une vraie
        # erreur HTTP — comparer explicitement à None.
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:400] if exc.response is not None else ""
        if status in (401, 403):
            raise SharePointAuthError(
                f"Accès refusé ({status}). Vérifiez les permissions Files.ReadWrite.All."
            ) from exc
        raise SharePointError(f"Téléchargement SharePoint échoué (HTTP {status}) : {body}") from exc
    except requests.exceptions.RequestException as exc:
        raise SharePointError(f"Erreur réseau lors du téléchargement SharePoint : {exc}") from exc

    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    size_kb = file_path.stat().st_size / 1024
    logger.info(f"Fichier SharePoint téléchargé avec succès : '{file_path.name}' ({size_kb:.1f} Ko)")
    return str(file_path)


# ──────────────────────────────────────────────────────────────────────────────
# Point d'entrée public
# ──────────────────────────────────────────────────────────────────────────────

def upload_to_sharepoint(local_filepath: str, cfg: Config) -> None:
    """
    Dépose un fichier local dans SharePoint via Microsoft Graph API.

    Choisit automatiquement entre upload simple (≤ 4 Mo) et fragmenté (> 4 Mo).

    Args:
        local_filepath: Chemin local du fichier à déposer.
        cfg:            Configuration du pipeline.

    Raises:
        SharePointError:     Si le dépôt échoue.
        SharePointAuthError: Si l'authentification échoue.
    """
    file_path = Path(local_filepath)
    if not file_path.exists():
        raise SharePointError(f"Fichier introuvable : {local_filepath}")

    if not cfg.sp_drive_id:
        raise SharePointError(
            "SHAREPOINT_DRIVE_ID non renseigné dans .env. "
            "Récupérez-le via GET /sites/{site-id}/drives."
        )

    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"Envoi de '{file_path.name}' ({file_size_mb:.2f} Mo) vers SharePoint "
        f"(drive={cfg.sp_drive_id}, dossier='{cfg.sp_folder_path}')…"
    )

    token = _get_access_token(cfg)

    if file_size_mb <= _SIMPLE_UPLOAD_LIMIT_MB:
        item = _upload_simple(file_path, token, cfg)
    else:
        item = _upload_chunked(file_path, token, cfg)

    web_url      = item.get("webUrl", "N/A")
    size_returned = item.get("size", 0)
    logger.info(
        f"Fichier déposé avec succès dans SharePoint. "
        f"Taille confirmée : {size_returned / 1024:.1f} Ko. URL : {web_url}"
    )
