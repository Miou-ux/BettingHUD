# Backtest — Top 5 proba vs Top 5 EV (grille EV min)

Recherche **mai 2026** : comparer la sélection journalière par **`p_model`** (onglet « Top 5 proba ») vs par **EV décroissante**, pour plusieurs seuils **EV min** (5, 10, 15, 20 %) avec **EV max 100 %**.

**Script** : `scripts/compare_top5_proba_vs_ev_2026.py`  
**Exports** : `data/reports/compare_top5_proba_vs_ev_{year}.csv`, `compare_top5_proba_vs_ev_2024_2026.csv`  
**Protocole commun** (no-leak, filtres, Kelly) : voir [BACKTEST_TOP10_PROBA_SIMULATIONS.md](BACKTEST_TOP10_PROBA_SIMULATIONS.md) § 1.

---

## 1. Protocole

| Élément | Valeur |
|---------|--------|
| Données | `data/backtest_{year}_bets.csv` (`scripts/backtest_2026.py`, entraînement pré-année) |
| Circuits | ATP + WTA |
| Niveaux | G, M, A |
| Exclusions tournoi | Olympics, Davis Cup, BJK Cup, United Cup, ATP/WTA Finals, Laver Cup |
| EV max | **100 %** (appliqué à la simulation) |
| EV min (grille) | **5 %, 10 %, 15 %, 20 %** |
| Sélection / jour | **5 paris** max |
| **Top 5 proba** | Tri par **`p_model`** décroissant (proba du côté parié) |
| **Top 5 EV** | Tri par **`ev`** décroissant |
| Mise référence | **1 unité fixe** (sans réinvestissement) |
| Kelly | Demi-Kelly × facteur Brier segment, plafond 15 % liquidité intraday |

> Le filtre EV à la **génération** du CSV peut être plus large (ex. EV ≥ 8 %) ; les seuils 5–20 % sont appliqués **au replay** via `load_and_filter_bets_csv`.

---

## 2. Résultats — Top 5 **proba** (mise 1 u)

| Année | EV min | Paris | Jours | Hit % | ROI 1 u | Profit 1 u | Brier | DD 1 u % |
|-------|--------|-------|-------|-------|---------|------------|-------|----------|
| **2024** | 5 % | 1305 | 302 | 75,2 | +23,55 | +307 | **0,156** | — |
| **2024** | 10 % | 1262 | 297 | 72,7 | +24,73 | +312 | 0,168 | — |
| **2024** | 15 % | 1220 | 292 | 71,4 | +26,25 | +320 | 0,172 | — |
| **2024** | 20 % | 1186 | 288 | 68,6 | **+26,85** | **+319** | 0,185 | — |
| **2025** | 5 % | 1289 | 301 | **80,6** | +32,57 | +420 | **0,119** | — |
| **2025** | 10 % | 1253 | 298 | 79,3 | +38,03 | +477 | 0,131 | — |
| **2025** | 15 % | 1212 | 296 | 76,5 | +37,86 | +459 | 0,140 | — |
| **2025** | 20 % | 1156 | 288 | 74,6 | **+40,23** | **+465** | 0,148 | — |
| **2026** † | 5 % | 482 | 111 | 70,1 | +15,29 | +74 | **0,176** | — |
| **2026** † | 10 % | 463 | 110 | 67,2 | +15,72 | +73 | 0,188 | — |
| **2026** † | 15 % | 450 | 108 | 65,1 | **+18,31** | **+82** | 0,191 | — |
| **2026** † | 20 % | 429 | 108 | 62,2 | **+19,13** | **+82** | 0,204 | — |

† **2026** = année partielle dans le CSV au moment du test (~janv.–mai).

---

## 3. Résultats — Top 5 **EV** (mise 1 u)

| Année | EV min | Paris | Hit % | ROI 1 u | Profit 1 u | Brier |
|-------|--------|-------|-------|---------|------------|-------|
| **2024** | 5 % | 1305 | 58,7 | **+26,27** | **+343** | 0,200 |
| **2024** | 15 % | 1220 | 58,5 | **+27,57** | **+336** | 0,201 |
| **2024** | 20 % | 1186 | 58,3 | **+28,16** | **+334** | 0,202 |
| **2025** | 5 % | 1289 | 62,6 | **+40,32** | **+520** | 0,177 |
| **2025** | 15 % | 1212 | 62,5 | **+42,36** | **+514** | 0,178 |
| **2025** | 20 % | 1156 | 62,3 | **+43,63** | **+504** | 0,179 |
| **2026** † | 5 % | 482 | 51,7 | +11,62 | +56 | 0,212 |
| **2026** † | 15 % | 450 | 50,7 | +12,88 | +58 | 0,218 |
| **2026** † | 20 % | 429 | 50,8 | +14,92 | +64 | 0,219 |

Même nombre de paris que le top 5 proba à seuil EV égal (même pool, autre tri intra-jour).

---

## 4. Proba vs EV — lecture par année

### 2024–2025 (années complètes)

| Critère | Top 5 **proba** | Top 5 **EV** |
|---------|-----------------|--------------|
| Hit rate | **~69–81 %** | ~59–63 % |
| Brier | **0,12–0,18** | ~0,18–0,20 |
| ROI 1 u (EV min 15–20 %) | +26 à +40 % | **légèrement supérieur** (+28 à +44 %) |
| Kelly (×BR) | Énorme (non interprétable) | Idem |

