# Pipeline iTop → Power BI

Pipeline Python qui transforme les incidents iTop en un modèle de données
prêt pour Power BI : nettoyage, masquage des données sensibles, modélisation
en étoile, enrichissement optionnel par IA, contrôle qualité, puis dépôt vers
SharePoint ou SQL pour actualisation automatique dans Power BI.

Deux modes d'entrée :
- **Mode API** (`INPUT_MODE=api`) : extraction automatique depuis l'API REST iTop
- **Mode XLSX** (`INPUT_MODE=xlsx`) : traitement d'un fichier Excel exporté manuellement depuis iTop
- **Mode SharePoint** (`INPUT_MODE=sharepoint`) : téléchargement du fichier source depuis SharePoint, puis ETL, IA et ré-export vers SharePoint

Deux formats de sortie :
- **Modèle en étoile** (défaut) : classeur multi-feuilles `incidents_itop_model.xlsx`
  (`fact_incidents` + dimensions), prêt pour un modèle relationnel Power BI
- **Table plate** (`--legacy-flat`) : fichier unique `incidents_itop_clean.xlsx`,
  comportement historique du pipeline avant l'introduction du modèle en étoile

---

## Vue d'ensemble du flux

```
Source (API iTop ou Excel local)
        │
        ▼
  transform.py         — E1 renommage/mapping · E2 nettoyage/masquage PII · E3 colonnes analytiques
        │
        ▼
  ai_enrichment.py      — (optionnel) classification IA des causes/solutions + synthèse mensuelle
        │
        ▼
  modeling.py           — construction du modèle en étoile (fact + 6 dimensions)
        │
        ▼
  quality_checks.py     — contrôles E6, rapport PASS/WARN/FAIL, JSON horodaté dans logs/
        │
        ▼
  Export Excel (.xlsx) ─┬─▶ sharepoint_loader.py ─▶ SharePoint ─▶ Power BI (connexion fichier)
                        └─▶ sql_loader.py        ─▶ SQL Server / PostgreSQL ─▶ Power BI (connexion SQL)
```

