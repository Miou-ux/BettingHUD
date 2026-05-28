# Backtest — simulation « Top 10 probas / jour »

Documentation des campagnes de simulation **mai 2026** : sélection par **probabilité modèle** (pas par EV), bande EV, filtres alignés dashboard, gestion Kelly live.

**Scripts** : `scripts/simulate_top10_proba_2026.py`, `scripts/bets_to_br_target.py`, `scripts/export_backtest_bets_sample.py`  
**Données** : `data/backtest_{year}_bets.csv` (générés par `scripts/backtest_2026.py`)  
**Exports** : `data/reports/`

---

## 1. Protocole commun

### 1.1 Intégrité (no-leak)

| Élément | Règle |
|---------|--------|
| Entraînement | Uniquement matchs avec `tourney_date < cutoff` (défaut **1er janvier** de l’année cible) |
| Test | Matchs de l’année cible, cotes **tennis-data** (`data/raw/tennis_data/`, `tennis_data_wta/`) |
| Modèle backtest | Ré-entraînement XGB + calibration **sigmoid** segmentée dans `backtest_2026.py` (écart volontaire vs bundle prod v47 isotonique BO3/BO5) |
| Features live-only | Forcées à **0** dans le dataset backtest |

### 1.2 Éligibilité d’un pari

1. **EV** sur le côté parié : bande configurable (défaut campagne : **15 % ≤ EV ≤ 100 %**).
2. **Circuit** : ATP + WTA.
3. **Niveau tournoi** : **G**, **M**, **A** (Grand Chelem, Masters/WTA 1000, 250/500).
4. **Exclusions** (sous-chaînes dans `tournament`) : Olympics, Davis Cup, Billie Jean King Cup, United Cup, ATP/WTA Finals, Laver Cup.

### 1.3 Sélection journalière — **top 10 par `p_model`**

> **Important** : le classement intra-jour est sur **`p_model`** (proba du **côté parié**), **pas** sur l’EV ni le `priority_score`.

Par jour calendaire (`date` = jour tennis-data) :

1. Filtrer les paris éligibles ce jour-là.
2. Trier par **`p_model` décroissant**.
3. Garder au plus **10** paris.

Implémentation : `select_top_proba_per_day()` dans `scripts/simulate_top10_proba_2026.py`.

### 1.4 Gestion bankroll (Kelly live)

Alignée sur `bets_db.py` / `kelly_ab_analysis_2025.py` / onglet Backtest dashboard — implémentation : **`simulate_sequential_intraday()`** dans `scripts/backtest_staking_sim.py`.

```text
kelly_full = (b × p − q) / b        avec b = cote − 1
brier_factor = max(0, 1 − Brier_segment / 0.25)
stake_frac = 0.5 × kelly_full × brier_factor
mise_brute = stake_frac × liquidité_disponible
mise = min(mise_brute, 15 % × liquidité, budget_jour_restant, liquidité)
liquidité ← liquidité − mise
… pari suivant du même jour …
BR_fin_jour = BR_matin + Σ PnL_mises_du_jour
```

| Paramètre | Valeur |
|-----------|--------|
| BR départ | **100 €** |
| Kelly base | **0,5** (demi-Kelly) |
| Plafond par pari | **15 %** de la **liquidité intraday** restante (`stake_cap_basis='liquid'`) |
| Budget journalier | **100 %** de la BR du matin (`daily_stake_budget_pct=100`) |
| Brier segment | Clés du bundle v47 via `resolve_match_brier_segment_key` |
| PnL | Appliqué en **fin de journée** (somme des PnL des mises du jour) |

**Ordre intraday** : tri par `date`, puis ordre d’entrée du DataFrame (proba la plus haute en premier après `select_top_proba_per_day`).

**Garanties séquentielles** (vérifiées par `tests/test_backtest_staking_sim.py`) :

1. Chaque mise **préleève** la liquidité du jour (`liquid -= stake`) avant le pari suivant.
2. Le pari suivant ne peut miser que sur le **reste** (`stake ≤ liquid` et `stake ≤ budget_jour − Σ mises`).
3. La BR globale n’est **recalculée qu’en clôture** de journée (`BR = BR_matin + day_pnl`).

