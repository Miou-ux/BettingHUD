# Fiabilité des données live

Garde-fous et score de confiance pour les lignes du snapshot (`live_matches_snapshot.full.joblib`), le Live Tracker, le Top probas jour et les sélections automatiques (Top 5, 1D1P).

**Code** : `app/dashboard.py` (build snapshot, enrichissement), `scripts/stats_engine.py` (identité WTA, rangs), `scripts/match_rank_quality.py` (`match_data_reliability_score` / `compute_match_reliability`), `scripts/reliability_context.py` (contexte TE/stale).

---

## Correctifs juin 2026 (analyse tier 3 — 23/06/2026)

| Problème | Symptôme prod | Correctif |
|----------|---------------|-----------|
| **Prédiction dupliquée** | Gibson–Keys et Tirante–Fearnley : même `capped_p1_prob` | Clé snapshot `(p1, p2, tournoi, prematch_id)` ; vérif IDs + `prematch_id` avant réutilisation enrich ; `deepcopy` du `feature_snapshot` ; gap rang/proba 30 (ex. Gibson 66 vs Keys 28 @ 74 % → repredict) |
| **Homonyme WTA** | Britton D. → mauvaise joueuse ITF (`WTA::270485`) | Multimap `_wta_name_to_ids` + `_pick_wta_pid_candidate` (slug TE `/player/…`, match non-ITF récent, rang) ; `get_player_id_meta` utilise enfin `source_url` |
| **Rang placeholder** | Boluda rank 1500 / 3 pts | `rank ≥ 1500` ou `points < 10` ignorés dans `rankings_wta_current` (pas d’overlay sur un bon `wta_matches`) |

**Après déploiement** : lancer un rebuild **complet** (`scripts/rebuild_live_projection.py`), pas un enrich cotes seules — les lignes déjà en cache gardent les anciennes probas tant qu’elles ne sont pas recalculées.

---

## Score `data_reliability_score` (0–100)

Champ snapshot : `data_reliability_score` + `data_reliability_flags` (liste de codes).

| Score | Lecture |
|-------|---------|
| **≥ 85** | Données propres — pari automatique / Top probas OK |
| **70–84** | Prudence (écart book, preview tier, données un peu vieilles) |
| **< 70** | À éviter pour sélection auto — vérifier manuellement |
| **< 50** | Quasi certain bug ou conflit identité/rang |

### Pénalités (base 100)

| Code flag | Pénalité | Déclencheur |
|-----------|----------|-------------|
| `rang_vs_proba` | −40 | `unreliable` / contradiction rang vs proba modèle |
| `hist_te_conflict` | −20 | Dernier match base officielle ancien mais profil TE plus récent |
| `p1_unresolved_id` / `p2_unresolved_id` | −20 chacun | Pas de `player_id` résolu |
| `p1_rank_placeholder` / `p2_rank_placeholder` | −20 chacun | Rang ≥ 1500 ou points < 10 (hors défaut modèle déjà flaggé) |
| **`p1_default_model_stats` / `p2_default_model_stats`** | **−20 chacun** | **rank=100 / pts=1000** ou source `rank_points_default` / `no_ranking_source` / `preview_default` — **+ exclusion dure publication** (v5) |
| `p1_te_estimate` / `p2_te_estimate` | −15 | `stats_source = tennisexplorer_estimate` |
| `preview_tier` | −15 | Ligne encore en phase preview (build rapide) |
| `p1_no_rank_source` / `p2_no_rank_source` | −12 | Pas de source rang officielle |
| `p1_stale_rank_ref` / `p2_stale_rank_ref` | −10 | `stats_reference_date` > 12 mois |
| `book_gap_high` | jusqu’à −20 | `book_gap_pp` > 25 pp |
| **`model_odds_inconsistent`** | **−25** (défaut) | `true_odd_p*` diverge de `capped_p1_prob` (> 3 pp) |
| `p1_ref_date_stale` / `p2_ref_date_stale` | −8 | Badge fraîcheur référence |
| `p1_data_stale` / `p2_data_stale` | −6 | Données joueur anciennes |
| **`duplicate_model_prob`** | **−20** (défaut, `BETTINGHUD_DUP_PROB_PENALTY`) | Même `capped_p1_prob` sur ≥ 2 matchs distincts du snapshot |

Le booléen **`unreliable`** continue de **bloquer le bouton Parier** dans l’UI ; le score sert au **tri**, au **filtrage** et aux **analyses** sans remplacer ce garde-fou binaire.

### `duplicate_model_prob` — détection et publication Top 5 (juillet 2026)

