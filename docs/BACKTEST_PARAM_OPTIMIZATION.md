# Optimisation des paramètres live (EV, top N, sélection)

Comment passer d’essais manuels à une **recherche systématique**, sans sur-ajuster l’historique.

**Script** : `scripts/optimize_selection_params.py`  
**Simulations manuelles** : `scripts/compare_top5_proba_vs_ev_2026.py`, `scripts/compare_topn_proba_grid.py`

---

## 1. Ce qu’on cherche à optimiser

| Paramètre | Rôle | Plage typique |
|-----------|------|----------------|
| **EV min** | Évite les micro-edges | 5–30 % |
| **EV max** | Exclut EV extrêmes (souvent bruit) | 50–150 % * |
| **Top N / jour** | Volume vs concentration | 3–15 |
| **Tri** | `proba` (`p_model`) ou `ev` | `proba` recommandé |
| **p_model min** (optionnel) | Pool = favoris minimum | 0, 0.55, 0.60, 0.65 |
| **Niveaux tournoi** | G/M/A (fixe ou grille) | G,M,A |
| **Kelly / budget jour** | Hors grille EV — calibrer à part | voir `backtest_staking_sim` |

\* Tant que `backtest_{year}_bets.csv` n’a **aucun pari EV > 100 %**, faire varier **EV max au-delà de 100 % est inutile**. Régénérer avec `backtest_2026.py --ev-max 2.0` si tu veux tester des plafonds hauts.

---

## 2. Principe : ne pas « optimiser sur tout »

Un grid search sur 2024+2025+2026 et choisir le meilleur ROI **sur-apprend** le passé (régimes, surfaces, drift modèle).

**Procédure recommandée — walk-forward :**

```text
Fold 1 : calibrer sur 2024  →  mesurer sur 2025
Fold 2 : calibrer sur 2025  →  mesurer sur 2026 (partiel)
Fold 3 : calibrer sur 2024+2025  →  mesurer sur 2026

Retenir les paramètres stables sur plusieurs folds (pas un pic isolé).
```

En live : **re-optimiser 1–2× par an** (après RG / fin de saison), pas chaque semaine.

---

## 3. Fonction objectif (multi-critère)

Éviter de maximiser le ROI seul.

**Score composite** (implémenté dans le script) :

```text
score = ROI_1u_moyen(validation)
        - pénalité × max(0, Brier - 0.19)
        - pénalité × max(0, DD_1u - 15 %)
        - pénalité si trop peu de paris / an
        - pénalité si ROI minimal sur une année de val. très négatif
```

**Métriques à regarder en parallèle :**

| Métrique | Usage |
|----------|--------|
| **ROI 1 u** | Edge réaliste (sans Kelly composé) |
| **Hit %** | Confort psychologique / variance |
| **Brier** | Calibration du modèle sur les paris joués |
| **Profit total** | Volume × edge |
| **DD 1 u** | Risque série de pertes (mise fixe) |
| **Nombre de paris** | Éviter une stratégie « 30 paris/an » |

**Ne pas utiliser** la BR Kelly finale absolue comme objectif (explosion numérique).

---

## 4. Méthodes de recherche

| Méthode | Quand l’utiliser |
|---------|------------------|
| **Grille complète** | &lt; 500–2000 combinaisons (script actuel) |
| **Grille aléatoire** | Grande dimension : tirer 300–500 combos au hasard |
| **Optuna / Bayesian** | Même espace mais converge plus vite (à brancher si besoin) |
| **Frontière de Pareto** | Tracer ROI vs Brier vs volume, choisir à la main |

Commande rapide :

```powershell
py -3 scripts/optimize_selection_params.py --quick
```

Grille complète (plus long) :

```powershell
py -3 scripts/optimize_selection_params.py --train-years 2024 --val-years 2025
py -3 scripts/optimize_selection_params.py --train-years 2024,2025 --val-years 2026
```

Export : `data/reports/optimize_selection_train2024_val2025.csv`

---

## 5. Résultats walk-forward (mai 2026)

Grille : EV min `5,10,15,20,25` · EV max `75,100,150` · top N `5,10,15` · tri `proba,ev` · `p_model_min` `0,0.60,0.65` (270 combos / fold).

