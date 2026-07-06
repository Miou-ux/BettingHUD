# Backtest Top 5 prod — 2025 & 2026

Journal des actions, scripts et résultats pour rejouer le **Top 5 production réel** (pas Pack 1/2) sur les CSV no-leak et le replay live.

> Voir aussi : [[TELEGRAM_TOP5]] · [[DATA_RELIABILITY]] · [[PREDICTION_ET_MISE]] · [[BACKTEST_TOP10_PROBA_SIMULATIONS]] · [[CHANGELOG_RECENT]]

**Dernière mise à jour** : juillet 2026

---

## 1. Objectif

Mesurer la performance qu’on **aurait eue** en suivant la sélection Top 5 telle qu’elle tourne en prod :

- **Sélection** : `pick_modes.TOP5` → `collect_top5_proba_picks` → `filter_telegram_display_picks` → max 5/jour
- **Sizing backtest** : Kelly **½ × Brier segment**, cap **15 %** liquidité matin, BR départ **100 €**
- **Flat de référence** : **5 €** par pick

---

## 2. Filtres prod (à ne pas confondre avec Pack 1/2)

| Paramètre | Valeur prod | Pack 1 (recherche — **pas prod**) |
|-----------|-------------|-----------------------------------|
| Fiabilité | `data_reliability_score ≥ 80`, null exclu | rel ≥ 85 |
| EV favori | +15 % → +100 % | idem |
| Proba favori | > 60 % | idem |
| **`duplicate_model_prob`** | **exclu** de la publication Top 5 (juil. 2026) | — |
| Tri | `p_model_fav` ↓ | idem |
| Max / jour | 5 | 5 |
| Cap surface | **aucun** | max 2/surface |
| Gap book | **aucun** | ≤ 15 pp |
| Confidence | **aucun** | — |

**Code source prod** : `scripts/daily_top_proba_store.py` (`collect_top5_proba_picks`), `scripts/telegram_top5_notify.py` (`filter_telegram_display_picks`), `scripts/match_rank_quality.py` (`passes_data_reliability_filter`, `excluded_duplicate_model_prob_from_top5`).

---

## 3. Chronologie des actions (juin 2026)

### 3.1 Couverture fiabilité (`data_reliability_score`)

| # | Action | Script / fichier | Statut |
|---|--------|------------------|--------|
| 1 | Backfill DB historique prod | `scripts/backfill_db_reliability_scores.py` | ✅ exécuté prod — 776 `daily_top_proba_picks` + 1292 `algo_opportunities` ; **855/855** picks live scorés |
| 2 | Backtests lisent la fiabilité CSV | `scripts/backtest_csv_pick_rows.py`, màj `backtest_pack12_global_2026.py`, `backtest_portfolio_ytd_csv_2026.py` | ✅ |
| 3 | Persistance à chaque capture | `ensure_match_reliability_scored()` dans `match_rank_quality.py` ; appelé depuis `daily_top_proba_store.py`, `live_tracker_picks.py` ; `bets_db.py` COALESCE | ✅ |
| 4 | Enrichissement à la génération CSV | `backtest_2026.py` → `augment_backtest_dataframe()` (`enrich_backtest_csv_reliability.py`) | ✅ |

```bash
# Backfill DB (si besoin)
python scripts/backfill_db_reliability_scores.py

# Enrichir CSV 2025/2026 sur disque
python scripts/enrich_backtest_csv_reliability.py --year 2025 --year 2026 --in-place
```

### 3.2 Backtests Top 5 prod

| Étape | Détail |
|-------|--------|
| **Erreur initiale** | `_run_prod_top5_2026_backtest.py` utilisait les filtres **Pack 1** (gap ≤ 15 pp, cap 2/surface) — **supprimé** |
| **Script de référence** | `scripts/backtest_prod_top5_2026.py` — logique prod réelle |
| **2026** | Hybride : CSV `< 2026-05-18` + replay `daily_top_proba_picks` `≥ 2026-05-18` |
| **2025** | `data/backtest_2025_live_replay.csv` (build `build_backtest_2026_live_replay.py --year 2025`) ; fallback `backtest_2025_bets.csv` |
| **Correctif méthodo** | Live-replay : train `< YYYY-01-01`, orientation P1/P2 randomisée, cadre **favori modèle** + `data_reliability_score` |

