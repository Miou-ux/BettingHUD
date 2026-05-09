# BettingHUD (Tennis)

Système d'analyse de paris sportifs pour le tennis : scraping live + cotes prematch,
moteur statistique et modèle ML pour détecter les Value Bets.

## Architecture des données (par tour)

| Tour | Source primaire | Tables SQLite |
|------|----------------|---------------|
| **ATP** | [TennisMyLife](https://stats.tennismylife.org/) | `matches_recent` (`source='tennismylife'`) |
| **WTA** | [Tennis Abstract / Sackmann](https://github.com/JeffSackmann/tennis_wta) | `wta_matches`, `rankings_wta_current` |

Aucune cross-fallback : un joueur ATP introuvable dans TML ne retombe pas sur Sackmann ATP
(et inversement). Cela garantit que la source affichée dans l'UI correspond bien à celle
qui a alimenté les stats.

## Pré-requis

- Python 3.9+
- Playwright (pour les scrapers)

## Installation

```bash
python -m venv venv
# Windows :
venv\Scripts\activate
# macOS/Linux :
source venv/bin/activate

pip install -r requirements.txt
playwright install
```

## Initialisation des données

```bash
# 1. ATP -> sync TennisMyLife (~30s pour 2010-2026)
python scripts/sync_tml_recent.py

# 2. WTA -> ingestion Sackmann (matches + rankings)
#    Pré-requis : data/raw/tennis_wta/wta_matches_*.csv + wta_rankings_current.csv
python scripts/pipeline_quality.py

# 3. (optionnel) re-train du modèle ML ATP
python scripts/update_model_tml.py --min-year 2010
```

Pour purger les anciennes tables Sackmann ATP héritées (`matches`, `players`, `rankings_atp_current`) :

```bash
python scripts/purge_sackmann_atp.py
```

## Utilisation

```bash
streamlit run app/dashboard.py
```

Scrapers :

```bash
python scripts/scraper_prematch.py    # cotes prematch Flashscore
python scripts/scraper_live.py        # score live (placeholder)
```

## Structure du projet

- `app/dashboard.py` — interface Streamlit
- `scripts/`
  - `stats_engine.py` — moteur de stats par tour (ATP TML / WTA Sackmann)
  - `ml_model.py` — modèle Random Forest entraîné sur TML
  - `sync_tml_recent.py` — sync ATP TennisMyLife
  - `ingest_sackmann_wta.py` — ingestion WTA Sackmann
  - `ingest_rankings_current.py` — ingestion classement WTA courant
  - `pipeline_quality.py` — orchestration ingest + index SQLite
  - `apply_sqlite_indexes.py` — index sur matches_recent / wta_matches
  - `update_model_tml.py` — sync TML + retraining ML
  - `evaluate_data_coverage.py` — vérif des volumes en base
  - `purge_sackmann_atp.py` — DROP des tables Sackmann ATP héritées
  - `scraper_prematch.py`, `scraper_profiles.py` — scraping Playwright
  - `value_detector.py` — comparaison cote / true odd (EV %)
  - `player_identity.py` — utilitaires de normalisation de noms
