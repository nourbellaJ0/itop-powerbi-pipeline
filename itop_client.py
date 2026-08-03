"""
itop_client.py — Client REST pour l'API iTop.

Ce module gère :
  - L'authentification par token iTop
  - La construction des requêtes OQL (Object Query Language)
  - La récupération paginée des incidents
  - La gestion des erreurs réseau avec retry automatique

╔══════════════════════════════════════════════════════════════════════════╗
║  ZONE D'ADAPTATION — Modifier cette section selon votre instance iTop   ║
║                                                                          ║
║  1. ITOP_OUTPUT_FIELDS : liste des champs techniques iTop à récupérer   ║
║  2. OQL dans get_incidents() : adapter la clause WHERE si nécessaire    ║
║  3. Si votre iTop utilise un header HTTP pour le token, décommenter     ║
║     la ligne "Auth-Token" dans _post()                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

Format standard de l'API iTop REST v1.3 :
  POST {base_url}/webservices/rest.php
  Content-Type: application/x-www-form-urlencoded
  Body: json_data={"version":"1.3","auth_user":"token","auth_token":"<token>",
                   "operation":"core/get","class":"UserRequest","key":"...","output_fields":"..."}
"""

import json
import time
from datetime import datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config
from logger_config import setup_logger

logger = setup_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# ZONE D'ADAPTATION — Champs iTop à extraire
#
# Utiliser les NOMS TECHNIQUES iTop (pas les labels français de l'interface).
# Pour un objet lié, "champ_id_friendlyname" renvoie le nom d'affichage.
# Exemple : "caller_id_friendlyname" = nom complet du demandeur.
#
# COMMENT TROUVER LES NOMS TECHNIQUES :
#   1. Dans iTop, allez dans Admin > Outils > Export de données
#   2. Sélectionnez la classe UserRequest et exportez en CSV
#   3. Les en-têtes de colonnes sont les noms techniques
#   OU
#   4. Utilisez l'API avec output_fields="*" pour tout récupérer une fois
# ──────────────────────────────────────────────────────────────────────────────

