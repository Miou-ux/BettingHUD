# Changelog récent — BettingHUD (mai 2026)

Document de synthèse des **évolutions récentes** du dépôt : ML, données, live, outillage et sauvegarde.  
Les détails d’architecture restent dans `ARCHITECTURE.md` ; probabilité, EV, Kelly et backtest dans `PREDICTION_ET_MISE.md` ; le correctif **`last_round_reached_diff`** et les métriques **snapshot v45** restent historisés dans `MODELE_V45_CHANGELOG_ET_PERFORMANCE.md`.
La référence opérationnelle actuelle complète est `ARCHITECTURE_ACTUELLE_ET_MISES.md`.

---

# Ops — faux positifs watchdog TG (28 août 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | Alertes Telegram récurrentes `🚨 OPS — Watchdog PROD — anomalie` avec `portfolio daemon heartbeat > 900s` (~1×/h) |
| **Cause** | Heartbeat touché **seulement au début** de chaque passe ; cycle réel ≈ **16 min** (scrape Playwright ~6 min + pause 10 min) > seuil watchdog **15 min** |
| **Impact** | Faux positifs — services actifs, daemon en scrape normal |
| **Correctif** | `portfolio_results_daemon.py` : heartbeat aussi **en fin de passe** ; `prod_health_watchdog.py` : seuil défaut **900 → 1200 s** |
| **Prod** | Déployé 28/08/2026 — `git pull` + restart `bettinghud-daemon` |

Voir `docs/OPS_PROD_DEPANNAGE.md` § watchdog heartbeat.

Variables : `BETTINGHUD_WATCHDOG_DAEMON_MAX_AGE_SEC` (déf. 1200), heartbeat `data/cache/.portfolio_results_daemon.heartbeat`.

---

# Ops — réconciliation prod ↔ GitHub (28 août 2026)

| Élément | Détail |
|---------|--------|
| **Problème** | Prod bloquée à `f888350` avec 13 fichiers modifiés localement ; `git pull` impossible |
| **Cause** | Hotfixes scp/SSH sans commit ni pull (crons matin, morning pipeline, backup) |
| **Action** | `git reset --hard origin/main` + réinstall crons + restart services |
| **Backup** | `backups/prod/prod_drift_before_reconcile_20260828_002804.patch` sur serveur |
| **État final** | HEAD `1638006` = GitHub ; watchdog OK ; scripts `_*.py` diag prod conservés untracked |

Runbook : `docs/OPS_PROD_DEPANNAGE.md` § 0quater.

---

# Revert — projection jour J+1 jusqu'à 07:00 (30 août 2026)

