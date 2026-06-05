# Planning des mises à jour — BettingHUD

Vue **centralisée** : quand tournent scrape, snapshot, ML train, daemon, Telegram, etc.  
Dernière mise à jour : **29 mai 2026**.

> Environnements : [[ENVIRONNEMENTS]] · Ops PROD : [[OPS_PROD_DEPANNAGE]] · Flags détaillés : [[CHANGELOG_RECENT]] §7–9.

---

## 1. Timeline PROD (UTC)

```text
02:00 Paris     Pipeline matin (cron) + Telegram Top 5
               ├─ scrape TE jour + demain
               ├─ snapshot full (v47)
               ├─ sync algo_opportunities
               └─ Telegram Top 5 (si TELEGRAM_TOP5_AFTER_MORNING=1)

04:15 Paris     CourtAlphaX — pick safe du jour (ou « pas de value »)
10:00–23:30     CourtAlphaX — tweet résultat + BR (*/30 min)
Dim. 20:00      CourtAlphaX — récap hebdo (lun–dim)

Toute la journée (dashboard actif)
               ├─ prematch re-scrape si CSV > ~20 min
               ├─ snapshot refresh ~15 min (live-data-daemon)
               ├─ sync tours ATP/WTA ~24 h
               └─ retrain ML ~7 j (2 h après boot, puis hebdo)

Toutes les 10 min (bettinghud-daemon)
               ├─ capture top 15 probas/jour
               ├─ sync résultats paris / algo_opportunities
               └─ scrape résultats TE si paris « En cours »

24/7           Telegram bot (polling /top5, /jour)
~30 s          Auto-refresh UI Live Tracker (affichage)
```

**PREPROD** : même logique en manuel ou tâche Windows **02:00** (`scripts/register_morning_task.ps1`) — pas d’envoi Telegram réel.

---

## 2. Pipeline matin

| | |
|---|---|
| **Script** | `scripts/morning_live_pipeline.py` |
| **PROD** | Cron **02:00 Europe/Paris** — `deploy/cron/morning-pipeline` → `/etc/cron.d/bettinghud-morning` |
| **PREPROD** | Manuel ou `register_morning_task.ps1` (défaut **07:00** locale) |
| **Logs PROD** | `data/logs/morning_pipeline_cron.log` + `data/cache/logs/morning_pipeline_*.log` |

**Étapes** :

1. Index SQLite (`db_indexes`)
2. Ingest classements WTA courants (Sackmann)
3. **Scrape Tennis Explorer** (jour + demain) → `data/scraped/prematch_odds_*.csv`
4. **Build snapshot full** (preview + enrichissement ML v47)
5. **Sync report algo** → table `algo_opportunities`
6. **Telegram Top 5** (PROD uniquement, si `.env` configuré)

Le pipeline matin **désactive** le retrain ML auto (`BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY=0`).

---

## 3. Snapshot live

Alimente : **Live Tracker**, **Paris du jour**, **Top probas jour**, **Telegram /top5**.

| Déclencheur | Fréquence | Script / mécanisme |
|-------------|-----------|-------------------|
| Pipeline matin | 1×/jour | `morning_live_pipeline.py` |
| Thread **live-data-daemon** | **~15 min** | `BETTINGHUD_LIVE_DATA_DAEMON_INTERVAL_SEC` (déf. 900) |
| CSV prematch périmé | si âge > TTL | Re-scrape puis rebuild (même thread) |
| Bouton UI | Manuel | « Actualiser le Live Tracker » |
| CLI | Manuel | `scripts/rebuild_live_projection.py` |
| Déploiement modèle | Manuel | `scp` `.pkl` + rebuild |

**Fichiers** : `data/cache/live_matches_snapshot.joblib`, `.full.joblib`, `.nextday.full.joblib`  
**TTL disque** : 24 h (`BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC=86400`)  
**Verrou** : `data/cache/.live_snapshot_build.lock` (max 30 min)