| Étape | Comportement |
|-------|----------------|
| **Détection** | `duplicate_model_prob_keys()` : même `capped_p1_prob` **dans le même tournoi** (évite faux positifs cross-événement) |
| **Snapshot** | `dashboard.py` finalize : `duplicate_model_prob=True` + flag dans `data_reliability_flags` |
| **Score** | Pénalité −20 (défaut) — un pick peut encore atteindre **rel ≥ 80** et passer `passes_data_reliability_filter` |
| **Top 5 / 1D1P publication** | **Exclusion dure** : `excluded_duplicate_model_prob_from_top5()` dans `collect_hybrid_proba_picks` / `select_hybrid_picks` et `filter_telegram_display_picks` |
| **Hors périmètre** | `/jour`, Live Tracker — le flag reste un signal d’audit / pénalité score uniquement |

**Helpers** : `has_duplicate_model_prob_flag()`, `excluded_duplicate_model_prob_from_top5()`, `match_in_duplicate_model_prob_cluster()`.

### Score v3 (juillet 2026) — plus de matchs éligibles sans assouplir Top 5

| Changement | Effet |
|------------|--------|
| **hist_te_soft** | Conflit Base/TE mais rangs officiels frais (ATP/WTA) → pénalité **−8** au lieu de **−20** |
| **ref_date_stale** | Ignoré si `stale_rank_ref` déjà appliqué sur le même joueur (évite double pénalité) |
| **duplicate** | Cluster par **(proba, tournoi)** — pas cross-tournoi |
| **Rescore** | `data_reliability_version=3` ; `ensure_match_reliability_scored` rescoring auto |
| **ref_date_stale** | Pénalité seulement si référence rang **> 12 mois** (sinon badge UI sans malus score) |
| **Diagnostic** | `scripts/diagnose_reliability_funnel.py` |

Après déploiement : **`py -3 scripts/rebuild_live_projection.py`** pour recalculer le snapshot.

**Backtest** : `scripts/backtest_p0_vs_prod_2026.py` — comparatif prod vs exclusion dup / cap EV sur 2026.

### Score v4 + gates publics unifiés (14 juillet 2026)

| Changement | Effet |
|------------|--------|
| **`RELIABILITY_SCORE_VERSION = 4`** | Rescore auto hors snapshot rebuild |
| **`model_odds_inconsistent`** | Flag + pénalité **−25** si `true_odd_p*` diverge de `capped_p1_prob` (> 3 pp par défaut) |
| **`passes_public_pick_gates()`** | Filtre unifié : caps présents, odds cohérents, fiabilité ≥80, pas de `duplicate_model_prob` |
| **Lignes matérialisées Top5** | `_is_materialized_pick_row()` : picks sans `feature_snapshot` (post-`collect_top5_proba_picks`) — fiabilité + duplicate seulement |
| **Source proba affichage** | `capped_p1_prob` / `model_prob_for_side` — **plus** `1 / true_odd` pour Live Tracker public |

**Helpers** : `capped_p1_prob_from_match`, `model_prob_for_side`, `model_true_odd_for_side`, `reconcile_match_true_odds_from_caps`, `normalize_matches_model_probs`.

Canaux alignés : dashboard Top 5 Action, `daily_top_proba_picks`, hybride Top 5 / 1D1P, `filter_telegram_display_picks`, `live_tracker_picks` (`/jour`).

### Score v5 + exclusion dure stats modèle par défaut (23 juillet 2026)

| Changement | Effet |
|------------|--------|
| **`RELIABILITY_SCORE_VERSION = 5`** | Rescore auto (flags `p1_default_model_stats` / `p2_default_model_stats`) |
| **Stats imputées** | `stats_engine._stats_from_match_row` : rang/points manquants → `stats_source = rank_points_default` (plus `matches_recent` + 100/1000 silencieux) |
| **Exclusion dure** | **`match_has_any_default_player_stats()`** : un seul joueur en **rank=100 / pts=1000** (ou source `no_ranking_source`, `preview_default`, `rank_points_default`, …) → **aucun pari proposé** |
| **`match_rank_exclude_reason`** | Avant : les deux joueurs en défaut ; **après** : **un seul** suffit (`default_stats_placeholder`) |
| **`passes_public_pick_gates()`** | Rejet si stats par défaut sur le match snapshot **ou** flags `p*_default_model_stats` sur pick matérialisé |
| **`collect_top5_proba_picks`** | Vérifie aussi `match_has_rank_points_source()` (aligné pool Paris du jour) |
| **Dashboard** | Comparatif popover : rang/points `(défaut ML)`, avantage masqué si un côté est en défaut |

**Cas prod corrigé** : Van Assche vs Carreno-Busta (23/07) — un côté à 100/1000 passait encore rel≥80 ; le match ne peut plus être publié Top 5 / 1D1P.

**Helpers** : `is_default_player_stats`, `match_has_any_default_player_stats`, `_default_model_stats_flags`.

**Prod** : déployé 23/07/2026 (`match_rank_quality.py`, `stats_engine.py`, `daily_top_proba_store.py`, `app/dashboard.py`) — services `bettinghud-*` redémarrés.

