# Prédiction & Mise (BettingHUD)

Document de référence fonctionnelle pour :

- estimation de probabilité (`scripts/ml_model.py`)
- détection de value (`scripts/value_detector.py`)
- recommandation de mise (Live Tracker dans `app/dashboard.py`)

**Chronologie des évolutions** : `docs/CHANGELOG_RECENT.md` (bundle v47, calibration duale, nouvelles features, backtest, backup).
**Référence actuelle détaillée** : `docs/ARCHITECTURE_ACTUELLE_ET_MISES.md` (Live Tracker, ELO match réel, Report Opportunités, trajectoire de bankroll).

## 1) État actuel du modèle (v4.7 — bundle v47)

- Changelog **historique v45** (correctif `last_round_reached_diff`, tableau Brier snapshot de l’époque) : **`docs/MODELE_V45_CHANGELOG_ET_PERFORMANCE.md`**.
- Bundle par défaut : **`models/xgb_model_tml_v47.pkl`** (importance : `models/feature_importance_tml_v47.png`).

Le modèle principal est un `XGBClassifier` avec :

- `enable_categorical=True`
- colonnes catégorielles natives : `surface_encoded`, `tour_encoded`, `tournament_level_encoded`
- **Calibration** : après le fit du booster, **deux** calibrateurs **isotoniques** (`fit_dual_branch_calibrator`) — branches **BO3** et **BO5** (routage via `ROUTING_COLS_BO5` + `bo5_mask_from_features`). L’inférence passe par **`predict_proba_calibrated_routed`**.
- **Objectif d’entraînement** : `objective="reg:squarederror"` (régression sur la cible binaire 0/1).
- contraintes monotones natives :
  - `points_diff` : +1
  - `service_elo_diff` : +1
  - `rank_diff` : -1

Important : les hard caps post-prédiction ont été retirés (pas de clamps manuels type `cap_*`).

## 2) Features utilisées

`self.features` dans `TennisMLModel` est la source de vérité et est persistée dans le bundle.  
Toute modification de cette liste impose un retrain.

Vue d’ensemble des **ajouts / thèmes récents** (détail et fichiers : `docs/CHANGELOG_RECENT.md`) :

- charge récente : `minutes_played_last7d_diff` ; tie-breaks : `tb_win_pct_52w_diff`
- météo / surface : `humidity_impact`, `temperature_impact` (CPI effectif)
- marché : `market_sentiment_signal`
- calendrier / fatigue : `pre_slam_fatigue`, `travel_fatigue_index`, `age_x_travel_fatigue`, `age_x_inactivity`
- points à défendre (proxy) : `points_defending_pct`
- style : `style_drift_detected`, `style_cluster_distance_diff`, `style_matchup_bias`, `style_cross_surface_impact`
- clutch agrégé : `clutch_diff`

Les croisements déjà documentés restent en prod :

- `age_x_travel_fatigue = age_diff * travel_fatigue_index`
- `age_x_inactivity = age_diff * inactivity_decay_weight`

## 3) Probabilité et cote juste

Dans `predict_match` :

1. calcul des signaux pré-match (ELO match réel, micro-Elo service/return, forme, tactique, voyage, météo sur CPI, etc.)
2. construction de `X_new` dans l’ordre exact de `self.features`
3. `p1_prob` via **`predict_proba_calibrated_routed`** (branche BO3/BO5 selon le contexte) ; en repli interne, équivalent à la proba calibrée positive classe « P1 gagne ».
4. `p2_prob = 1 - p1_prob`
5. cotes justes :
   - `p1_true_odd = 1 / p1_prob`
   - `p2_true_odd = 1 / p2_prob`

La confiance UI reste dérivée de :

- `confidence = abs(p1_prob - 0.5) * 2`

## 4) Détection de value (EV)

Pour une cote book `O_book` et une cote juste `O_true` :

- `p = 1 / O_true`
- `EV = p * (O_book - 1) - (1 - p)`
- `value_pct = EV * 100`

Le `ValueDetector` applique un seuil minimal (éventuellement ajusté par la confiance) et peut **pénaliser** la value si la ligne s’éloigne défavorablement entre ouverture et cote actuelle (voir `value_detector.py`).

## 5) Brier segment & priorisation des values

Chaque match live reçoit une clé **`brier_segment_key`** via `resolve_match_brier_segment_key()` (`scripts/ml_model.py`) :

- recherche la clé la plus fine présente dans `segment_brier_scores` du bundle (ex. `WTA_Clay_G`, `ATP_Hard_M`, repli `tour_WTA`) ;
- **ne pas confondre** avec `segment_calibration_key` (`dual_bo3` / `dual_bo5`) renvoyé par `predict_match`.

**Kelly adaptatif** : le facteur `(1 − Brier_segment / 0,25)` réduit la mise quand le segment est mal calibré historiquement.

**Tri des opportunités** (défaut **Composite**) :  
`priority_score = (Sharpe_unitaire / Brier_segment) × (1 − Brier_segment / 0,25)` — implémentation : `scripts/priority_scoring.py`.

