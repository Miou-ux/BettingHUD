# Changelog récent — BettingHUD (mai 2026)

Document de synthèse des **évolutions récentes** du dépôt : ML, données, live, outillage et sauvegarde.  
Les détails d’architecture restent dans `ARCHITECTURE.md` ; probabilité, EV, Kelly et backtest dans `PREDICTION_ET_MISE.md` ; le correctif **`last_round_reached_diff`** et les métriques **snapshot v45** restent historisés dans `MODELE_V45_CHANGELOG_ET_PERFORMANCE.md`.
La référence opérationnelle actuelle complète est `ARCHITECTURE_ACTUELLE_ET_MISES.md`.

---

## 0. Mise à jour 5 juin 2026 — BettingHUD-Web (React PREPROD)

**Doc** : **`docs/WEB_REACT.md`** · Projet frère : `O:\Miouppy\Documents\BettingHUD-Web\`

| Livrable | Détail |
|----------|--------|
| Option B | Dossier **séparé** — Streamlit / prod **inchangés** |
| API | FastAPI lecture seule : `/api/health`, `/api/live/*`, `/api/picks/*` |
| Front | Vite + React + TS — onglets Live / Picks / Top 5 |
| Config | `BETTINGHUD_ROOT` → moteur existant, venv partagé |
| Doc Web | `BettingHUD-Web/docs/` + `AGENTS.md` (doc obligatoire à chaque changement) |
| Sauvegarde prod | DB + archive full du **2026-06-05** avant chantier React |

---

## 0. Mise à jour 28 mai 2026 (c) — Backtest majeurs EV 15–200 % (2026)

**Doc** : **`docs/BACKTEST_MAJOR_EV_2026.md`** · **Script** : `scripts/backtest_major_ev_2026.py`

Main draw ATP/WTA 250+, EV +15 % → +200 %, scénarios tous paris / Top 5 / Top 10 proba. Exports `data/reports/backtest_major_ev_2026_*.csv`.

---

## 0. Mise à jour 28 mai 2026 (b) — Approbation accès bot Telegram

| Livrable | Détail |
|----------|--------|
| `/start` non autorisé | Notification admin + boutons **Approuver** / **Refuser** |
| Persistance | `data/cache/telegram_allowed_chats.json` (fusionné avec `.env`) |
| Fichier | `scripts/telegram_access.py` |

---

## 0. Mise à jour 28 mai 2026 — Auth web, bankroll Telegram avancée

### 0.23 Authentification dashboard (`miouppy`)

**Doc** : **`docs/WEB_AUTH.md`**

| Livrable | Détail |
|----------|--------|
| Login Streamlit | `scripts/web_auth.py` · `data/web_users.json` (hash, gitignored) |
| Reset par e-mail | `web_email.py` + jetons 1 h · lien `/?reset_token=…` |
| Compte owner | `miouppy` · e-mail reset · `telegram_user_id` **7113749284** |
| Paris dashboard | `save_bet` avec `telegram_user_id` si session web liée |
| CLI | `scripts/init_web_user.py --email …` |

Variables SMTP : `BETTINGHUD_SMTP_*`, `BETTINGHUD_WEB_BASE_URL`.

### 0.24 Bankroll Telegram par utilisateur + commande `/brstats`

**Doc** : **`docs/TELEGRAM_TOP5.md`** § 3.5 · § 4

| Thème | Détail |
|-------|--------|
| BR par `telegram_user_id` | Tous paris app + bot (`compute_telegram_user_bankroll_eur`) |
| `/br` | Synthèse : dispo, engagé, P/L, `/brset`, `/brajust` |
| **`/brstats`** | ROI, win rate, forme 10 derniers, 7 j, par `tracker_source`, top paris en cours |
| Alias | `/bradv`, `/brdetail` |
| Scripts migration | `link_app_bets_to_telegram_user.py`, `sync_telegram_br_user.py` |

Fichiers : `scripts/bets_db.py` (`compute_telegram_user_br_advanced_stats`), `scripts/telegram_bet_flow.py`, `scripts/telegram_bot_daemon.py`.

---

## 0. Mise à jour 1 juin 2026 — Challengers, tournois, Telegram

**Doc dédiée** : **`docs/CHALLENGERS_ET_TOURNOIS.md`**

### 0.22 Challengers Live Tracker, WTA 125, `/jourchallenger`, Top 5 main draw (1 juin)

| Thème | Détail |
|-------|--------|
| **Live Tracker** | Toggle **« Inclure les Challengers »** (`live_include_challengers`) — masqué par défaut |
| **Build snapshot** | Inclut ATP/WTA + **`category=Challenger`** + WTA 125 ; enrichissement **points vainqueur** TE |
| **Classification** | `scripts/tournament_tier.py` — main draw **≥ 250 pts** vs challenger tier (125, nom, URL) |
| **Cas Foggia** | TE affiche « Foggia » sans « challenger » → filtré via **125 pts** (plus seulement le nom) |
| **Paris du jour / `/top5`** | `is_major_tournament_match` — **hors** Challenger / WTA 125 / ITF |
| **Telegram** | **`/jourchallenger`** (alias `/challengers`) — EV 15–100 %, tri **proba** ↓ |
| **Scrape** | `tournament_url`, `tourney_winner_points` sur chaque ligne CSV |

Commits : `54cf276`, `bb3911b`, `28e8ee6`, `5d63936`, `5cc9e83`.

Commandes PROD typiques après déploiement :

```bash
./venv/bin/python scripts/scraper_prematch.py
BETTINGHUD_HEADLESS=1 ./venv/bin/python scripts/rebuild_live_projection.py
```

### 0.22b Terminologie PROD — serveur dédié (1 juin)

**Doc** : `docs/ENVIRONNEMENTS.md` — PROD = **serveur dédié** (pas VPS mutualisé). Harmonisation `DEPLOY_SERVEUR.md`, `TELEGRAM_TOP5.md`, etc. Commit `d32d24b`.

---

## 0. Mise à jour 29 mai 2026 — Bot Telegram

### 0.21 Bot Telegram — commande `/strategie` (29 mai)

**Doc** : **`docs/TELEGRAM_TOP5.md`** § 4.1

| Commande | Alias | Contenu |
|----------|-------|---------|
| `/strategie` | `/strategy` | Synthèse sélection (Top 5, EV 15–100 %) + mise Kelly ½ × Brier, cap 15 % |

Fichiers : `scripts/telegram_top5_notify.py` (`format_bot_strategy_message`), `scripts/telegram_bot_daemon.py`.

Aperçu local : `py -3 scripts/telegram_top5_notify.py --strategy`

---

### 0.20 Quick wins ops & UI — audit picks, état système, empty states (29 mai)

**Doc** : **`docs/OPS_UI_QUICK_WINS.md`**

| # | Livrable | Fichiers |
|---|----------|----------|
| **1** | Audit parité Paris du jour / Telegram / DB | `scripts/audit_daily_picks_parity.py` |
| **2** | Bandeau **État système** (5 indicateurs) dans Paramètres | `app/dashboard.py` — `_render_system_status_banner()` |
| **3** | Empty states entonnoir EV (Paris, Top probas, Live Tracker) | `app/dashboard.py` — `_compute_favorite_ev_funnel_stats()` |

Commande audit : `py -3 scripts/audit_daily_picks_parity.py` (exit 0 = Paris ≡ Telegram).

---

### 0.19 Backtest Roland-Garros 2026 — 3 stratégies (29 mai)

**Doc** : **`docs/BACKTEST_RG_2026.md`** · **Script** : `scripts/backtest_rg_strategies.py`

Replay réel `algo_opportunities` depuis le **18/05/2026** : Top 5 proba vs Top 5 EV vs tous paris **p_model ≥ 65 %** (pool EV 15–100 %).

Exports : `data/reports/backtest_rg_strategies_*.csv`

---

### 0.18 Bot Telegram @BettingHUDbot (29 mai)

**Doc** : **`docs/TELEGRAM_TOP5.md`** (documentation complète)

| Élément | Détail |
|---------|--------|
| **Bot** | `@BettingHUDbot` — notifications + commandes **PROD uniquement** |
| **`/jour`** | Tous les matchs scannés **Live Tracker (Aujourd'hui)**, sans filtre EV — `scripts/live_tracker_picks.py` |
| **`/top5`** | Top 5 proba Paris du jour (EV favori 15–100 %) — `collect_top5_proba_picks` |
| **Matinal** | `TELEGRAM_TOP5_AFTER_MORNING=1` → envoi Top 5 en fin de `morning_live_pipeline.py` |
| **Daemon** | `bettinghud-telegram-bot.service` — polling `/jour`, `/top5`, `/help` |
| **Config** | `/opt/bettinghud/.env` : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (jamais commité) |
| **PREPROD** | `--dry-run` seulement ; pas de tâche Windows Telegram |

Fichiers : `scripts/telegram_top5_notify.py`, `scripts/telegram_bot_daemon.py`, `scripts/live_tracker_picks.py`, `deploy/systemd/bettinghud-telegram-bot.service`.

---

## 0. Mise à jour 28 mai 2026 — Paris du jour, UI, déploiement serveur

### 0.17 Ops PROD — écran noir, UI vide, nginx (28 mai)

**Doc** : **`docs/OPS_PROD_DEPANNAGE.md`** (guide complet)

| Incident | Cause | Correctif |
|----------|-------|-----------|
| Écran noir | `matplotlib` / `bs4` manquants ; WebSocket nginx ; thème sombre pendant chargement | `pip install -r requirements.txt` ; `deploy/nginx/bettinghud.conf` ; bandeau chargement UI |
| Seul « Prêt. » + ligne modèle | `BETTINGHUD_HEADLESS=1` sur `bettinghud-dashboard.service` | Retirer la variable — réservée aux scripts CLI |
| Données PREPROD ≠ PROD | Pas de sync auto de `bettinghud.db` | Paris réels en PROD ; `scp` manuel si besoin |

Fichiers : `.streamlit/config.toml` (proxy), `deploy/systemd/bettinghud-dashboard.service` (sans `BETTINGHUD_HEADLESS`).

### 0.16 Convention PREPROD / PROD (28 mai)

**Doc** : **`docs/ENVIRONNEMENTS.md`**

| Environnement | Machine | Variable |
|---------------|---------|----------|
| **PREPROD** | PC local | `BETTINGHUD_ENV=preprod` (défaut) |
| **PROD** | Serveur dédié | `BETTINGHUD_ENV=prod` (systemd) |

Dashboard : bandeau + titre onglet navigateur `[PREPROD]` / `[PROD]` dans l’onglet Paramètres.

**Données** : paris, BR et caches **ne sont pas poussés** automatiquement PREPROD → PROD (voir `ENVIRONNEMENTS.md` § règles).

### 0.14 Dashboard — onglet Paris du jour & navigation (28 mai)

**Fichiers** : `app/dashboard.py`

| Élément | Détail |
|---------|--------|
| **Paris du jour** | 1er onglet : top **5** probas favori (EV 15–100 %), cote réelle éditable, mise Kelly/Brier, enregistrement portefeuille, surbrillance verte si pari posé |
| **Lien Live Tracker** | Bouton « Ouvrir ce match dans Live Tracker » : pré-filtre joueur + circuit, bascule auto vers l’onglet Live Tracker |
| **Mon Portefeuille** | 2e onglet (après Paris du jour) |
| **Paramètres** | Ancienne sidebar : fraîcheur ATP/WTA, scrape/sync, entraînement ML |
| **Masqués** | Onglets Pari Live, Human Factors ; section « Report journalier algo » dans Portefeuille |

### 0.15 Déploiement serveur Ubuntu + GitHub (28 mai)

**Doc** : **`docs/DEPLOY_SERVEUR.md`**

| Élément | Détail |
|---------|--------|
| **Production** | Serveur dédié Ubuntu 24.04 — app via **nginx** → Streamlit `127.0.0.1:8501` |
| **Services** | `deploy/systemd/bettinghud-dashboard.service`, `bettinghud-daemon.service` |
| **Install** | `deploy/install_ubuntu.sh` |
| **Cron** | `deploy/cron/morning-pipeline` — pipeline matin 05:00 UTC |
| **Données** | `data/` et `models/` hors Git — copie `scp` depuis le PC de dev |
| **Dépôt** | Push `main` GitHub — `git pull` sur `/opt/bettinghud` pour mettre à jour le code |

**Correctifs ops** : ingest WTA nécessite `sqlalchemy` ; `portfolio_results_daemon` lancé via chemin script + `PYTHONPATH` (pas `-m scripts` sans package).

---

## 0. Mise à jour 27 mai 2026 — Top 15 probas, filtre EV partagé Live / Top probas

### 0.12 Backtest top 15 probas · 2024–2026 (27 mai)

**Doc** : **`docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md`** § 4 (variante C)

- Simulation **top 15/jour** · EV 15–100 % · Kelly ½ × Brier · cap **15 % liquidité intraday** (mise suivante sur reste du jour).
- Export : `data/reports/compare_top15_proba_years.csv`
- Tests : `tests/test_backtest_staking_sim.py` (amputation liquidité + clôture journalière BR).
- Script `bets_to_br_target.py` : option **`--top-n 15`**.

---

**Fichiers** : `app/dashboard.py`, **`docs/CHART_TOP_PROBAS_JOUR.md`**

| Élément | Détail |
|---------|--------|
| **Top probas jour** | Tableau + chart Altair : **top 15** matchs du jour (Europe/Paris), tri **proba favori modèle** ↓. |
| **Toggle EV** | Bande **EV favori** +15 % à +100 % (`EV = p_fav × cote_fav − 1`). Libellé : `Top 15 · EV favori +15 % à +100 % (tri proba favori ↓)`. |
| **État partagé** | Clé canonique `favorite_ev_band_filter` ; widgets Streamlit distincts (`…_live` / `…_topprobas`) pour éviter les clés dupliquées entre onglets. |
| **Live Tracker** | Même toggle (état synchronisé). Quand actif : filtre matchs EV favori, **≤ 15 tuiles** value bets, tri **proba favori ↓**, **côté favori modèle uniquement** ; le sélecteur Composite/Sharpe/EV est masqué. |
| **Toggle off** | Live Tracker : tri habituel (Composite / Sharpe / EV). Top probas : top 15 sans filtre EV. |

Spec complète : **`docs/CHART_TOP_PROBAS_JOUR.md`**. Distinction backtest **top 10/jour** : **`docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md`**.

### 0.13 Replay réel — stockage top 15 ATP/WTA/jour (27 mai)

**Doc** : **`docs/DAILY_TOP_PROBA_REPLAY.md`**

| Élément | Détail |
|---------|--------|
| **SQLite** | Table `daily_top_proba_picks` — 15 rangs max par `(calendar_date, ATP\|WTA)` |
| **JSONL** | `data/exports/daily_top_proba/{YYYY-MM-DD}.jsonl` — historique append-only par capture |
| **Capture auto** | Sync report algo (dashboard), rebuild live, pipeline matin, **`portfolio_results_daemon`** (10 min) |
| **Résultats** | `sync_daily_top_proba_from_results` via daemon portefeuille (chaque passe) |
| **CLI** | `py -3 scripts/persist_daily_top_proba.py` |
| **Backfill** | `py -3 scripts/backfill_daily_top_proba.py` — JSONL + proxy `algo_opportunities` (18–26/05 récupérés) |

---

## 0bis. Mise à jour 26 mai 2026 — Filtre EV, données live calées, rebuild snapshot

### 0.1 Rebuild snapshot (26/05 ~09:21)

Commande : `py -3 scripts/rebuild_live_projection.py`

| Indicateur | Valeur |
|------------|--------|
| Matchs snapshot | **84** (J+0 / J+1) |
| Durée build | ~**503 s** (mode `full`, cache joueur purgé) |
| CSV prematch | `prematch_odds_20260526_090727.csv` |
| Bundle ML | `xgb_model_tml_v47.pkl` (mtime 25/05, **pas de retrain**) |
| Report algo sync | **29** opportunités (EV ≥ 15 %, seuil classique) |

Audit post-rebuild (`py -3 scripts/audit_projection_day.py --gap-pp 25`) : **17/84** matchs avec écart modèle/book ≥ 25 pp — **informatif** ; les value bets ne sont plus masqués automatiquement pour cet écart (voir §0.2).

### 0.2 Filtre EV & qualité snapshot (26 mai, révisé)

**Fichier** : `app/dashboard.py`

| Comportement | Statut |
|--------------|--------|
| Filtre EV auto (écart modèle/book > 20 pp, sens marché) | **Retiré** (26/05 après-midi) — toutes les values EV ≥ seuil sont à nouveau listées. |
| Seuil EV minimum (`BETTINGHUD_LIVE_EV_THRESHOLD_PCT`, défaut **15 %**) | **Actif** (inchangé). |
| Garde-fou **rang vs proba** (`_prediction_contradicts_rank_points`) | **Actif** — avertissement UI + paris in-play désactivés sur ces matchs uniquement. |
| Champs snapshot `book_gap_pp` | **Info audit** (écart max modèle/book, ne bloque rien). |
| Champs `unreliable` / `data_alert` au build | Uniquement si **rang vs proba** (plus d’exclusion pour écart book). |
| `model_mtime_at_predict` | **Actif** — repredict si le bundle `.pkl` change. |

> Un **rebuild snapshot** (`py -3 scripts/rebuild_live_projection.py`) ou sync report algo recalcule `unreliable` / `book_gap_pp` avec la logique courante.

### 0.3 Données joueur & forme calées sur la date du match

**Fichier** : `scripts/stats_engine.py` (`_STATS_CACHE_SHAPE_VER = 4`)

- `get_recent_form`, `get_recent_fatigue`, `get_recent_match_quality` acceptent **`ref_date`** (date du match live, pas `max(tourney_date)` en base).
- Fenêtres **7 j / 14 j** : uniquement les matchs avec `tourney_date ≤ ref_date` et dans l’intervalle glissant — évite de compter des « victoires récentes » sur une fin d’historique DB figée en avril alors que le match est en mai.
- **WTA** : `_overlay_wta_current_rankings()` — si `rankings_wta_current` est présent, **rang/points** prioritaires sur le dernier match `wta_matches` (`stats_source` → `rankings_wta_current`).

**Build live** (`_build_live_matches_core`) : `ref_date_by_player` dérivé du CSV ; passé au cache features et à `_merge_live_profile`.

**Pas de plafond tactique en Grand Chelem** : les signaux `_compute_live_advanced_signals` restent actifs. Contrôle manuel ou scripts d’audit recommandés si l’écart modèle/book est grand.

### 0.4bis Correctif date classement WTA (`1970-01-01`)

Le CSV Sackmann stocke `ranking_date` en entier **`YYYYMMDD`** (ex. `20260105`). `pd.to_datetime(20260105)` l’interprétait comme des **nanosecondes** depuis l’epoch Unix → affichage **`1970-01-01`** pour Kalinskaya et d’autres joueuses en « Classement WTA courant ».

**Correctif** : `_parse_yyyymmdd_int` dans `stats_engine.py`, ingest ISO (`2026-01-05`), requête rankings avec `ORDER BY ranking_date DESC`.

Après mise à jour : `py -3 scripts/ingest_rankings_current.py` puis rafraîchir snapshot / cache joueur.

### 0.4 Pipeline matin & ingest rankings WTA

**`scripts/morning_live_pipeline.py`**

- Ingest **`rankings_wta_current`** avant le scrape prematch.
- **`BETTINGHUD_LIVE_INCREMENTAL_ENRICH=0`** par défaut → rebuild **full** des probas (évite de garder d’anciennes `true_odd_*` après retrain).

**`scripts/ingest_rankings_current.py`**

- Fonctionne avec **sqlalchemy** ou, à défaut, **sqlite3** seul (plus d’échec silencieux si sqlalchemy absent).

**`scripts/rebuild_live_projection.py`** (inchangé mais documenté)

- Purge snapshots + cache joueur, `LIVE_INCREMENTAL_ENRICH=0`, `force_full=True`.

### 0.5 Enrichissement snapshot & modèle

- `_match_needs_full_repredict()` : repredict si **`model_mtime_at_predict`** ≠ mtime actuel du `.pkl`.
- `_match_snapshot_quality_flags()` : `unreliable` + `book_gap_pp` (audit) au build.

### 0.8 Charte UI « Terminal quant » (26 mai)

**Fichiers** : `app/dashboard.py` (`_inject_quant_terminal_theme()`), **`docs/UI_THEME_QUANT.md`**

- Thème nuit institutionnel (#0B0C10 / #1C1D24), néons success/danger/accent/warning.
- Typo Inter + chiffres JetBrains Mono (`.quant-num`, métriques, tableaux).
- Cartes Value Bet premium : liseré vert via `:has(.vb-card-premium-marker)` ; badges ATP/WTA et segment.
- Boutons **Parier** : `type="primary"`, vert foncé → hover émeraude.
- Tableaux compacts (padding serré).
- Correctif rendu : injection CSS via **`st.html`** + ordre d’appel avant `st.status` (évite l’affichage du CSS en texte brut).

### 0.9 Documentation & coffre Obsidian (26 mai)

**Fichiers** : `docs/Home.md`, **`docs/GUIDE_OBSIDIAN.md`**, `docs/.obsidian/`, `.gitignore`

- Coffre unique **BettingHUDDOCS** → dossier `docs/` (ancien `BettingHuD/` supprimé).
- **Convention** : toute doc durable du projet dans `docs/` ; index [[Home]], guide [[GUIDE_OBSIDIAN]].
- Notes perso / journal : sous `docs/notes/` (liens `[[...]]` vers changelog et archi).

### 0.7 Onglet « Top probas jour » + chart (26 mai)

**Fichiers** : `app/dashboard.py`, **`docs/CHART_TOP_PROBAS_JOUR.md`**

- Nouvel onglet **📈 Top probas jour** : tableau **top 15** des matchs du **jour calendrier** (Europe/Paris), triés par proba favori (`capped_p1_prob` du snapshot).
- Colonnes : rang, proba fav, P1 %, tour, **favori modèle** (surbrillance), adversaire, tournoi, cotes F/U, **EV favori**, gap book (pp).
- **Toggle EV favori 15–100 %** (partagé Live Tracker depuis § 0.11).
- **Graphique Altair** (au-dessus du tableau) : barres horizontales = proba modèle favori ; trait jaune = proba book implicite ; pointillés 50 / 70 / 80 % ; couleur ATP/WTA.
- Spécification complète : **`docs/CHART_TOP_PROBAS_JOUR.md`**.
- Données = snapshot live (même source que le Live Tracker) ; se met à jour au rebuild quotidien / bouton **Actualiser le Live Tracker**.

### 0.6 Outils d’audit

| Script | Usage |
|--------|--------|
| `scripts/audit_projection_day.py` | Écarts modèle/book, décomposition core vs tactique (`--deep`) |
| `scripts/audit_rg_wta_snapshot.py` | Audit ciblé Roland-Garros WTA |
| `scripts/diagnose_live_incoherence.py` | Rejoue minimal/full vs snapshot stocké |
| `scripts/portfolio_results_daemon.py` | Sync résultats portefeuille toutes les 10 min (voir README) |

### 0.7 Retrain nécessaire ?

**Non** pour cette livraison : corrections **inférence + données + filtre UI**. Le bundle **v47** reste celui du 25/05. Retrain utile seulement si vous modifiez `self.features`, ré-ingérez massivement l’historique, ou voulez recalibrer le poids des signaux tactiques **dans le modèle** lui-même.

### 0.8 Diagnostic connu (écarts modèle / marché)

Même après rebuild, des écarts importants peuvent subsister lorsque les **signaux tactiques live** poussent la proba loin du book. Utiliser `audit_projection_day.py --deep` pour distinguer « core ML » vs « + tactique » avant de parier.

### 0.10 Simulations backtest « top 10 probas / jour » (26 mai)

**Doc complète** : **`docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md`**

| Script | Rôle |
|--------|------|
| `scripts/simulate_top10_proba_2026.py` | Simulation annuelle ou `--compare-years 2024,2025,2026` |
| `scripts/bets_to_br_target.py` | Nombre de paris pour passer 100 € → 1 000 € (Kelly) |
| `scripts/export_backtest_bets_sample.py` | Export CSV détaillé (cotes, mises, PnL) |

Protocole : EV **15–100 %**, **top 10/jour par `p_model`** (pas EV), filtres G/M/A, Kelly **½** × Brier, cap **15 %** liquidité. Résultats clés (1 €/pari) : 2024 **+499 €**, 2025 **+685 €**, 2026 partiel **+71 €**. Variante EV plafond **50 %** documentée dans la même note.

---

## 0bis. Mise à jour 18 mai 2026 — ELO match réel, Report Opportunités et retrain v47

- Ajout d'alias par nom pour les micro-Elo service/return afin de réduire les fallbacks `1500` lorsque le Live ne résout pas `player_id`.
- Ajout d'un **ELO match réel** winner/loser, distinct du micro-Elo service/return, avec piste globale et piste surface.
- Ajout des features ML `match_elo_diff` et `surface_match_elo_diff`.
- Retrain complet du bundle `models/xgb_model_tml_v47.pkl` avec ces features.
- Ajout de `scripts/refresh_elo_maps_fast.py` pour rafraîchir rapidement les cartes ELO sans retrain XGBoost.
- Ajout du Report Opportunités historique : opportunités détectées, performance théorique Kelly/Brier/composite, performance réelle sur cote réellement saisie.
- Simulation théorique par trajectoire de bankroll : la BR de fin de journée devient le capital de la journée suivante.

Métriques du retrain du 18 mai 2026 (`python scripts/update_model_tml.py --skip-sync --min-year 2020`) :

- Accuracy test : `0.7243`.
- Brier global : `0.1797`.
- Dataset supervisé : `66 850` exemples.
- Test temporel : `13 370` exemples.
- `surface_match_elo_diff` devient la 3e feature la plus importante.
- `match_elo_diff` devient la 4e feature la plus importante.

Pour le détail des Brier par segment et des règles de mise, voir `ARCHITECTURE_ACTUELLE_ET_MISES.md`.

---

## 1. Modèle ML (`scripts/ml_model.py`) — bundle **v47**

| Sujet | Description |
|--------|-------------|
| **Chemins par défaut** | `models/xgb_model_tml_v47.pkl`, `models/feature_importance_tml_v47.png`. |
| **Objectif XGBoost** | `objective="reg:squarederror"`, `eval_metric="rmse"` (régression sur la cible 0/1). |
| **Calibration duale** | Après le fit du `XGBClassifier` de base, deux calibrateurs isotoniques distincts : **`calibrator_bo3`** et **`calibrator_bo5`**, entraînés sur les sous-ensembles détectés par `bo5_mask_from_features` + colonnes de routage `ROUTING_COLS_BO5`. |
| **Inférence** | `predict_proba_calibrated_routed(X, routing=…)` choisit la branche BO3 ou BO5 ligne à ligne ; `predict_match` construit le frame de routage depuis l’état live / prédiction. |
| **Champ legacy** | `self.model` pointe en pratique vers le calibrateur **BO3** pour compatibilité ; le bundle sérialise `calibrator_bo3`, `calibrator_bo5`, `model`, métadonnées. |
| **Segments sigmoid historiques** | Sur un **`train()`** récent, **`model_segments`** est laissé **vide** dans le bundle ; la calibration principale est la **duale isotonique BO3/BO5**. Le script **`backtest_2026.py`** conserve une logique de segments **sigmoid** + blend pour le paper trading (écart volontaire avec le bundle prod). |

### 1.1 Features notables (liste `self.features`)

Outre le cœur Elo / forme / tactique déjà documenté :

- **Charge récente** : `minutes_played_last7d_diff` (minutes cumulées sur fenêtre glissante **strictement pré-match** ; le match courant n’est pas dans l’historique deque au moment du cumul).
- **Tie-breaks** : `tb_win_pct_52w_diff` (pourcentages glissants sur deque **historique** ; pas de fuite du score du match courant dans ce signal).
- **Météo / surface** : `humidity_impact`, `temperature_impact` — à partir de `humidity_pct`, `temp_c` quand présents, via `scripts/surface_speed.py` (`weather_impact_scalars`, `effective_surface_speed_cpi`, `infer_outdoor`) ; sinon valeurs neutres / dérivées des défauts.
- **Marché** : `market_sentiment_signal` (écart de proba implicite open → current quand les cotes sont disponibles ; sinon 0 en historique).
- **Défense de points** : `points_defending_pct` (proxy « points à défendre » vs points actuels, logique N-1 / niveau tournoi).
- **Calendrier** : `pre_slam_fatigue`.
- **Style** : `style_drift_detected`, `style_cluster_distance_diff`, `style_matchup_bias`, `style_cross_surface_impact` (KMeans tactique + historique).
- **Voyage / âge** : `travel_fatigue_index`, `age_x_travel_fatigue`, `age_x_inactivity`.
- **Clutch** : `clutch_diff` (différentiel dérivé des signaux clutch 52 semaines côté code de préparation).

Toute modification de **`self.features`** impose un **retrain complet** et la régénération du bundle.

**Dernier entraînement documenté** (17 mai 2026, `python scripts/update_model_tml.py`) : ~159k exemples, Brier test global ~**0,174** ; segments affinés dont **`WTA_Clay_G`**. Reporter les métriques détaillées dans `MODELE_V45_CHANGELOG_ET_PERFORMANCE.md` après chaque run significatif.

---

## 2. Météo & vitesse de surface (`scripts/surface_speed.py`)

- Ajustement **CPI** (vitesse de court effective) selon **outdoor vs indoor**, **humidité** et **température** lorsque les champs sont renseignés.
- Helper **`infer_outdoor`** aligné sur les mots-clés tournoi « indoor » connus du projet.
- Utilisé dans **`prepare_data()`** pour recalculer `surface_speed` avant les interactions type `serve_speed_interaction`.

---

## 3. Value & sentiment (`scripts/value_detector.py`)

- **`market_sentiment_signal_p1`** : variation de proba implicite entre cote d’ouverture et cote actuelle (côté P1).
- **`detect_value`** : pénalisation optionnelle (`confidence_penalty`) lorsque la ligne se déplace défavorablement par rapport à la prise (drift + implied).
- **`calculate_clv_score`** : score CLV pour suivi portefeuille.

---

## 4. Live & stats (`scripts/stats_engine.py`, `app/dashboard.py`)

- **`defending_ratio_live`** (ou équivalent dans le flux) : proxy « points à défendre » / points actuels avec données **strictement antérieures** au match (documenté dans `ml_model.py`).
- **Live Tracker** : filtres jour (**Aujourd’hui** / **Demain** / **Tous**), circuit (**ATP** / **WTA**), tournoi, recherche joueur, exclusion doubles, recommandation Kelly (fractions type **1/2**, plafonds bankroll), EV basé sur la **cote saisie** ; tâches de fond pilotées par variables **`BETTINGHUD_*`** (voir § 7 et § 9).
- **Top probas jour** : top 15 + chart Altair (spec `CHART_TOP_PROBAS_JOUR.md`), favori en surbrillance, gap book informatif, toggle EV favori 15–100 % (partagé avec Live Tracker).
- **Live Tracker (toggle EV actif)** : jusqu’à 15 tuiles value bets triées par proba favori modèle (côté favori) — voir § 0.11.
- **Exclusion intra-épreuve** pour `last_round_reached` : voir doc v45 ; le comportement reste valide côté causalité.
- **Origine rang/points homogène** : un match n’est affiché que si les deux joueurs partagent la même `stats_source` officielle (`matches_recent`, `wta_matches`, `rankings_wta_current`, etc.) — pas de mélange TML + Sackmann sur une même ligne.
- **Matchs passés du jour** : après le build, les créneaux dont l’heure (`HH:MM`) est déjà passée sont retirés ; les lignes **`Demain …`** restent visibles jusqu’au lendemain.

### 4.1 Brier segment live & Kelly adaptatif

| Élément | Description |
|--------|-------------|
| **`resolve_match_brier_segment_key()`** | Dans `scripts/ml_model.py` : résout la clé la plus fine présente dans `segment_brier_scores` du bundle (ex. `WTA_Clay_G` pour Roland-Garros, sinon `WTA_Clay`, repli `tour_WTA`). **Distinct** de `segment_calibration_key` (`dual_bo3` / `dual_bo5`). |
| **`brier_segment_key`** | Stockée sur chaque match live ; utilisée pour badge segment, Kelly adaptatif et filtres premium. |
| **`WTA_Clay_G`** | Segment d’entraînement / calibration ajouté au bundle v47 (Grand Chelem terre battue WTA). |
| **`scripts/priority_scoring.py`** | `priority_score_composite = (Sharpe / Brier_segment) × (1 − Brier/0,25)` ; `enrich_value_metrics` enrichit les dicts value ; `is_premium_segment` (seuil Brier **&lt; 0,18**). |
| **Tri Value Bets** | Options : **Composite (priorité)** (défaut), **Sharpe seul**, **EV décroissant**. |
| **Filtre premium** | Toggle « Segments bien calibrés (Brier &lt; 0,18) » sur le live. |
| **Backtest / Kelly A/B** | `backtest_2026.py` et `kelly_ab_analysis_2025.py` utilisent la même résolution de segment Brier. |

### 4.2 Alertes qualité données (UI)

| Pastille | Signification |
|----------|----------------|
| **⚠ ambre** | Données historiques **&gt; 60 j** depuis le dernier match / référence rang (`_STALE_PLAYER_DATA_DAYS`). |
| **⚠ ATP/TE** ou **⚠ WTA/TE** | Conflit **historique officiel** (longue absence en `matches_recent` ou `wta_matches`) vs activité récente sur le **profil Tennis Explorer** — le modèle utilise le pont d’inactivité TE (`_blend_inactivity_days_with_te`). |

**Filtre « Alertes données »** (selectbox live) : masquer / isoler conflits **Base/TE**, **ATP/TE**, **WTA/TE**, ou toutes alertes (données anciennes incluses).

### 4.3 Bouton « Actualiser joueurs » (par match)

- **`_force_refresh_live_match()`** : purge caches TE / SQLite live / stats engine pour les deux joueurs ; re-scrape TE avec **`force_refresh=True`** (`scripts/scraper_profiles.py` supprime le JSON cache) ; re-résout identité + stats ; recalcule prédiction ML.
- Disponible sur les cartes **Value Bet** et dans l’expander **« Voir tous les matchs »**.
- **Ne rafraîchit pas** les cotes book du CSV prematch global — seulement profils / stats / proba.
- Horodatage affiché : **`te_profile_last_sync`** sur les stats joueur après scrape réussi.

### 4.4 Limites de volumétrie live

| Paramètre | Défaut | Rôle |
|-----------|--------|------|
| **`BETTINGHUD_MAX_LIVE_MATCHES_BUILD`** | **200** (24 en `FAST_LIVE_MODE`) | Plafond de lignes CSV analysées (ML + identité) par build. |
| **`_cap_live_build_prioritize_demain()`** | — | Si le CSV dépasse le plafond : **toutes** les lignes `Demain …` d’abord, puis le surplus du jour courant — évite de perdre Rome / Rabat / Strasbourg quand le jour courant remplit seul les 200 premières lignes. |
| **`BETTINGHUD_MAX_PROFILE_FETCH`** | 100 | Scrapes **réseau** max par build journée (cache disque : toutes URLs). Pipeline matin : **sans limite** (`BETTINGHUD_MORNING_BUILD=1`). |
| **`BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW`** | `true` | Filtre calendaire J+0 / J+1 sur le CSV avant build. |

**Note affichage** : en soirée, avec le filtre **« Aujourd’hui »**, les tournois dont il ne reste que des matchs **`Demain …`** (ex. Rabat, Strasbourg) semblent absents — passer à **« Demain »** ou **« Tous »** + circuit **WTA**.

---

## 5. Backtest (`scripts/backtest_2026.py`)

- Garde-fou : le code vérifie que **`TennisMLModel.model_path`** référence bien **`xgb_model_tml_v47.pkl`** (alignement nommage prod).
- **Ré-entraînement no-leak** : uniquement les lignes dont la date de tournoi est **strictement antérieure** au cutoff (défaut : 1er janvier de l’année cible), puis prédictions sur l’année demandée.
- Colonnes **`ml.features`** absentes du pipeline historique simplifié sont **forcées à 0.0** (signaux purement live).
- **`predict_proba_calibrated_routed`** : après entraînement backtest, tant que `calibrator_bo3` n’est pas défini, le repli utilise le **`model`** fraîchement entraîné (pas de chargement silencieux du `.pkl` disque si `model` est déjà défini — voir `_load_bundle_if_needed`).
- Cotes : fichiers Excel **tennis-data** sous `data/raw/tennis_data/<year>.xlsx` et `data/raw/tennis_data_wta/<year>.xlsx`.

---

## 6. Sauvegarde projet (`scripts/create_full_project_backup.py`)

- Génère `backups/BettingHUD_Full_<timestamp>.zip` avec **`RESTAURATION.md`** à la racine de l’archive.
- Copie le même texte en **`backups/BettingHUD_Full_<timestamp>_RESTAURATION.md`** à côté du ZIP.
- **Exclut** : `venv/`, caches (`__pycache__`, `.pytest_cache`, …), `*.pyc`, et **`backups/*.zip`** pour éviter l’emboîtement d’archives.
- **Inclut** : code, `data/`, `models/`, `.git/`, `docs/`, etc.

---

## 7. Snapshot live & daemon (`scripts/live_snapshot.py`)

- **Fichier** : `data/cache/live_matches_snapshot.joblib` (signature : chemin CSV, mtime CSV, schéma cache profils, mtime modèle, version moteurs).
- **`load_live_snapshot` / `save_live_snapshot`** : au démarrage Streamlit, chargement instantané si signature + TTL OK (**24 h** par défaut : `BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC`).
- **`start_live_data_daemon()`** (thread daemon, défaut **activé**) :
  - refresh prematch si CSV &gt; **`BETTINGHUD_PREMATCH_TTL_MIN`** (30 min) ;
  - préchauffe profils TE par lots de 12 URLs / cycle ;
  - rebuild snapshot ML si absent ou signature obsolète (lock fichier `.live_snapshot_build.lock`).
- **Session stable** : si un nouveau CSV arrive en fond, l’UI conserve l’ancien cache session jusqu’à **« Rafraîchir les données »** (sidebar), qui invalide `get_latest_scraped_data`, supprime le snapshot disque et vide `_live_matches_cache`.
- **Caches SQLite live** (identité + features joueur) : TTL **24 h** (`BETTINGHUD_LIVE_PLAYER_CACHE_TTL_SEC`, `BETTINGHUD_LIVE_PLAYER_FEATURES_CACHE_TTL_SEC`).
- **Cache profils TE** : **`BETTINGHUD_PROFILE_CACHE_HOURS`** (défaut **24**).

---

## 8. Dépendances & runtime

- Fichier **`requirements.txt`** figé (Streamlit, XGBoost 3.x, scikit-learn, Playwright, pandas 3.x, etc.).
- **Excel** : pour `pandas.read_excel`, prévoir **`openpyxl`** si besoin (`pip install openpyxl`).

---

## 9. Variables d’environnement live (référence rapide)

| Variable | Défaut | Effet |
|----------|--------|--------|
| `BETTINGHUD_LIVE_DATA_DAEMON` | `1` | Thread prematch + snapshot + prewarm profils. |
| `BETTINGHUD_LIVE_DATA_DAEMON_INTERVAL_SEC` | `900` | Période du daemon (15 min). |
| `BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC` | `86400` | Âge max du snapshot disque. |
| `BETTINGHUD_PREMATCH_TTL_MIN` | `30` | Re-scrape TE si CSV plus vieux. |
| `BETTINGHUD_MAX_LIVE_MATCHES_BUILD` | `200` | Lignes max par build ML. |
| `BETTINGHUD_MAX_PROFILE_FETCH` | `100` | Scrapes réseau max par build (cache disque : toutes URLs). |
| `BETTINGHUD_MORNING_BUILD` | `0` | `1` dans `morning_live_pipeline` : pré-pass + scrape TE **sans aucune limite**. |
| `BETTINGHUD_PROFILE_CACHE_HOURS` | `24` | TTL cache JSON profils TE. |
| `BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW` | `true` | Limite J+0 / J+1. |
| `BETTINGHUD_FAST_LIVE_MODE` | `false` | Stats fictives, cap 24 matchs. |
| `BETTINGHUD_ENABLE_PROFILE_SCRAPE` | `true` | Scrape TE forme / fatigue. |
| `BETTINGHUD_PERF_LOG_LIVE_BUILD` | `false` | Logs timing `[live-build]`. |
| `BETTINGHUD_AUTO_SYNC_TOURS` | `true` | Sync ATP/WTA quotidienne. |
| `BETTINGHUD_AUTO_ML_TRAIN_WEEKLY` | `true` | Retrain ML hebdo en fond. |
| `BETTINGHUD_LIVE_EV_THRESHOLD_PCT` | `15` | Seuil EV minimum (value bets + in-play). |
| `BETTINGHUD_LIVE_INCREMENTAL_ENRICH` | `1` (dashboard) / `0` (pipeline matin) | Réutilise snapshot existant si signature OK. |
| `BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC` | `600` | Intervalle daemon sync résultats paris. |

Liste complète des `os.getenv("BETTINGHUD_*")` : `app/dashboard.py` (sidebar + en-têtes de module).

---

## 10. Tests ajoutés

| Fichier | Couverture |
|---------|------------|
| `tests/test_brier_segment_key.py` | `resolve_match_brier_segment_key`, repli `WTA_Clay` / `WTA_Clay_G`. |
| `tests/test_priority_scoring.py` | `priority_score_composite`, premium Brier, ordre cohérent. |
| `tests/test_value_sharpe.py` | Métriques Sharpe côté value (régression). |

Lancer : `python -m pytest tests/test_brier_segment_key.py tests/test_priority_scoring.py -q`

---

## 11. Fichiers touchés (vue matrice)

| Fichier / dossier | Rôle dans les changements récents |
|-------------------|-----------------------------------|
| `scripts/ml_model.py` | Features, dual calibration, `resolve_match_brier_segment_key`, segments Brier dont `WTA_Clay_G`. |
| `scripts/priority_scoring.py` | Score composite Sharpe × Brier segment. |
| `scripts/live_snapshot.py` | Snapshot disque + lock build. |
| `scripts/surface_speed.py` | CPI, météo, outdoor. |
| `scripts/value_detector.py` | Drift, sentiment, CLV, pénalisation value. |
| `scripts/scraper_profiles.py` | `force_refresh`, cache 24 h. |
| `scripts/stats_engine.py` | Live, identité, sources ATP/WTA séparées. |
| `scripts/backtest_2026.py` | No-leak, priority_score, assert v47. |
| `scripts/kelly_ab_analysis_2025.py` | Kelly A/B avec clé Brier segment live. |
| `scripts/create_full_project_backup.py` | Archive + guide restauration. |
| `app/dashboard.py` | UI live, daemon, alertes, refresh match, tri composite, `ref_date` build, garde-fou rang/proba. |
| `scripts/audit_projection_day.py` | Audit écarts modèle/book + replay core/tactique. |
| `scripts/audit_rg_wta_snapshot.py` | Audit French Open WTA. |
| `scripts/diagnose_live_incoherence.py` | Diagnostic cohérence snapshot vs rejoué. |
| `scripts/morning_live_pipeline.py` | Pipeline matin : rankings WTA + scrape + snapshot. |
| `scripts/portfolio_results_daemon.py` | Daemon sync résultats portefeuille (10 min). |
| `models/xgb_model_tml_v47.pkl` | Bundle courant (à régénérer après train). |
| `tests/test_*.py` | Brier segment, priority scoring, value Sharpe. |
| `docs/*.md` | Cette chronique + mises à jour des pages existantes. |

---

## 12. WTA qual / ITF Sackmann (`wta_matches_qual_itf_YYYY.csv`)

| Composant | Changement |
|-----------|------------|
| **`fetch_wta_sackmann_raw.py`** | `wta_matches_YYYY` + `wta_matches_qual_itf_YYYY` depuis **2010** (`BETTINGHUD_WTA_SACKMANN_MIN_YEAR`). |
| **`ingest_sackmann_wta.py`** | Fusionne **main + qual/ITF** dans `wta_matches` ; **≥ 2010** seulement (aligné ATP / `prepare_data`). |
| **`stats_engine`** | Charge `wta_matches` avec le même filtre année (live plus rapide). |
| **Effet live** | `get_player_stats` / dernier match WTA peuvent utiliser des matchs qualifs, ITF, W125, etc. — dates et rangs plus récents quand Sackmann les publie là avant le fichier `wta_matches_YYYY.csv`. |

Après déploiement : `python scripts/fetch_wta_sackmann_raw.py` puis `python scripts/ingest_sackmann_wta.py` (ou `sync_tours_daily.py`), puis **Rafraîchir les données** dans l’app.

---

## 13. Dette / prochaines mises à jour doc

- Après chaque **run d’entraînement** significatif : reporter dans `MODELE_V45_CHANGELOG_ET_PERFORMANCE.md` (ou une page v47 dédiée) les **Brier globaux et segmentaires** mesurés sur le même protocole de test.
- Aligner **`README.md`** racine (encore en partie sur v45) lors d’une prochaine passe README.

*Dernière mise à jour de ce fichier : 26 mai 2026.*
