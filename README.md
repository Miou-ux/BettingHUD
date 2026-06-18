# BettingHUD (Tennis)

BettingHUD est un outil d'aide à la décision pour le pari tennis :

- **PREPROD** : PC local (développement et tests)
- **PROD** : serveur dédié Ubuntu (usage réel)

Voir **`docs/ENVIRONNEMENTS.md`** pour le workflow complet (PREPROD vs PROD, données, variables).

**Production** : http://192.95.30.217 — installation **`docs/DEPLOY_SERVEUR.md`**, ops & dépannage **`docs/OPS_PROD_DEPANNAGE.md`**.

**Backup PROD → PC** (quotidien) : `scripts/backup_prod_db_to_local.ps1` · tâche : `scripts/register_prod_backup_task.ps1` → `backups/prod/`.

- ingestion ATP/WTA vers SQLite,
- scraping prematch / profils,
- estimation de probabilité via modèle ML,
- détection de value (EV),
- recommandation de mise (Kelly adaptatif),
- suivi portefeuille / diagnostics.

## Sources de données

- **ATP** : TennisMyLife (`matches_recent`, `source='tennismylife'`)
- **WTA** : Sackmann / Tennis Abstract (`wta_matches`, `rankings_wta_current`)

Le système reste strict par tour : pas de fallback ATP<->WTA pour rang/points.

## Pré-requis

- Python 3.9+
- Playwright installé pour les scrapers

## Installation

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
pip install streamlit-autorefresh
playwright install
playwright install-deps   # Linux uniquement
```

## Pipeline de base

```bash
# Sync ATP + WTA, puis entraînement complet et export bundle ML
python scripts/update_model_tml.py --min-year 2010
```

Utilitaires séparés si besoin :

```bash
# Sync ATP TennisMyLife uniquement
python scripts/sync_tml_recent.py

# Ingest WTA (si run manuel)
python scripts/ingest_sackmann_wta.py
python scripts/ingest_rankings_current.py
```

## Lancer l'application

```bash
streamlit run app/dashboard.py
```

Le dashboard lance des tâches en arrière-plan (daemon live, sync tours, retrain périodique), configurables via `BETTINGHUD_*` — voir `docs/CHANGELOG_RECENT.md`.

Onglets principaux (ordre gauche → droite) :

1. **Paris du jour** — top 5 probas favori, cote réelle, mise Kelly, lien vers Live Tracker
2. **Mon Portefeuille** — paris réels, stats, CLV
3. **Live Tracker** — value bets, filtres jour/circuit/joueur
4. **Top probas jour** — top 15 + graphique (toggle **EV favori 15–100 %** — `docs/CHART_TOP_PROBAS_JOUR.md`)
5. **Backtest Kelly (CSV)**, **Diagnostics modèle**, **Tracking modèle (réel)**
6. **Paramètres** — fraîcheur données, sync/scrape manuel, entraînement ML

Onglets masqués : Pari Live, Human Factors.

**Live Tracker + toggle EV actif** : filtre matchs sur EV favori 15–100 %, affiche jusqu’à **15 tuiles** value bets triées par proba favori modèle (côté favori).

Charte graphique (thème sombre type terminal quant) : **`docs/UI_THEME_QUANT.md`**.

### Hébergement serveur (Ubuntu)

Déploiement production documenté dans **`docs/DEPLOY_SERVEUR.md`** (systemd, nginx, cron matin, `git pull`).

```bash
# Sur le serveur après clone + copie data/models
bash deploy/install_ubuntu.sh
```

### Bot Telegram (PROD)

Notifications et commandes **@CourtAlphabot** : `/today`, `/top5`, `/1pick1day`, envoi matinal après pipeline.

Documentation : **`docs/TELEGRAM_TOP5.md`**. PREPROD : `py -3.11 scripts/telegram_top5_notify.py --dry-run` uniquement.

### Sync portefeuille (résultats des paris)

Daemon dédié — résolution TE/Sackmann toutes les **10 minutes** (même Streamlit fermé).  
En parallèle, **capture automatique** du top 15 probas ATP/WTA/jour depuis le snapshot live (SQLite + JSONL) — voir `docs/DAILY_TOP_PROBA_REPLAY.md`.

```bash
py -3 -m scripts.portfolio_results_daemon
# ou Windows : scripts\run_portfolio_daemon.bat
```

Variables : `BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC` (défaut `600`), `BETTINGHUD_PORTFOLIO_SCRAPE_LOCK_MAX_SEC` (défaut `1200`). Une passe unique : `--once`.

### Projection live du jour (snapshot)

```bash
# Pipeline matin (scrape + rankings WTA + snapshot full)
py -3 scripts/morning_live_pipeline.py

# Rebuild forcé après changement code / modèle / CSV
py -3 scripts/rebuild_live_projection.py

# Audit qualité modèle vs book
py -3 scripts/audit_projection_day.py --gap-pp 25 --deep
```

Seuil EV par défaut **15 %** (`BETTINGHUD_LIVE_EV_THRESHOLD_PCT`). Garde-fou UI si proba modèle incompatible avec le classement affiché. Écarts modèle/book : audit via `audit_projection_day.py` (non bloquants). Voir `docs/CHANGELOG_RECENT.md` §0.

## Modèle ML (état actuel)

Le modèle dans `scripts/ml_model.py` est un XGBoost calibré :

- `XGBClassifier(..., enable_categorical=True)`
- colonnes `surface_encoded`, `tour_encoded`, `tournament_level_encoded` en type `category`
- calibration isotonic avec `CalibratedClassifierCV` (`TimeSeriesSplit(n_splits=5)`)
- contraintes monotones natives V2 :
  - `points_diff`: +1
  - `service_elo_diff`: +1
  - `rank_diff`: -1
- plus de hard caps post-prédiction dans `predict_match`

Bundle exporté par défaut :

- `models/xgb_model_tml_v47.pkl`
- `models/feature_importance_tml_v47.png`

## Documentation

- `docs/Home.md` : **index Obsidian** du coffre documentation (liens vers toutes les notes ci-dessous).
- `docs/GUIDE_OBSIDIAN.md` : utilisation du coffre Obsidian + **convention : tout documenter dans `docs/`**.
- `docs/CHANGELOG_RECENT.md` : **journal des évolutions récentes** (live, snapshot, Brier segment, alertes, env).
- `docs/ARCHITECTURE.md` : structure du projet, flux de données, automation.
- `docs/PREDICTION_ET_MISE.md` : détails ML, features, calibration, EV, Kelly, filtres live, backtest.
- `docs/CHART_TOP_PROBAS_JOUR.md` : onglet Top probas jour (top 15, chart Altair, toggle EV favori partagé avec Live Tracker).
- `docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md` : campagne backtest top 10 / **top 15** probas/jour (2024–2026, Kelly séquentiel intraday, comparatif €).
- `docs/DAILY_TOP_PROBA_REPLAY.md` : stockage top 15 ATP/WTA/jour (replay réel).
- `docs/ENVIRONNEMENTS.md` : convention **PREPROD** (PC) vs **PROD** (serveur), workflow de déploiement, sync données.
- `docs/DEPLOY_SERVEUR.md` : installation Ubuntu, systemd, nginx, mise à jour GitHub.
- `docs/OPS_PROD_DEPANNAGE.md` : ops production, variables (`BETTINGHUD_HEADLESS`), incidents (écran noir, UI vide), checklist.
- `docs/PROD_RESILIENCE.md` : redémarrage auto après crash app ou reboot serveur (systemd).
- `docs/MODELE_V45_CHANGELOG_ET_PERFORMANCE.md` : historique v45 / métriques snapshot d’époque.