**Mode toggle « Top 15 · EV favori 15–100 % »** (Live Tracker + Top probas, spec `docs/CHART_TOP_PROBAS_JOUR.md`) : filtre EV du **favori modèle**, tri des tuiles par **proba favori** ↓, max **15** cartes, côté favori uniquement. État partagé via `favorite_ev_band_filter`.

**Filtre premium** : n’afficher que les matchs avec Brier segment **&lt; 0,18** (`PREMIUM_SEGMENT_BRIER_MAX`).

## 6) Recommandation de mise (Kelly live)

Principe Live Tracker :

1. proba de côté : `p_model_side = 1 / odd_true_side`
2. cote utilisée : cote réellement saisie (`custom_odd`)
3. Kelly plein :
   - `b = custom_odd - 1`
   - `f_full = (b*p - (1-p)) / b`
4. Kelly partiel : coefficient de base **0,65** (prod, juillet 2026)
5. ajustement prudent par qualité calibration (**Brier** segment via `brier_segment_key` — Kelly « adaptatif » dans le dashboard)
6. plafond de fraction bankroll (`KELLY_RECO_BANKROLL_CAP_FRAC`)
7. mise reco en € = fraction finale × bankroll disponible

La cote utilisée pour la mise est la **cote réelle saisie par l'utilisateur** dans le Live Tracker, pas forcément la cote détectée par le scraper. Le Report Opportunités conserve les deux notions : `odd_book` pour la détection et `real_odd` pour la performance réelle.

Le Report Opportunités théorique ne mise pas `1U` fixe : il simule la même règle Kelly/Brier/plafond, trie les opportunités par `priority_score`, consomme la liquidité de la journée, puis reporte la bankroll de fin de journée comme capital du lendemain. Voir `docs/ARCHITECTURE_ACTUELLE_ET_MISES.md` § 8.

## 7) Filtres live & fiabilité des données

Le dashboard peut **masquer** ou **isoler** les matchs selon la qualité des signaux :

| Filtre / pastille | Règle |
|-------------------|--------|
| **Origine rang homogène** | Les deux joueurs doivent avoir la même `stats_source` (ex. les deux en `wta_matches`). |
| **⚠ données anciennes** | Dernier signal historique **&gt; 60 j** avant la date du match. |
| **⚠ ATP/TE** ou **⚠ WTA/TE** | Écart entre absence longue en base officielle et activité récente sur Tennis Explorer (pont inactivité). |
| **Filtre jour** | **Aujourd’hui** exclut les heures `Demain …` ; en soirée, les créneaux du jour déjà passés disparaissent aussi. |
| **Top 15 · EV favori** | Toggle partagé Live / Top probas : bande EV favori **+15 % à +100 %** ; Live → tri tuiles proba favori ↓ (max 15). Voir `CHART_TOP_PROBAS_JOUR.md`. |

**Actualiser joueurs** (bouton par match) : force le scrape TE + recalcul ML ; ne met pas à jour les cotes book du CSV global.

Détails et variables d’env : `docs/CHANGELOG_RECENT.md` § 4 et § 9.

## 8) Backtest et comparabilité

- Script de référence : **`scripts/backtest_2026.py`** (ré-entraînement sans fuite, cotes réelles tennis-data, CSV de paris). Garde-fou de nommage sur **`xgb_model_tml_v47.pkl`**. Détails : `CHANGELOG_RECENT.md`.
- **Campagne « top 10 probas / jour »** (EV 15–100 %, Kelly ½, comparatif 2024–2026) : **`docs/BACKTEST_TOP10_PROBA_SIMULATIONS.md`** — scripts `simulate_top10_proba_2026.py`, `bets_to_br_target.py`, `export_backtest_bets_sample.py`.
- Les signaux **uniquement live** sont à **0** dans le dataset backtest (colonnes alignées sur `ml.features`).
- Les anciens CSV de backtest restent utilisables si le format colonnes est inchangé.
- Les résultats changent dès que le bundle modèle ou le code de features change.
- Pour comparer proprement : exécuter A/B sur le même intervalle de données.

**CLI** : l’option par défaut `--ev-max` vaut **1.0** (100 %) ; le bandeau du script mentionne parfois un plafond EV plus étroit — passer explicitement `--ev-max 0.30` si tu veux coller à ce scénario.

## 9) KPI à suivre

- Accuracy
- Brier global
- Brier par segments (Hard/Clay/Grass × niveau + WTA_Clay_M)
- ROI value
- Drawdown / Sharpe / Profit factor
- CLV (portefeuille réel)

## 10) Checklist après changement ML

1. Mettre à jour `scripts/ml_model.py`
2. Retrain complet : `python scripts/update_model_tml.py --min-year 2010`
3. Vérifier Brier global + segment
4. Redémarrer app Streamlit
5. Mettre à jour `docs/CHANGELOG_RECENT.md` et, si besoin, `MODELE_V45_CHANGELOG_ET_PERFORMANCE.md` (métriques mesurées)
