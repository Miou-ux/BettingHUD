# Modèle TML v45 — changelog ML et performance

> **Note (mai 2026)** : ce document conserve le **récit technique et les métriques snapshot** autour du bundle **v45** et du correctif **`last_round_reached_diff`**. Pour l’état **courant** du code et du bundle (**v47**, calibration duale BO3/BO5, nouvelles features, backtest, sauvegarde), voir **`docs/CHANGELOG_RECENT.md`** et les sections mises à jour de **`docs/ARCHITECTURE.md`** / **`docs/PREDICTION_ET_MISE.md`**.

Document unique dans `docs/` recensant les **changements récents du pipeline ML**, le **comportement causal** des features sensibles, et les **métriques de performance** du bundle courant (`models/xgb_model_tml_v45.pkl`).  
Pour l’architecture globale du projet, voir `ARCHITECTURE.md`. Pour probabilité, value et mise, voir `PREDICTION_ET_MISE.md`.

---

## 1. Résumé exécutif

| Thème | Changement |
|--------|------------|
| **Fuites temporelles / intra-épreuve** | La feature `last_round_reached_diff` ne doit plus s’enrichir avec des matchs du **même** `tourney_id` que le match prédit (tri global par `tourney_date` identique pour tout un tournoi). |
| **Entraînement** | La deque `hist` stocke `tourney_id` ; le max de profondeur pour `last_round` exclut l’épreuve courante ; repli élargi si les 5 dernières lignes sont toutes le même tournoi. |
| **Live** | `get_recent_match_quality(..., exclude_tourney_id=...)` exclut ce tournoi **uniquement** pour `last_round_reached` ; `wins_last7d` / `three_setters_last14d` restent sur l’historique complet. |
| **Dashboard** | Si le CSV contient `tourney_id`, il est propagé au cache features et à l’exclusion ; la clé de cache inclut ce `tourney_id`. |
| **Diagnostic Brier** | Après entraînement : Brier **global** + Brier par **segments** (tour, surface, croisement), persistés dans le bundle ; clé `global_isotonic` alignée sur le Brier global pour le Kelly adaptatif. |

---

## 2. Correctif : `last_round_reached_diff` (anti-fuite intra-tournoi)

### 2.1 Problème identifié

Les lignes d’historique sont triées par `tourney_date` (souvent **identique** pour tous les matchs d’un même tournoi). La deque `hist` était mise à jour **avant** la lecture « profondeur max sur les dernières entrées », ce qui permettait d’intégrer des **matchs déjà joués dans l’épreuve en cours** — signal non causal au moment de prédire un match **ultérieur** du même tournoi (fuite / *target leakage* sur la profondeur atteinte).

### 2.2 `scripts/ml_model.py` — construction des features

- **Structure `hist`** : chaque entrée est un tuple  
  `(dt, won, mins, sets, ssr, round_depth, is_3plus_setter, tourney_id)`.  
  Les indices 0–6 restent inchangés pour `wins_last7d`, `three_setters_last14d`, momentum, SSR, etc.
- **Calcul `last_round`** : à partir des dernières entrées (puis repli sur toute la deque si nécessaire), on **exclut** les lignes dont `tourney_id` normalisé égale celui de la ligne courante ; `last_round = max(round_depth)` sur le reste.
- **Helpers** : `_hist_tourney_id_token`, `_hist_same_tourney_id` pour comparaisons robustes (NaN, types mixtes).

Si `tourney_id` est absent sur les lignes d’historique, la comparaison ne filtre pas par tournoi (comportement proche de l’ancien code, sans garantie intra-épreuve).

### 2.3 `scripts/stats_engine.py` — live

- Signature : `get_recent_match_quality(player_id, tour_hint=None, exclude_tourney_id=None)`.
- Pour **`last_round_reached` uniquement** : sous-ensemble du `DataFrame` joueur où `tourney_id` normalisé ≠ `exclude_tourney_id` ; puis même logique qu’avant (dernier `tourney_id`, max des tours, promotion titre si finale gagnée).

