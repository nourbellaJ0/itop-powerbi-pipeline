# Pipeline iTop → Power BI

Pipeline Python pour récupérer les incidents iTop, les nettoyer, les enrichir si besoin, les modéliser en étoile, puis les déposer dans SharePoint ou SQL pour Power BI.

La documentation complète du projet se trouve dans [PROJECT_DETAILS.md](PROJECT_DETAILS.md).

## Ce que fait le projet

- Source API iTop, fichier XLSX local, ou source déposée dans SharePoint
- ETL de nettoyage et masquage PII
- Enrichissement IA optionnel pour les causes et la synthèse mensuelle
- Classification Monétique optionnelle
- Modèle en étoile ou export table plate
- Contrôle qualité avant export
- Dépôt final vers SharePoint ou SQL

## Démarrage rapide

```bash
python main.py
python main.py --input sharepoint --output sharepoint
python main.py --input xlsx --output sharepoint --skip-ai
```

## Modes principaux

- `api` : lecture directe depuis iTop
- `xlsx` : lecture d’un export local
- `sharepoint` : téléchargement du fichier source depuis SharePoint avant ETL

## Options utiles

- `--output sharepoint|sql`
- `--legacy-flat`
- `--full-refresh`
- `--ai`
- `--ai-dry-run`
- `--ai-monetique-dry-run`
- `--skip-ai`

## Configuration

Copie `.env.example` vers `.env`, puis renseigne les variables de connexion iTop, SharePoint, SQL et Groq selon le mode utilisé.

## Tests

```bash
python -m pytest tests/
```

## Déclenchement à distance (GitHub Actions)

Le workflow [`.github/workflows/run-pipeline.yml`](.github/workflows/run-pipeline.yml)
exécute le pipeline en mode `sharepoint → sharepoint` sur demande
(`workflow_dispatch`), ce qui permet de le déclencher depuis Power Automate
via l'API REST GitHub :

```
POST /repos/{owner}/{repo}/actions/workflows/run-pipeline.yml/dispatches
{ "ref": "main", "inputs": { "skip_ai": "true", "full_refresh": "false" } }
```

Toutes les valeurs sensibles sont lues depuis `secrets.*` (Settings → Secrets
and variables → Actions) — voir l'en-tête du fichier YAML pour la liste des
secrets attendus. Le run se termine avec un code de sortie non-zéro (donc
visible comme échec dans Actions) si le pipeline échoue, y compris sur une
exception non prévue. Le statut (`En cours` / `OK` / `Erreur`) est aussi
écrit dans une liste SharePoint si `SHAREPOINT_STATUS_LIST_NAME` est défini.

## Publication GitHub

La branche courante est `feature/sharepoint-source-flow`. Pour pousser les changements :

```bash
git add .
git commit -m "..."
git push -u origin feature/sharepoint-source-flow
```