---

## 4. Scrape prematch (cotes TE)

| | |
|---|---|
| **Script** | `scripts/scraper_prematch.py` (`FlashscoreScraper`) |
| **Sortie** | `data/scraped/prematch_odds_YYYYMMDD_HHMMSS.csv` |
| **Auto** | Si dernier CSV > **`BETTINGHUD_PREMATCH_TTL_MIN`** (déf. **20 min** dans `dashboard.py`) |
| **Garanti** | Pipeline matin |

---

## 5. Train ML (bundle v47)

| | |
|---|---|
| **Script** | `scripts/update_model_tml.py` |
| **Sortie** | `models/xgb_model_tml_v47.pkl` |
| **Auto** | Thread dashboard si `BETTINGHUD_AUTO_ML_TRAIN_WEEKLY=1` (déf. oui) |
| **Intervalle** | **7 jours** (`BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC=604800`) |
| **1er run** | **2 h** après démarrage Streamlit (`AUTO_ML_TRAIN_INITIAL_DELAY_SEC=7200`) |
| **Manuel** | `py -3 scripts/update_model_tml.py --skip-sync --min-year 2020` |

**Important** : le retrain auto ne tourne que si le **dashboard Streamlit est démarré** (pas de cron ML séparé).

**Promotion PREPROD → PROD** :

```powershell
scp models/xgb_model_tml_v47.pkl bettinghud:/opt/bettinghud/models/
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/rebuild_live_projection.py"
```

---

## 6. Sync données tours (SQLite ATP/WTA)

| | |
|---|---|
| **Script** | `scripts/sync_tours_daily.py` |
| **Fréquence** | **1×/24 h** (`BETTINGHUD_AUTO_SYNC_INTERVAL_SEC=86400`) |
| **Délai initial** | 2 min après boot dashboard |
| **Log** | `data/logs/tours_auto_sync.log` |
| **PROD** | `BETTINGHUD_AUTO_SYNC_TOURS=1` (systemd dashboard) |

---

## 7. Daemon portefeuille (`bettinghud-daemon`)

| | |
|---|---|
| **Service** | `deploy/systemd/bettinghud-daemon.service` |
| **Script** | `scripts/portfolio_results_daemon.py` |
| **Intervalle** | **10 min** (`BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC=600`) |
| **PREPROD** | `scripts/run_portfolio_daemon.bat` ou `--once` |

**À chaque passe** :

- Capture **top 15 probas/jour** → `daily_top_proba_picks` + JSONL
- Sync statuts **`algo_opportunities`**
- Si paris « En cours » : scrape résultats TE (Playwright, verrou fichier)

---

## 8. Telegram

| Événement | Quand |
|-----------|--------|
| **Top 5 matinal** | Après pipeline matin (~**02:00 Paris**) si `TELEGRAM_TOP5_AFTER_MORNING=1` |
| **Commandes** | Bot polling 24/7 — `/top5`, `/jour`, `/help` |
| **Service PROD** | `bettinghud-telegram-bot.service` |

Doc : [[TELEGRAM_TOP5]] · Config : `/opt/bettinghud/.env`

**PREPROD** : pas d’envoi réel (`--dry-run` ou garde-fou env).

---

## 8b. CourtAlphaX (compte public X)

Doc complète : [[COURTALPHAX_X]] · modèle `.env` : `docs/env.courtalphax.example`

| Événement | Quand (Paris) | Script |
|-----------|---------------|--------|
| Pick safe du jour | **04:15** (+ retry **04:30**, **05:00**) | `courtalphax_daily_pick.py` |
| Vérif preflight | **04:05**, **04:10** | `courtalphax_preflight.py` |
| Tweet résultat + BR | **10:00–23:30**, */30 min | `courtalphax_result_notify.py` |
| Récap hebdo | **Dimanche 20:00** | `courtalphax_weekly_recap.py` |