### 2.4 `app/dashboard.py` — orchestration live

- Construction de `exclude_tourney_id_by_player` à partir du CSV si la colonne **`tourney_id`** existe.
- Passage à `get_recent_match_quality(..., exclude_tourney_id=...)`.
- **Clé de cache** des features joueur enrichie avec ce `tourney_id` pour éviter un `last_round` réutilisé entre deux événements différents.

### 2.5 Limite opérationnelle (CSV sans `tourney_id`)

Si le fichier prematch **ne fournit pas** `tourney_id`, `exclude_tourney_id` reste `None` : le live **ne peut pas** appliquer l’exclusion côté `stats_engine` — il faut enrichir le scraping / le CSV avec un identifiant de tournoi **aligné** sur celui des tables SQLite (`matches_recent` / `wta_matches`) pour une étanchéité live systématique sur `last_round_reached`.

---

## 3. Métriques Brier par segment

### 3.1 Implémentation (`scripts/ml_model.py`)

- Méthode statique **`brier_segments_test_split(X_test, y_test, y_prob, min_samples=400)`** après le hold-out temporel (même jeu de test que le Brier global).
- Segments écrits dans **`segment_brier_scores`** ; effectifs associés dans **`segment_train_sizes`** (effectifs du **jeu de test** pour chaque clé, malgré le nom historique du champ bundle).
- **`global_isotonic`** : même valeur que le Brier global test — utilisée par `resolve_segment_brier_score` lorsque le dashboard passe `segment_calibration_key="global_isotonic"`.
- Un segment n’est calculé que si \(n \geq \texttt{min\_samples}\) **et** les **deux classes** sont présentes dans `y_test` (sinon Brier non défini).

### 3.2 Clés produites (exemples)

| Préfixe / forme | Signification |
|-----------------|-----------------|
| `global_isotonic` | Brier sur tout le test (alias calibration globale). |
| `tour_ATP`, `tour_WTA` | Découpage `tour_encoded` (0 = ATP, 1 = WTA). |
| `surf_Hard`, `surf_Clay`, `surf_Grass`, `surf_Carpet` | `surface_encoded`. |
| `ATP_Hard`, `WTA_Clay`, … | Croisement tour × surface. |

Les segments trop petits (ex. Carpet sur le fold test) peuvent être absents.

À la fin de **`train()`**, un bloc stdout liste ces Briers ; ils sont **sérialisés** dans le bundle `joblib` avec le modèle.

---

## 4. Performance du modèle actuel (référence mesures projet)

Les valeurs ci-dessous correspondent à une exécution cohérente avec le code actuel : **split temporel 80 % train / 20 % test**, même `prepare_data()` (ATP TML + WTA Sackmann), calibration **isotonic** avec **`TimeSeriesSplit(n_splits=5)`**, et métriques sur le **test final** (dernier bloc chronologique).

### 4.1 Données et taille du dataset (exemple de run)

- Lignes brutes nettoyées : **87 829** (ATP **45 669**, WTA **42 160**).
- Micro-Elo (stats serve/return utilisées) : **81 412** matchs.
- **Dataset orienté P1/P2** (deux lignes par match historique) : **159 354** exemples pour l’entraînement supervisé.

### 4.2 Métriques globales (jeu de test, \(n = 31\,871\))

| Métrique | Valeur |
|----------|--------|
| **Précision (accuracy)** | **0,7087** |
| **Brier score** (plus bas = mieux) | **0,1877** |

### 4.3 Brier par segment (même test, `min_samples = 400`)

| Segment | Brier | n |
|---------|-------|---|
| global_isotonic | 0,1877 | 31 871 |
| tour_ATP | 0,1931 | 21 660 |
| tour_WTA | 0,1762 | 10 211 |
| surf_Hard | 0,1887 | 20 689 |
| surf_Clay | 0,1864 | 8 246 |
| surf_Grass | 0,1846 | 2 936 |
| ATP_Hard | 0,1924 | 13 740 |
| ATP_Clay | 0,1970 | 6 042 |
| ATP_Grass | 0,1858 | 1 878 |
| WTA_Hard | 0,1813 | 6 949 |
| WTA_Clay | 0,1571 | 2 204 |
| WTA_Grass | 0,1826 | 1 058 |

