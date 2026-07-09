# Autonomie PROD — audit & manques (juillet 2026)

Évaluation de la capacité de BettingHUD à tourner **sans intervention manuelle** (retrain, publications, sync, alertes, déploiement).

> Voir aussi : [[SCHEDULE_MISES_A_JOUR]] · [[CRONS_SEMAINE]] · [[OPS_PROD_DEPANNAGE]] · [[PROD_RESILIENCE]] · [[PROD_AUDIT]] · [[ENVIRONNEMENTS]] · [[ML_BUNDLE_ROLLBACK]]

**Dernière mise à jour** : 9 juillet 2026

---

## 1. Verdict court

| Domaine | Autonome ? | Commentaire |
|---------|------------|-------------|
| **Publications matin** (Top5, 1D1P, TG) | ✅ Oui | Cron 02:00 build + 05:00 publish + QC |
| **Sync données** (ATP/WTA) | ✅ Oui* | Cron 03:30 ; *WTA delta fragile |
| **Retrain ML hebdo** | ✅ Oui | Cron dim. 04:00 + hook rebuild/restart auto |
| **Settlement paris / résultats** | ✅ Oui | Daemon portfolio 10 min |
| **Closing odds archive** | ⚠️ Partiel | Daemon 04:00 — archives récentes seulement |
| **Alertes incident** | ✅ Oui | TG admin via `cron_run_with_alert` + watchdog 5 min |
| **Déploiement code** | ❌ Non | `git pull` + restart SSH manuel |
| **Promotion modèle** | ❌ Non | `scp` + rebuild manuel |
| **Backup DB hors serveur** | ⚠️ Partiel | Backup **serveur** 04:15 ; copie PC si allumé |
| **Monitoring externe** | ❌ Non | Watchdog interne OK ; UptimeRobot optionnel |

**Bottom line** : le **quotidien betting** et les **ops P0** (alertes, backup serveur, watchdog, post-train) tournent seuls. Restent manuels : deploy code, promotion modèle, backup off-site PC, sonde externe.

---

## 2. Ce qui tourne automatiquement (PROD)

### Systemd 24/7

| Service | Rôle | Fichier |
|---------|------|---------|
| `bettinghud-dashboard` | Streamlit + live-data thread (~15 min) | `deploy/systemd/bettinghud-dashboard.service` |
| `bettinghud-daemon` | Portfolio, top15, résultats, closing odds | `deploy/systemd/bettinghud-daemon.service` |
| `bettinghud-telegram-bot` | Bot `/top5`, `/jour`, etc. | `deploy/systemd/bettinghud-telegram-bot.service` |

`Restart=always` + `enabled` → crash recovery au boot. Voir [[PROD_RESILIENCE]].

### Crons (`deploy/cron/` → `/etc/cron.d/`)

| Horaire (Paris) | Job | Log |
|-----------------|-----|-----|
| **02:00** | `morning_live_pipeline.py --build-only` | `morning_build_cron.log` |
| **03:30** | `sync_tours_daily.py` (ATP + WTA) | `tours_auto_sync.log` |
| **04:00** dim. | `update_model_tml.py --min-year 2020` | `ml_train_cron.log` |
| **04:55** | `generate_og_snapshot.py` | `acquisition.log` |
| **05:00** | `morning_live_pipeline.py --morning-publish` | `morning_publish_cron.log` |
| **02:15** dim. | `backup_wta_sackmann_archive.py` | `wta_backup_cron.log` |
| **08:00** lun. | `ml_weekly_telegram_notify.py` | `ml_weekly_telegram.log` |
| **08:10** mar. | `shadow_weekly_telegram_notify.py` | `shadow_weekly_telegram.log` |

### Portfolio daemon (toutes les 10 min)

- Top 15 probas → DB + export JSONL
- Sync résultats TE (Playwright) si paris en cours
- 1D1P résultats → TG + Discord
- Archive closing odds (~04:00 Paris)
- Heartbeat : `data/cache/.portfolio_results_daemon.heartbeat`

---

## 3. Retrain ML — état réel

### Automatisé