| Fold | Train | Validation | Export |
|------|-------|------------|--------|
| A | 2024 | 2025 | `optimize_selection_train2024_val2025_full.csv` |
| B | 2025 | 2026 | `optimize_selection_train2025_val2026_full.csv` |
| C | 2024+2025 | 2026 | `optimize_selection_train2024_2025_val2026_full.csv` |

### 5.1 Ce que le score composite favorise

Dans le **top 30** des 3 folds, le mode dominant est :

- **EV min 25 %** (puis 20 %)
- **`p_model_min = 0,65`** (gros favoris uniquement)
- **Top 5 ou 10**, tri **`proba`** (folds B/C) ou **`ev`** (fold A sur 2025)
- **EV max 100 % = 150 %** (identique : pas de paris &gt; 100 % EV dans les CSV)

Exemple **#1 fold C** (val 2026) : `EV 25–100 %`, top **5**, **proba**, **`p_model ≥ 65 %`** → ROI 1 u **+53,6 %**, hit **79,5 %**, mais seulement **~166 paris** sur la période partielle 2026.

### 5.2 Preset plus « live » (sans seuil p_model)

Même fold C, **`p_model_min = 0`**, tri **proba**, top **5** :

| EV min | EV max | Val 2026 ROI 1 u | Hit | Paris | Brier |
|--------|--------|------------------|-----|-------|-------|
| **15 %** | 100 % | **+39,2 %** | 65 % | **395** | 0,179 |
| **20 %** | 100 % | +40,5 % | 63 % | 363 | 0,187 |

→ Moins de ROI « affiché » qu’avec `p_model ≥ 65 %`, mais **2× plus de paris** et plus proche du dashboard actuel (**15–100 %**, top 5 proba).

### 5.3 Recommandation pratique

| Profil | Paramètres suggérés |
|--------|---------------------|
| **Défaut live** | EV **15–100 %**, top **5**, tri **proba**, pas de `p_model_min` |
| **Plus sélectif** | EV **20–100 %**, idem, ou tester **`p_model_min = 0,60`** |
| **Agressif (backtest)** | EV **25 %**, **`p_model ≥ 65 %`** — valider le volume réel avant prod |

**Attention** : le filtre **`p_model ≥ 65 %`** + **EV min 25 %** maximise le score sur 2026 partiel mais **réduit fortement le nombre de paris** → risque de sur-ajustement / journées vides.

---

## 6. Paramètres souvent « optimaux » (indication, pas vérité)

D’après les campagnes [BACKTEST_TOP5_PROBA_VS_EV.md](BACKTEST_TOP5_PROBA_VS_EV.md) :

- **Tri** : `proba` (surtout 2026 ; 2024–25 EV peut gagner en ROI 1 u mais Brier/hit pires).
- **EV min** : **15–20 %** bon compromis ROI / volume sur top 5 proba.
- **EV max** : **100 %** suffit avec les CSV actuels.
- **Top N** : **5** (actionnable live) ou **10–15** (plus de volume, hit un peu plus bas).

Valider toujours sur **l’année de validation** non utilisée pour choisir la grille.

---

## 7. Extensions possibles

1. **Régénérer CSV** avec `--ev-max 1.5` ou `2.0` pour que la grille EV max ait un effet réel.
2. **Filtre `priority_score`** ou **gap book vs modèle** si colonnes présentes dans le CSV.
3. **Par circuit** : grilles séparées ATP / WTA si objectifs différents.
4. **Bootstrap par mois** : intervalle de confiance sur le ROI (stabilité).
5. **Intégration dashboard** : lire le CSV `optimize_selection_*.csv` et afficher le top 3 paramètres.

---

## 8. Pièges à éviter

- Optimiser sur **2026 seul** (trop court, partiel).
- Changer les paramètres après chaque **semaine perdante** (overfitting au bruit).
- Comparer **top EV** et **top proba** sans regarder le **Brier**.
- Croire qu’un **EV max à 200 %** change quelque chose sans paris &gt; 100 % dans les données.

---

*Voir aussi : [BACKTEST_TOP10_PROBA_SIMULATIONS.md](BACKTEST_TOP10_PROBA_SIMULATIONS.md), [BACKTEST_TOP5_PROBA_VS_EV.md](BACKTEST_TOP5_PROBA_VS_EV.md).*
