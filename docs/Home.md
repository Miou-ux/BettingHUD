# BettingHUD — Documentation

Coffre Obsidian aligné sur le dépôt Git `BettingHUD/docs/`.  
Code et dashboard : `O:\Miouppy\Documents\BettingHUD\` (Cursor / Streamlit).

> **Convention** : toute doc durable du projet vit dans **`docs/`** — voir [[GUIDE_OBSIDIAN]].

---

## Démarrer

| Note | Rôle |
|------|------|
| [[GUIDE_OBSIDIAN]] | **Comment utiliser Obsidian** + quoi documenter où |

---

## Référence opérationnelle

| Note | Contenu |
|------|---------|
| [[ARCHITECTURE_ACTUELLE_ET_MISES]] | Architecture courante, Live Tracker, modèle v47, mises |
| [[CHANGELOG_RECENT]] | Évolutions récentes (mai–juillet 2026) |
| [[ENVIRONNEMENTS]] | **PREPROD** (PC local) vs **PROD** (serveur) — règles et workflow |
| [[SCHEDULE_MISES_A_JOUR]] | **Planning** scrape, snapshot, ML train, daemon, Telegram (PREPROD/PROD) |
| [[CRONS_SEMAINE]] | **Crons PROD** — vue hebdomadaire synthétique (matin, sync, dimanche) |
| [[DEPLOY_SERVEUR]] | **Hébergement Ubuntu** (systemd, nginx, SSH, git pull) |
| [[OPS_PROD_DEPANNAGE]] | **Ops & dépannage PROD** (sync données, HEADLESS, incidents, checklist) |
| [[PUBLISHED_PICKS_REPLAY]] | **Picks publiés + settlement algo** — replay CourtAlpha, flux daemons (juil. 2026) |
| [[WTA_SACKMANN_ARCHIVE]] | **Archive WTA Sackmann** — backup, delta prod, Brier, restauration |
| [[PROD_RESILIENCE]] | **Redémarrage auto** serveur + app (systemd, boot, crash) |
| [[PROD_AUDIT]] | **Audit PROD** résilience & sécurité (checklist améliorations) |
| [[PREDICTION_ET_MISE]] | Probabilités, EV, Kelly, backtest |
| [[BACKTEST_TOP10_PROBA_SIMULATIONS]] | Simulations top 10 / **top 15** proba/jour (2024–2026, Kelly, comparatif) |
| [[BACKTEST_TOP5_PROBA_VS_EV]] | Top **5** proba vs top **5** EV · grille EV min 5–20 % (2024–2026) |
| [[BACKTEST_PARAM_OPTIMIZATION]] | Recherche auto EV min/max, top N (walk-forward, score composite) |
| [[BACKTEST_RG_2026]] | **Backtest Roland-Garros 2026** — Top 5 proba vs EV vs p≥65 % |
| [[BACKTEST_PROD_TOP5_2025_2026]] | **Top 5 prod réel** — replay 2025/2026, fiabilité, audit hit rate, scripts |
| [[BACKTEST_OPTIMIZATION_JUIL2026]] | **Grilles optimisation Top5** (juil. 2026) — combos, CLV, slates, manques data |
| [[AUTONOMIE_PROD_AUDIT]] | **Autonomie PROD** — crons, retrain, gaps, roadmap sans intervention manuelle |
| [[notes/LIVRES_BETTING_ALGOS]] | Bibliographie experte betting quant/algos + notes actionnables BettingHUD |
| [[HYBRID_PICK_SELECTION]] | Sélection hybride **Top 5 / 1D1P** (règles verrouillées P/EV/rel) |
| [[SHADOW_TEST_TOP5]] | **Shadow test** stratégie candidate Top 5 (capture, sync, go/no-go) |
| [[COMMS_LOCALE]] | **Langue TG & Discord** — communications publiques en **anglais** |
| [[ONE_DAY_ONE_PICK]] | **1 Day 1 Pick** — sélection, web, TG, Discord, cron, dépannage |
| [[TELEGRAM_TOP5]] | **Bot Telegram** — `/today`, `/top5`, `/1pick1day`, envoi matinal (EN) |
| [[TELEGRAM_CHANNEL_ACQUISITION]] | **Canal Telegram public** — 1 Day 1 Pick acquisition (EN) |
| [[DISCORD_1D1P]] | **Discord** — webhook 1 Day 1 Pick (EN) |
| [[GOOGLE_SEARCH_CONSOLE]] | **SEO** — Search Console + sitemap |
| [[LLM_VISIBILITY]] | **LLM** — llms.txt, IndexNow, Bing |
| [[ACQUISITION_TRAFFIC]] | **Acquisition** — crons trafic, rapports, UTM |
| [[GSC_BING_BIO_CHECKLIST]] | **SEO ops** — GSC, Bing, bio X |
| [[COMMUNITY_SEEDING_FR]] | **Seeding** — templates Reddit / Discord / X |
| [[COURTALPHAX_X]] | **Compte public X (référence complète)** — CourtAlphaX, BR 100 €, picks, résultats, récap hebdo, cron, API, runbook |
| [[WEB_AUTH]] | **Login dashboard** — compte web, reset mot de passe par e-mail |
| [[BILLING_ETH]] | **Premium ETH HD** — wallet dépôt, indexer, checkout `/pricing` |
| [[WEB_REACT]] | **Interface React** — projet frère `CourtAlpha` (PROD courtalpha.tech) |
| [[CHALLENGERS_ET_TOURNOIS]] | **Challengers / WTA 125** — filtre Live Tracker, tier TE, Top 5 main draw |
| [[OPS_UI_QUICK_WINS]] | **Quick wins** #1 audit picks · #2 état système · #3 empty states |

---

## Interface & live

| Note | Contenu |
|------|---------|
| [[UI_THEME_QUANT]] | Charte graphique dashboard (thème terminal quant) |
| [[DAILY_TOP_PROBA_REPLAY]] | Stockage top 15 ATP/WTA/jour pour replay réel |
| [[CHART_TOP_PROBAS_JOUR]] | Top 15 probas jour + toggle EV favori (partagé Live Tracker) |
| [[DATA_RELIABILITY]] | Score fiabilité données live + correctifs tier 3 (homonymes, rangs, snapshot) |
| [[ML_BUNDLE_ROLLBACK]] | **Rollback / promote** bundle ML v47 ↔ candidats v48 (freeze, tour routing PREPROD) |

**Onglet Paris du jour** (dashboard) : **Top 5 hybride** (aligné TG matin / `/top5`) · P≥77 % · EV tier1/tier2 · tri EV ↓ · cote réelle · Kelly · pari direct · lien Live Tracker — voir [[HYBRID_PICK_SELECTION]] et [[CHANGELOG_RECENT]] § Challenger hybride P77.

---

## Historique & modèle

| Note | Contenu |
|------|---------|
| [[ARCHITECTURE]] | Vue d’ensemble (document historique) |
| [[MODELE_V45_CHANGELOG_ET_PERFORMANCE]] | Archive correctifs / perf modèle v45 |

---

## Commandes utiles

```bash
# Dashboard
streamlit run app/dashboard.py

