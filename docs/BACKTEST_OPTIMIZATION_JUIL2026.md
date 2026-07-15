# Backtest & optimisation Top 5 — juillet 2026

Synthèse des explorations offline (PREPROD / univers prod) : grilles de scénarios, pistes testées, manques data, candidats shadow.

> Voir aussi : [[BACKTEST_PROD_TOP5_2025_2026]] · [[HYBRID_PICK_SELECTION]] · [[SHADOW_TEST_TOP5]] · [[DATA_RELIABILITY]] · [[CHALLENGERS_ET_TOURNOIS]]

**Dernière mise à jour** : 15 juillet 2026  
**Règle** : COMBO_VOLUME (tiers 35/55) **déployé prod** — voir `CHANGELOG_RECENT` § COMBO_VOLUME.

---

## 1. Univers & outils

| Élément | Chemin / script |
|---------|-----------------|
| Univers brut ATP/WTA 250+ | `data/reports/backtest_universe_250plus_2025.from_prod.csv`, `..._2026.from_prod.csv` |
| Grille scénarios unifiée | `scripts/backtest_top5_scenario_grid.py` (`--grid coarse\|full\|hybrid\|combo`) |
| Comparaison mensuelle flat | `scripts/_compare_combo_monthly.py` |
| Exploration CLV + skip slates | `scripts/_explore_clv_slate_skip.py` |
| Exports CSV | `data/reports/top5_scenario_grid_*.csv`, `combo_vs_prod_monthly*.csv` |

**Méthodo** : hybrid prod réel (`select_prod_top5_day`), Kelly 0.65 × Brier, flat 10€, BR 100€, cap liquidité 15 %.

---

## 2. Référence prod (2025 + 2026)

| Métrique | Valeur |
|----------|--------|
| n picks | 432 |
| hit % | 86.8 % |
| flat | +660 € |
| Règles | l5, hybrid tier1 15–30 %, tier2 30–50 %, p≥80 % (hybrid), pool p>60 %, EV 15–100 %, rel≥80 |

---

## 3. Ce qui a été testé (et résultat)

### 3.1 Filtres pool seuls (EV, proba, rel, gap) à cap=5

| Levier | Effet |
|--------|-------|
| `ev_max` 75 vs 100 | **Aucun** (hybrid cappe à 50 % EV) |
| `pmin` 60 vs 65 % | **Aucun** (hybrid impose 80 %) |
| `gap ≤ 30` | Léger (+2 picks/an, +25 € flat avec l7) |
| Rel 85, EV min 18–20 | Marginal ou identique |

**Conclusion** : à cap fixe, le hybrid est le goulot — pas les filtres pool.

### 3.2 Cap journalier

| Cap | n | flat | vs prod |
|-----|---|------|---------|
| 5 (prod) | 432 | +660 € | — |
| 6 | 440 | +667 € | +7 € |
| 7 | 445 | +682 € | +22 € |
| 10 | — | Kelly 2026 ↓ | à éviter |

**Sweet spot volume** : **limit=7** (+ gap≤30 optionnel).

### 3.3 Hybrid tiers (t1_max × t2_max × limit)

| Candidat | Règles | n | flat | vs prod |
|----------|--------|---|------|---------|
| **Combo_qualité** | l5, gap≤30, tier1→28 %, tier2→55 % | 425 | +697 € | +37 € |
| **Combo_volume** | l5, gap≤30, tier1→35 %, tier2→55 % | 448 | +733 € | +73 € |
| Prod | l5, tier1→30 %, tier2→50 % | 432 | +660 € | — |

**Combo_volume** gagne la comparaison globale (flat, hit, stabilité mensuelle 11/17 mois > prod).

### 3.4 EV très hautes (50–100 %+)

- **EV ≥ 100 %** : 0 lignes dans l’univers (plafond prod `ev ≤ 100 %`).
- Lanes EV 50–100 % : hit 56–75 %, flat **−260 à −400 €** vs prod.
- **Ne pas pivoter** vers EV hautes.

### 3.5 Tous paris EV+ → 25 % (pas de cap/jour)

| Année | n | hit % | flat |
|-------|---|-------|------|
| 2025 | 171 | 78.9 % | +147 € |
| 2026 | 185–278 | 69–75 % | +30 à +126 € |

Bien **inférieur** au Top5 hybrid (sélection + cap).

### 3.6 Challengers (ATP + WTA 125)

| Test | Résultat |
|------|----------|
| Prod rules + pool CH | **0 pick** (rel=16, p max 76.6 %, hybrid bloque) |
| Lane `/jourchallenger` relaxée | 45 picks, hit 40 %, **−62 €** flat (fenêtre mai–juin 2026) |
| Backtest flat p≥65 %, EV≥15 % | 90 paris, hit 27.8 %, **−115 €** |

