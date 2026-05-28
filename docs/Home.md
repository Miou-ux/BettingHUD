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
| [[DEPLOY_SERVEUR]] | **Hébergement Ubuntu** (systemd, nginx, SSH, git pull) |
| [[PREDICTION_ET_MISE]] | Probabilités, EV, Kelly, backtest |
| [[BACKTEST_TOP10_PROBA_SIMULATIONS]] | Simulations top 10 / **top 15** proba/jour (2024–2026, Kelly, comparatif) |

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

# Mise à jour serveur (après git push)
ssh bettinghud "cd /opt/bettinghud && git pull && sudo systemctl restart bettinghud-dashboard bettinghud-daemon"

# Audit modèle vs book
py -3 scripts/audit_projection_day.py --gap-pp 25

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