# Snapshot live du jour
py -3 scripts/rebuild_live_projection.py

# Diagnostic fiabilité live (version score + entonnoir)
py -3 scripts/diagnose_reliability_funnel.py
py -3 scripts/diagnose_reliability_funnel.py --rescore

# Comparaison v3 vs scores stockés (historique 2025/2026)
py -3 scripts/compare_reliability_v3_backtest.py --year 2025 2026

# Pipeline matin
py -3 scripts/morning_live_pipeline.py

# Shadow test Top5 candidat (capture/sync/report)
py -3 scripts/shadow_top5.py --capture --sync-results --report
py -3 scripts/shadow_weekly_telegram_notify.py --dry-run

# Exploration split ATP/WTA — PREPROD uniquement (voir docs/ML_BUNDLE_ROLLBACK.md)
$env:BETTINGHUD_ENV = "preprod"
$env:BETTINGHUD_ML_TOUR_ROUTING = "1"
py -3 scripts/ml_bundle_cli.py tour-routing on
py -3 scripts/preprod_tour_routing_smoke.py
py -3 scripts/shadow_wta_candidate_replay.py
py -3 scripts/preprod_tour_routing_replay.py
py -3 scripts/ml_bundle_cli.py tour-routing off

# Backtest Top 5 prod (2025 / 2026) — voir docs/BACKTEST_PROD_TOP5_2025_2026.md
py -3 scripts/backtest_prod_top5_2026.py --year 2026
py -3 scripts/backtest_prod_top5_2026.py --year 2025

# Backtest RG 2026 (replay opportunites)
py -3 scripts/backtest_rg_strategies.py --end 2026-05-29
py -3.11 scripts/telegram_top5_notify.py --dry-run          # Top 5
py -3.11 scripts/telegram_top5_notify.py --dry-run --daily  # /jour Live Tracker

# Mise à jour serveur (après git push)
ssh bettinghud "cd /opt/bettinghud && git pull && sudo systemctl restart bettinghud-dashboard bettinghud-daemon"

# Audit modèle vs book
py -3 scripts/audit_projection_day.py --gap-pp 25

# Audit parité picks (Paris du jour vs Telegram vs DB)
py -3 scripts/audit_daily_picks_parity.py

# Simulation top 15 probas / jour (comparatif années, variante C)
py -3 scripts/simulate_top10_proba_2026.py --compare-years 2024,2025,2026 --skip-backtest --top-n 15 --ev-min-pct 15 --ev-max-pct 100

# Simulation top 10 (variante A)
py -3 scripts/simulate_top10_proba_2026.py --compare-years 2024,2025,2026 --skip-backtest --top-n 10 --ev-min-pct 15 --ev-max-pct 100

# CourtAlphaX — tweets X (dry-run PREPROD, prod via cron)
py -3 scripts/courtalphax_daily_pick.py --dry-run
py -3 scripts/courtalphax_result_notify.py --dry-run
py -3 scripts/courtalphax_weekly_recap.py --dry-run
# Doc complète : docs/COURTALPHAX_X.md
```

---

## Liens externes

- README projet : `../README.md`

---

## Coffre Obsidian (BettingHUDDOCS)

| Élément | Statut |
|---------|--------|
| Racine du coffre | `O:\Miouppy\Documents\BettingHUD\docs` |
| Config Obsidian | `docs/.obsidian/` |
| Note d’accueil | **[[Home]]** (bookmark recommandé) |
| Redirection | [[Bienvenue]] → Home |
| Guide d’usage | [[GUIDE_OBSIDIAN]] |

> **Astuce** : Obsidian → **Open folder as vault** → dossier `docs` (pas un sous-dossier). Nom affiché « BettingHUDDOCS » = label uniquement.
