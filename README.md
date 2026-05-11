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

# 3. Re-train / export du bundle ML (sync ATP+WTA incluse dans le script)
python scripts/update_model_tml.py
```

Pour purger les anciennes tables Sackmann ATP héritées (`matches`, `players`, `rankings_atp_current`) :

```bash
python scripts/purge_sackmann_atp.py
```

## Utilisation

```bash
streamlit run app/dashboard.py
```

Au premier lancement, le dashboard peut en arrière-plan **re-synchroniser la base** (`scripts/sync_tours_daily.py`) et, sur un intervalle long, **réentraîner le modèle** (`scripts/update_model_tml.py`). Désactivation possible via variables d’environnement (voir `docs/ARCHITECTURE.md`).

Scrapers :

```bash
python scripts/scraper_prematch.py    # cotes prematch Flashscore
python scripts/scraper_live.py        # score live (placeholder)
```

## Documentation

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — architecture du dépôt, flux de données, automatisation, limites.
- **[`docs/PREDICTION_ET_MISE.md`](docs/PREDICTION_ET_MISE.md)** — prédiction ML, features, Kelly adaptatif (Brier), backtest, KPI.

## Structure du projet (résumé)

- `app/dashboard.py` — interface Streamlit (live, portefeuille, diagnostics, backtest CSV, human factors).
- `scripts/`
  - `stats_engine.py` — moteur de stats par tour (ATP TML / WTA Sackmann)
  - `ml_model.py` — **XGBoost** calibré, features v4.x, bundle `joblib`
  - `sync_tours_daily.py` — sync **ATP + WTA** (TML + Sackmann / pipeline associé)
  - `sync_tml_recent.py` — utilitaire sync ATP TennisMyLife ciblé
  - `ingest_sackmann_wta.py` — ingestion WTA Sackmann
  - `ingest_rankings_current.py` — ingestion classement WTA courant
  - `pipeline_quality.py` — orchestration ingest + index SQLite
  - `apply_sqlite_indexes.py` — index sur matches_recent / wta_matches
  - `update_model_tml.py` — sync tours + entraînement + export `models/*.pkl`
  - `evaluate_data_coverage.py` — vérif des volumes en base
  - `purge_sackmann_atp.py` — DROP des tables Sackmann ATP héritées
  - `scraper_prematch.py`, `scraper_profiles.py` — scraping Playwright
  - `value_detector.py` — comparaison cote / true odd (EV %)
  - `player_identity.py` — utilitaires de normalisation de noms

Détail des chemins et du flux : **`docs/ARCHITECTURE.md`**.
