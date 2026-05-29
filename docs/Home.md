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
| [[CHANGELOG_RECENT]] | Évolutions récentes (mai 2026) |
| [[ENVIRONNEMENTS]] | **PREPROD** (PC local) vs **PROD** (serveur) — règles et workflow |
| [[SCHEDULE_MISES_A_JOUR]] | **Planning** scrape, snapshot, ML train, daemon, Telegram (PREPROD/PROD) |
| [[DEPLOY_SERVEUR]] | **Hébergement Ubuntu** (systemd, nginx, SSH, git pull) |
| [[OPS_PROD_DEPANNAGE]] | **Ops & dépannage PROD** (sync données, HEADLESS, incidents, checklist) |
| [[PROD_RESILIENCE]] | **Redémarrage auto** serveur + app (systemd, boot, crash) |
| [[PROD_AUDIT]] | **Audit PROD** résilience & sécurité (checklist améliorations) |
| [[PREDICTION_ET_MISE]] | Probabilités, EV, Kelly, backtest |
| [[BACKTEST_TOP10_PROBA_SIMULATIONS]] | Simulations top 10 / **top 15** proba/jour (2024–2026, Kelly, comparatif) |
| [[BACKTEST_TOP5_PROBA_VS_EV]] | Top **5** proba vs top **5** EV · grille EV min 5–20 % (2024–2026) |
| [[BACKTEST_PARAM_OPTIMIZATION]] | Recherche auto EV min/max, top N (walk-forward, score composite) |
| [[BACKTEST_RG_2026]] | **Backtest Roland-Garros 2026** — Top 5 proba vs EV vs p≥65 % |
| [[TELEGRAM_TOP5]] | **Bot Telegram** — `/jour` Live Tracker, `/top5`, envoi matinal |
| [[OPS_UI_QUICK_WINS]] | **Quick wins** #1 audit picks · #2 état système · #3 empty states |

---

## Interface & live

| Note | Contenu |
|------|---------|
| [[UI_THEME_QUANT]] | Charte graphique dashboard (thème terminal quant) |
| [[DAILY_TOP_PROBA_REPLAY]] | Stockage top 15 ATP/WTA/jour pour replay réel |
| [[CHART_TOP_PROBAS_JOUR]] | Top 15 probas jour + toggle EV favori (partagé Live Tracker) |

**Onglet Paris du jour** (dashboard) : top 5 probas · cote réelle · Kelly · pari direct · lien Live Tracker — détail dans [[CHANGELOG_RECENT]] § 0.14.

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

# Pipeline matin
py -3 scripts/morning_live_pipeline.py

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