### 1.5 Référence « 1 unité »

Mise **fixe 1 €** (ou 1 u) par pari, **sans** réinvestissement — sert de comparaison réaliste entre années.

---

## 2. Variantes testées

| ID | EV min | EV max | Top/jour | Note |
|----|--------|--------|----------|------|
| **A** (référence) | 15 % | 100 % | 10 | Campagne initiale + comparaison 2024–2026 |
| **B** | 15 % | 50 % | 10 | Exclut les EV extrêmes (&gt; 50 %) |
| **C** (dashboard) | 15 % | 100 % | **15** | Aligné toggle UI « Top 15 probas » — **résultats § 4** |

---

## 3. Résultats — variante A (EV 15–100 %, top 10 proba)

### 3.1 Vue d’ensemble (mise fixe 1 €)

| Année | Paris | Jours | Hit % | ROI 1u | Profit | BR fin. (100 €) | Max DD | Brier |
|-------|-------|-------|-------|--------|--------|-----------------|--------|-------|
| **2024** | 1 924 | 292 | 67,2 % | +26,0 % | +499 € | 599 € | 8,2 % | 0,182 |
| **2025** | 1 920 | 296 | 71,2 % | +35,7 % | +685 € | 786 € | 4,0 % | 0,160 |
| **2026** | 708 | 108 | 58,6 % | +10,0 % | +71 € | 171 € | 16,8 % | 0,201 |

**2026** = données partielles (~janv.–mai dans le CSV au moment des tests).

**Cumul 1 €/pari (3 années)** : ~**+1 255 €** sur ~4 552 paris (somme des profits annuels, pas une BR unique).

### 3.2 Par circuit (1 €)

| Année | ATP (ROI) | WTA (ROI) |
|-------|-----------|-----------|
| 2024 | +25,5 % (1 048 p.) | +26,5 % (876 p.) |
| 2025 | +36,4 % (1 025 p.) | +34,9 % (895 p.) |
| 2026 | **−16,6 %** (332 p.) | +33,5 % (376 p.) |

### 3.3 Kelly (BR 100 €) — indicateurs comparables

| Année | ROI sur volume | Sharpe j. | Profit factor | Max DD |
|-------|----------------|-----------|---------------|--------|
| 2024 | +17,0 % | 2,21 | 1,53 | 45,8 % |
| 2025 | +24,5 % | 2,32 | 1,81 | 38,0 % |
| 2026 | +13,4 % | 1,35 | 1,34 | 56,5 % |

**2026 (Kelly, année partielle)** : profit net **+123 457 €**, BR finale **123 557 €** (×1 236) — réinvestissement composé sur ~708 paris.

> Les BR Kelly **2024–2025** en euros absolus sont **non interprétables** (~10¹⁴–10¹⁸ €) : ~1 900 paris/an avec réinvestissement illimité, sans frais. Utiliser **ROI sur volume** ou **mise 1 €** pour comparer.

### 3.4 Objectif 100 € → 1 000 € (Kelly)

Nombre de **paris** pour franchir **1 000 €** de BR (rejeu séquentiel, script `bets_to_br_target.py`) :

| Année | Paris | Calendrier (approx.) | Max DD avant 1 000 € |
|-------|-------|----------------------|----------------------|
| **2024** | **175** | ~01/01 → 30/01 | 35,6 % |
| **2025** | **80** | ~06/01 → 15/01 | 15,2 % |
| **2026** | **140** | ~04/01 → 22/01 | 28,5 % |

---

## 4. Résultats — variante B (EV 15–50 %, top 10 proba, 2026 seul)

| Métrique | EV 15–100 % | EV 15–50 % |
|----------|-------------|------------|
| Paris (top 10) | 708 | **544** |
| Hit % | 58,6 % | 58,6 % |
| ROI 1u | +10,0 % | **+2,7 %** |
| Profit 1u | +71 € | **+15 €** |
| Kelly BR fin. (100 €) | 123 557 € | **3 589 €** |

Exclure les EV &gt; 50 % retire surtout les outsiders à très forte EV ; le hit rate reste stable mais le ROI baisse sur le replay 2026.

---