Extension `is_today_paris_match` (matchs J+1 jusqu'à 07:00 Paris) **annulée** — retour au filtre **date calendaire stricte**.

---

# Projection jour — matchs J+1 jusqu'à minuit (30 août 2026)

| Élément | Détail |
|---------|--------|
| **Besoin** | US Open : session du soir datée J+1 sur TE, mais jouée **avant minuit** Paris |
| **Règle** | `is_today_paris_match` : date **J** + matchs datés **J+1** tant qu'on est **avant 00:00 J+1** (Europe/Paris) |
| **Variable** | `BETTINGHUD_PROJECTION_DAY_CUTOFF_HOUR` (déf. `0` = minuit) |
| **≠ 07:00** | Pas de prolongation après minuit (nuit 01h–06h = jour suivant) |

---

# CourtAlpha Web — mise Kelly BetModal (26 août 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | Modal « Confirmer le pari » (Top 5, 1D1P, Paris) affichait **10 €** fixe au lieu de la mise Kelly |
| **Cause** | Régression : `BetModal` ne chargeait plus la bankroll app ni `computeKellyStake` (seul Live passait `defaultStake`) |
| **Correctif** | `deploy/courtalpha/frontend/src/components/BetModal.tsx` — fetch `/api/portfolio/summary`, Kelly **0,85 × Brier segment**, cap 15 % BR, recalcule à chaque changement de cote (sauf si l’utilisateur a modifié la mise à la main) |
| **Alignement** | Même logique que `scripts/telegram_bet_flow.kelly_stake_for_pick` et `ValueBetCard` (Live) |
| **Déploiement** | `npm run build` → `scp dist/` → `chmod a+rX` sur `/opt/courtalpha/frontend/dist` |

Voir aussi `docs/WEB_REACT.md` § Paris / Top 5 / Live.

---

# Qualité données ATP/WTA — nettoyage WTA (16 août 2026)

| Élément | Détail |
|---------|--------|
| **Incident** | Date WTA affichée **2029** ; **307 doublons C1** ; sync nocturne `rc=1` ; enrich metadata en échec |
| **Cause** | Ligne delta corrompue ; doublons multi-sources ; bug pandas dtype `round`/`entry` ; meta sync bloquée par QC |
| **Correctifs** | `drop_aberrant_wta_tourney_dates`, `ensure_wta_frame_writable`, filtre ingest, stamp `last_tours_sync_ts` sur ingest OK |
| **Prod** | 307 dupes + 1 ligne 2029 supprimés ; **409 028** lignes WTA ; QC **0 blocking** ; feature store rebuild |
| **Affichage** | Dates EU DD/MM/YYYY CET ; filtre SQL dates aberrantes (`bets_db._wta_sane_tourney_date_sql`) |

Doc complète : **`docs/DONNEES_ATP_WTA.md`**.

| Script | Rôle |
|--------|------|
| `scripts/fix_wta_data_cleanup.py` | Nettoyage manuel dedup + ingest + QC |
| `scripts/_audit_atp_wta_data.py` | Audit fraîcheur / couverture ATP+WTA |
| `scripts/wta_delta_qc_gates.py` | Gates C1 doublons + D1 rangs |

---

# Top picks du jour illimité + renommage (24 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Sélection** | HYB P75+P80-all **sans plafond journalier** (aligné backtest `_hyb_live_kelly_compare`) |
| **Affichage** | « Top 5 » → **Top picks du jour** (TG, dashboard, CourtAlpha) ; commande `/top` (+ alias `/top5`) |
| **Filtre TG** | **Retiré sur `/top`** — plus de couche EV≥15 % / proba>60 % ; `/today` conserve son filtre value |
| **Replay** | `live_replay_engine`, `top5_replay.py`, backfill : `limit=None` par défaut |
| **Prod ops** | Retirer `TELEGRAM_TOP5_LIMIT=5` du `.env` (ou `0`) ; re-backfill publications si besoin |

---

# Ledger portfolio Top5 / 1D1P + reset Miouppy (24 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Suivi théorique** | Tables `portfolio_tracking_config` + `portfolio_daily_bets` — 1 ligne par pari publié, P/L Kelly séquentiel reconstructible |
| **CourtAlpha** | Replay lit le ledger si config active (`replay_mode: portfolio_ledger`) ; filtre ≥ `start_date` |
| **Hooks** | Publication TG → ledger ; settlement algo → refresh ledger |
| **Pick du jour** | Merge settlement DB (fix affichage « Open » alors qu’`Annulé` en base, ex. walkover Oliynykova) |
| **Prod reset** | Start **2026-07-24**, BR théorique **300 €** (Top5 + 1D1P) |
| **Miouppy** | 102 `user_bets` supprimés, archivés JSON ; BR web + TG **300 €** (sans ajustement manuel) |
| **CLI** | `init_portfolio_tracking.py`, `reset_user_portfolio.py` |

Doc : `docs/PORTFOLIO_TRACKING.md`.

### Réconciliation

| Script | Rôle |
|--------|------|
| `scripts/reconcile_portfolio_tracking.py` | Compare ledger vs `kelly_replay_metrics` ; `--fail-on-drift` pour cron |

---

| Fichier | Rôle |
|---------|------|
| `scripts/portfolio_tracking_store.py` | Schéma, sync publish/settle, recompute Kelly |
| `scripts/init_portfolio_tracking.py` | Init suivi théorique |
| `scripts/reset_user_portfolio.py` | Reset utilisateur + option portfolio |
| `scripts/live_replay_engine.py` | Sélection historique + priorité ledger |
| `deploy/courtalpha/api/services/one_day_one_pick.py` | Replay 1D1P ledger + settlement today |
| `deploy/courtalpha/api/services/top5_replay.py` | Replay Top5 ledger |
| `tests/test_portfolio_tracking_store.py` | Test ledger |

---

# Kelly **0,85** prod (24 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Changement** | Fraction Kelly de base **0,65 → 0,85** (`scripts/kelly_policy.py` → `KELLY_BASE_FRAC`) |
| **Plafond** | Inchangé — **15 %** BR / liquidité + ajustement Brier segment |
| **Backtest live replay** (≥ 18 mai, HYB, BR 100 €) | 0,65 → **238 €** · 0,85 → **271 €** (+33 €, DD 32 → 38 %) |
| **Juillet seul** | +7 € vs 0,65 (100 €) — gain marginal, caps souvent actifs |
| **YTD 2026** | Tous les mois positifs en 0,85–1,25 ; sweet spot backtest ~0,85–1,0 |
| **UI / bot** | Dashboard, Telegram `/strategy`, `_algo_kelly_stake_frac` — source unique `kelly_policy` |

Doc : `PREDICTION_ET_MISE.md` §6, `TELEGRAM_TOP5.md`.

---

# HYB P75+P80-all — sélection prod (24 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Règle prod** | **P75-TIER** (max 6/j) **+** compléments **P≥80 % rel≥80** (EV libre), dédup match, tri proba ↓ |
| **1D1P** | Meilleure **proba** dans l’union (pas rang 1 liste) — `hyb_p75_p80_best_proba` |
| **Legacy** | Ancienne hybride P77 tier1/tier2 : `select_hybrid_picks_legacy()` (backtests) |
| **Code** | `scripts/hyb_p75_p80_selection.py`, `select_hybrid_picks()` |

Backtest juillet 2026 (pool illimité, Kelly) vs hybride P77 : +720 € Σ2025, +12,4k € 2026, +95 € live.

Doc détaillée : `docs/HYBRID_PICK_SELECTION.md`, `docs/ONE_DAY_ONE_PICK.md`.

---

# Exclusion stats modèle par défaut (23 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Règle** | Aucun pari Top 5 / 1D1P / Paris du jour si **un seul** joueur a rank=100 & pts=1000 (valeurs ML par défaut) |
| **Cause** | Van Assche vs Carreno-Busta : un côté en défaut passait rel≥80 |
| **Code** | `match_has_any_default_player_stats`, `passes_public_pick_gates`, `stats_source=rank_points_default` |
| **UI** | Dashboard comparatif : `(défaut ML)` sur rang/points |
| **Prod** | ✅ déployé 23/07 — `bettinghud-dashboard`, `bettinghud-daemon`, `bettinghud-telegram-bot` |

Doc : `docs/DATA_RELIABILITY.md` (section score v5).

### Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/match_rank_quality.py` | Exclusion dure + flags `p*_default_model_stats` |
| `scripts/stats_engine.py` | Source `rank_points_default` si imputation |
| `scripts/daily_top_proba_store.py` | `match_has_rank_points_source` dans collect Top5 |
| `app/dashboard.py` | Badge `(défaut ML)` comparatif |
| `tests/test_match_rank_quality.py` | Cas un côté en défaut |

---

# Picks publiés + résolution algo sans portefeuille (23 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Replay web** | Table `daily_published_picks` — archive Top5/1D1P **au moment de l’envoi TG** (plus re-hybrid sur archive intraday) |
| **Exemple corrigé** | 22/07 : Droguet + Hanfmann affichés (plus Vacherot seul) |
| **Settlement soir** | `portfolio_results_daemon` scrape TE si picks algo ouverts, même sans pari `user_bets` |
| **Perf scrape** | Fenêtre 7 j · refresh TE forcé 2 j seulement |

### Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/published_picks_store.py` | Schéma + save/load replay |
| `scripts/scraper_results.py` | `open_algo_resolution_dates`, scrape sans portefeuille |
| `scripts/portfolio_results_daemon.py` | Déclenche scrape si picks algo 7j |
| `scripts/telegram_top5_notify.py` | Archive Top5 à l’envoi |
| `scripts/telegram_1d1p_notify.py` | Archive 1D1P à l’envoi |
| `scripts/backfill_published_picks.py` | Backfill manuel |
| `CourtAlpha/api/services/top5_replay.py` | Replay historique publié |
| `CourtAlpha/api/services/one_day_one_pick.py` | Idem 1D1P |
| `docs/PUBLISHED_PICKS_REPLAY.md` | Doc complète + diagramme daemons |

---

# Fallback rel≥80 si 0 pick (22 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Règle** | rel≥**85** par défaut ; si **0 pick** ce jour → repli rel≥**80** |
| **Backtest 2026** | +370 € flat vs +367 (85 seul) · fallback déclenché 1j historique (+Molcan W) |
| **Live aujourd'hui** | Publie Droguet + Hanfmann (rel=80, en cours) au lieu de 0 pick |
| **Pool** | `collect_top5_proba_picks` inclut rel≥80 ; `select_hybrid_picks` tente rel≥85 puis repli |

### Fichiers (fallback)

| Fichier | Rôle |
|---------|------|
| `scripts/hybrid_pick_selection.py` | `select_hybrid_picks`, `hybrid_criteria_*` |
| `scripts/daily_top_proba_store.py` | Pool candidats rel≥80 avant sélection |
| `scripts/discord_1d1p_core.py` | Comptage pool 1D1P aligné |
| `app/dashboard.py` | Onglet Top 5 — texte via `hybrid_criteria_plain` |
| `scripts/telegram_top5_notify.py` | `/help`, `/strategy` |
| `scripts/discord_1d1p_format.py` | Embed no-pick 1D1P |
| `scripts/discord_general_format.py` | Présentation canal Discord |

---

| Élément | Détail |
|---------|--------|
| **Changement** | Fiabilité **75 → 85** · tri **EV → proba** · cap **5 → 6 picks/j** |
| **Inchangé** | P≥77 % · gap≤30 pp · EV tier1 15–35 % · tier2 30–55 % |
| **Backtest 2026** | flat **+367 €** (+19 vs ancien) · Kelly Σ/mois **+1 176 €** (+103) · hit **84,0 %** |
| **Backtest 2025** | flat +467 € (+4) · quasi neutre |
| **Live ≥ 18 mai** | flat +125 € (+16) · Kelly Σ +312 € (+49) · hit **92,8 %** |

### Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/hybrid_pick_selection.py` | Constantes prod (`HYBRID_*`) |
| `scripts/daily_top_proba_store.py` | `collect_hybrid_proba_picks` |
| `scripts/pick_modes.py` | Web / TG / Discord Top 5 |
| `scripts/discord_1d1p_core.py` | 1D1P rang 1 |
| `scripts/telegram_top5_notify.py` | Bot TG matin + critères |
| `app/dashboard.py` | Onglet Top 5 hybride |
| `docs/HYBRID_PICK_SELECTION.md` | Référence règles |

---

# Ops — renfort jobs de nuit + alertes (16 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Anti-doublon TG** | `ops_telegram_alert` : cooldown 20 min (`BETTINGHUD_OPS_ALERT_COOLDOWN_SEC`), clé partagée QC FAIL ↔ Sync tours |
| **Cron wrapper** | `BETTINGHUD_IN_CRON_ALERT=1` ; corps d’échec enrichi (BLOCK/QC + `morning_chain_state`) |
| **QC sous cron** | Skip alerte FAIL inline (le wrapper cron porte le message) ; WARN QC toujours envoyé |
| **Sync tours** | Exception `qc_post_sync` → `rc=1` + alerte (plus de silence) |
| **05:00** | Soft-fail tours → alerte ops dédiée même si publish continue |
| **02:00** | `validate_build` post-build ; FAIL → exit 1 + alerte |
| **04:40** | Nouveau cron `preflight_morning_chain.py` (alerte si KO) |
| **04:15** | Backup wrappé `cron_run_with_alert` |
| **06:30 digest** | Résumé chaîne `tours_sync` / `qc_post_sync` du jour |

### Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/ops_telegram_alert.py` | Dedup + clés |
| `scripts/cron_run_with_alert.py` | Env cron + corps enrichi |
| `scripts/qc_post_sync.py` / `sync_tours_daily.py` | Alertes QC / exceptions |
| `scripts/morning_orchestrator.py` / `morning_live_pipeline.py` | Soft-fail + validate 02:00 |
| `deploy/cron/morning-pipeline` | + preflight 04:40 |
| `deploy/cron/ops-p0` / `data-sync` | Backup wrap + dedup-key sync |

---

# Ops — C1 WTA doublons + timezone cron Paris (16 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | `sync_tours_daily` en `exit 1` chaque nuit alors que sync ATP/WTA OK |
| **Cause C1** | Alias `Quevedo` → `lys e.` **sans remap `loser_id`** → 2 lignes Waltert/Bastad (IDs 220332 vs 259733) → gate `wta_c1_duplicates` bloquante dans `qc_post_sync` |
| **Cause TZ** | Serveur en `Etc/UTC` ; cron Ubuntu **ignore `CRON_TZ`** pour le scheduling → jobs « 05:00 Paris » tournaient à **05:00 UTC** (07:00 Paris) |
| **Fix code** | `wta_name_aliases.py` remappe les IDs ; `enrich_wta_delta_metadata` tie-break dédup ; `fix_wta_c1_duplicates.py` ; `install_ubuntu.sh` pose `Europe/Paris` |
| **Fix prod** | Dedup CSV + `timedatectl set-timezone Europe/Paris` + réinstall `/etc/cron.d/bettinghud-*` + suppression legacy `bettinghud-billing` (typos `scipts/…`) |

### Fichiers

| Fichier | Changement |
|---------|------------|
| `scripts/wta_name_aliases.py` | Remap `winner_id`/`loser_id` vers ID canonique de la cible d’alias |
| `scripts/enrich_wta_delta_metadata.py` | Tie-break dédup : score puis IDs croissants |
| `scripts/fix_wta_c1_duplicates.py` | Outil one-shot alias+dedup+QC |
| `scripts/sync_tours_daily.py` | Dedup post-Flashscore (déjà local — déployer si absent prod) |
| `deploy/install_ubuntu.sh` | `timedatectl set-timezone Europe/Paris` |
| `docs/CRONS_SEMAINE.md` | Doc limite `CRON_TZ` Ubuntu |

---

# Hybride COMBO_VOLUME — tiers EV élargis (15 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Objectif** | Promouvoir le candidat **COMBO_VOLUME** (grille `backtest_top5_scenario_grid --grid combo`, juil. 2026) en prod |
| **Changement** | EV tier1 **15–30 % → 15–35 %** · tier2 **30–50 % → 30–55 %** (P77, rel≥75, gap≤30, tri EV, max 5/j **inchangés**) |
| **Backtest** | 2025+2026 Kelly : **même DD** que P77, **+206 k€ / +11 k€** Kelly, flat **+35 / +21 u** vs ancien tiers |
| **Sizing** | Kelly **0,65** inchangé — seule la **sélection** change |

### Fichiers

| Fichier | Changement |
|---------|------------|
| `scripts/hybrid_pick_selection.py` | `HYBRID_TIER1_EV_MAX_PCT=35`, `HYBRID_TIER2_EV_MAX_PCT=55` |
| `scripts/backtest_top5_scenario_grid.py` | Defaults scénario PROD alignés 35/55 |
| UI / comms | `dashboard.py`, `telegram_top5_notify.py`, `discord_*` (textes critères via constantes ou doc) |
| `docs/HYBRID_PICK_SELECTION.md`, `TELEGRAM_TOP5.md`, `ONE_DAY_ONE_PICK.md`, `BACKTEST_OPTIMIZATION_JUIL2026.md` | Doc prod |

### Déploiement prod

```bash
scp scripts/hybrid_pick_selection.py scripts/backtest_top5_scenario_grid.py \
    scripts/telegram_top5_notify.py scripts/discord_general_format.py \
    scripts/discord_1d1p_core.py app/dashboard.py \
    bettinghud:/opt/bettinghud/
# Puis restart API / bot / dashboard (cf. HYBRID_PICK_SELECTION.md)
```

---

# Challenger hybride Top 5 / 1D1P — P77 (15 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Objectif** | Remplacer l’ancien Top 5 (P80, rel≥80, tri proba) par la variante **challenger** validée en backtest Kelly 2026 |
| **Règles** | P≥**77 %** · fiabilité **≥75** · gap book **≤30 pp** · EV tier1 **15–30 %** + tier2 **30–50 %** · tri **EV** ↓ · max **5/j** |
| **1D1P** | **Rang 1** de la même sélection hybride (plus de logique circuit séparée) |
| **Live `/jour`** | **Inchangé** (`PickMode.TODAY` — value bets EV≥15 %, tous tournois) |
| **Backtest 2026** | Kelly ~**+22 k€** vs prod P80 ~+15 k€ (même DD ~16 %, +40–50 picks) |

### Fichiers

| Fichier | Changement |
|---------|------------|
| `scripts/hybrid_pick_selection.py` | P77, rel 75, gap 30 pp, tri EV |
| `scripts/daily_top_proba_store.py` | `collect_hybrid_proba_picks` + `min_reliability_score` |
| `scripts/discord_1d1p_core.py` | Pool hybride + rang 1 |
| `scripts/telegram_top5_notify.py` | Texte critères dynamique (`hybrid_criteria_line`) |
| `app/dashboard.py` | Onglet Paris du jour → `collect_hybrid_proba_picks` |
| `docs/HYBRID_PICK_SELECTION.md`, `TELEGRAM_TOP5.md`, `ONE_DAY_ONE_PICK.md` | Alignement doc |

### Déploiement prod

```bash
scp scripts/hybrid_pick_selection.py scripts/daily_top_proba_store.py \
    scripts/pick_modes.py scripts/discord_1d1p_core.py scripts/discord_1d1p_format.py \
    scripts/telegram_top5_notify.py bettinghud:/opt/bettinghud/scripts/
ssh bettinghud "sudo systemctl restart bettinghud-telegram-bot"
```

---

# Alignement affichage public + incident Waltert (14 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | Waltert S. vs Kawa K. affiché **95,9 %** sur app / TG / Discord (notif ~07:27) alors qu’un diagnostic local PREPROD montrait **70,9 %** et exclusion Top 5 |
| **Cause #1 (diagnostic)** | Vérification initiale sur **PREPROD locale** (`data/bettinghud.db` + snapshot cache PC) au lieu du serveur `/opt/bettinghud` — faux écart perçu |
| **Cause #2 (code)** | `live_tracker_picks.py` utilisait `p_model = 1 / true_odd` au lieu de `capped_p1_prob` → désync possible **Live Tracker `/jour`** vs Top 5 quand `true_odd_*` stale |
| **Cause #3 (prod réelle)** | Après rebuild snapshot prod, **95,9 %** est la sortie modèle v47 cohérente (`capped_p1_prob` ≈ implied `true_odd` ~1,04) — pas un artefact d’affichage sur Top 5 |
| **Régression TG (midi)** | `passes_public_pick_gates()` appelé sur lignes Top5 **sans** `feature_snapshot` → **0 pick hybride** TG jusqu’au hotfix `_is_materialized_pick_row` |

### Correctifs code (commits `808d491` + hotfix 14/07)

| Fichier | Changement |
|---------|------------|
| `scripts/match_rank_quality.py` | `passes_public_pick_gates`, `capped_p1_prob_from_match`, `model_prob_for_side`, `match_model_odds_inconsistent`, `reconcile_match_true_odds_from_caps` ; fiabilité **v4** ; hotfix lignes matérialisées Top5 |
| `scripts/live_tracker_picks.py` | Proba / Kelly via `model_prob_for_side` (plus `1/true_odd`) |
| `scripts/daily_top_proba_store.py` | Filtres publication via `passes_public_pick_gates` sur match brut |
| `scripts/hybrid_pick_selection.py` | `hybrid_base_ok` via gates publics |
| `scripts/telegram_top5_notify.py` | `filter_telegram_display_picks` via gates publics |
| `app/dashboard.py` | Top 5 Action aligné ; reconcile caps après predict |
| `scripts/live_snapshot.py` | Normalisation caps au chargement |
| `scripts/morning_orchestrator.py` | Invalidation cache TG avant publish |
| `scripts/qc_live_snapshot.py` | QC cohérence caps / `true_odd_*` |

### Déploiement prod (14/07/2026 matin)

1. `scp` des 10 fichiers ci-dessus → `/opt/bettinghud/`
2. `scripts/rebuild_live_projection.py` (~13 min, 75 matchs)
3. `systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot`
4. Hotfix `match_rank_quality.py` (lignes matérialisées) + `git pull` + restart bot

### Règle opérationnelle

| Canal | Source données PROD |
|-------|---------------------|
| Dashboard, TG, Discord | `/opt/bettinghud/data/bettinghud.db` + `data/cache/live_matches_snapshot*.joblib` |
| PREPROD locale | **Ne jamais** utiliser pour valider un pick prod sans `ssh bettinghud` ou `scp` DB/snapshot |

### Scripts diagnostic

```bash
# Snapshot vs DB vs picks runtime (PROD)
ssh bettinghud "cd /opt/bettinghud && BETTINGHUD_ENV=prod BETTINGHUD_HEADLESS=1 ./venv/bin/python scripts/_diag_waltert_display.py"

# Funnel hybride / filtres TG (PROD)
ssh bettinghud "cd /opt/bettinghud && BETTINGHUD_ENV=prod BETTINGHUD_HEADLESS=1 ./venv/bin/python scripts/_diag_tg_waltert.py"

# Dry-run Top 5 TG
ssh bettinghud "cd /opt/bettinghud && BETTINGHUD_ENV=prod ./venv/bin/python scripts/telegram_top5_notify.py --dry-run"
```

### Waltert 14/07 — verdict prod

- Proba modèle : **95,9 %** · EV **+38 %** · fiabilité **100** (flag `book_gap_high` seul)
- **Seul pick hybride** du jour (P≥80 %, EV tier2 30–50 %) — les autres favoris ont EV >50 % ou P<80 %
- **Pas** publié en 1D1P si EV hors bandes hybrides au moment du publish matin (selon snapshot de 05:00)

---

# WTA — classements post-Sackmann + fix `book_gap_pp` (10 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Contexte** | Archive Sackmann figée au **2026-06-08** (repo GitHub 404) — `wta_rankings_current.csv` ne se mettait plus à jour |
| **Refresh rangs** | `scripts/refresh_wta_rankings_current.py` : dernier rang/points par joueuse dans `wta_matches*` + cache TE si plus récent → `ingest_rankings_current.py` |
| **Crons** | `sync_tours_daily` (03:30) et `morning_live_pipeline` (02:00) avant build snapshot |
| **stats_engine** | `_overlay_wta_current_rankings` : overlay seulement si `ranking_date ≥ stats_reference_date` du match |
| **book_gap_pp** | `dashboard.py` : passer `feature_snapshot` à `_match_snapshot_quality_flags` (évite faux `book_gap_high` ~32 pp) |
| **Prod** | `MAX(ranking_date)` → **2026-07-10** ; Vandromme rank **161** (ref 9 juil.) ; rebuild snapshot recommandé après déploiement |

---

# WTA — backfill rangs, pont FS qual/ITF, retrain ML (10 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Backfill rangs** | `backfill_wta_delta_ranks.py` + TE cache dans `fill_ranks_if_missing` ; cron : Flashscore **puis** backfill |
| **Pont qual/ITF** | `sync_wta_flashscore_results` : prematch TE **WTA+ITF** → `wta_matches_qual_itf_*` (+ rangs à l’insertion) |
| **Probe source** | `_probe_tdcuk_wta_tiers.py` — verdict `source_no_itf_in_xlsx` (tennis-data sans ITF) |
| **Prod données** | qual/ITF max **2026-07-10** (ex-06-02) ; rangs post-cutoff **1256/1510** |
| **Retrain** | `update_model_tml.py --min-year 2020 --skip-sync` · Brier `tour_WTA` **0.1692** (ex-0.1718) |
| **Modèle joint ATP+WTA** | Brier global **0.1830** (+0.0081) et `tour_ATP` **0.1906** (+0.0131) — split 80/20 commun + features partagées ; rollback : `xgb_model_tml_v47.pkl.elo_backup` |
| **Vérif** | `_probe_ml_bundle_brier.py` · `_probe_tdcuk_wta_tiers.py` · `_probe_ml_wta_rows.py` |

---

# WTA données P0 — alias noms, serve retry, QC gates, ATP FS live (10 juillet 2026)

| Élément | Détail |
|---------|--------|
| **P0-A alias** | `config/wta_name_aliases.json` + `scripts/wta_name_aliases.py` — corrections tennis-data (ex. Quevedo→Lys) ; intégré dans `fill_ranks`, matching Flashscore |
| **P0-B serve** | 2ᵉ passe `enrich_wta_delta_te_stats.py --main-tour-only` après backfill rangs (lignes `w_svpt` NULL) |
| **P0-C QC** | `scripts/wta_delta_qc_gates.py` (C1 doublons, D1 rangs main, couverture SQLite) → fusionné dans `qc_post_sync` + alerte TG ops |
| **P0-D ATP live** | `stats_engine` : pont `matches_recent` `source='flashscore'` si match TML absent (dédup par date+noms) |
| **Pipeline** | `sync_tours_daily` : aliases avant metadata + après Flashscore ; retry serve main ; `qc_post_sync` alerte si FAIL/WARN |

### Pipeline WTA quotidien (prod, depuis P0 10/07/2026)

```
03:30 sync_tours_daily
  sync_wta_delta
  → wta_name_aliases.py
  → enrich_wta_delta_metadata --dedup
  → refresh_wta_rankings_current --ingest
  → enrich_wta_delta_te_stats
  → sync_wta_flashscore_results (main + qual/ITF)
  → backfill_wta_delta_ranks
  → wta_name_aliases.py (post-FS)
  → enrich_wta_delta_te_stats --main-tour-only (retry serve)
  → pipeline_quality (ingest SQLite)
  → build_feature_store + refresh_elo_maps_fast
  → qc_post_sync (+ wta_delta_qc_gates, alerte TG si FAIL)

02:00 morning_live_pipeline
  → refresh_wta_rankings_current + ingest_rankings_current
  → scrape + rebuild snapshot
```

---

# Ops PROD — doublon cron matin supprimé (10 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | Build 02:00 et publish 05:00 en échec partiel (`code 1` / `code 2`, verrou snapshot) alors que la chaîne P0 finissait en succès |
| **Cause** | Deux fichiers cron actifs : legacy `bettinghud-morning` (juin) + P0 `bettinghud-morning-pipeline` (juil.) — même horaire |
| **Correctif** | `sudo rm -f /etc/cron.d/bettinghud-morning` en prod ; `install_ubuntu.sh` supprime ce legacy à l’install |
| **Canonique** | `deploy/cron/morning-pipeline` → `/etc/cron.d/bettinghud-morning-pipeline` |

---

# Incident PROD CourtAlpha — picks publiés absents de l'historique (8 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme utilisateur** | Pick **Kostyuk (06/07)** vu sur Telegram/Discord mais absent de `/api/picks/top5-replay` et `/api/picks/one-day-one-pick` |
| **Cause racine** | `daily_top_proba_picks` est un état **upsert par `pick_key={date|tour|rank}`** ; des captures intraday tardives écrasaient les picks publiés le matin |
| **Preuve** | `data/exports/daily_top_proba/2026-07-06.jsonl` contient plusieurs captures avec Kostyuk (matin), mais la table SQL finale ne le contient plus |
| **Correctif prod (final)** | 1) Replay historique **revenu sur `daily_top_proba_picks` SQL** (pas de remplacement global par JSONL, qui avait réduit l'historique fin mai). 2) **Backfill ciblé** du snapshot publication du `2026-07-06` depuis JSONL. 3) **Verrou durable** dans `upsert_daily_top_proba_picks` : après capture matin (`>= 05:00` Paris), les passes intraday ne peuvent plus écraser le match publié au même `pick_key` |
| **Impact** | Historique complet restauré (`start_date=2026-05-18`, ~26–27 jours) **et** Kostyuk du 06/07 visible en Top5 + 1P1D ; régression bloquée à la source |
| **Compat serveur** | Fallback ajouté si module optionnel `scripts.reliability_pick_match` absent (évite 500) |
| **Validation** | `top5_replay` : 58 picks / 27 jours ; `one_day_one_pick` : 26 picks ; `Krueger A. vs Kostyuk M. (12)` au `2026-07-06` en **Gagné** |

### Vérification post-fix (prod)

```bash
ssh bettinghud "cd /opt/courtalpha && /opt/bettinghud/venv/bin/python - <<'PY'
import sys
sys.path.insert(0,'/opt/courtalpha')
sys.path.insert(0,'/opt/bettinghud')
from api.services.top5_replay import build_top5_replay
from api.services.one_day_one_pick import build_one_day_one_pick_replay
p5=build_top5_replay(db_path='/opt/bettinghud/data/bettinghud.db')
p1=build_one_day_one_pick_replay(db_path='/opt/bettinghud/data/bettinghud.db')
print([r for r in p5['picks'] if str(r.get('calendar_date'))=='2026-07-06'])
print([r for r in p1['picks'] if str(r.get('calendar_date'))=='2026-07-06'])
PY"
```

---

# Incident PROD CourtAlpha — replay `Best pick` / `Top 5 probas > historique` (8 juillet 2026)

| Élément | Détail |
|---------|--------|
| **Symptôme** | `GET /api/picks/top5-replay` renvoie **500** dans CourtAlpha ; écran « Top 5 probas > historique » vide |
| **Route touchée** | `api/routes/picks.py` → `build_top5_replay()` (`/opt/courtalpha`) |
| **Cause racine #1** | Dépendance Python absente côté serveur : `api/services/top5_replay.py` importe `scripts.backtest_prod_top5_2026`, mais le fichier n'était pas présent sous `/opt/bettinghud/scripts/` |
| **Cause racine #2** | Résolution du bundle ML fragile hors repo BettingHUD : `TennisMLModel.model_path` restait relatif (`models/xgb_model_tml_v47.pkl`) ; depuis `/opt/courtalpha`, `load_1d1p_today_pick()` cherchait donc le bundle au mauvais endroit |
| **Impact connexe** | `/api/picks/one-day-one-pick` pouvait aussi tomber en erreur « Modèle non entraîné et non trouvé » pour la même raison de chemin relatif |
| **Vérification** | `journalctl -u courtalpha-api` : `ModuleNotFoundError: No module named 'scripts.backtest_prod_top5_2026'` |
| **Correctif code** | `scripts/ml_model.py` résout désormais les bundles relatifs **depuis la racine du projet BettingHUD**, pas depuis le `cwd` appelant |
| **Correctif déploiement** | Déployer aussi `scripts/backtest_prod_top5_2026.py` sur PROD avant restart `courtalpha-api` |

### Commandes de diagnostic utiles

```bash
ssh bettinghud "journalctl -u courtalpha-api -n 80 --no-pager"
ssh bettinghud "ls /opt/bettinghud/scripts/backtest_prod_top5_2026.py"
ssh bettinghud "cd /opt/courtalpha && /opt/bettinghud/venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '/opt/courtalpha')
sys.path.insert(0, '/opt/bettinghud')
from api.services.top5_replay import build_top5_replay
print(build_top5_replay(db_path='/opt/bettinghud/data/bettinghud.db')['period'])
PY"
```

---

# Exploration split ATP / WTA — niveau 1 (PREPROD uniquement)

| Élément | Détail |
|---------|--------|
| **Statut** | **Exploration PREPROD** — aucun déploiement PROD, aucun impact publication Top 5 / 1D1P |
| **Objectif** | Tester si un **bundle WTA candidat** (delta Sackmann + retrain) améliore le circuit WTA **sans dégrader l’ATP**, plutôt que de promouvoir un bundle unifié |
| **Hypothèse** | Un retrain global sur données WTA enrichies peut améliorer `tour_WTA` mais dégrader `tour_ATP` / `ATP_Clay` ; le routage niveau 1 isole l’effet WTA |
| **Mécanisme** | `TourModelRouter` (`scripts/ml_tour_router.py`) : **v47 baseline pour ATP**, **`xgb_wta_delta_candidate.pkl` pour WTA** |
| **Garde-fou PROD** | `BETTINGHUD_ENV=prod` → routage **toujours ignoré**, même si `BETTINGHUD_ML_TOUR_ROUTING=1` |
| **Doc opérationnelle** | **`docs/ML_BUNDLE_ROLLBACK.md`** § Routage ATP/WTA · **`docs/PREDICTION_ET_MISE.md`** § 1bis (distinction BO3/BO5 vs ATP/WTA) |
| **Code** | `ml_tour_router.py`, `ml_bundle_cli.py tour-routing`, `ml_model.model_for_inference()` |
| **Tests** | `tests/test_ml_tour_router.py` (garde-fou PROD, enable/disable config) |

### Protocole d’évaluation (PREPROD)

| Étape | Script | Rôle |
|-------|--------|------|
| 1. Activer routage | `ml_bundle_cli.py tour-routing on` + `BETTINGHUD_ML_TOUR_ROUTING=1` | Écrit `models/.ml_tour_routing_preprod.json` |
| 2. Smoke | `preprod_tour_routing_smoke.py` | Vérifie chargement bundles + cohérence facade / backend |
| 3. Hold-out Brier | `shadow_wta_candidate_replay.py` | Compare `segment_brier_scores` v47 vs candidat (`tour_WTA`, `tour_ATP`, mix pondéré) |
| 4. Replay Top 5 | `preprod_tour_routing_replay.py` | Hybride cap 5 : v47 unifié vs routé ATP/WTA (mode rapide ou `--full-feature-store`) |
| 5. Rollback | `ml_bundle_cli.py tour-routing off` | Supprime la config PREPROD |

DB PREPROD typique : `data/preprod/bettinghud_wta_delta.db` (pipeline `run_wta_delta_preprod.py`).

### Contexte Brier (référence juin 2026, voir `WTA_SACKMANN_ARCHIVE.md`)

| Bundle | `global_test_brier` | `tour_WTA` |
|--------|---------------------|------------|
| v47 prod (unifié) | 0,1749 | 0,1718 |
| v47 pré-delta (rollback) | 0,1816 | 0,1664 |

Le candidat WTA delta peut être **meilleur en WTA** tout en **dégradant le global** (effet ATP) — d’où l’exploration routage avant tout `promote` unifié.

### Décision attendue (non tranchée)

| Option | Quand |
|--------|-------|
| **Routage niveau 1 en PROD** | Si replay Top 5 2026 routé ≤ v47 **et** `tour_WTA` candidat ≤ baseline — **pas encore validé** |
| **`promote` bundle unifié** | Si gate J6 PASS **et** ATP non dégradé (seuils `ML_BUNDLE_ROLLBACK.md`) |
| **Abandon candidat WTA** | Si routage et bundle unifié échouent aux gates |

Les **résultats chiffrés** de chaque run replay restent dans la sortie console des scripts ; les consigner ici après un run significatif.

---

# Session documentation & QA — 8 juillet 2026

| Action | Statut doc | Détail |
|--------|------------|--------|
| **Audit QA anti-fuite ML** (`minutes_played_last7d`, `tb_win_pct_52w`, ordre `hist.append`) | ✅ § ci-dessous + `PREDICTION_ET_MISE.md` §2 | Aucune fuite constatée sur les trois points vérifiés |
| **Backtest utilisable sans modif** | ✅ § 5 + smoke test ci-dessous | `backtest_2026.py` aligné v47 ; features manquantes → 0.0 |
| **Smoke test `build_dataset_with_identity`** | ✅ ci-dessous | 87 829 matchs → 175 658 lignes ; `missing_features []` ; `na_in_features 0` |
| **Backup projet complet** | ✅ § 6 | `create_full_project_backup.py` + `RESTAURATION.md` dans le ZIP |
| **Mise à jour docs** | ✅ | `ARCHITECTURE.md`, `PREDICTION_ET_MISE.md`, `MODELE_V45_*`, ce fichier |
| **Rollback bundle ML** | ✅ | `docs/ML_BUNDLE_ROLLBACK.md` (index `Home.md`) |
| **Exploration split ATP/WTA (PREPROD)** | ✅ | `CHANGELOG_RECENT.md` § dédié · `ML_BUNDLE_ROLLBACK.md` · `PREDICTION_ET_MISE.md` § 1bis · `ENVIRONNEMENTS.md` |

### Audit QA ML — causalité features temporelles (juillet 2026)

Vérification manuelle du pipeline `scripts/ml_model.py` (`_build_temporal_features`) après gain Brier ~0,174 :

1. **`minutes_played_last7d_diff`** : `_sum_mins_last7d` ne lit que `hist[pid]` ; le match courant n’est ajouté à `hist` qu’**après** le calcul des features de la ligne → pas d’inclusion des minutes du match en cours.
2. **`tb_win_pct_52w_diff`** : `_tb_win_pct_gliding` ne lit que `hist_tb` ; `_infer_tiebreaks_from_score(row.score)` sert à l’**append** post-features, pas au calcul de `w_tb52` / `l_tb52` pour la ligne courante.
3. **Ordre deque** : section « update with current match » — `hist` / `hist_tb` / `hist_clutch` **append** systématiquement **après** toutes les features pré-match de la ligne.

**Verdict** : pipeline cohérent sans correction de code requise pour ces trois points. Hardening optionnel : borne stricte `x[0] < ref_dt` ou exclusion par `match_id` (défense en profondeur, pas nécessaire tant que l’ordre append est respecté).

### Smoke test backtest dataset (12 mai 2026, rejouable)

```powershell
py -3 -c "import sys, pandas as pd; sys.path.insert(0, '.'); from scripts.ml_model import TennisMLModel; from scripts.backtest_2026 import build_dataset_with_identity; ml = TennisMLModel('data/bettinghud.db'); ds, _, _ = build_dataset_with_identity(ml); print('missing', [f for f in ml.features if f not in ds.columns]); print('rows', len(ds))"
```

Résultat attendu : `missing []`, `rows` ≈ 175 658 (double orientation P1/P2). Durée ~12 min sur machine de dev. Warning pandas « fragmented DataFrame » (ligne ~248 `backtest_2026.py`) : cosmétique perf, non bloquant.

---

# Sélection hybride Top 5 / 1 Day 1 Pick — juillet 2026

| Livrable | Détail |
|----------|--------|
| **Règle** | P≥80 %, rel≥80, tier1 EV 15–30 %, tier2 EV 30–50 % (complément), max 5/jour, tri proba ↓ |
| **Top 5** | `collect_hybrid_proba_picks()` via `pick_modes.TOP5` |
| **1D1P** | Rang 1 de la même sélection hybride (`load_1d1p_today_pick`) |
| **Code central** | `scripts/hybrid_pick_selection.py` |
| **Hors périmètre** | `/jour` Live Tracker, Paris du jour mineurs — logique value bet inchangée |
| **Doc** | **`docs/HYBRID_PICK_SELECTION.md`** · **`docs/TELEGRAM_TOP5.md`** § 3.4 · **`docs/ONE_DAY_ONE_PICK.md`** |
| **Backtest 2026** | Hybride cap 5 : +296 € flat, hit 87,5 % (vs prod ancien +265 €, 65,8 %) |
| **CourtAlpha** | Aligner `one_day_one_pick.py` replay sur hybride (dépôt CourtAlpha) |
| **Deploy** | `courtalpha-api`, `bettinghud-telegram-bot`, `bettinghud-dashboard` |

---

# Mise en place Shadow Test Top 5 — 7 juillet 2026

| Livrable | Détail |
|----------|--------|
| **Objectif** | Tester une stratégie candidate en prod **sans impacter** la publication Top 5 / 1D1P |
| **Stratégie candidate** | `top5_ev25_rel85_p80` : P≥80 %, EV≥25 %, rel≥85, cap 5/jour, tri proba ↓ |
| **Code** | `scripts/shadow_top5.py` (capture, sync résultats, reporting) |
| **Stockage** | Nouvelle table SQLite `shadow_top5_picks` (`data/bettinghud.db`) |
| **Orchestration** | `scripts/morning_orchestrator.py` : capture shadow non bloquante après publications 05:00 |
| **Hebdo admin** | `scripts/shadow_weekly_telegram_notify.py` + cron `deploy/cron/shadow-weekly-telegram` |
| **A/B shadow** | Variantes `top5_ev25_rel85_p80` (A) et `top5_p80_ev15_30_rel80` (B), comparées à prod avec recommandation `KEEP/TEST+/SWITCH` |
| **Doc** | `docs/SHADOW_TEST_TOP5.md` |

---

## 0. Mise à jour 6 juillet 2026 — Fiabilité data v3 déployée PROD

| Livrable | Détail |
|----------|--------|
| **Score v3** | `data_reliability_version=3` : `hist_te_soft` (−8), duplicate par `(proba, tournoi)`, malus `ref_date_stale` limité aux références > 12 mois |
| **Code** | `scripts/match_rank_quality.py`, `scripts/reliability_context.py`, `scripts/daily_top_proba_store.py`, `app/dashboard.py` |
| **Diagnostic** | `scripts/diagnose_reliability_funnel.py` (snapshot live) |
| **A/B historique** | `scripts/compare_reliability_v3_backtest.py` (pool, picks, ROI ; interprétation prudente) |
| **Déploiement** | rebuild complet `scripts/rebuild_live_projection.py` + restart services prod |
| **Résultat PROD (06/07)** | Snapshot v3 reconstruit : 23 matchs raw, **18 rel ≥ 80**, 8 value bets EV ≥ 15 %, Top 5 hybride 0 (règles P/EV) |

---

## 0. Mise à jour 3 juillet 2026 — Kelly **0,65** (ex-½)

| Livrable | Détail |
|----------|--------|
| **Règle** | Fraction Kelly de base **0,65** × facteur Brier segment, plafond **15 %** BR / liquidité |
| **Code** | `scripts/kelly_policy.py` → `KELLY_BASE_FRAC` ; `bets_db._algo_kelly_stake_frac` ; `simulate_top10_proba_2026.KELLY_BASE` ; dashboard `KELLY_RECO_ADAPTIVE_BASE_FRAC` |
| **Périmètre** | Top 5, 1D1P, Telegram **Bet**, Live Tracker, CourtAlpha (reco mise) |
| **Backtest 2026 Top5** | +16 714 € Kelly (vs +6 377 € en 0,5), DD 16,8 % (vs 13,1 %) |
| **Doc** | `PREDICTION_ET_MISE.md` · `TELEGRAM_TOP5.md` · `ONE_DAY_ONE_PICK.md` |

---

**Docs** : **`docs/DATA_RELIABILITY.md`** (§ `duplicate_model_prob`) · **`docs/TELEGRAM_TOP5.md`** § 3.4 · **`docs/BACKTEST_PROD_TOP5_2025_2026.md`** § 2

| Livrable | Détail |
|----------|--------|
| **Règle** | Les matchs du cluster **`duplicate_model_prob`** (même `capped_p1_prob` sur ≥ 2 matchs distincts) sont **exclus de la publication Top 5** — pas seulement pénalisés au score |
| **Code** | `scripts/match_rank_quality.py` → `has_duplicate_model_prob_flag()`, `excluded_duplicate_model_prob_from_top5()` |
| **Sélection** | `collect_top5_proba_picks()` · `filter_telegram_display_picks()` · backtest `backtest_prod_top5_2026.py` |
| **Périmètre** | Top 5 Telegram matin, `/top5`, API CourtAlpha `/api/picks/top5`, **1 Day 1 Pick** — **pas** Paris du jour `/jour` |
| **Backfill rang** | Le pick suivant au tri proba ↑ remplace le slot libéré (max 5/jour conservé) |
| **Backtest 2026** | vs prod actuel sur l’année : flat **+2 €**, hit **+5,4 pp**, DD **−8,8 pp** ; exclusion `dup` seule **+20 €** flat (voir analyse juillet 2026) |
| **Tests** | `tests/test_daily_top_proba_store.py` · `tests/test_match_rank_quality.py` |
| **Prod** | Scripts déployés + restart `courtalpha-api`, `bettinghud-telegram-bot`, `bettinghud-dashboard` |

**Contexte** : la pénalité score (`BETTINGHUD_DUP_PROB_PENALTY`, défaut **−20**) laissait passer des picks à rel 80 (ex. Wimbledon R1 juin 2026, hit ~38 % sur 8 picks live). L’exclusion publication est validée sur **476 picks** 2026 hybride, pas sur la seule fenêtre post-lancement (~96 picks).

---

## 0. Mise à jour 26 juin 2026 — 1D1P repli EV+ si proba &lt; 70 %

**Docs** : **`docs/ONE_DAY_ONE_PICK.md`** · **`docs/DISCORD_1D1P.md`**

| Livrable | Détail |
|----------|--------|
| **Règle** | Pick standard EV 15–100 % ; si `p_model_fav` &lt; 70 % → repli premier candidat **EV &gt; 0** par circuit (cap 100 %) |
| **Code** | `scripts/discord_1d1p_core.py` → `select_1d1p_pick()` (TG, Discord, web live via `load_1d1p_today_pick`) |
| **Replay web** | `CourtAlpha/api/services/one_day_one_pick.py` — `_select_one_pick_per_day` aligné |
| **Backtest** | `scripts/backtest_prod_1d1p_2026.py` |
| **Republication** | `scripts/repost_1d1p_today.py --apply` (Discord + canal TG + bot) |

---

## 0. Mise à jour juin 2026 (c) — Backtest Top 5 prod 2025/2026 + couverture fiabilité

**Doc** : **`docs/BACKTEST_PROD_TOP5_2025_2026.md`** · **`docs/DATA_RELIABILITY.md`** (§ persistance étendue)

| Livrable | Détail |
|----------|--------|
| **Backtest prod** | `scripts/backtest_prod_top5_2026.py` — logique `collect_top5_proba_picks` (pas Pack 1/2) ; `--year 2025` ou `2026` |
| **Cadre favori CSV** | `enrich_favorite_rows()` dans le backtest (aligné live) |
| **Fiabilité DB** | `scripts/backfill_db_reliability_scores.py` — backfill prod 855/855 picks live |
| **Fiabilité CSV** | `enrich_backtest_csv_reliability.py` + `backtest_csv_pick_rows.py` |
| **Persistance capture** | `ensure_match_reliability_scored()` ; `bets_db.py` COALESCE |
| **Audit hit rate** | `scripts/_audit_backtest_2025_hit_rate.py` — biais sélection / orientation CSV |
| **Supprimé** | `_run_prod_top5_2026_backtest.py` (utilisait Pack 1 par erreur) |

Résultats synthèse : 2026 partiel **+352 € flat** (290 picks) ; 2025 **+1 730 € flat** (651 picks) — interpréter hit rate avec prudence (voir doc).

---

## 0. Mise à jour 23 juin 2026 (b) — Filtre fiabilité data ≥ 80

| Livrable | Détail |
|----------|--------|
| **Filtre** | `passes_data_reliability_filter` sur Top 5, 1D1P, Paris du jour, Live Tracker, algo report |
| **Seuil** | `BETTINGHUD_MIN_DATA_RELIABILITY=80` (défaut) |
| **Persistance** | `data_reliability_score` + `data_reliability_flags` dans `daily_top_proba_picks` et `algo_opportunities` |
| **Doc** | `docs/DATA_RELIABILITY.md`, critères Telegram (`comms_locale`) |

---

## 0. Mise à jour 23 juin 2026 — Fiabilité données live (tier 3)

**Doc** : **`docs/DATA_RELIABILITY.md`**

| Livrable | Détail |
|----------|--------|
| **Snapshot** | Clé enrich `(p1, p2, tournoi, prematch_id)` ; repredict si IDs joueurs changent ; `feature_snapshot` deep-copié |
| **Rang vs proba** | Gap 80 → **30** + cas haute proba (ex. Gibson–Keys) |
| **WTA homonymes** | `_wta_name_to_ids` + disambiguation slug Tennis Explorer |
| **Rang placeholder** | `rankings_wta_current` ignoré si rank ≥ 1500 ou pts < 10 |
| **Score fiabilité** | `data_reliability_score` 0–100 + `data_reliability_flags` sur chaque ligne snapshot (`match_rank_quality.py`) |
| **PROD** | Rebuild complet `rebuild_live_projection.py` après déploiement |

---

## 0. Mise à jour juin 2026 — Bot Telegram @CourtAlphabot

**Doc** : **`docs/TELEGRAM_TOP5.md`**, **`docs/COMMS_LOCALE.md`**

| Livrable | Détail |
|----------|--------|
| **Rename** | `@BettingHUDbot` → **`@CourtAlphabot`** (display name CourtAlpha) |
| **Code** | `scripts/comms_locale.py` — défaut `TELEGRAM_BOT_USERNAMES=CourtAlphabot` |
| **Discord** | `scripts/discord_general_format.py` — liens `t.me` alignés |
| **Docs** | `TELEGRAM_TOP5.md`, `TELEGRAM_CHANNEL_ACQUISITION.md`, `COMMS_LOCALE.md`, `README.md` |
| **Outillage** | `scripts/patch_env_telegram_bot.py` — mise à jour `.env` PROD |
| **PROD** | `/opt/bettinghud/.env` : nouveau token + username ; restart `bettinghud-telegram-bot` |

Voir aussi § **0.18** (historique mai 2026, bot d’origine `@BettingHUDbot`).

---

## 0. Mise à jour 12 juin 2026 — 1 Day 1 Pick multi-canal

**Doc de référence** : [[ONE_DAY_ONE_PICK]]

| Livrable | Détail |
|----------|--------|
| Publication auto | **07:05 Paris** : pick TG (broadcast) + Discord via `od1p_publish.py` |
| Résultats auto | Daemon portfolio : `publish_1d1p_results()` → TG + Discord |
| Discord track record | Message unique édité quotidiennement (`--performance-board`) |
| Telegram UX | Menu clavier, onboarding accès, âge snapshot visible |
| Parité EV | Telegram **EV ≥ 15 %** aligné web/Discord |
| SQLite | `open_db()` WAL + `busy_timeout` ; lock JSON registre paris TG |
| Dédup | Snapshot TE doublons → `dedupe_top_proba_rows_by_match` |
| Affichage web | Historique picks **date décroissante** (récent en haut) |
| Tables | `telegram_1d1p_posts`, `discord_1d1p_posts` |
| Tests | `test_1d1p_selection`, `test_telegram_menu_freshness` |

---

## 0. Mise à jour 9 juin 2026 — Déploiement PROD + Top 5 interactif

| Livrable | Détail |
|----------|--------|
| PROD | Scripts BettingHUD + CourtAlpha déployés (`/opt/bettinghud`, `/opt/courtalpha`, `courtalpha.tech`) |
| Telegram | Top 5 matinal **interactif** (`run_notify(..., interactive=True)`) — boutons Parier à 04:00 et 07:05 |
| Cron | Resync matin **07:00** build + **07:05** Telegram (`--source morning-sync`) |
| Web | `BetModal` — cote observée + **Kelly auto** sur Top 5 / Paris |
| Web | Badge **« Déjà parié »** sur tuiles (`existing_stake_eur`) |
| X | Tweets auto **en pause** (`COURTALPHAX_X_ENABLED=0`) |
| Doc | `TELEGRAM_TOP5.md`, `DEPLOY_SERVEUR.md`, `WEB_AUTH.md`, `WEB_REACT.md`, `OPS_PROD_DEPANNAGE.md` |

---

## 0. Mise à jour 5 juin 2026 (b) — Exclusion UI stats rang/points > 12 mois

| Livrable | Détail |
|----------|--------|
| Module | `scripts/match_rank_quality.py` — source + fraîcheur `stats_reference_date` |
| PROD | Live Tracker + Paris du jour / report algo (`app/dashboard.py`) |
| PREPROD | API React (`/api/live/*`, `/api/picks/*`) via `filter_matches_for_daily_top_proba` |
| Config | `BETTINGHUD_STALE_RANK_STATS_MAX_DAYS` (défaut 365) |
| Tests | `tests/test_match_rank_quality.py` |

Cas typique : challenger avec TML figé (ex. Moeller M. — ref 2016) masqué alors que le profil TE est à jour.

---

## 0. Mise à jour 5 juin 2026 (d) — CourtAlpha UI (charte, EV, logo, tuiles)

**Doc détaillée** : `CourtAlpha/docs/CHANGELOG.md` (section « session UI ») · `CourtAlpha/docs/UI_DESIGN.md`

| Livrable | Détail |
|----------|--------|
| Logo | PNG transparent `courtalpha-logo.png` (traitement damier + blanc) |
| Charte | Lime / teal / cyan / charcoal — `index.css`, `lib/brand.ts` |
| EV lisible | Paliers colorés (`evDisplay.ts`, `EvPill`, `EvLegend`) — lime ≥15 %, teal 8–15 %, rouge &lt;0 |
| Sémantique | Cyan = proba · jaune = mise Kelly · plus de tout-en-lime |
| Typo | Échelle compacte (~14 px base) |
| Tuiles | Survol `.tile-lift` (lift sans flou) |
| Moteur | Inchangé · garde-fou `match_rank_quality.py` partagé avec API |

---

## 0. Mise à jour 5 juin 2026 (c) — CourtAlpha (rebrand + proba modèle UI)

**Doc** : **`docs/WEB_REACT.md`** · Projet frère : `O:\Miouppy\Documents\CourtAlpha\`

| Livrable | Détail |
|----------|--------|
| Rebrand | `BettingHUD-Web` renommé **CourtAlpha** (UI, logo, docs) |
| Proba modèle | Affichée sur cartes match (`p_model_pct`, `p_model_fav`, `capped_p1_prob`) |
| UX | Cote observée éditable conservée · polish typo / hiérarchie |
| Moteur | `BETTINGHUD_ROOT` inchangé — toujours `BettingHUD/` |

---

## 0. Mise à jour 5 juin 2026 — BettingHUD-Web / CourtAlpha (React PREPROD)

**Doc** : **`docs/WEB_REACT.md`** · Projet frère : `O:\Miouppy\Documents\CourtAlpha\` (anciennement `BettingHUD-Web`)

| Livrable | Détail |
|----------|--------|
| Option B | Dossier **séparé** — Streamlit / prod **inchangés** |
| API | FastAPI lecture seule : `/api/health`, `/api/live/*`, `/api/picks/*` |
| Front | Vite + React + TS — onglets Live / Picks / Top 5 |
| Config | `BETTINGHUD_ROOT` → moteur existant, venv partagé |
| Doc Web | `CourtAlpha/docs/` + `AGENTS.md` (doc obligatoire à chaque changement) |
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
| `/strategie` | `/strategy` | Synthèse sélection (Top 5, EV 15–100 %) + mise Kelly **0,85** × Brier, cap 15 % |

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

> **Juin 2026** : bot renommé **`@CourtAlphabot`** — voir entrée « Mise à jour juin 2026 — Bot Telegram @CourtAlphabot » en tête de ce changelog.

**Doc** : **`docs/TELEGRAM_TOP5.md`** (documentation complète)

| Élément | Détail |
|---------|--------|
| **Bot** | `@BettingHUDbot` (puis `@CourtAlphabot` depuis juin 2026) — notifications + commandes **PROD uniquement** |
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

> **Mise à jour juillet 2026** : sélection **hybride challenger** (P77) — mêmes 5 picks que TG `/top5`. Voir § « Challenger hybride Top 5 / 1D1P — P77 ».

| Élément | Détail |
|---------|--------|
| **Paris du jour** | 1er onglet : top **5** hybride (P≥77 %, EV tier1/tier2, tri EV ↓), cote réelle éditable, mise Kelly/Brier, enregistrement portefeuille, surbrillance verte si pari posé |
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
| `scripts/ml_bundle_cli.py`, `scripts/ml_bundle_registry.py`, `scripts/ml_tour_router.py` | Freeze / rollback / promote v47 ; routage tour PREPROD (`ML_BUNDLE_ROLLBACK.md`). |
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

*Dernière mise à jour de ce fichier : 23 juillet 2026.*
