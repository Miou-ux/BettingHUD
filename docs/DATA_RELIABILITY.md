# Fiabilité des données live

Garde-fous et score de confiance pour les lignes du snapshot (`live_matches_snapshot.full.joblib`), le Live Tracker, le Top probas jour et les sélections automatiques (Top 5, 1D1P).

**Code** : `app/dashboard.py` (build snapshot, enrichissement), `scripts/stats_engine.py` (identité WTA, rangs), `scripts/match_rank_quality.py` (`match_data_reliability_score`).

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
| `p1_rank_placeholder` / `p2_rank_placeholder` | −20 chacun | Rang ≥ 1500 ou points < 10 |
| `p1_te_estimate` / `p2_te_estimate` | −15 | `stats_source = tennisexplorer_estimate` |
| `preview_tier` | −15 | Ligne encore en phase preview (build rapide) |
| `p1_no_rank_source` / `p2_no_rank_source` | −12 | Pas de source rang officielle |
| `p1_stale_rank_ref` / `p2_stale_rank_ref` | −10 | `stats_reference_date` > 12 mois |
| `book_gap_high` | jusqu’à −20 | `book_gap_pp` > 25 pp |
| `p1_ref_date_stale` / `p2_ref_date_stale` | −8 | Badge fraîcheur référence |
| `p1_data_stale` / `p2_data_stale` | −6 | Données joueur anciennes |

Le booléen **`unreliable`** continue de **bloquer le bouton Parier** dans l’UI ; le score sert au **tri**, au **filtrage** et aux **analyses** sans remplacer ce garde-fou binaire.

### Évolutions prévues

- Filtre Top probas / Top 5 : « score fiabilité ≥ 80 »
- Pastille UI + colonne tableau Live Tracker
- Exclusion auto daily_top_proba si score < seuil configurable (`BETTINGHUD_MIN_DATA_RELIABILITY`)

---

## Diagnostic manuel

```bash
# Rebuild complet après changement stats_engine / dashboard
venv/bin/python scripts/rebuild_live_projection.py

# Script ad hoc (prod) — picks « fiables » du jour
venv/bin/python scripts/_today_reliable_picks.py --date YYYY-MM-DD
```

Voir aussi `docs/OPS_PROD_DEPANNAGE.md`, `docs/CHART_TOP_PROBAS_JOUR.md`.