## 4. Résultats — variante C (EV 15–100 %, **top 15 proba**)

> Rejoué le **27 mai 2026** sur CSV no-leak existants (`backtest_{year}_bets.csv`). Kelly = séquentiel intraday avec amputation liquidité (§ 1.4).

### 4.1 Vue d’ensemble (mise fixe 1 €)

| Année | Paris | Jours | Hit % | ROI 1u | Profit | BR fin. (100 €) | Max DD | Brier |
|-------|-------|-------|-------|--------|--------|-----------------|--------|-------|
| **2024** | 2 307 | 292 | 64,4 % | +24,1 % | +555 € | 655 € | 10,5 % | 0,185 |
| **2025** | 2 282 | 296 | 68,2 % | +35,2 % | +802 € | 902 € | 4,3 % | 0,165 |
| **2026** | 828 | 108 | 55,9 % | +10,2 % | +85 € | 185 € | 24,1 % | 0,204 |

**Cumul 1 €/pari (3 années)** : ~**+1 442 €** sur ~4 417 paris (somme des profits annuels).

Comparaison vs **top 10** (variante A) : +383 paris/an en moyenne, hit légèrement plus bas, profit 1 € **supérieur** (+555 vs +499 en 2024, +802 vs +685 en 2025, +85 vs +71 en 2026).

### 4.2 Par circuit (1 €)

| Année | ATP (ROI) | WTA (ROI) |
|-------|-----------|-----------|
| 2024 | +24,4 % (1 243 p.) | +23,7 % (1 064 p.) |
| 2025 | +34,9 % (1 215 p.) | +35,4 % (1 067 p.) |
| 2026 | **−12,9 %** (411 p.) | +33,0 % (417 p.) |

### 4.3 Kelly (BR 100 €) — indicateurs comparables

| Année | ROI sur volume | Sharpe j. | Profit factor | Max DD |
|-------|----------------|-----------|---------------|--------|
| 2024 | +17,9 % | 2,17 | 1,56 | 52,7 % |
| 2025 | +23,6 % | 2,30 | 1,76 | 37,9 % |
| 2026 | +13,6 % | 1,36 | 1,34 | 58,2 % |

**2026 (Kelly, année partielle)** : profit net **+162 111 €**, BR finale **162 211 €** (×1 622) — réinvestissement composé sur 828 paris.

> Les BR Kelly **2024–2025** en euros absolus restent **non interprétables** (réinvestissement illimité ~2 300 paris/an). Utiliser **ROI sur volume**, **Sharpe** ou **mise 1 €**.

### 4.4 Objectif 100 € → 1 000 € (Kelly, top 15)

Script : `python scripts/bets_to_br_target.py --top-n 15 --years 2024,2025,2026`

| Année | Paris | Calendrier (approx.) | Max DD avant 1 000 € |
|-------|-------|----------------------|----------------------|
| **2024** | **214** | ~01/01 → 29/01 | 30,6 % |
| **2025** | **90** | ~06/01 → 14/01 | 15,2 % |
| **2026** | **175** | ~04/01 → 22/01 | 28,7 % |

---

## 5. Fichiers & reproduction

### 5.1 Générer les CSV backtest (si absents)

```powershell
python scripts/backtest_2026.py --year 2024 --ev-min 0.08 --out data/backtest_2024_bets.csv
python scripts/backtest_2026.py --year 2025 --ev-min 0.08 --out data/backtest_2025_bets.csv
python scripts/backtest_2026.py --year 2026 --ev-min 0.08 --out data/backtest_2026_bets.csv
```

Le filtre EV **15–100 %** est appliqué **à la simulation**, pas obligatoirement à la génération du CSV.

### 5.2 Lancer les simulations