```bash
# Sur prod (données autoritaires)
ssh bettinghud "cd /opt/bettinghud && venv/bin/python3 scripts/backtest_prod_top5_2026.py --year 2026"
ssh bettinghud "cd /opt/bettinghud && venv/bin/python3 scripts/backtest_prod_top5_2026.py --year 2025"

# Local (replay live incomplet sans DB prod)
python scripts/backtest_prod_top5_2026.py --year 2026
python scripts/backtest_prod_top5_2026.py --year 2025
```

### 3.4 Exclusion `duplicate_model_prob` (juillet 2026)

| # | Action | Script / fichier | Statut |
|---|--------|------------------|--------|
| 1 | Exclusion publication Top 5 | `excluded_duplicate_model_prob_from_top5()` · `collect_top5_proba_picks` · `filter_telegram_display_picks` | ✅ prod 03/07/2026 |
| 2 | Backtest aligné | `backtest_prod_top5_2026.py` (`_candidate_passes_prod_pool`) | ✅ |
| 3 | Comparatif P0 vs prod 2026 | `scripts/backtest_p0_vs_prod_2026.py` | ✅ |

Résultat indicatif **2026 full** (476 picks prod vs 387 avec exclusion dup + EV≤50 %) : voir `CHANGELOG_RECENT` § 03/07/2026. L’exclusion **dup seule** améliore le flat **+20 €** sur l’année ; le cap EV 50 % reste **non retenu** (gain flat négligeable, risque sur-adaptation fenêtre live courte).

```bash
ssh bettinghud "cd /opt/bettinghud && venv/bin/python3 scripts/backtest_p0_vs_prod_2026.py"
```

---

### 3.3 Audit hit rate 2025 (fiabilité des chiffres)

Script : `scripts/_audit_backtest_2025_hit_rate.py`

| Question | Réponse |
|----------|---------|
| Bug sur `won` ? | **Non** — labeling cohérent (côté WINNER = pari sur le vainqueur réel) |
| Fuite entraînement 2025 ? | **Non** — `backtest_2026.py` entraîne sur données **< 2025-01-01** |
| Fuite fiabilité ? | **Non** — rangs TML/Sackmann à la date du match |
| Pourquoi hit rate élevé ? | **Biais de sélection** : tri proba ↓ sur pool déjà filtré EV ; CSV orienté vainqueur = P1 |

**Chiffres audit live-replay** (juin 2025, `backtest_2025_live_replay.csv`, 2 422 matchs) :

| Stratégie | Picks | Hit % | Flat € |
|-----------|-------|-------|--------|
| Pool complet (proba>60 %, EV≥15 %) | 1 785 | 78,3 % | +3 599 |
| Random 5/jour (même pool prod) | 499 | 75,2 % | — |
| **Top 5 prod (script référence)** | **499** | **76,4 %** | **+749** |
| Pool rel≥80 seul (sans top5) | 1 579 | 64,9 % | — |

→ Le hit ~76 % n’est **pas** un bug de labeling ni une fuite : c’est le **biais de sélection** (pool EV+proba déjà fort). L’ancien CSV orienté vainqueur=P1 gonflait à 86–91 %.

**Chiffres historiques** (ancien `backtest_2025_bets.csv`, avant live-replay) :

| Stratégie | Picks | Hit % |
|-----------|-------|-------|
| Top 5 prod (ancien harness) | 673 | 86,5 % |
| Top 5 cadre favori (`enrich_favorite_rows`) | 1 013 | 84,4 % |

→ Privilégier le **flat PnL** et le suivi live ; ignorer Kelly composé sur longues séries.

---

## 4. Résultats synthèse

### 4.1 Top 5 prod — 2026 (partiel, ~janv.–25 juin)

| Métrique | Valeur |
|----------|--------|
| Paris / jours | 290 / 88 |
| Hit % | 71,4 % |
| Flat 5 € | **+352 €** (+24,3 % ROI) |
| Kelly ½+Brier PnL | +16 318 € (BR finale ~16 418 €) |
| Max DD Kelly | **48,0 %** |

| Période | Picks | Hit % | Flat € |
|---------|-------|-------|--------|
| CSV `< 2026-05-18` | 159 | 78,6 % | +314 |
| Live `≥ 2026-05-18` | 131 | 62,6 % | +38 |