### Comparaison v3 vs scores stockés (historique)

| Action | Script |
|--------|--------|
| A/B live jour courant (snapshot) | `scripts/diagnose_reliability_funnel.py` + `--rescore` |
| A/B historique 2025/2026 (pool, picks, ROI) | `scripts/compare_reliability_v3_backtest.py --year 2025 2026` |

Limite importante : l'A/B historique CSV sous-estime l'impact live de v3 (pas de `feature_snapshot` TE complet sur les lignes replay).

---

## `book_gap_pp` — définition unique (juin 2026)

Écart entre la **probabilité modèle du favori** et la **probabilité implicite du book** (cote publique), en **points de pourcentage** :

```
p_model_fav = max(p1, 1 − p1)     # capped_p1_prob dans feature_snapshot
p_implicit_fav = 1 / odd_fav      # cote publique du même favori (pas true_odd)
book_gap_pp = |p_model_fav − p_implicit_fav| × 100
```

| Règle | Détail |
|-------|--------|
| **Code** | `scripts/match_rank_quality.py` — `book_gap_pp_from_favorite`, `book_gap_pp_from_match`, `attach_book_gap_pp` |
| **Snapshot live** | Calculé au build (`dashboard.py`) après prédiction |
| **Top probas / DB** | Recalculé dans `daily_top_proba_store._match_favorite_metrics` (ne repose plus sur un champ hérité incohérent) |
| **Score fiabilité** | Si `book_gap_pp` > **25** (`BETTINGHUD_BOOK_GAP_HIGH_PP`) → flag `book_gap_high`, pénalité jusqu’à −20 |
| **Sélection auto** | **Aucun filtre dur** sur `book_gap_pp` (Top 5, 1D1P) — signal d’audit / pénalité score seulement |
| **≠ marge book** | L’écart cote publique vs `true_odd` (vig) n’est **pas** `book_gap_pp` |

Exemple Muchova @ 1.47, modèle 93.3 % → book ~68.0 % → **~25 pp** (pas l’ancienne métrique vig ~2 pp).

### Filtre actif (juin 2026 — renforcé juillet 2026)

Tous les **paris proposés** (Top 5 Telegram, 1D1P, Paris du jour, tuiles Live Tracker, value bets persistés) passent par **`passes_public_pick_gates`** (match snapshot) ou équivalent sur lignes matérialisées Top5 :

- seuil par défaut **`BETTINGHUD_MIN_DATA_RELIABILITY=80`**
- exclusion si `unreliable=True` ou score absent
- exclusion si **`match_has_any_default_player_stats`** (un joueur en rank/points modèle par défaut — v5)
- exclusion si `duplicate_model_prob` (cluster même tournoi)
- exclusion match brut si `model_odds_inconsistent` (`true_odd_*` stale vs `capped_p1_prob`)

### Persistance

| Table | Colonnes |
|-------|----------|
| `daily_top_proba_picks` | `data_reliability_score`, `data_reliability_flags` |
| `algo_opportunities` | idem |

Score et flags sont copiés depuis le snapshot au moment de la capture (`reliability_fields_from_match`).

`ensure_match_reliability_scored()` recalcule le score si absent avant filtre Top 5 / Live Tracker.  
`bets_db.py` : upsert avec **COALESCE** — un score existant n’est pas écrasé par `null`.

### Backfill & CSV historiques (juin 2026)

| Action | Script |
|--------|--------|
| Backfill tables DB prod | `scripts/backfill_db_reliability_scores.py` |
| Enrichir `data/backtest_{year}_bets.csv` | `scripts/enrich_backtest_csv_reliability.py --year 2025 --year 2026 --in-place` |
| Lecture fiabilité dans backtests | `scripts/backtest_csv_pick_rows.py` |

Backfill prod exécuté : **855/855** lignes `daily_top_proba_picks` scorées (moy. 93,6 ; 752 ≥ 80).

Voir journal complet : **`docs/BACKTEST_PROD_TOP5_2025_2026.md`**.

### Évolutions prévues

- Pastille UI + colonne tableau Live Tracker
- Randomisation P1/P2 dans `backtest_2026.py` (réduire biais orientation vainqueur)

---

## Diagnostic manuel

```bash
# Rebuild complet après changement stats_engine / dashboard
venv/bin/python scripts/rebuild_live_projection.py

# Script ad hoc (prod) — picks « fiables » du jour
venv/bin/python scripts/_today_reliable_picks.py --date YYYY-MM-DD

# A/B fiabilité v3 sur backtest 2025/2026 (interprétation prudente)
py -3 scripts/compare_reliability_v3_backtest.py --year 2025 2026
```

Voir aussi `docs/OPS_PROD_DEPANNAGE.md`, `docs/CHART_TOP_PROBAS_JOUR.md`.
