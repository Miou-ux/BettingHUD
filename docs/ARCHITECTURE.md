# Architecture BettingHUD

Cette page décrit l’organisation technique du projet (flux data, composants, automatisation).  
Le détail mathématique du modèle et des mises est dans `docs/PREDICTION_ET_MISE.md`.  
La référence opérationnelle actuelle (architecture Live, ELO, Report Opportunités, mises Kelly/BR) est dans `docs/ARCHITECTURE_ACTUELLE_ET_MISES.md`.  
La **synthèse des changements récents** (v47, calibration duale, météo, value, backtest, backup) : **`docs/CHANGELOG_RECENT.md`**.  
Le **changelog ML v45** (correctif causal `last_round_reached_diff`, métriques Brier snapshot de l’époque) reste dans `docs/MODELE_V45_CHANGELOG_ET_PERFORMANCE.md`.

## 1) Vue système

BettingHUD est une app locale Streamlit qui :

1. synchronise des historiques ATP/WTA dans SQLite ;
2. scrape des matchs/cotes prematch et profils joueurs ;
3. charge ou entraîne un bundle ML (`joblib`) ;
4. estime proba/cote juste, EV, mise recommandée ;
5. suit les paris et diagnostics.

## 2) Sources et persistance

- Sources ATP : TennisMyLife → table `matches_recent`
- Sources WTA : Sackmann/TennisAbstract → `wta_matches` (main + qual/ITF, **≥ 2010** comme l’ATP), `rankings_wta_current`
- Scraping prematch/profils : fichiers `data/scraped/*.csv` + cache `data/cache/*.json`
- Base principale : `data/bettinghud.db`
- Modèle : `models/xgb_model_tml_v47.pkl` (importance : `models/feature_importance_tml_v47.png`)

## 3) Composants principaux

- `app/dashboard.py`
  - UI Live Tracker, Top probas jour, Pari Live, Backtest CSV, Portefeuille, Diagnostics, Human Factors
  - orchestration du flux live et des jobs auto
- `scripts/ml_model.py`
  - préparation dataset, features, entraînement, calibration, prédiction
  - sérialisation bundle modèle
- `scripts/stats_engine.py`
  - résolution identité, stats joueur, H2H, signaux historiques
- `scripts/update_model_tml.py`
  - pipeline complet sync ATP/WTA + train + export modèle
- `scripts/sync_tours_daily.py`
  - sync périodique des bases ATP/WTA
- `scripts/surface_speed.py`
  - indices de vitesse de court (CPI) ; ajustements humidité / température / outdoor pour `surface_speed` en entraînement
- `scripts/value_detector.py`
  - EV, drift de ligne, sentiment marché, CLV, pénalisations de confiance
- `scripts/backtest_2026.py`
  - backtest annuel no-leak (ré-entraînement avant cutoff, cotes tennis-data)
- `scripts/create_full_project_backup.py`
  - archive ZIP quasi complète + `RESTAURATION.md`
- `scripts/scraper_prematch.py`, `scripts/scraper_profiles.py`, `scripts/scraper_results.py`
  - cotes prematch, enrichissement profils (`force_refresh` pour MAJ manuelle), résultats / résolution de paris
- `scripts/live_snapshot.py`
  - snapshot joblib des matchs live analysés (signature CSV + modèle) ; lock anti-build concurrent
- `scripts/priority_scoring.py`
  - score composite `(Sharpe / Brier_segment) × qualité calibration` pour tri Value Bets et backtest
- `scripts/refresh_elo_maps_fast.py`
  - refresh ciblé des cartes ELO du bundle sans réentraîner XGBoost : alias nom micro-Elo + ELO match winner/loser

## 4) Pipeline ML (résumé architecture)

1. `prepare_data()` fusionne ATP/WTA, construit les features orientées P1/P2 (dont ELO match réel, micro-Elo service/return, charge 7j, TB 52 sem., météo sur CPI, style KMeans, voyage, défense de points, etc. — voir `CHANGELOG_RECENT.md` et `ARCHITECTURE_ACTUELLE_ET_MISES.md`).
2. Colonnes catégorielles encodées (`surface_encoded`, `tour_encoded`, `tournament_level_encoded`) converties en `category`.
3. Entraînement `XGBClassifier` avec `enable_categorical=True`, contraintes monotones, `objective="reg:squarederror"`.
4. **Calibration duale isotonique** : un calibrateur entraîné sur les matchs **BO3** et un sur les matchs **BO5** (détection via `bo5_mask_from_features` + `ROUTING_COLS_BO5`) ; à l’inférence, `predict_proba_calibrated_routed` route chaque ligne.
5. Export du bundle joblib (`calibrator_bo3`, `calibrator_bo5`, `model`, `features`, Elo maps, objets style, `segment_brier_scores`, etc.).

## 5) Automatisation dans le dashboard

Au runtime, `dashboard.py` peut lancer en tâche de fond :

- sync tours (`sync_tours_daily.py`) selon intervalle configuré ;
- retrain périodique (`update_model_tml.py`) ;
- **daemon live** (`start_live_data_daemon`) : prematch TE, prewarm profils TE, rebuild snapshot → `data/cache/live_matches_snapshot.joblib` (voir `CHANGELOG_RECENT.md` § 7).

Paramétrage via variables d’environnement (`BETTINGHUD_*`).

## 6) Invariants de maintenance

- Toute modification de `self.features` dans `ml_model.py` impose un retrain complet.
- Le bundle et le code doivent rester synchrones (ordre et présence des features).
- Les docs doivent être mises à jour après changement du pipeline ML ou des règles de mise — en priorité **`docs/CHANGELOG_RECENT.md`**.