Juin 2026 live : volume élevé mais hit ~54 % — phase la plus faible.

### 4.2 Top 5 prod — 2025 (live-replay CSV, 2 422 matchs)

| Métrique | Valeur |
|----------|--------|
| Paris / jours | **499** / **135** |
| Hit % | **76,4 %** (cohérent avec biais sélection — § 3.3) |
| Flat 5 € | **+749 €** (+30,0 % ROI) |
| Kelly ½+Brier | DD **23,3 %** — montant Kelly composé non interprétable |
| Source pool | 939 lignes avec `data_reliability_score` dans le CSV |

| Métrique | Valeur (ancien `backtest_2025_bets.csv`, **obsolète**) |
|----------|--------------------------------------------------------|
| Paris / hit / flat | 651 / 91,2 % / +1 730 € |
| Harness legacy | 673 / 86,5 % / +1 500 € |

---

## 5. Scripts — référence rapide

| Script | Rôle | Utiliser ? |
|--------|------|------------|
| `scripts/backtest_prod_top5_2026.py` | **Backtest Top 5 prod** (`--year 2025` ou `2026`) | ✅ référence |
| `scripts/_audit_backtest_2025_hit_rate.py` | Audit hit rate / biais CSV 2025 | ✅ diagnostic |
| `scripts/backfill_db_reliability_scores.py` | Backfill scores DB | ✅ ops |
| `scripts/enrich_backtest_csv_reliability.py` | Scores sur CSV historiques | ✅ ops |
| `scripts/backtest_csv_pick_rows.py` | Lecture fiabilité + confidence depuis CSV | ✅ lib |
| `scripts/backtest_pack12_global_2026.py` | Pack 1 + Pack 2 (recherche portfolio) | ❌ pas = prod Top 5 |
| `scripts/backtest_top5_pack1_vs_base_global_2026.py` | Comparaison Pack1 vs BASE | ❌ recherche |
| `_run_prod_top5_2026_backtest.py` | Ancien script erroné (Pack 1) | ❌ **supprimé** |

---

## 6. Limites connues & prochaines étapes

| Limite | Impact | Piste |
|--------|--------|-------|
| CSV `backtest_2026.py` : vainqueur toujours P1 | Probas/features biaisées vs fixture live | Randomiser P1/P2 à la prédiction (comme `simulate_day.py`) |
| Hit rate gonflé par tri proba | Attente live surestimée | Reporter pool / random-5 / flat PnL ensemble |
| Kelly composé long terme | BR absurde sur 651+ picks | Lire ROI vol + DD, pas BR finale |
| Pas de replay live 2025 en DB | Pas de cross-check out-of-sample 2025 | Backfill `daily_top_proba_picks` 2025 si besoin |
| Écart backtest vs bundle prod | Calibration sigmoid (backtest) vs isotonique BO3/BO5 (live) | Documenté dans [[BACKTEST_TOP10_PROBA_SIMULATIONS]] |

---

## 7. Checklist reprise

Pour relancer une analyse depuis zéro :

1. Vérifier CSV enrichis : `data/backtest_2025_bets.csv`, `data/backtest_2026_bets.csv` (colonne `data_reliability_score`)
2. Sur prod : DB live à jour (`backfill_db_reliability_scores.py` si scores manquants)
3. Lancer `backtest_prod_top5_2026.py --year YYYY`
4. Si hit rate suspect : `_audit_backtest_2025_hit_rate.py`
5. Interpréter **flat PnL** en priorité ; comparer CSV vs live pour 2026

---

## 8. Liens code

| Fichier | Rôle |
|---------|------|
| `scripts/daily_top_proba_store.py` | `collect_top5_proba_picks`, persistance picks |
| `scripts/telegram_top5_notify.py` | Envoi matinal `/top5` |
| `scripts/match_rank_quality.py` | Score fiabilité + `ensure_match_reliability_scored` |
| `scripts/_report_april_backtest_no_leak.py` | `enrich_favorite_rows()` — cadre favori modèle |
| `scripts/backtest_staking_sim.py` | Kelly séquentiel intraday |
| `scripts/backtest_pack12_global_2026.py` | Constantes BR, Kelly, cutoff live `2026-05-18` |