- **Cron dimanche 04:00** : `scripts/update_model_tml.py --min-year 2020`
  1. `run_sync_bundle()` (ATP TML + WTA delta) sauf `--skip-sync`
  2. `TennisMLModel.train()` → `models/xgb_model_tml_v47.pkl`
  3. `last_ml_train_ts` dans `bets_meta`

### Non automatisé (gaps critiques)

| Étape | Statut | Risque |
|-------|--------|--------|
| Rebuild snapshot après train | ❌ Manuel | Probas live **stale** jusqu’à rebuild |
| `systemctl restart` dashboard/daemon | ❌ Manuel | Modèle en mémoire pas rechargé |
| Gate Brier J6 post-train | ❌ PREPROD only | Régression possible en prod |
| Rollback auto si régression | ❌ | `ml_bundle_cli.py rollback` manuel |
| Promotion candidat WTA | ❌ | PREPROD exploration seulement |

### Dashboard weekly train

- Thread `start_weekly_ml_train()` dans `app/dashboard.py` — **redondant** si cron OK.
- **Désactivé** pendant le pipeline matin (`BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY=0`).

### Rollback / promotion

Documenté : [[ML_BUNDLE_ROLLBACK]] — `ml_bundle_cli.py freeze|rollback|promote`. **Toujours manuel.**

---

## 4. Interventions manuelles encore requises

| Action | Quand | Doc |
|--------|-------|-----|
| `git push` → SSH `git pull` + `pip install` + restart | Chaque deploy code | [[OPS_PROD_DEPANNAGE]] §7 |
| `scp` modèle `.pkl` PREPROD → PROD | Promotion ML | [[ENVIRONNEMENTS]] |
| `rebuild_live_projection.py` | Après train ou nouveau modèle | [[OPS_PROD_DEPANNAGE]] §6.9 |
| Install / fix crons (`sudo cp deploy/cron/*`) | Setup, CRLF | [[CRONS_SEMAINE]] |
| `.env` secrets (TG, billing) | Setup, rotation | [[OPS_PROD_DEPANNAGE]] §3 |
| Backup DB → PC Windows | Quotidien si PC on | `scripts/backup_prod_db_to_local.ps1` |
| CourtAlpha deploy séparé | Frontend/API | [[OPS_PROD_DEPANNAGE]] §0 |
| WTA ingest urgence | Delta en échec | [[WTA_SACKMANN_ARCHIVE]] |
| Power-on serveur OVH | Après coupure longue | [[PROD_RESILIENCE]] |

**`deploy/install_ubuntu.sh`** : installe venv, Playwright, **3 systemd** — **n’installe PAS les crons**.

---

## 5. Points de défaillance & monitoring

### SPOF

- Serveur unique (tout : DB SQLite, ML, scrapers, crons)
- Playwright / Tennis Explorer (build matin, résultats, closing)
- Chaîne WTA delta (peut faire échouer tout `sync_tours_daily.py`)
- `.env` sur disque

### Alertes existantes

| Mécanisme | Fréquence | Limite |
|-----------|-----------|--------|
| `qc_notify_ops.py` | 05:00 publish | Build 02:00 **non alerté** |
| `ml_weekly_telegram_notify.py` | Lundi 08:00 | Train dimanche : **découverte 1–2 j après** |
| systemd restart | Continu | Pas de notification |

### Monitoring absent

- Sonde uptime externe (UptimeRobot, etc.)
- Watchdog crons (fichier cron supprimé / CRLF)
- Alerte disque plein / OOM
- Alerte prematch stale entre 05:00 et 02:00
- Health `courtalpha-api` (hors install script BettingHUD)

---

## 6. Chaîne data — fraîcheur

| Donnée | Refresh auto | Peut vieillir si |
|--------|--------------|------------------|
| ATP `matches_recent` | 03:30 cron | Sync fail |
| WTA delta | 03:30 cron | Flashscore / delta fail |
| Prematch TE | 02:00, 05:00, live-daemon 15 min | Dashboard down entre crons |
| Snapshot `.joblib` | Matin + daemon | Build lock / crash |
| Top 15 probas | Daemon 10 min | Daemon down |
| Modèle `.pkl` | Dim. 04:00 | Train fail ; pas de restart |
| Closing odds archive | Daemon ~04:00 | Daemon down ; **historique court** |
| `bettinghud.db` paris | Jamais sync PREPROD↔PROD | By design |