**Bloqué par data + calibration**, pas par les règles seules.

### 3.7 CLV gate (exploration juil. 2026)

| Source | Couverture picks prod | Verdict |
|--------|----------------------|---------|
| `closing_odds_state.json` | **0 / 432** | Pas de match noms/dates vs univers backtest |
| Proxy prematch (1ère vs dernière snap TE) | **43 / 432** (9.7 %) | Échantillon trop petit ; CLV>0 sous-performe |

→ Piste **non validée** — voir § 5.1.

### 3.8 Skip bad slates (exploration juil. 2026)

- Corr flat/jour vs `n_pool` hybrid : **+0.41** (signal faible).
- Skip bottom 10–30 % slates par score ex-ante : flat **−56 à −159 €** vs prod.
- Même le quartile bas des slates reste **+121 €** flat.

→ **Skip binaire non rentable** ; piste alternative : **sizing** par qualité de slate (non testé).

---

## 4. Candidats shadow (non déployés)

| Profil | Config | Usage |
|--------|--------|-------|
| ~~**Volume**~~ | ~~`limit=5`, `gap≤30`, tier1 15–**35** %, tier2 30–**55** %~~ | **→ Déployé prod 15 juil. 2026** (COMBO_VOLUME) |
| Qualité | idem tier1 **28** % | +37 € flat, moins de picks |
| Volume max | `limit=7` + gap≤30 + tier2 55 % | +25 € flat vs l7 seul (grille combo) |

Procédure shadow : [[SHADOW_TEST_TOP5]].

---

## 5. Manques data & infra (bloquants)

### 5.1 Closing Line Value (CLV)

| Manque | Impact |
|--------|--------|
| Pas d’archive `data/scraped/closing_odds/closing_odds_*.csv` en local/PREPROD | Backtest CLV impossible sur 2025 |
| Index state TE ≠ clés univers backtest 250+ | 0 % match sur picks historiques |
| Cote « open » backtest ≠ 1ère snapshot TE | Biais proxy prematch |
| CLV non utilisé en sélection prod aujourd’hui | Piste innovante non exploitable |

**Actions pour débloquer** :
1. Laisser tourner l’archive nocturne prod (`closing_odds_archive.py` via daemon) plusieurs semaines.
2. Backfill archives sur matchs `daily_top_proba_picks` / univers CSV.
3. Aligner normalisation noms (`_norm_name`) pick ↔ TE.
4. Retest gate `CLV > 0` sur échantillon ≥ 100 picks.

### 5.2 Challengers / WTA 125

| Manque | Impact |
|--------|--------|
| Pas d’historique cotes TE challengers avant mai 2026 | Pas de backtest 2025 |
| `data_reliability_score` ≈ 16 sur tout le pool CH | Exclusion prod (seuil 80) |
| Modèle calibré majors 250+ ; p max ~76 % sur CH | Hybrid (p≥80 %) incompatible |
| 6651+ matchs sans résultat en cache sur gros backtest CH | PnL partiel |

**Actions** : segment modèle dédié, seuils rel/proba adaptés, pipeline fiabilité CH — **hors scope prod actuel**.

### 5.3 Univers backtest

| Manque | Note |
|--------|------|
| Rebuild local full universe depuis prod DB | Timeout ; on utilise CSV `*.from_prod` |
| Replay 2026 live incomplet sans snapshot prod | OK avec CSV + `_live_rows()` |

### 5.4 Pistes innovantes non testées

| Piste | Statut |
|-------|--------|
| Kelly sizing par quartile slate | **À faire** |
| Meta-model correction sur picks prod | **À faire** |
| Recalibration proba par segment avant EV | **À faire** |
| Bandit entre prod / combo_volume / l7 | **À faire** |
| WTA routing candidat (preprod) | Modèle prêt, **pas promu prod** |

---

## 6. Commandes utiles

```bash
# Grille combo (limit × gap × tiers)
py -3 scripts/backtest_top5_scenario_grid.py --grid combo --export data/reports/top5_scenario_grid_combo.csv

# Exploration CLV + slates (offline)
py -3 scripts/_explore_clv_slate_skip.py

# Comparaison mensuelle combo vs prod
py -3 scripts/_compare_combo_monthly.py
```

---

## 7. Décision actuelle

**Aucun scénario ne bat clairement le prod sur tous les critères avec une marge suffisante pour basculer sans shadow.**

- Meilleur compromis offline : **Combo_volume** (+73 € flat, hit stable).
- Leviers volume : **cap 7** et **tier2→55 %** — pas EV hautes ni challengers.
- Prochaines explorations : **CLV** (après data), **sizing slate**, **meta-model**, **WTA routing**.