Cron : `deploy/cron/courtalphax-x` → `/etc/cron.d/bettinghud-courtalphax-x` · logs : `data/logs/courtalphax_x.log`

**PREPROD** : `--dry-run` uniquement (garde-fou `require_prod_for_x_post`).

---

## 9. UI dashboard (refresh affichage)

| Composant | Intervalle | Variable |
|-----------|------------|----------|
| Live Tracker auto-refresh | **~30 s** | `BETTINGHUD_LIVE_TRACKER_AUTO_REFRESH_SEC` (déf. 30) |
| Sync résultats portefeuille (UI) | **~3 min** | `BETTINGHUD_PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC` (déf. 180) |

Ces refresh **relisent** le snapshot / la DB — ils ne remplacent pas le pipeline matin ni le rebuild complet.

---

## 10. Tableau PREPROD vs PROD

| Tâche | PREPROD | PROD |
|-------|---------|------|
| Pipeline matin | Manuel / tâche Windows 02:00 | Cron **02:00 Paris** |
| live-data-daemon | Si dashboard ouvert | systemd dashboard |
| Retrain ML hebdo | Si dashboard ouvert | Idem |
| Daemon portefeuille | `.bat` / `--once` | `bettinghud-daemon.service` |
| Telegram | Non | Oui |
| CourtAlphaX (X) | `--dry-run` | Cron + tweets réels |
| Snapshots / cache | Rebuild local fréquent | Pipeline + daemon + manuel |

**Non synchronisés** entre env : `bettinghud.db`, `data/cache/`, paris réels — voir [[ENVIRONNEMENTS]].

---

## 11. Commandes utiles

```powershell
# PREPROD — pipeline matin manuel
py -3 scripts/morning_live_pipeline.py

# Rebuild snapshot CLI
py -3 scripts/rebuild_live_projection.py

# Retrain ML manuel
py -3 scripts/update_model_tml.py --skip-sync --min-year 2020

# PROD — statut services
ssh bettinghud "systemctl status bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot"

# PROD — logs pipeline
ssh bettinghud "tail -30 /opt/bettinghud/data/logs/morning_pipeline_cron.log"

# PROD — rebuild manuel
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/rebuild_live_projection.py"
```

---

## 12. Variables clés (`BETTINGHUD_*`)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `BETTINGHUD_PREMATCH_TTL_MIN` | 20 | Re-scrape TE si CSV plus vieux |
| `BETTINGHUD_LIVE_DATA_DAEMON_INTERVAL_SEC` | 900 | Période thread live (15 min) |
| `BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC` | 86400 | Validité snapshot disque (24 h) |
| `BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC` | 604800 | Retrain ML (7 j) |
| `BETTINGHUD_AUTO_SYNC_INTERVAL_SEC` | 86400 | Sync tours (24 h) |
| `BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC` | 600 | Daemon portefeuille (10 min) |
| `BETTINGHUD_LIVE_EV_THRESHOLD_PCT` | 15 | EV min live / value bets |
| `TELEGRAM_TOP5_AFTER_MORNING` | 0 / 1 | Envoi Top 5 post-pipeline |

Liste complète : [[CHANGELOG_RECENT]] · commentaires en tête de `app/dashboard.py`.

---

## 13. Voir aussi

- [[ENVIRONNEMENTS]] — workflow PREPROD/PROD, promotion modèle
- [[OPS_PROD_DEPANNAGE]] — cron, systemd, rebuild, incidents
- [[ARCHITECTURE_ACTUELLE_ET_MISES]] — flux scrape → proba → Kelly
- [[TELEGRAM_TOP5]] — bot et envoi matinal
- [[COURTALPHAX_X]] — compte public X (pick, résultats, récap hebdo)
- [[CHART_TOP_PROBAS_JOUR]] — top 15 + toggle EV
- [[DAILY_TOP_PROBA_REPLAY]] — stockage replay top probas