État chaîne matin : `data/cache/morning_chain_state.json`.

---

## 7. Roadmap autonomie (priorisée)

### P0 — pour « ne plus y toucher » au quotidien

**Statut : déployé en prod le 2026-07-09** (`scripts/deploy_p0_ops_prod.sh`, crons `bettinghud-ops-p0`, sudoers `bettinghud-ops`).

| # | Action | Statut | Fichiers |
|---|--------|--------|----------|
| 1 | **Post-train hook** : `rebuild_live_projection.py` + restart dashboard + daemon | ✅ | `scripts/post_ml_train_hook.py`, hook auto dans `update_model_tml.py` si `BETTINGHUD_ENV=prod` |
| 2 | **Alerte TG immédiate** si 02:00 build, 03:30 sync ou dim. train fail | ✅ | `scripts/cron_run_with_alert.py` + `ops_telegram_alert.py` (crons morning + data-sync) |
| 3 | **Backup DB sur le serveur** (rétention 30j) | ✅ | `scripts/backup_prod_db_server.py`, cron 04:15 |
| 4 | **Watchdog santé** (5 min, restart auto + TG) | ✅ | `scripts/prod_health_watchdog.py` ; sonde **externe** (UptimeRobot) toujours optionnelle |

### P1 — robustesse plateforme

| # | Action |
|---|--------|
| 5 | Installer crons dans `install_ubuntu.sh` |
| 6 | Deploy script unique : `git pull` + pip + healthcheck + restart |
| 7 | Gate Brier J6 post-train prod + rollback auto si KO |
| 8 | Prematch refresh cron toutes les 2–4 h (indépendant dashboard) |

### P2 — confort / ML

| # | Action |
|---|--------|
| 9 | Promotion WTA routing quand validé PREPROD |
| 10 | Backfill closing odds 6+ mois pour CLV |
| 11 | Plan DR (restore DB + models depuis backup) |

---

## 8. Checklist « est-ce que je dois faire quelque chose ? »

| Situation | Action requise ? |
|-----------|------------------|
| Jour normal, pas de deploy | **Non** (si crons + systemd OK) |
| Après `git push` code | **Oui** — SSH pull + restart |
| Après retrain dimanche | **Non** — hook auto rebuild + restart + alerte TG |
| PC éteint la nuit | Backup DB local **raté** ; prod continue |
| Serveur OVH éteint longtemps | **Oui** — power-on panel |
| WTA delta en échec | **Oui** — voir [[OPS_PROD_DEPANNAGE]] §6.4 |
| Nouveau modèle validé PREPROD | **Oui** — scp + rebuild prod |

---

## 9. PREPROD (PC local)

| Tâche | Auto ? |
|-------|--------|
| Morning build/publish | Tâche planifiée `register_morning_task.ps1` (02:00 + 05:00) |
| Backup prod DB → PC | `register_prod_backup_task.ps1` (~05:30) |
| Portfolio daemon | Manuel / `run_portfolio_daemon.bat` |

PREPROD = bac à sable ; **aucune sync auto** vers prod (data/models).

---

## 10. Liens scripts clés

| Script | Rôle |
|--------|------|
| `scripts/morning_live_pipeline.py` | Pipeline 02:00 / 05:00 |
| `scripts/morning_orchestrator.py` | Chaîne publish + QC |
| `scripts/portfolio_results_daemon.py` | Daemon portfolio |
| `scripts/update_model_tml.py` | Train ML |
| `scripts/sync_tours_daily.py` | Sync ATP/WTA |
| `scripts/closing_odds_archive.py` | Closing odds |
| `scripts/ml_weekly_telegram_notify.py` | Santé ML hebdo |
| `deploy/cron/*` | Définitions cron |
| `deploy/systemd/*` | Services |