ITOP_OUTPUT_FIELDS: str = ",".join([
    # ── Identifiants ──────────────────────────────────────────────────────
    "ref",                            # Référence (ex: R-000123)
    "title",                          # Objet / titre de l'incident
    "description",                    # Description de l'incident
    "finalclass",                     # Sous-classe de ticket
    "status",                         # Statut (new, assigned, resolved…)
    "operational_status",             # Statut opérationnel
    "origin",                         # Origine (portal, phone, email…)
    "priority",                       # Priorité (1=critique … 4=faible)
    "urgency",                        # Urgence
    "impact",                         # Impact

    # ── Dates ─────────────────────────────────────────────────────────────
    "start_date",                     # Date de début
    "assignment_date",                # Date d'assignation
    "resolution_date",                # Date de résolution
    "close_date",                     # Date de fermeture
    "end_date",                       # Date de fin
    "last_update",                    # Dernière mise à jour (utilisé pour le filtre incrémental)
    "last_pending_date",              # Dernière date de suspension

    # ── Organisation/Demandeur ────────────────────────────────────────────
    "org_id",                         # ID organisation du demandeur
    "org_id_friendlyname",            # Nom de l'organisation du demandeur
    "org_name",                       # Nom court de l'organisation

    # ── Demandeur (données partiellement sensibles) ────────────────────────
    "caller_id",                      # ID du demandeur
    "caller_id_friendlyname",         # Nom complet du demandeur  ← MASQUÉ
    "caller_name",                    # Nom du demandeur           ← MASQUÉ
    "caller_firstname",               # Prénom du demandeur        ← MASQUÉ
    "caller_email",                   # Email du demandeur         ← MASQUÉ
    "caller_employee_number",         # N° employé demandeur       ← MASQUÉ
    "caller_org_name",                # Organisation du demandeur (gardé)

    # ── Intervenant/Agent ─────────────────────────────────────────────────
    "agent_id",                       # ID de l'intervenant
    "agent_id_friendlyname",          # Nom complet de l'intervenant
    "agent_name",                     # Nom de l'intervenant
    "agent_email",                    # Email de l'intervenant    ← MASQUÉ
    "agent_employee_number",          # N° employé intervenant    ← MASQUÉ
    "agent_org_name",                 # Organisation de l'intervenant

    # ── Equipe ────────────────────────────────────────────────────────────
    "team_id_friendlyname",           # Nom complet de l'équipe
    "team_name",                      # Nom court de l'équipe
    "team_email",                     # Email de l'équipe          ← MASQUÉ
    "team_org_name",                  # Organisation de l'équipe

    # ── Service ───────────────────────────────────────────────────────────
    "service_id_friendlyname",        # Nom complet du service
    "service_name",                   # Nom du service
    "servicesubcategory_id_friendlyname",  # Nom complet sous-catégorie service
    "servicesubcategory_name",        # Nom de la sous-catégorie de service

    # ── CI (Configuration Item) ───────────────────────────────────────────
    "functionalci_id_friendlyname",   # Nom complet du CI
    "functionalci_name",              # Nom du CI

    # ── Résolution ────────────────────────────────────────────────────────
    "resolution_code",                # Code de résolution
    "root_cause",                     # Cause racine
    "solution",                       # Solution appliquée
    "customer_satisfaction",          # Satisfaction client (si applicable)

    # ── SLA ───────────────────────────────────────────────────────────────
    "tto_deadline",                   # Echéance TTO
    "tto_passed",                     # SLA TTO dépassé ?
    "tto_over",                       # Dépassement SLA TTO
    "ttr_deadline",                   # Echéance TTR
    "ttr_passed",                     # SLA TTR dépassé ?
    "ttr_over",                       # Dépassement SLA TTR

    # ── Suspension ────────────────────────────────────────────────────────
    "total_suspend_time",             # Temps cumulé de suspension

    # ── CHAMPS PERSONNALISÉS iTop ─────────────────────────────────────────
    # Décommenter et adapter selon la configuration réelle de votre iTop.
    # Les noms techniques des champs personnalisés varient selon l'instance.
    # Pour les trouver, utilisez output_fields="*" lors d'un premier appel.
    #
    # "attrib_1re_relance",           # 1ère Relance
    # "attrib_2eme_relance",          # 2ème Relance
    # "attrib_3eme_relance",          # 3ème Relance
    # "attrib_action_prestataire",    # Action à confier au prestataire
    # "attrib_commentaire_client",    # Commentaire client            ← MASQUÉ
    # "attrib_date_prestataire",      # Date Prestataire
    # "attrib_date_rejet",            # Date de Reject
    # "attrib_detection_effective",   # Détection Effective
    # "attrib_mobile_phone",          # Mobile phone                  ← MASQUÉ
    # "attrib_ancien_inventaire",     # Ancien N° d'inventaire / N° de série
    # "attrib_nouveau_inventaire",    # Nouveau N° d'inventaire / N° de série
    # "attrib_raison_rejet",          # Raison de Reject
    # "attrib_raison_surveillance",   # Raison de surveillance
    # "attrib_raison_suspension",     # Raison de suspension
    # "attrib_retablissement",        # Rétablissement Effectif
    # "attrib_ticket_surveiller",     # Ticket à surveiller
    # "attrib_type_ci",               # Type CI
    # "attrib_type_intervention",     # Type intervention
    # "attrib_type_resolution",       # Type Resolution
    # "attrib_cumule_prestataire",    # Cumulé prestataire
    # "attrib_categorie_statut",      # Catégorie statut
    # "attrib_delai_resolution",      # Délai de résolution
    # "parent_change_id_friendlyname",    # Changement parent
    # "parent_incident_id_friendlyname",  # Incident parent
    # "problem_id_friendlyname",          # Problème lié
    # "provider_org_name",                # Prestataire->Nom organisation
    # "org_code",                         # Code organisation demandeur
    # "ci_type_dab",                      # CI->Type DAB
    # "ci_asset_ref",                     # CI->REF Asset
])

# ──────────────────────────────────────────────────────────────────────────────
# Constantes réseau
# ──────────────────────────────────────────────────────────────────────────────