`main.py` orchestre l'ensemble ; chaque étape peut être désactivée ou
contournée via les options de ligne de commande (voir [Exécution](#exécution)).

### Mode API (extraction automatique)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PIPELINE PYTHON                              │
│                                                                      │
│  ┌──────────┐    HTTPS     ┌─────────────┐   ┌──────────────────┐  │
│  │          │  + Token API │             │   │                  │  │
│  │  iTop    │─────────────▶│itop_client  │──▶│   transform.py   │  │
│  │  API     │              │    .py      │   │  (nettoyage +    │  │
│  │          │              └─────────────┘   │   masquage PII)  │  │
│  └──────────┘                                └────────┬─────────┘  │
│                                                       │             │
│                                              ┌────────▼─────────┐  │
│                                              │ ai_enrichment.py │  │
│                                              │  (Groq, optionnel)│  │
│                                              └────────┬─────────┘  │
│                                                       │             │
│                                              ┌────────▼─────────┐  │
│                                              │   modeling.py     │  │
│                                              │ (modèle en étoile)│  │
│                                              └────────┬─────────┘  │
│                                                       │             │
│                                              ┌────────▼─────────┐  │
│                                              │  quality_checks  │  │
│                                              │      .py         │  │
│                                              └────────┬─────────┘  │
│                                                       │             │
│                                              ┌────────▼─────────┐  │
│                                              │    main.py        │  │
│                                              │ (orchestrateur)   │  │
│                                              └───┬───────────┬───┘  │
└──────────────────────────────────────────────────┼───────────┼──────┘
                                                   │           │
                    ┌──────────────────────────────▼┐         │
                    │      sharepoint_loader.py      │         │
                    │   Graph API → SharePoint       │         │
                    │   incidents_itop_model.xlsx    │         │
                    └──────────────────┬─────────────┘         │
                                       │             ┌──────────▼──────────┐
                                       │             │    sql_loader.py     │
                                       │             │ SQLAlchemy → SQL DB  │
                                       │             │ fact + dimensions    │
                                       │             └──────────┬───────────┘
                                       │                        │
                    ┌──────────────────▼────────────────────────▼───────────┐
                    │                    Power BI                             │
                    │   Connexion fichier SharePoint  OU  Connexion SQL       │
                    │           Actualisation automatique planifiée           │
                    └─────────────────────────────────────────────────────────┘
```

### Mode XLSX (fichier Excel local)

```
Export iTop (.xlsx local) → xlsx_loader.py → transform.py (FIELD_MAP_XLSX)
    → ai_enrichment.py (optionnel) → modeling.py → quality_checks.py
    → main.py → sharepoint_loader.py / sql_loader.py → Power BI
```

> En mode XLSX, aucun appel réseau vers iTop n'est effectué.
> `last_run.json` est ignoré. Le script traite intégralement le fichier fourni.

### Mode SharePoint (`INPUT_MODE=sharepoint`)

Télécharge le fichier source Excel depuis le dossier SharePoint configuré,
puis applique exactement le même ETL, le même enrichissement IA et le même
export final vers SharePoint.

Ce mode est utile quand le classeur d'incidents est déjà déposé dans le même
dossier SharePoint que le modèle final.

#### Configuration .env pour le mode SharePoint

```ini
INPUT_MODE=sharepoint
OUTPUT_MODE=sharepoint
SHAREPOINT_SOURCE_FILE_NAME=incidents_itop_clean.xlsx
SHAREPOINT_FOLDER_PATH=Rapports/iTop
```

---

## Prérequis

| Composant | Version minimale |
|-----------|-----------------|
| Python | 3.10+ |
| pip | 23+ |
| iTop | 2.5+ (avec API REST et token activé, si mode API) |
| Azure AD App | requis si `OUTPUT_MODE=sharepoint` |
| ODBC Driver | 17+ for SQL Server, requis si `OUTPUT_MODE=sql` avec `mssql+pyodbc` |
| Clé API Groq | requise uniquement si `AI_ENRICHMENT_ENABLED=True` |

---

## Installation

### 1. Copier le projet

```bash
cd C:\Scripts\itop_powerbi_pipeline
```

### 2. Créer un environnement virtuel (recommandé)

```bash
python -m venv .venv

# Activation Windows
.venv\Scripts\activate

# Activation Linux/Mac
source .venv/bin/activate
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt

# Pour SQL Server (Windows) :
pip install pyodbc

# Pour PostgreSQL :
pip install psycopg2-binary
```

### 4. Configurer les variables d'environnement

```bash
copy .env.example .env        # Windows
cp .env.example .env          # Linux

notepad .env                  # Windows
nano .env                     # Linux
```

---

## Modes d'entrée

### Mode API (`INPUT_MODE=api`)

Interroge l'API REST iTop (`itop_client.py`) via une requête OQL, avec
extraction incrémentale : seuls les incidents modifiés depuis la dernière
exécution réussie (`last_run.json`) sont récupérés, sauf `--full-refresh`.
Pagination automatique par blocs de 500 objets.

### Mode XLSX (`INPUT_MODE=xlsx`)

Lit un fichier Excel déjà exporté manuellement depuis l'interface iTop
(`xlsx_loader.py`), sans connexion réseau. Utile quand :
- l'API iTop n'est pas accessible depuis la machine d'exécution
- vous avez déjà un export récent à nettoyer et déposer
- vous testez le pipeline sans configuration réseau

`last_run.json` est ignoré dans ce mode : chaque exécution retraite
intégralement le fichier fourni. Le fichier source n'est jamais modifié
(lecture seule).

#### Configuration .env pour le mode XLSX

```ini
INPUT_MODE=xlsx
LOCAL_XLSX_PATH=./data/Export de Incidents -Maintenance & supports (45).xlsx
LOCAL_XLSX_SHEET_NAME=Sheet1

OUTPUT_MODE=sharepoint
SHAREPOINT_TENANT_ID=...
SHAREPOINT_CLIENT_ID=...
SHAREPOINT_CLIENT_SECRET=...
SHAREPOINT_DRIVE_ID=...
SHAREPOINT_FOLDER_PATH=Rapports/iTop
```

#### Adapter le mapping si les noms de colonnes diffèrent

Si votre export iTop utilise des noms de colonnes légèrement différents,
modifier `FIELD_MAP_XLSX` dans [transform.py](transform.py) (seules les
colonnes présentes dans ce dictionnaire sont conservées ; les autres sont
ignorées silencieusement — un log DEBUG liste les colonnes ignorées).

---

## Traitement des données (transform.py)

Le module applique le même pipeline E1 → E2 → E3 aux deux modes d'entrée,
après renommage via `FIELD_MAP_API` ou `FIELD_MAP_XLSX` :

- **E1 — Sélection et renommage** : seules les colonnes cartographiées sont
  conservées, renommées en snake_case anglais commun aux deux modes.
- **E2 — Nettoyage** : suppression des colonnes PII (`SENSITIVE_COLUMNS_TO_DROP`),
  conversion des dates, trim/normalisation du texte, remplacement des
  marqueurs vides (`.`, `-`, `N/A`, `RAS`…), normalisation statut/priorité/
  urgence/site, gestion de `agent_name` (clair ou hash selon
  `INCLUDE_AGENT_NAME`), dédoublonnage sur `reference` (conserve la ligne
  avec `last_update` la plus récente).
- **E3 — Colonnes analytiques** : `nature` (Fonctionnel/Technique),
  `tto_real_min`, `ttr_real_hours`, `ttr_bucket`, `is_open`/`is_resolved`/
  `is_rejected`/`is_critical`, SLA recalculés (`sla_tto_breached_calc`,
  `sla_ttr_breached_calc`, `is_out_of_target` par comparaison à
  `SLA_TARGET_HOURS`), `root_cause_category` (classification par patterns
  regex, `ROOT_CAUSE_PATTERNS` dans transform.py), indicateurs de
  complétude (`has_root_cause`, `has_ci`, `has_parent_change`),
  `created_month`, `created_date`/`resolved_date`, `days_since_last_update`.

### Masquage des données sensibles (PII)

Quel que soit `INCLUDE_AGENT_NAME`, les colonnes suivantes sont **toujours
supprimées** après renommage : email et nom complet du demandeur, numéro
d'employé, téléphone mobile, commentaire client, email de l'intervenant/
équipe/prestataire. Un filet de sécurité regex (`_PII_PATTERN`) émet un
WARNING si une colonne au nom suspect subsiste malgré tout.

| `INCLUDE_AGENT_NAME` | Comportement |
|---|---|
| `True` (défaut) | `agent_name` conservé en clair, vide → `"Non assigné"` — utile pour la page Power BI *"Tickets ouverts : qui les détient"* |
| `False` | `agent_name` remplacé par un hash SHA-256 de 8 caractères, salé par `PII_SALT` (doit rester stable entre exécutions pour que les hashes soient comparables) |

---

## Modélisation en étoile (modeling.py)

Par défaut (sans `--legacy-flat`), le pipeline transforme la table de faits
nettoyée en un **modèle en étoile** à 7 tables, exporté en classeur Excel
multi-feuilles ou en tables SQL séparées :

| Table | Contenu |
|---|---|
| `fact_incidents` | Mesures, dates, clés étrangères (`*_key`) et colonnes analytiques |
| `dim_service` | Service (nom, nature) |
| `dim_subcategory` | Sous-catégorie de service, `is_generic` (Ticket à chaud), `team_manager` (responsable, via `SUBCATEGORY_MANAGERS` dans config.py) |
| `dim_team` | Équipe |
| `dim_ci` | Configuration Item (nom, classe) |
| `dim_priority` | Priorité, avec cible SLA associée (`SLA_TARGET_HOURS`) |
| `dim_date` | Calendrier continu du premier incident à aujourd'hui (mois/trimestre/jour de semaine en français) |
| `ai_insights` | (si IA activée) historique des synthèses exécutives mensuelles — table en mode append |

Convention d'intégrité référentielle : chaque dimension reçoit une ligne
clé `0` / libellé `"Non renseigné"` pour les valeurs manquantes ou inconnues,
garantissant qu'aucune clé étrangère de `fact_incidents` n'est orpheline
(vérifié par `check_referential_integrity()`, contrôlé en E6.8).

Pour revenir à l'ancien comportement (table plate unique, sans
modélisation), utiliser `--legacy-flat` — voir [Exécution](#exécution).

---

## Enrichissement IA — Groq (ai_enrichment.py, optionnel)

Module entièrement optionnel et désactivé par défaut
(`AI_ENRICHMENT_ENABLED=False`) : s'il est désactivé ou si `GROQ_API_KEY`
est vide, aucun appel réseau n'est effectué et le pipeline se comporte
exactement comme sans ce module (colonnes IA à `NaN`).

- **UC1 — Classification des causes/solutions** : complète la
  classification par regex (`root_cause_category`) avec une classification
  par LLM (`GROQ_MODEL`, ex. `llama-3.1-8b-instant`), appelée en lots de
  `AI_BATCH_SIZE` tickets (défaut 10) sur l'ensemble des tickets éligibles
  (ceux ayant `root_cause_raw` ou `solution` renseigné). Résultat :
  `ai_cause_category`, `ai_cause_confidence`, `ai_summary`, et
  `root_cause_category_final` (= catégorie IA si confiance ≥
  `AI_CONFIDENCE_THRESHOLD`, sinon repli sur la catégorie regex).
- **UC4 — Synthèse exécutive mensuelle** : un seul appel par mois à un
  modèle plus qualitatif (`GROQ_MODEL_SUMMARY`, ex. `openai/gpt-oss-120b`),
  à partir d'agrégats chiffrés du dernier mois complet (volume, variation,
  répartition, top causes, backlog…) — **jamais de données ligne par
  ligne**. Le texte généré est ajouté à la feuille/table `ai_insights`
  (historisée, append).

**Rédaction obligatoire avant tout envoi à l'API** (`redact()`) : emails,
références internes (MAT/FT…), RIB/IBAN, téléphones et numéros de compte
sont masqués par des placeholders dans tout texte envoyé à Groq. Les champs
`reference`, `agent_name`, `requester_org` ne sont jamais transmis.

**Cache** : les classifications sont mises en cache (`cache/ai_classifications.json`).
En flux normal, une référence déjà présente dans le cache n'est pas
reclassée : seules les nouvelles références partent à l'IA.

**Robustesse** : retry exponentiel sur 429/5xx, respect du `Retry-After`,
rate limiting côté client sur requêtes/minute (`GROQ_MAX_REQ_PER_MIN`) et
sur tokens/minute (`GROQ_MAX_TOKENS_PER_MIN` — souvent la contrainte
réellement bloquante). Un échec durable de l'API ne bloque jamais le
pipeline : les tickets concernés sont marqués `"Indéterminé"`.

### Commandes liées à l'IA

```bash
# Forcer l'enrichissement IA même si AI_ENRICHMENT_ENABLED=False
python main.py --ai

# Tester l'IA sur les 20 premiers tickets éligibles seulement,
# affiche les classifications et une estimation de coût,
# sans toucher au cache définitif ni au reste du pipeline
python main.py --ai-dry-run
```

---

## Classification "Support Monétique" (ai_enrichment_monetique.py, optionnel)

Traitement indépendant de UC1 (flag `MONETIQUE_AI_ENABLED`, désactivé par
défaut) : classe uniquement les tickets où `service_subcategory == "Support
Monétique"`, à partir du **titre** (`ticket_title`) et de la **description**
(`ticket_description`) du ticket — pas de `root_cause_raw`/`solution`.

Référentiel métier figé (`MONETIQUE_MACRO_CATEGORIES`, `MONETIQUE_THEMES`
dans `ai_enrichment_monetique.py`) : 3 macro-catégories (chemins iTop
d'origine) et 17 thèmes fins avec responsabilité associée (`Métier`,
`BIAT-IT`, `Les deux`, ou `À qualifier` pour le repli `"Autre / à
qualifier"`). Seul le thème est réellement choisi par le LLM ; la
macro-catégorie, la responsabilité et les actions métier/IT sont **toujours**
dérivées du référentiel par lookup — un thème halluciné (hors référentiel)
retombe automatiquement sur `"Autre / à qualifier"` avec confiance 0.

Résultat : `monetique_macro_category`, `monetique_theme`,
`monetique_responsabilite`, `monetique_action_metier`,
`monetique_action_it`, `monetique_ai_confidence`. Colonnes à `NaN` hors
périmètre Monétique, ou si le module est désactivé/sans clé.

**Cache incrémental** : namespace dédié (`"__monetique__"`) dans le même
fichier `cache/ai_classifications.json` que UC1, indexé par référence (pas
par référence+hash comme UC1) pour pouvoir détecter le cas "texte vidé
depuis". Un `content_hash` (titre + description normalisés) est stocké par
entrée : hash inchangé → réutilisation sans appel API ; hash différent →
reclassification ; titre et description devenus vides après une
classification précédente → colonnes remises à `NaN`, **aucun appel API**,
entrée cache **conservée** (audit) et `WARNING` loggé.

```bash
# Tester la classification Monétique sur les 20 premiers tickets éligibles,
# sans toucher au cache définitif ni au reste du pipeline
python main.py --ai-monetique-dry-run
```

---

## Contrôle qualité (quality_checks.py)

Exécuté avant tout export. Génère un rapport texte (logs) et un rapport
JSON horodaté dans `logs/quality_report_YYYYMMDD.json`. Trois niveaux :
**PASS** (tout est correct), **WARN** (anomalie non bloquante), **FAIL**
(erreur critique — le dépôt est annulé, le pipeline retourne le code 1).

| Contrôle | Niveau | Condition |
|---|---|---|
| Colonnes obligatoires | FAIL | `reference`, `status`, `created_at`, `last_update` absentes |
| Références vides | FAIL | `reference` vide sur au moins une ligne |
| Doublons résiduels | FAIL | doublon sur `reference` après dédoublonnage |
| Logique des dates | FAIL | `resolved_at` < `created_at` |
| Validité dates critiques | FAIL / WARN | % de `created_at`/`last_update` manquants au-delà de 5 % (FAIL) ou en dessous (WARN) |
| Valeurs de statut | WARN | statut hors de `VALID_STATUSES` |
| Complétude cause racine | WARN | `root_cause_raw` renseigné < 10 % |
| Complétude CI | WARN | `ci_name`/`has_ci` renseigné < 20 % |
| SLA discriminant | WARN | `sla_tto_breached_itop`/`sla_ttr_breached_itop` à modalité unique |
| Tickets résolus non fermés | WARN | ticket `Résolue` sans fermeture depuis > 30 jours |
| Pollution délai résolution | WARN | `itop_resolution_delay_min` > 1 an sur > 1 % des lignes |
| Réconciliation et FK | FAIL | `fact_incidents` ≠ nombre de lignes source après dédoublonnage, ou clé étrangère orpheline |
| Classification IA | WARN | > 20 % de réponses IA `"Indéterminé"` (signal que le prompt/modèle est à revoir) |
| Classification Monétique | WARN | > 15 % des tickets Monétique classifiés en `"Autre / à qualifier"` (signal que le référentiel/prompt est à revoir) |

Les seuils (`CRITICAL_ERROR_THRESHOLD`, `WARN_ROOT_CAUSE_MIN_PCT`, etc.)
sont ajustables en tête de `quality_checks.py`.

---

## Variables de configuration (.env)

### Mode d'entrée

| Variable | Description | Valeurs |
|----------|-------------|---------|
| `INPUT_MODE` | Source des données | `api` (défaut), `xlsx` ou `sharepoint` |
| `LOCAL_XLSX_PATH` | Chemin du fichier Excel **(obligatoire si xlsx)** | ex: `./data/export.xlsx` |
| `LOCAL_XLSX_SHEET_NAME` | Nom de la feuille | vide = première feuille |

### Connexion iTop (mode API)

| Variable | Description | Exemple |
|----------|-------------|---------|
| `ITOP_BASE_URL` | URL de base iTop **(obligatoire si api)** | `https://itop.mabanque.local` |
| `ITOP_API_TOKEN` | Token API iTop **(obligatoire si api)** | `abc123xyz...` |
| `ITOP_API_VERSION` | Version API REST | `1.3` |
| `ITOP_CLASS` | Classe iTop à interroger | `UserRequest` |
| `ITOP_VERIFY_SSL` | Vérification SSL (`True` en prod) | `True` |

#### Comment obtenir un token iTop
1. Se connecter à iTop en tant qu'administrateur
2. Menu **Admin > Gestion des tokens REST** (ou **Admin > Tokens** selon version)
3. Cliquer **Nouveau token**
4. Donner un nom descriptif : `pipeline_powerbi`
5. Choisir le profil de droits minimum : accès lecture seule sur `UserRequest`
6. Copier le token dans `ITOP_API_TOKEN`

### Pipeline

| Variable | Description | Valeurs |
|----------|-------------|---------|
| `OUTPUT_MODE` | Destination des données | `sharepoint` ou `sql` |
| `LAST_RUN_FILE` | Fichier d'état incrémental (mode API) | `last_run.json` |
| `EXPORT_FILE` | Fichier plat legacy (`--legacy-flat`) | `incidents_itop_clean.xlsx` |
| `MODEL_EXPORT_FILE` | Classeur multi-feuilles modèle en étoile (défaut) | `incidents_itop_model.xlsx` |
| `LOG_LEVEL` | Verbosité des logs | `INFO`, `DEBUG`, `WARNING` |

### Sécurité / PII

| Variable | Description | Valeurs |
|----------|-------------|---------|
| `INCLUDE_AGENT_NAME` | Conserver le nom de l'intervenant en clair | `True` (défaut) ou `False` |
| `PII_SALT` | Sel SHA-256 (actif uniquement si `INCLUDE_AGENT_NAME=False`) | Chaîne aléatoire ≥ 32 chars |

### SharePoint (si OUTPUT_MODE=sharepoint)

| Variable | Description |
|----------|-------------|
| `SHAREPOINT_TENANT_ID` | ID du tenant Azure AD |
| `SHAREPOINT_CLIENT_ID` | Client ID de l'app Azure AD |
| `SHAREPOINT_CLIENT_SECRET` | Secret de l'app Azure AD |
| `SHAREPOINT_SITE_ID` | ID du site SharePoint |
| `SHAREPOINT_DRIVE_ID` | ID de la bibliothèque de documents |
| `SHAREPOINT_FOLDER_PATH` | Chemin du dossier cible |

#### Configuration Azure AD pour SharePoint
1. Aller dans **Azure Portal > Azure Active Directory > App registrations**
2. Cliquer **Nouvelle inscription**
3. Nom : `itop-powerbi-pipeline`
4. Type de compte : **Comptes de cet annuaire d'organisation uniquement**
5. Aller dans **Autorisations d'API > Ajouter une autorisation**
6. Sélectionner **Microsoft Graph > Autorisations de l'application**
7. Ajouter : `Files.ReadWrite.All`
8. Cliquer **Accorder le consentement administrateur**
9. Aller dans **Certificats et secrets > Nouveau secret client**
10. Copier la valeur dans `SHAREPOINT_CLIENT_SECRET`

#### Trouver le Site ID et Drive ID
```
GET https://graph.microsoft.com/v1.0/sites/mabanque.sharepoint.com:/sites/DSI
GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives
```
Utiliser l'[Explorateur Microsoft Graph](https://developer.microsoft.com/graph/graph-explorer) pour tester ces requêtes.

L'upload bascule automatiquement entre PUT simple (≤ 4 Mo) et upload
fragmenté par sessions (> 4 Mo, requis pour le classeur multi-feuilles).

### SQL (si OUTPUT_MODE=sql)

| Variable | Description | Exemple |
|----------|-------------|---------|
| `SQL_DIALECT` | Driver SQLAlchemy | `mssql+pyodbc`, `postgresql+psycopg2` ou `sqlite` (tests) |
| `SQL_HOST` | Hôte du serveur | `sql-server.mabanque.local` |
| `SQL_PORT` | Port TCP | `1433` |
| `SQL_DATABASE` | Nom de la base | `DataWarehouse` |
| `SQL_USER` | Utilisateur SQL | `itop_pipeline_user` |
| `SQL_PASSWORD` | Mot de passe | `VotreMotDePasseIci` |
| `SQL_TABLE` | Table cible (mode `--legacy-flat` uniquement) | `incidents_itop_clean` |

En mode modèle en étoile, chaque table (`fact_incidents`, `dim_*`,
`ai_insights`) est chargée dans sa propre table SQL du même nom, avec un
index sur sa clé. Stratégie **REPLACE** (DROP + CREATE + INSERT) à chaque
run pour toutes les tables sauf `ai_insights`, qui est en **append**
(historique conservé).

### Enrichissement IA — Groq (optionnel)

| Variable | Description | Défaut |
|----------|-------------|--------|
| `AI_ENRICHMENT_ENABLED` | Active la classification (UC1) et la synthèse mensuelle (UC4) | `False` |
| `GROQ_API_KEY` | Clé API Groq (jamais loggée) | — |
| `GROQ_MODEL` | Modèle pour UC1 (classification, lots de 10) | `llama-3.1-8b-instant` |
| `GROQ_MODEL_SUMMARY` | Modèle pour UC4 (synthèse mensuelle, 1 appel/mois) | `openai/gpt-oss-120b` |
| `GROQ_BASE_URL` | URL de base, compatible OpenAI | `https://api.groq.com/openai/v1` |
| `GROQ_MAX_REQ_PER_MIN` | Limite requêtes/minute | `30` |
| `GROQ_MAX_TOKENS_PER_MIN` | Limite tokens/minute (souvent la contrainte réelle) | `6000` |
| `AI_CONFIDENCE_THRESHOLD` | Seuil pour retenir la catégorie IA plutôt que la regex | `0.6` |
| `AI_BATCH_SIZE` | Tickets regroupés par appel (UC1) | `10` |

### Classification Monétique — Groq (optionnel, indépendant)

| Variable | Description | Défaut |
|----------|-------------|--------|
| `MONETIQUE_AI_ENABLED` | Active la classification "Support Monétique" (indépendant de `AI_ENRICHMENT_ENABLED`) | `False` |
| `MONETIQUE_CONFIDENCE_THRESHOLD` | Seuil de reporting uniquement (pas de repli regex) | `0.6` |

---

## Exécution

### Exécution simple (utilise les valeurs du .env)
```bash
python main.py
```

### Options disponibles
```bash
# ── Mode d'entrée ──────────────────────────────────────────────────────────
python main.py --input api                    # Forcer le mode API iTop
python main.py --input xlsx                   # Forcer le mode fichier Excel local
python main.py --input sharepoint             # Télécharger la source depuis SharePoint

# ── Mode de sortie ─────────────────────────────────────────────────────────
python main.py --output sharepoint
python main.py --output sql

# ── Format de sortie ───────────────────────────────────────────────────────
python main.py                                 # Modèle en étoile (défaut) → incidents_itop_model.xlsx
python main.py --legacy-flat                   # Table plate → incidents_itop_clean.xlsx

# ── Options spécifiques mode API ───────────────────────────────────────────
python main.py --input api --full-refresh      # Ignore last_run.json, récupère tout

# ── Enrichissement IA ──────────────────────────────────────────────────────
python main.py --ai                            # Force l'IA même si désactivée dans .env
python main.py --ai-dry-run                    # Teste l'IA sur 20 tickets, sans toucher au cache
python main.py --ai-monetique-dry-run          # Teste la classification Monétique sur 20 tickets
python main.py --skip-ai                       # Ignore UC1 + Monétique, mais garde l'export et le dépôt

# ── Test (tous modes) ──────────────────────────────────────────────────────
python main.py --dry-run                       # Génère le fichier Excel localement, sans dépôt

# ── Combinaisons typiques ──────────────────────────────────────────────────
python main.py --input xlsx --output sharepoint
python main.py --input xlsx --output sharepoint --skip-ai
python main.py --input sharepoint --output sharepoint
python main.py --input xlsx --dry-run
python main.py --input api --full-refresh --dry-run
python main.py --legacy-flat --dry-run
```

Si vous voulez envoyer le classeur vers SharePoint sans relancer les enrichissements IA, utilisez `--skip-ai` avec `--output sharepoint`. `--dry-run` garde le dépôt désactivé par conception.

### Codes de sortie

`main.py` retourne `0` en cas de succès (y compris quand la source ne
contient aucun incident) et `1` en cas d'échec (erreur de configuration,
erreur API/SharePoint/SQL, ou contrôle qualité en `FAIL`) — utile pour
détecter un échec depuis le planificateur de tâches ou cron.

---

## Tests

Suite de tests unitaires (`tests/`) exécutée avec pytest :

```bash
python -m pytest tests/
```

| Fichier | Couvre |
|---|---|
| `test_transform.py` | Renommage, nettoyage, masquage PII, colonnes analytiques (E1-E3) |
| `test_modeling.py` | Construction du modèle en étoile, intégrité référentielle des clés |
| `test_quality.py` | Contrôles qualité (PASS/WARN/FAIL) sur des DataFrames de test |
| `test_ai_enrichment.py` | Rédaction PII, cache, consolidation IA/regex, agrégats UC4 (sans appel réseau réel) |
| `test_ai_enrichment_monetique.py` | Référentiel, cache incrémental (4 cas), repli sur hallucination, filtre de sous-catégorie (sans appel réseau réel) |

---

## Planification automatique

### Windows — Task Scheduler

1. Ouvrir **Planificateur de tâches** (taskschd.msc)
2. Cliquer **Créer une tâche de base**
3. Nom : `Pipeline iTop Power BI`
4. Déclencheur : **Quotidien** à **06:00** (ou selon votre besoin)
5. Action : **Démarrer un programme**
   - Programme : `C:\Scripts\itop_powerbi_pipeline\.venv\Scripts\python.exe`
   - Arguments : `main.py`
   - Démarrer dans : `C:\Scripts\itop_powerbi_pipeline`
6. Options : Exécuter même si l'utilisateur n'est pas connecté
7. Utiliser un **compte de service dédié** (sans droits admin)

### Linux — Cron

```bash
crontab -e

# Exécution du lundi au vendredi à 6h00
0 6 * * 1-5 /opt/itop_pipeline/.venv/bin/python /opt/itop_pipeline/main.py >> /var/log/itop_pipeline_cron.log 2>&1

# Exécution toutes les heures
0 * * * * /opt/itop_pipeline/.venv/bin/python /opt/itop_pipeline/main.py
```

---

## Connexion Power BI

### Mode SharePoint (classeur multi-feuilles)

1. Dans Power BI Desktop : **Obtenir les données > SharePoint Online (fichier)**
2. URL du site SharePoint : `https://mabanque.sharepoint.com/sites/DSI`
3. Naviguer jusqu'au fichier `incidents_itop_model.xlsx`
4. Sélectionner les feuilles `fact_incidents` et les `dim_*` nécessaires
5. Dans Power BI, créer les relations entre `fact_incidents.*_key` et la
   clé de chaque dimension (`service_key`, `subcategory_key`, `team_key`,
   `ci_key`, `priority_key`, `created_date`↔`dim_date.date`)
6. **Actualisation planifiée** : publier le rapport dans Power BI Service,
   puis configurer une actualisation quotidienne dans les paramètres du
   jeu de données.

### Mode SQL

1. Dans Power BI Desktop : **Obtenir les données > SQL Server** (ou PostgreSQL)
2. Serveur : `sql-server.mabanque.local`
3. Base de données : `DataWarehouse`
4. Mode de connectivité : **Import** (recommandé) ou DirectQuery
5. Sélectionner `fact_incidents` et les tables `dim_*`/`ai_insights` nécessaires
6. **Actualisation planifiée** : configurer dans Power BI Service après publication.

> **Conseil** : En mode Import, Power BI fait une copie des données.
> L'actualisation planifiée suffit (quotidienne ou horaire). En mode
> DirectQuery, chaque interaction Power BI interroge directement la base
> SQL — à utiliser uniquement si la fraîcheur en temps réel est critique.

---

## Bonnes pratiques de sécurité

### Secrets et credentials
- Ne **jamais** écrire un token, une clé API ou un mot de passe dans le code source
- Utiliser un fichier `.env` local (jamais commité dans Git)
- En production : utiliser Azure Key Vault, AWS Secrets Manager ou les variables d'environnement système
- Ajouter `.env`, `last_run.json` et `cache/` à `.gitignore`
- Le token iTop doit avoir des **droits lecture seule** sur les incidents uniquement
- Le compte SQL doit avoir uniquement les droits `SELECT`, `INSERT`, `CREATE TABLE`, `DROP TABLE` sur la base cible
- La clé Groq n'est jamais loggée ; les prompts/réponses complets envoyés à l'API ne sont pas loggés non plus

### Réseau et SSL
- `ITOP_VERIFY_SSL=True` est **obligatoire** en production
- Vérifier que le certificat iTop est valide et à jour
- Utiliser HTTPS uniquement (pas HTTP)

### Logs
- Les logs ne contiennent **jamais** de tokens, mots de passe ou données personnelles
- Les fichiers de log sont stockés dans `logs/` avec rotation automatique (10 Mo, 5 archives)
- Vérifier les permissions du dossier `logs/` (lecture/écriture par le compte de service uniquement)

### Rotation du token iTop
En cas de compromission suspectée ou rotation préventive :
1. Générer un nouveau token dans iTop Admin
2. Mettre à jour `ITOP_API_TOKEN` dans `.env` (ou dans le gestionnaire de secrets)
3. Révoquer l'ancien token dans iTop

---

## Structure du projet

```
itop_powerbi_pipeline/
│
├── main.py                 # Orchestrateur principal — point d'entrée
├── config.py                # Chargement/validation config + constantes métier (SLA, responsables)
├── itop_client.py            # Client REST iTop (mode API uniquement)
├── xlsx_loader.py            # Lecture fichier Excel local (mode XLSX uniquement)
├── transform.py              # E1 renommage · E2 nettoyage/masquage PII · E3 colonnes analytiques
├── ai_enrichment.py           # Enrichissement IA optionnel (Groq) — UC1 classification, UC4 synthèse
├── ai_enrichment_monetique.py # Classification "Support Monétique" optionnelle (Groq), indépendante de UC1
├── modeling.py                # Construction du modèle en étoile (fact + dimensions)
├── quality_checks.py          # Contrôles qualité E6
├── sharepoint_loader.py        # Dépôt vers SharePoint via Graph API (upload simple ou fragmenté)
├── sql_loader.py                # Insertion dans SQL via SQLAlchemy (table plate ou modèle en étoile)
├── logger_config.py             # Configuration centralisée des logs
│
├── requirements.txt            # Dépendances Python
├── .env.example                 # Modèle de configuration (pas de secrets)
├── .env                          # Configuration réelle (à créer, jamais commiter)
│
├── last_run.json                 # Etat de la dernière extraction API (auto-généré, mode API)
├── incidents_itop_model.xlsx      # Classeur multi-feuilles modèle en étoile (auto-généré, défaut)
├── incidents_itop_clean.xlsx       # Fichier plat legacy (auto-généré, --legacy-flat)
│
├── cache/
│   └── ai_classifications.json     # Cache des classifications IA (UC1 + namespace "__monetique__")
│
├── tests/
│   ├── test_transform.py
│   ├── test_modeling.py
│   ├── test_quality.py
│   ├── test_ai_enrichment.py
│   └── test_ai_enrichment_monetique.py
│
└── logs/
    ├── itop_pipeline.log           # Logs applicatifs avec rotation automatique
    └── quality_report_YYYYMMDD.json  # Rapport qualité horodaté (un par jour d'exécution)

data/                             # (optionnel) Dossier pour les fichiers Excel sources
└── Export de Incidents (45).xlsx
```

---

## Points à adapter selon votre iTop

### 1. Noms techniques des champs (mode API)

Dans `itop_client.py`, la liste `ITOP_OUTPUT_FIELDS` contient les noms
**techniques** des champs iTop. Ces noms peuvent varier selon votre configuration.

**Pour les trouver :**
```
# Modifier temporairement dans itop_client.py :
ITOP_OUTPUT_FIELDS = "*"
# Lancer avec --dry-run pour voir toutes les colonnes disponibles
python main.py --dry-run
```

### 2. Champs personnalisés

Les champs sur mesure ajoutés à votre iTop ont des noms techniques
spécifiques (ex: `attrib_xxx`). Ils sont commentés dans :
- `itop_client.py` → `ITOP_OUTPUT_FIELDS` (décommenter pour activer)
- `transform.py` → `FIELD_MAP_API` (décommenter et adapter le nom technique)

### 3. Classe iTop

```ini
ITOP_CLASS=Incident       # si vos incidents utilisent une classe différente
ITOP_CLASS=UserRequest    # standard iTop
```

### 4. Requête OQL

La requête OQL est dans `itop_client.py`, méthode `get_incidents()`.
Modifier la clause `WHERE` pour filtrer différemment (organisation, priorité, etc.)

### 5. Statuts iTop

Dans `quality_checks.py`, mettre à jour `VALID_STATUSES` avec les statuts
configurés dans votre instance iTop.

### 6. Cibles SLA et responsables de sous-catégorie

Dans `config.py` :
```python
SLA_TARGET_HOURS: dict[str, int] = {
    "critique": 4, "haute": 8, "moyenne": 24, "basse": 72,
}
SUBCATEGORY_MANAGERS: dict[str, str] = {
    "Support Monétique": "Sana Sellami",
    "Nouvelle sous-catégorie": "Prénom Nom",
    ...
}
```
La correspondance de `SUBCATEGORY_MANAGERS` est insensible à la casse, aux
espaces multiples et aux apostrophes typographiques. Toute sous-catégorie
absente du dictionnaire reçoit `"Non défini"` dans `dim_subcategory` — un
WARNING est loggé si une clé du dictionnaire ne correspond à aucune donnée.

### 7. Catégories de causes racines

Dans `transform.py`, `ROOT_CAUSE_PATTERNS` contient les patterns regex de
classification automatique. Modifier ce dictionnaire pour ajouter/affiner
des catégories métier (ces mêmes catégories sont utilisées comme liste
fermée par la classification IA en UC1).

---

## Logs et monitoring

Les logs sont écrits dans `logs/itop_pipeline.log`.

```bash
# Suivre les logs en temps réel (Linux)
tail -f logs/itop_pipeline.log

# Chercher les erreurs
grep "ERROR\|CRITICAL\|FAIL" logs/itop_pipeline.log

# Voir la dernière exécution
grep "DEBUT DU PIPELINE\|TERMINE AVEC SUCCES\|FAIL" logs/itop_pipeline.log | tail -20
```

Exemple de sortie nominale (modèle en étoile, IA désactivée) :
```
2026-07-20 06:00:01 | INFO     | main | DEBUT DU PIPELINE iTop → Power BI
2026-07-20 06:00:01 | INFO     | main | Mode d'entrée  : API
2026-07-20 06:00:01 | INFO     | main | Mode de sortie : SHAREPOINT
2026-07-20 06:00:01 | INFO     | main | Format sortie  : MODELE EN ETOILE
2026-07-20 06:00:02 | INFO     | itop_client | Requête OQL iTop : SELECT UserRequest WHERE last_update >= '2026-07-19 06:00:01'
2026-07-20 06:00:04 | INFO     | itop_client | Total incidents récupérés depuis iTop : 47
2026-07-20 06:00:04 | INFO     | transform | [MODE API] Transformation terminée : 47 lignes × 44 colonnes.
2026-07-20 06:00:04 | INFO     | ai_enrichment | Enrichissement IA désactivé (AI_ENRICHMENT_ENABLED=False) — colonnes IA à NaN.
2026-07-20 06:00:04 | INFO     | modeling | Construction du modèle en étoile…
2026-07-20 06:00:04 | INFO     | main | Résultat global : PASS ✓
2026-07-20 06:00:05 | INFO     | main | Export modèle en étoile : 8 feuilles → 'incidents_itop_model.xlsx' (1.42 Mo)
2026-07-20 06:00:06 | INFO     | sharepoint_loader | Fichier déposé avec succès dans SharePoint.
2026-07-20 06:00:06 | INFO     | main | PIPELINE TERMINE AVEC SUCCES en 5.2s
```

---

## Limites connues

| Limite | Description | Solution |
|--------|-------------|----------|
| Fichiers > 4 Mo (SharePoint) | Géré automatiquement par upload fragmenté | Déjà implémenté dans `sharepoint_loader.py` (`createUploadSession`) |
| Pagination iTop | Par défaut 500 objets/page | Ajuster `_PAGE_SIZE` dans `itop_client.py` |
| Timeout iTop | 30s par défaut | Ajuster `_API_TIMEOUT_SECONDS` dans `itop_client.py` |
| Custom fields | Noms techniques non connus | Utiliser `output_fields="*"` en mode test |
| Python 3.10+ requis | Syntaxe `X \| Y` pour les types | Migrer vers Python 3.10+ ou remplacer par `Optional[X]` |
| SSL auto-signé iTop | `ITOP_VERIFY_SSL=False` requis en test | Importer le certificat CA dans le magasin Windows/Linux |
| Limite Groq (TPM) | Le débit tokens/minute peut ralentir l'enrichissement IA sur de gros volumes | Ajuster `GROQ_MAX_TOKENS_PER_MIN`/`AI_BATCH_SIZE` selon le tier du compte Groq |
| Coût IA estimé | `estimated_cost_usd*` est une approximation (≈4 car./token), pas une facturation réelle | Se référer à la facturation Groq officielle |

---

## Dépannage rapide

### `EnvironmentError: LOCAL_XLSX_PATH n'est pas définie`
→ Vous êtes en `INPUT_MODE=xlsx` mais `LOCAL_XLSX_PATH` est absent du `.env`
→ Ajouter : `LOCAL_XLSX_PATH=./data/votre_fichier.xlsx`

### `XlsxLoaderError: Fichier Excel introuvable`
→ Vérifier que le chemin `LOCAL_XLSX_PATH` est correct et que le fichier existe
→ Utiliser un chemin absolu pour éviter les ambiguïtés

### `XlsxLoaderError: Feuille 'Sheet1' introuvable`
→ Vérifier le nom exact de la feuille dans Excel (onglet en bas du classeur)
→ Ou laisser `LOCAL_XLSX_SHEET_NAME=` vide pour utiliser la première feuille

### `EnvironmentError: ITOP_API_TOKEN n'est pas définie`
→ Vous êtes en `INPUT_MODE=api` : vérifier `.env`
→ En `INPUT_MODE=xlsx`, cette variable n'est pas requise

### `ITopAuthError: Authentification refusée`
→ Vérifier la valeur de `ITOP_API_TOKEN` et les droits du token dans iTop Admin

### `SSL verification failed`
→ Vérifier que `ITOP_BASE_URL` utilise `https://` et que le certificat est valide
→ Tester dans un navigateur : si le certificat est auto-signé, importer la CA

### `SharePointAuthError: Accès refusé (403)`
→ Vérifier que la permission `Files.ReadWrite.All` a le consentement administrateur

### `SQLConnectionError: Impossible de se connecter`
→ Vérifier `SQL_HOST`, `SQL_PORT`, l'ODBC driver installé, et les règles firewall

### `Quality check FAIL — références vides` ou intégrité FK
→ Vérifier le mappage du champ `ref`/`Référence` dans `FIELD_MAP_API`/`FIELD_MAP_XLSX` (transform.py)
→ Vérifier que le champ correspondant est bien dans `ITOP_OUTPUT_FIELDS` (itop_client.py, mode API)

### L'enrichissement IA ne fait rien / colonnes `ai_*` toujours à NaN
→ Vérifier `AI_ENRICHMENT_ENABLED=True` et `GROQ_API_KEY` renseignée dans `.env`
→ Utiliser `--ai` pour forcer l'activation sur une exécution, ou `--ai-dry-run` pour diagnostiquer

### Beaucoup de tickets classés `"Indéterminé"` par l'IA
→ Contrôle qualité `classification_ia` en WARN si > 20 %
→ Vérifier que `root_cause_raw`/`solution` contiennent du texte exploitable
→ Ajuster le prompt système (`_SYSTEM_PROMPT_UC1` dans ai_enrichment.py) ou `AI_CONFIDENCE_THRESHOLD`

---

## Colonnes analytiques notables

### `fact_incidents.agent_name`
Contrôlé par `INCLUDE_AGENT_NAME` :
- `True` (défaut) : nom en clair, espaces normalisés, vide → `"Non assigné"`
- `False` : hash SHA-256 de 8 chars (pseudonymisation, sel = `PII_SALT`)

### `fact_incidents.days_since_last_update`
Nombre entier de jours écoulés depuis la dernière mise à jour (`Dernière
mise à jour` dans iTop), recalculé à chaque exécution. Utile pour
identifier les tickets ouverts sans activité récente. `NaN` si la date est absente.

### `fact_incidents.root_cause_category_final`
Catégorie de cause consolidée : catégorie IA (`ai_cause_category`) si sa
confiance dépasse `AI_CONFIDENCE_THRESHOLD`, sinon repli sur la catégorie
regex (`root_cause_category`), sinon `NaN`.

### `dim_subcategory.team_manager`
Responsable associé à chaque sous-catégorie, configuré dans
`SUBCATEGORY_MANAGERS` dans [config.py](config.py). Correspondance
insensible à la casse/espaces. Sous-catégories sans correspondance →
`"Non défini"`.