Interprétation courte : le modèle est **mieux calibré en moyenne sur WTA test** que sur ATP test sur ce découpage ; la terre battue WTA affiche le Brier segment le plus bas (**0,1571**) parmi les segments listés.

### 4.4 Importance relative des features (extrait top, même run)

Ordre indicatif des gains XGBoost (non normalisés) — la feature corrigée reste contributive mais sans fuite intra-épreuve :

| Rang (indicatif) | Feature | Importance |
|------------------|---------|------------|
| 1 | points_diff | 0,1675 |
| 2 | wins_last7d_diff | 0,1114 |
| 3 | service_elo_diff | 0,1080 |
| … | … | … |
| ≈17 | last_round_reached_diff | 0,0213 |

Graphique exporté : `models/feature_importance_tml_v45.png`.

### 4.5 Hyperparamètres principaux (rappel)

- `XGBClassifier` : `n_estimators=600`, `max_depth=4`, `learning_rate=0.03`, `subsample=0.9`, `colsample_bytree=0.9`, `reg_alpha=0.5`, `reg_lambda=1.5`, `random_state=42`, `n_jobs=4`, `monotone_constraints` alignées sur `self.features`, `enable_categorical=True`.
- Pondération d’échantillon : surtout matches à fort écart Micro-Elo + boost WTA ×1,5 (voir code `train()`).

---

## 5. Fichiers impactés (changelog technique)

| Fichier | Rôle des changements |
|---------|----------------------|
| `scripts/ml_model.py` | `hist` + `tourney_id`, filtre `last_round`, `brier_segments_test_split`, persistance `segment_brier_scores` / `segment_train_sizes`, stdout segmenté en fin de `train()`. |
| `scripts/stats_engine.py` | `_norm_tourney_id_filter`, `exclude_tourney_id` sur le calcul `last_round_reached` uniquement. |
| `app/dashboard.py` | `exclude_tourney_id_by_player`, clé de cache, appel `get_recent_match_quality` avec exclusion. |
| `models/xgb_model_tml_v45.pkl` | Bundle : modèle calibré + dictionnaires Brier segmentés après un **nouvel** entraînement avec ce code. |
| `models/feature_importance_tml_v45.png` | Figure d’importance régénérée au train. |

Scripts secondaires non modifiés pour la logique ML mais utiles au pipeline : `scripts/update_model_tml.py` (sync + `train()`), `scripts/test_dashboard_replay.py` (appels `get_recent_match_quality` sans `exclude_tourney_id` — repli stats inchangé pour `last_round`).

---

## 6. Reproduire métriques et entraînement

Depuis la racine du dépôt :

```text
py -3 -c "import sys; sys.path.insert(0, '.'); from scripts.ml_model import TennisMLModel; TennisMLModel().train()"
```

Entraînement complet + sync réseau (plus long) :

```text
py -3 scripts/update_model_tml.py
```

Recalcul **Brier segmenté** sans refit (modèle déjà sur disque) : charger le bundle, `prepare_data()`, même index de split `int(len(dataset)*0.8)`, `predict_proba` sur `X_test`, puis `TennisMLModel.brier_segments_test_split(...)`.

---

## 7. Suivi / dette documentaire

- Pour toute évolution de **`self.features`**, mettre à jour ce fichier **et** `PREDICTION_ET_MISE.md`, puis **retrain** obligatoire.
- Pour étanchéité **live** complète sur `last_round` : documenter ici le schéma CSV / scraping dès que `tourney_id` est disponible en prod.

*Dernière mise à jour de ce document : alignée sur le code et les métriques mesurées en mai 2026 dans l’environnement du dépôt. Pour les évolutions postérieures au bundle v45 (v47, calibration duale, etc.), se référer à `docs/CHANGELOG_RECENT.md`.*