```powershell
# Top 10 (variante A)
python scripts/simulate_top10_proba_2026.py --year 2026 --skip-backtest --top-n 10 --ev-min-pct 15 --ev-max-pct 100
python scripts/simulate_top10_proba_2026.py --compare-years 2024,2025,2026 --skip-backtest --top-n 10 --ev-min-pct 15 --ev-max-pct 100

# Top 15 (variante C — aligné dashboard)
python scripts/simulate_top10_proba_2026.py --year 2026 --skip-backtest --top-n 15 --ev-min-pct 15 --ev-max-pct 100
python scripts/simulate_top10_proba_2026.py --compare-years 2024,2025,2026 --skip-backtest --top-n 15 --ev-min-pct 15 --ev-max-pct 100

# Variante EV plafonnée à 50 % (top 10)
python scripts/simulate_top10_proba_2026.py --year 2026 --skip-backtest --top-n 10 --ev-min-pct 15 --ev-max-pct 50

# Paris pour atteindre 1 000 € depuis 100 € (Kelly séquentiel)
python scripts/bets_to_br_target.py --top-n 15 --years 2024,2025,2026 --br-start 100 --target 1000

# Export détail (100 premiers paris avec mises)
python scripts/export_backtest_bets_sample.py --limit 100

# Tests liquidité intraday
python -m pytest tests/test_backtest_staking_sim.py -q
```

### 5.3 Exports générés

| Fichier | Contenu |
|---------|---------|
| `data/reports/compare_top10_proba_years.csv` | Comparatif multi-années — **top 10** |
| `data/reports/compare_top15_proba_years.csv` | Comparatif multi-années — **top 15** |
| `data/reports/backtest_top10_2026_first100_bets.csv` | 100 premiers paris détaillés (mises Kelly, cotes, PnL) |

---

## 6. Simulation délai publication TML / Sackmann (21 jours)

Hypothèse : les résultats mettent **~3 semaines** à apparaître dans TML / Sackmann. Au moment d’un match, les features ne « voient » que l’historique publié jusqu’à **date − 21 j**.

| Mécanisme | Implémentation |
|-----------|----------------|
| Micro-Elo | Mises à jour différées (`scripts/micro_elo_engine.py`, `data_lag_days`) |
| Forme / fenêtres glissantes | Lectures à `ref_dt = date − lag` (`ml_model._build_temporal_features`) |
| H2H / éditions | Mises à jour **immédiates** (approximation — voir limite ci-dessous) |

```powershell
python scripts/backtest_2026.py --year 2026 --data-lag-days 21 --out data/backtest_2026_bets_lag21.csv
python scripts/compare_2026_data_lag.py
```

### Comparaison top 10 proba · EV 15–100 % · 2026 (BR 100 €)

| Scénario | Paris | Hit % | ROI 1u | Profit 1u | Profit Kelly | Sharpe j. |
|----------|-------|-------|--------|-----------|--------------|-----------|
| Données à jour | 708 | 58,6 % | +10,0 % | +71 € | +123 k€ | 1,35 |
| **Délai 21 j** | 787 | 53,4 % | +12,5 % | +99 € | +41 k€ | 0,76 |

**Lecture** : le délai **dégrade nettement le Kelly** (profit et Sharpe) malgré un ROI 1u légèrement meilleur ; les probas / sélection changent (plus de paris). Le modèle est ré-entraîné avec features lag sur tout l’historique (pas seulement le test 2026).

---

## 7. Limites & interprétation

1. **Pas de frais de transaction** ni de limite de liquidité marché.
2. **Kelly sur liquidité 100 %/jour** : surestime la croissance en € sur années complètes.
3. **Écart backtest vs prod** : calibration sigmoid segmentée (backtest) vs isotonique duale BO3/BO5 (live v47).
4. **2026 incomplet** : ne pas extrapoler linéairement (708 paris top 10 / 828 top 15) sur 12 mois.
5. **Top 10 vs top 15 vs UI live** : backtest = sélection **`p_model`** côté parié sur tennis-data ; dashboard = **`capped_p1_prob`** favori sur snapshot TE (toggle EV favori 15–100 %). Voir [[CHART_TOP_PROBAS_JOUR]].

---

## 8. Liens

- [[PREDICTION_ET_MISE]] — EV, Kelly, backtest général
- [[CHANGELOG_RECENT]] — § backtest no-leak
- [[ARCHITECTURE_ACTUELLE_ET_MISES]] — règles de mise live
- [[CHART_TOP_PROBAS_JOUR]] — onglet UI top 15 + toggle EV favori 15–100 % (partagé Live Tracker)
