# Backtest Roland-Garros 2026 — 3 stratégies

Replay **réel** depuis `algo_opportunities` (opportunités capturées par le Live Tracker + résultats TE/Sackmann).

**Script** : `scripts/backtest_rg_strategies.py`  
**Exports** : `data/reports/backtest_rg_strategies_{debut}_{fin}.csv`, `backtest_rg_strategies_bets_*.csv`

> Distinct du backtest CSV no-leak (`backtest_2026_bets.csv`) : le CSV 2026 **ne contient pas encore** Roland-Garros au moment du test (données jusqu’au ~22/05, tournois Strasbourg/Rabat).

---

## 1. Période et source

| Élément | Valeur |
|---------|--------|
| Début RG | **2026-05-18** (qualifs / début capture) |
| Fin (dernier run) | **2026-05-29** (ajuster avec `--end`) |
| Filtre tournoi | `French Open` / Roland Garros / French |
| Table | `algo_opportunities` dans `data/bettinghud.db` |
| Déduplication | 1 ligne max par `(jour, bet_on, match_name)` — meilleure `p_model` |

---

## 2. Protocole commun

| Élément | Valeur |
|---------|--------|
| Pool de base | EV **+15 % → +100 %** (bande favori modèle, comme Paris du jour) |
| Mise référence | **1 unité fixe** par pari terminé |
| Kelly | ½ × Brier segment, cap **15 %** liquidité intraday, BR **100 €** |
| Paris « En cours » | Exclus du PnL, comptés à part |

### Stratégies comparées

| # | Stratégie | Sélection |
|---|-----------|-----------|
| **A** | **Top 5 proba** | Top **5 / jour** (ATP+WTA combinés), tri **`p_model`** ↓ |
| **B** | **Top 5 EV** | Top **5 / jour**, tri **`ev`** ↓ |
| **C** | **p_model ≥ 65 %** | **Tous** les paris du pool avec `p_model ≥ 0,65` (sans limite top N) |

---

## 3. Résultats

### 3.1 Depuis le 18/05 (qualifs + tableau)

Dernière exécution : `py -3 scripts/backtest_rg_strategies.py --end 2026-05-29`  
Pool après déduplication : **270** lignes · **12** jours · **634** lignes brutes RG.

| Stratégie | Total | Settled | Open | Hit % | ROI 1 u | Profit 1 u | Kelly Δ€ | BR finale | DD Kelly |
|-----------|------:|--------:|-----:|------:|--------:|-----------:|---------:|----------:|---------:|
| **Top 5 proba** | 59 | 43 | 16 | 53,5 % | **-14,2 %** | **-6,1 u** | -12,7 € | 87,3 € | 34,8 % |
| **Top 5 EV** | 59 | 43 | 16 | 20,9 % | **-26,1 %** | **-11,2 u** | -40,8 € | 59,2 € | 48,6 % |
| **p_model ≥ 65 %** | 73 | 58 | 15 | 55,2 % | **-3,1 %** | **-1,8 u** | **+5,4 €** | **105,4 €** | 21,7 % |

**PnL 1 u cumulé (terminés)** : Top 5 proba **-6,11 u** · Top 5 EV **-11,21 u** · p≥65 % **-1,77 u**.

### 3.2 Depuis le 25/05 (tableau principal)

`py -3 scripts/backtest_rg_strategies.py --start 2026-05-25 --end 2026-05-29`  
Pool : **107** lignes · **5** jours · **238** lignes brutes.

| Stratégie | Total | Settled | Open | Hit % | ROI 1 u | Profit 1 u | Kelly Δ€ | BR finale | DD Kelly |
|-----------|------:|--------:|-----:|------:|--------:|-----------:|---------:|----------:|---------:|
| **Top 5 proba** | 25 | 15 | 10 | **73,3 %** | **+15,2 %** | **+2,3 u** | **+15,0 €** | **115,0 €** | 6,6 % |
| **Top 5 EV** | 25 | 15 | 10 | 26,7 % | -14,0 % | -2,1 u | +2,0 € | 102,0 € | 25,2 % |
| **p_model ≥ 65 %** | 37 | 25 | 12 | 60,0 % | +1,6 % | +0,4 u | +13,5 € | 113,5 € | 3,1 % |

**PnL 1 u cumulé (terminés)** : Top 5 proba **+2,28 u** · Top 5 EV **-2,10 u** · p≥65 % **+0,39 u**.

### Lecture

- Sur RG **2026 partiel**, le **Top 5 EV** est nettement pire (hit ~21 % global, ~27 % depuis le 25/05).
- **Depuis le 25/05 (tableau)** : le **Top 5 proba** repasse **positif** (+2,3 u, hit 73 %, Kelly +15 €).
- **`p_model ≥ 65 %`** : positif mais modeste en 1 u (+0,4 u) ; Kelly +13,5 € avec DD faible (3 %).
- Les **qualifs (18–24/05)** tirent la perf globale vers le bas — voir § 3.1 vs 3.2.

---

## 4. Commandes

```powershell
cd O:\Miouppy\Documents\BettingHUD

# Période complète RG à ce jour
py -3 scripts/backtest_rg_strategies.py --end 2026-05-29

# Tableau principal seulement (depuis 25/05)
py -3 scripts/backtest_rg_strategies.py --start 2026-05-25 --end 2026-05-29

# PROD (après déploiement du script)
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/backtest_rg_strategies.py --end 2026-05-29"
```

Options : `--ev-min-pct`, `--ev-max-pct`, `--p-model-min`, `--db`, `--export`.

---

## 5. Limites / biais

| Limite | Impact |
|--------|--------|
| **Capture opportunités** | Dépend du snapshot live / daemon — pas replay historique Sackmann |
| **Backfill early RG** | Jours 18–26/05 : source souvent `backfill_algo_opportunities` (proxy) |
| **Doublons** | Script déduplique `(jour, joueur, match)` ; vérifier exports `bets_*.csv` |
| **Paris en cours** | Non inclus dans ROI / Kelly |
| **Qualifs vs tableau** | Mélange ATP/WTA ; qualifs plus volatiles |

Pour un replay plus propre des **top probas** : voir aussi `scripts/audit_rg_daily_top_proba.py` (`daily_top_proba_picks`).

---

## 6. Liens

- [[BACKTEST_TOP5_PROBA_VS_EV]] — comparaison annuelle CSV no-leak
- [[BACKTEST_PARAM_OPTIMIZATION]] — grille `p_model ≥ 65 %`
- [[TELEGRAM_TOP5]] — stratégie live Top 5 proba
- [[CHANGELOG_RECENT]] — § backtest RG