_API_TIMEOUT_SECONDS: int = 30    # Timeout par requête
_MAX_RETRIES: int = 3             # Nombre de tentatives en cas d'erreur transitoire
_RETRY_DELAY_SECONDS: int = 5     # Délai entre deux tentatives
_PAGE_SIZE: int = 500             # Nombre d'objets par page (0 = pas de pagination iTop)


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions personnalisées
# ──────────────────────────────────────────────────────────────────────────────

class ITopAPIError(Exception):
    """Levée quand l'API iTop retourne un code d'erreur ou une réponse invalide."""


class ITopAuthError(ITopAPIError):
    """Levée en cas d'échec d'authentification iTop (code 401 ou code iTop != 0)."""


# ──────────────────────────────────────────────────────────────────────────────
# Client iTop
# ──────────────────────────────────────────────────────────────────────────────

class ITopClient:
    """
    Client pour l'API REST iTop.

    Gère l'authentification par token, la construction des requêtes OQL,
    la pagination et le retry automatique.

    Utilisation :
        client = ITopClient(cfg)
        records = client.get_incidents(since_date=datetime(2024, 1, 1))
    """

    def __init__(self, cfg: Config) -> None:
        self._base_url = cfg.itop_base_url
        self._token = cfg.itop_api_token
        self._api_version = cfg.itop_api_version
        self._itop_class = cfg.itop_class
        self._verify_ssl = cfg.itop_verify_ssl

        # Session requests avec retry automatique sur erreurs réseau
        self._session = self._build_session()

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/webservices/rest.php"

    @staticmethod
    def _build_session() -> requests.Session:
        """Configure une session requests avec retry intégré."""
        session = requests.Session()
        retry_strategy = Retry(
            total=_MAX_RETRIES,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
            backoff_factor=2,       # 2s, 4s, 8s entre tentatives
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _build_json_payload(self, operation: str, extra: dict[str, Any]) -> dict[str, Any]:
        """
        Construit le payload JSON pour l'API iTop.

        L'authentification iTop par token utilise :
          auth_user = "token" (littéral, placeholder iTop)
          auth_token = <valeur du token>

        ADAPTER ZONE : Si votre iTop utilise auth_user/auth_pwd à la place,
        remplacez auth_token par auth_pwd et passez le mot de passe.
        """
        return {
            "version": self._api_version,
            "auth_user": "token",       # Placeholder iTop pour l'auth par token
            "auth_token": self._token,  # Token API iTop (issu de .env)
            "operation": operation,
            **extra,
        }

    def _post(self, json_payload: dict[str, Any]) -> dict[str, Any]:
        """
        Envoie une requête POST à l'API iTop avec gestion des erreurs.

        L'API iTop attend le payload dans un champ form-encoded 'json_data'.

        Args:
            json_payload: Dictionnaire du payload JSON (sans sérialisation).

        Returns:
            Dictionnaire JSON de la réponse iTop.

        Raises:
            ITopAuthError: En cas d'erreur d'authentification.
            ITopAPIError:  En cas d'erreur réseau ou de réponse invalide.
        """
        form_data = {"json_data": json.dumps(json_payload)}

        headers = {
            "Accept": "application/json",
            # ADAPTER ZONE : Si votre iTop utilise un header HTTP pour le token,
            # décommenter la ligne ci-dessous et retirer auth_token du payload.
            # "Auth-Token": self._token,
        }

        try:
            response = self._session.post(
                self._endpoint,
                data=form_data,
                headers=headers,
                verify=self._verify_ssl,
                timeout=_API_TIMEOUT_SECONDS,
            )
        except requests.exceptions.SSLError as exc:
            logger.error(
                "Echec de la vérification SSL. "
                "Vérifiez le certificat iTop et la variable ITOP_VERIFY_SSL."
            )
            raise ITopAPIError("Erreur SSL — connexion sécurisée impossible.") from exc
        except requests.exceptions.Timeout:
            raise ITopAPIError(
                f"L'API iTop n'a pas répondu dans les {_API_TIMEOUT_SECONDS}s."
            )
        except requests.exceptions.ConnectionError as exc:
            raise ITopAPIError(f"Erreur de connexion à iTop : {exc}") from exc

        # Vérification du statut HTTP
        if response.status_code == 401:
            raise ITopAuthError(
                "Authentification refusée (HTTP 401). Vérifiez ITOP_API_TOKEN."
            )
        if response.status_code == 403:
            raise ITopAuthError(
                "Accès refusé (HTTP 403). Le token n'a pas les droits suffisants."
            )

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise ITopAPIError(f"Erreur HTTP {response.status_code} depuis iTop.") from exc

        try:
            return response.json()
        except ValueError as exc:
            raise ITopAPIError(
                f"Réponse iTop non-JSON : {response.text[:200]}"
            ) from exc

    def _parse_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extrait les enregistrements d'une réponse iTop valide.

        Format de réponse iTop :
            {
              "code": 0,
              "objects": {
                "UserRequest::123": {
                  "key": "123",
                  "fields": { "ref": "R-000123", ... }
                }
              }
            }

        Args:
            response: Dictionnaire JSON retourné par iTop.

        Returns:
            Liste de dictionnaires de champs (un par incident).

        Raises:
            ITopAuthError: Code iTop 10 (authentification échouée).
            ITopAPIError:  Tout autre code d'erreur iTop.
        """
        code = response.get("code", -1)
        message = response.get("message", "Aucun message")

        if code == 10:
            raise ITopAuthError(
                f"Authentification iTop refusée : {message}. "
                "Vérifiez ITOP_API_TOKEN."
            )
        if code != 0:
            raise ITopAPIError(f"Erreur API iTop (code={code}) : {message}")

        objects = response.get("objects") or {}
        if not objects:
            return []

        records = []
        for obj_key, obj_data in objects.items():
            if not isinstance(obj_data, dict):
                continue
            fields = obj_data.get("fields", {})
            # Injecter la clef primaire iTop dans les champs
            fields["id"] = obj_data.get("key", "")
            records.append(fields)

        return records

    def get_incidents(
        self,
        since_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Récupère les incidents iTop modifiés depuis ``since_date``.

        Utilise une pagination interne par blocs de _PAGE_SIZE pour éviter
        les timeouts sur les gros volumes de données.

        Args:
            since_date: Date de référence pour le filtre incrémental.
                        Si None, tous les incidents sont retournés
                        (attention : peut être très long sur les grandes instances).

        Returns:
            Liste de dictionnaires de champs iTop (un par incident).

        Raises:
            ITopAPIError: En cas d'erreur API.
        """
        # ── Construction de la requête OQL ────────────────────────────────
        # ADAPTER ZONE : Modifier la clause WHERE selon vos besoins.
        # Exemples :
        #   "WHERE status != 'closed'"              → ignorer les tickets fermés
        #   "WHERE org_id = 12"                     → filtrer par organisation
        #   "WHERE last_update >= '2024-01-01 00:00:00'"  → filtre incrémental
        #
        # Syntaxe OQL iTop : SELECT <Classe> WHERE <condition>
        # Les dates doivent être au format 'YYYY-MM-DD HH:MM:SS'
        # ─────────────────────────────────────────────────────────────────

        if since_date:
            date_str = since_date.strftime("%Y-%m-%d %H:%M:%S")
            oql = (
                f"SELECT {self._itop_class} "
                f"WHERE last_update >= '{date_str}'"
            )
        else:
            oql = f"SELECT {self._itop_class}"

        logger.info(f"Requête OQL iTop : {oql}")

        all_records: list[dict[str, Any]] = []
        limit = _PAGE_SIZE
        offset = 0

        while True:
            json_payload = self._build_json_payload(
                operation="core/get",
                extra={
                    "class": self._itop_class,
                    "key": oql,
                    "output_fields": ITOP_OUTPUT_FIELDS,
                    "limit": limit,
                    "offset": offset,
                },
            )

            logger.debug(f"Requête iTop — offset={offset}, limit={limit}")
            response = self._post(json_payload)
            page_records = self._parse_response(response)

            if not page_records:
                break

            all_records.extend(page_records)
            logger.info(
                f"Page récupérée : {len(page_records)} enregistrement(s) "
                f"(total cumulé : {len(all_records)})"
            )

            # Si la page est incomplète, on a tout récupéré
            if len(page_records) < limit:
                break

            offset += limit
            # Pause courte pour ne pas saturer l'API iTop
            time.sleep(0.5)

        logger.info(f"Total incidents récupérés depuis iTop : {len(all_records)}")
        return all_records
