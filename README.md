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

## Publication GitHub

La branche courante est `feature/sharepoint-source-flow`. Pour pousser les changements :

```bash
git add .
git commit -m "..."
git push -u origin feature/sharepoint-source-flow
```