Sur **2024–2025**, le tri par **EV** peut battre le tri par **proba** en **ROI 1 u** à seuil équivalent, au prix d’un **hit bien plus bas** et d’un **Brier nettement plus mauvais** (moins bien calibré).

### 2026 (partiel)

| Critère | Top 5 **proba** | Top 5 **EV** |
|---------|-----------------|--------------|
| ROI 1 u (EV min 15 %) | **+18,3 %** | +12,9 % |
| Hit | **65 %** | 51 % |
| Brier | **0,19** | 0,22 |

En **2026 YTD**, le **top 5 proba** domine clairement le top 5 EV.

---

## 5. Effet du seuil EV min (top 5 **proba**)

Tendance **stable sur les 3 années** :

1. **EV min plus bas (5 %)** → **Brier plus bas**, **hit plus haut**, favoris à forte `p_model` et EV modeste encore éligibles.
2. **EV min plus haut (20 %)** → **ROI 1 u souvent meilleur** (sélection parmi paris à plus forte value perçue), mais **hit** et **calibration** se dégradent.
3. **Compromis live** : **EV min 15 %**, **EV max 100 %** — bon équilibre volume / edge en 2026 ; en 2024–2025 le **20 %** maximise le ROI 1 u proba avec peu de paris en moins.

### Pourquoi le Brier baisse à 5 % d’EV min ?

Le tri reste sur **`p_model`**, mais le **pool** s’élargit : on garde des favoris (proba élevée, EV 5–12 %). En montant le seuil, ces matchs sortent ; les 5 du jour viennent de cotes plus hautes / probas plus basses → erreurs `(p − y)²` plus grandes. Voir composition (ex. 2026) : `p_model` moy. **0,78** à 5 % vs **0,74** à 20 % ; cote moy. **1,87** vs **2,20**.

---

## 6. Recommandations (live / dashboard)

| Décision | Recommandation |
|----------|----------------|
| Critère de sélection | **`p_model`** (top N probas), pas le tri EV seul |
| Bande EV live | **15 % – 100 %** (défaut UI) ; **10 %** si besoin de volume |
| Éviter | **Top EV** comme règle principale ; **5 %** EV min systématique (volume favoris, edge unitaire plus faible) |
| Métrique de décision | **ROI 1 u**, **hit**, **Brier** — pas la BR Kelly absolue (réinvestissement 100 % / jour) |
| 2026 vs historique | Sur 2026 partiel, proba >> EV ; sur 2024–25, EV peut gagner en ROI 1 u mais avec calibration faible — **prioriser proba** pour robustesse |

---

## 7. Reproduction

```powershell
cd O:\Miouppy\Documents\BettingHUD

# Une année
py -3 scripts/compare_top5_proba_vs_ev_2026.py --year 2026 --ev-mins 5,10,15,20

# 2024, 2025, 2026
py -3 scripts/compare_top5_proba_vs_ev_2026.py --years 2024,2025,2026 --ev-mins 5,10,15,20
```

CSV backtest manquants :

```powershell
py -3 scripts/backtest_2026.py --year 2024 --ev-min 0.05 --ev-max 1.0 --out data/backtest_2024_bets.csv
py -3 scripts/backtest_2026.py --year 2025 --ev-min 0.05 --ev-max 1.0 --out data/backtest_2025_bets.csv
py -3 scripts/backtest_2026.py --year 2026 --ev-min 0.05 --ev-max 1.0 --out data/backtest_2026_bets.csv
```

---

## 8. Optimisation automatique

Grille EV min/max, top N, tri proba/EV : [BACKTEST_PARAM_OPTIMIZATION.md](BACKTEST_PARAM_OPTIMIZATION.md) · `scripts/optimize_selection_params.py`

---

## 9. Liens

- Campagne top **10** / **15** proba : [BACKTEST_TOP10_PROBA_SIMULATIONS.md](BACKTEST_TOP10_PROBA_SIMULATIONS.md)
- UI « Top probas jour » : [CHART_TOP_PROBAS_JOUR.md](CHART_TOP_PROBAS_JOUR.md)
- Environnements PREPROD/PROD : [ENVIRONNEMENTS.md](ENVIRONNEMENTS.md)

---

---

## 10. Grille EV max (100 %, 150 %, 200 %)

Rejoué avec `--ev-max-pcts 100,150,200` (EV min 5 / 10 / 15 / 20 % inchangés).

**Résultat : identique pour les trois plafonds** sur 2024, 2025 et 2026.

**Cause** : dans les CSV `backtest_{year}_bets.csv`, **aucun pari n’a une EV &gt; 100 %** (génération / filtre `backtest_2026.py` + marchés tennis-data). Monter le plafond à 150 % ou 200 % **ne change donc rien** tant que le CSV ne contient pas d’EV extrêmes.

Export : `data/reports/compare_top5_proba_vs_ev_2024_2026_evmax_grid.csv` (72 lignes = 3 années × 3 EV max × 8 scénarios, valeurs dupliquées).

```powershell
py -3 scripts/compare_top5_proba_vs_ev_2026.py --years 2024,2025,2026 --ev-mins 5,10,15,20 --ev-max-pcts 100,150,200
```

---

*Dernière mise à jour : mai 2026 — rejoué sur `backtest_2024_bets.csv`, `backtest_2025_bets.csv`, `backtest_2026_bets.csv`.*
