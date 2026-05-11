# Prédiction des matchs et mise recommandée (BettingHUD)

Ce document décrit **méthode de prédiction** et **logique de Kelly / taille de mise** telles qu’implémentées dans le dépôt, pour permettre une **reproduction** ou une implémentation parallèle.

Pour l’**architecture** du projet (flux SQLite, scrapers, automatisation du dashboard), voir [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Fichiers de référence principaux** :

- `scripts/ml_model.py` — modèle, features (dont **Human Factors v4.5**), calibration, caps, sortie cote « juste » ; persistance **`style_kmeans`** / **`style_rank_map`** dans le bundle
- `scripts/micro_elo_engine.py` — construction des Micro-Elo (entraînement)
- `scripts/stats_engine.py` — stats joueur, H2H, résolution d’identité `player_id`, helpers **`tactical_vector_52weeks`**, **`clutch_score_52weeks`**, **`travel_fatigue_index_from_history`** (alignés sur les définitions ML live)
- `scripts/tournament_geo.py` — coordonnées approximatives et fuseaux (UTC ± h) par sous-chaîne de `tourney_name` ; Haversine **sans API**
- `scripts/player_identity.py` — normalisation des noms (`canonical_name`, `to_lastname_initial`)
- `scripts/value_detector.py` — définition de la « value » (EV) entre cote book et cote modèle
- `scripts/surface_speed.py` — indice de vitesse de surface (CPI), météo effective en entraînement / prédiction
- `app/dashboard.py` — Live Tracker : signaux historiques (`_compute_live_advanced_signals`), Kelly **1/2** (fraction de base configurable) × facteur **Brier segment**, plafond **15 %** de la BR dispo (`KELLY_RECO_BANKROLL_CAP_FRAC`), onglet **Human Factors**
- `scripts/sync_tml_recent.py` — synchronisation ATP TML (`matches_recent`), référence au dictionnaire tournois pour le voyage inféré
- `models/xgb_model_tml_v45.pkl` — bundle attendu (**v4.5**) : estimateur calibré + dictionnaires Elo + liste **`features`** + éventuellement **`style_kmeans`** / **`style_rank_map`** (un ancien **`v4`** avec moins de colonnes n’est pas compatible avec le code v4.5 actuel : il faut réentraîner)

---

## 1. Données d’entrée (avant le modèle)

### 1.1 Identité des joueurs

- **ATP** : index nom → `player_id` (chaîne) construit depuis `matches_recent` (TennisMyLife), clé canonique  
  `canonical_name(to_lastname_initial(nom))`.
- **WTA** : index nom → entier `winner_id` / `loser_id` depuis `wta_matches` (Sackmann), même clé canonique.
- **Live** : `get_player_id_meta` dans `stats_engine` utilise cette même clé pour retrouver l’ID à partir du libellé affiché (ex. « Prénom Nom » ou « Nom I. »).

Le modèle préfixe les IDs en clés internes : `ATP::<id>` ou `WTA::<id>` (entier en texte).

### 1.2 Stats affichées / injectées

`TennisStatsEngine.get_player_stats` fournit rang, âge, taille, points, main, etc.  
Forme, fatigue, qualité de matchs récents, profils vitesse / break-points : voir appels dans `dashboard.py` (`build_live_matches_list` / boucle `predict_match`).

### 1.3 Surface et tournoi

- **Surface** : déduite du nom du tournoi dans l’UI (`_infer_surface`) parmi Hard / Clay / Grass (sinon défaut Hard pour l’encodage).
- **Niveau** : `tournament_level` ou inféré depuis le nom (`_infer_tourney_level_from_name`) → codes **A** / **M** / **G** encodés en 1.0 / 2.0 / 3.0.
- **Tour** : `tour_encoded` = 1.0 si WTA, 0.0 si ATP.

---

## 2. Elo interne (Micro-Elo et « global » affiché)

### 2.1 Entraînement (hors ligne d’inférence)

Lors de l’entraînement du bundle, `run_micro_elo_scan` (`micro_elo_engine.py`) parcourt l’historique de matchs avec **stats détaillées** (points au service, 1ère / 2e balle, etc.) :

- **Base** `base_elo = 1500.0`, échelle des écarts `micro_elo_scale` (typiquement **200**).
- Attente de points gagnés au service via une forme type Elo :  
  `_expected_pt(serve_elo, return_elo, scale) = 1 / (1 + 10^((return_elo - serve_elo) / scale))`.
- Écart **observé − attendu** sur les points de service → mise à jour des ratings **service** et **retour** globaux et **par surface**, avec facteurs niveau tournoi (G/M/A), WTA Masters +15 % sur K, et **pondération vitesse de surface** (`micro_speed_baseline`, `micro_speed_alpha`) vs `lookup_surface_speed`.
- Clés joueur : ID si présent, sinon clé nom `f"{tour}::{name_key}"` avec `name_key` = `_name_key` sur le nom dans le CSV d’entraînement.

À la fin du scan, le bundle stocke notamment `player_service_elo`, `player_return_elo`, surfaces, compteurs, `last_seen`.  
`player_elo` (utilisé comme Elo « global » dans le ML) est la **moyenne (service + retour) / 2** par clé, alignée avec les clés du scan.

### 2.2 Inférence (`predict_match`)

Paramètres par défaut chargeables depuis le pickle : `elo_decay_tau_days` (365), `surface_blend_n0` (**30**).

1. **Décroissance temporelle** (si date du dernier match connue) :  
   pour un rating `r`, après 14 jours sans pénalité,  
   `r_new = 1500 + (r - 1500) * exp(-Δjours / tau)`.

2. **Elo global / surface « classiques »** : lecture dans `player_elo`, `player_surface_elo`, avec repli **1500** si absent, puis blends α = `n_surface / (n_surface + 30)`.

3. **Micro-Elo effectifs** (service / retour) : mélange global / piste surface avec le même **n0 = 30**, puis différentes **features** (écarts service, retour, interactions WTA, etc.).

4. Si l’Elo global manque mais le micro est trouvé : **Elo global de repli** = `(service_eff + return_eff) / 2` (tag `micro_avg` dans le snapshot).

Les libellés **(défaut ML) / (estim. micro)** dans l’UI viennent des tags `p*_global_elo_tag` / `p*_micro_elo_tag`.

---

## 3. Vecteur de features et modèle XGBoost

### 3.1 Liste exacte des colonnes (`TennisMLModel.features`)

Ordre fixe — **obligatoire** pour `predict_proba`. La liste est **persistée dans le pickle** (`bundle["features"]`) et doit coïncider avec les clés construites ligne à ligne dans `predict_match`.

Colonnes dans l’ordre actuel (v4.5) :

`surface_encoded`, `tournament_level_encoded`, `tour_encoded`, `rank_diff`, `age_diff`, `ht_diff`, `points_diff`, `service_elo_diff`, `return_elo_diff`, `speed_affinity`, `speed_performance_delta`, `serve_speed_interaction`, `wta_weighted_advantage`, `wta_speed_power_impact`, `wta_break_point_resilience`, `wins_last7d_diff`, `three_setters_last14d_diff`, `last_round_reached_diff`, `momentum5_diff`, `form90_surface_diff`, `second_srv_ratio3_diff`, `hold_surface_diff`, `break_surface_diff`, `first_srv_win10_diff`, `bp_conv10_diff`, `dominance_ratio_diff`, `elo_surface_recent_diff`, `hand_diff`, `h2h_ratio`, `h2h_significant`, `style_advantage_score`, `clutch_index_diff`, `inactivity_decay_weight`, `home_adv_diff`, `altitude`, `indoor`, `humidity_impact`, `temperature_impact`, `market_sentiment_signal`, `points_defending_pct`, `pre_slam_fatigue`, `style_matchup_bias`, `travel_fatigue_index`, `clutch_diff`.

Encodages et rôles notables :

- `surface_encoded` : Hard=0, Clay=1, Grass=2, Carpet=3.
- `hand_diff` : R=+1, L=-1, U=0.
- `h2h_significant` : 1 si au moins 4 matchs H2H, sinon 0.
- `weak_elo_h2h` (garde-fou interne, pas une colonne du DataFrame mais condition de clip sur d’autres signaux) : H2H=0 et petits écarts Elo (voir code).
- `style_matchup_bias`, `travel_fatigue_index`, `clutch_diff` — voir **§3.4**.

### 3.2 Sortie brute du classifieur

- Modèle principal : **XGBClassifier** (classe positive = victoire P1), chargé depuis le pickle.
- Probabilité brute : `global_p1 = model.predict_proba(X)[1]`.

### 3.3 Calibration par segment

- Clé segment : `{surface}_{niveau}` ; en WTA si présent : `WTA_{surface}_{niveau}`.
- Si un modèle segment existe dans `model_segments` :  
  `p1 = blend_w * seg_p1 + (1 - blend_w) * global_p1`  
  avec `blend_w = segment_blend_weight * volume_factor`,  
  `volume_factor = clamp(n_seg / 1500, 0.30, 1.0)`,  
  `segment_blend_weight` typiquement **0.7**.
- Sinon, si surface Clay et `model_clay` présent : probabilité issue du modèle clay.
- Sinon : `p1 = global_p1`.  
Libellé renvoyé dans `calibration_used`.

### 3.4 Facteurs « Human Factors » (v4.5)

Complètent les signaux **`style_advantage_score`** et **`clutch_index_diff`** (toujours dans le vecteur XGBoost).

**Style tactique (KMeans, k = 4)**  

- Durant **`_build_temporal_features`** : pour chaque match, à partir du **passé uniquement**, moyenne sur **≈52 semaines** de quatre métriques enregistrées dans `hist_tac` : Ace%, rapport **premières balles gagnées sur premières balles jouées**, BP sauvés%, proxy de **jeux au service gardés**.
- À l’entraînement, **KMeans** est ajusté sur l’empilement des vecteurs **vainqueur + perdant** ; les centroides sont classés par **ace décroissant** pour obtenir un **rang sémantique** 0→3 (labels d’usage : Big Server, Aggressive Baseliner, Tactical/Slicer, Counter-Puncher — voir **`STYLE_SEMANTIC_LABELS`** dans `ml_model.py`).
- **`style_matchup_bias`** encode un avantage antisymétrique P1/P2 avec une couche « surface lente » (**Clay** ou **`surface_speed` strictement inférieur à 0,68**), notamment un bonus contre-puncher contre gros serveur sur lent (**`style_matchup_bias_from_ranks`**).
- Le bundle peut contenir **`style_kmeans`** et **`style_rank_map`** ; sinon le biais prédiction est neutre (**0**) et les libellés de style snapshot restent génériques.

**Voyage / jetlag (sans API)**  

- **`tournament_geo.py`** : **(lat, lon, fuseau UTC indicatif ±h)** déduits du **nom de tournoi** (match de sous-chaînes).
- Déplacement **Haversine** entre le dernier événement connu dans l’historique et le lieu courant. **Malus 0,05** (valeur utilisée aussi comme différence ML) si (**distance supérieure à 4000 km** **ou** **écart de fuseau horaire strictement supérieur à 4 h**) **et** **moins de 4 jours** depuis le dernier match.
- **`travel_fatigue_index`** côté dataset orienté vainqueur = **pénalité(vainqueur) − pénalité(perdant)** ; en live, passer **`p1_travel_penalty_index`** / **`p2_travel_penalty_index`** (remplissage Dashboard depuis l’historique).

**Clutch 52 semaines**  

- Score **`clutch_score`** (entraînement / live) pour un joueur : **moyenne arithmétique** de trois moyennes sur **365 jours** avant la date de référence : BP sauvés%, BP convertis%, taux tie-breaks remportés — **pour les trois coefficients égaux (/3)**, valeur dans **[0, 1]** (distinct de **`clutch_index_diff`** qui pondère 0,4 / 0,4 / 0,2).
- **`clutch_diff`** = différence clutch **P1 − P2** dans `predict_match`.
- En plus du signal appris par XGBoost, une **Correction « match tendu »** : si **`raw_p1_prob`** (probabilité juste après calibration segment Clay / globale — voir §4) est dans **[0.45 ; 0.55]**, après les caps suivants il est ajouté **`+ 0.04 × clutch_diff`** puis clipping (voir `post_clutch_tight` dans le code).

---

## 4. Garde-fous (caps) sur la probabilité P1

Après calibration, le code conserve **`raw_p1_prob`** = valeur juste après mélange segment / Clay / globale (**avant** les caps suivants). Les étapes suivantes s’appliquent **sur `p1_prob`** :

1. **Matchs serrés** (écart de rang + écart d’Elo surface) : bornes dépendent du niveau **G / M / A** (voir blocs `cap_gs_*`, `cap_m_*`, `cap_a_*` dans le code).

2. **Fondamentaux** :  
   `fundamental_score = (p1_global_elo - p2_global_elo) / 100 + (p1_pts - p2_pts) / 500`  
   Si le modèle va à l’encontre de ce score avec une amplitude minimale, probabilité **pincée** vers 0.35–0.70 selon les paliers (`cap_fund_*`).

3. **Inactivité** : si `max(jours depuis dernier match)` > 45 / 60 / 90, probabilité resserrée vers 0.5 (`cap_inactivity_*`).

4. **Clutch hors modèle sur match équilibré au sens pré-calibration** : si **`0.45 ≤ raw_p1_prob ≤ 0.55`**, ajuster **`p1_prob ← p1_prob + 0.04 × clutch_diff`** (bornes internes dans le code), cf. **`post_clutch_tight`**.

Confiance affichée après l’ensemble des étapes ci-dessus : `confidence = abs(p1_prob - 0.5) * 2` ∈ [0, 1].

Le champ **`feature_snapshot`** peut inclure **`human_p1_style`**, **`human_p2_style`**, indicateurs **`p1_jetlag_alert`** / **`p2_jetlag_alert`**, ainsi que **`style_matchup_bias`**, **`travel_fatigue_index`**, **`p1_clutch52`**, **`p2_clutch52`**, utilisés aussi par l’onglet Dashboard **Human Factors**.

### 4.1 Cotes « justes »

Après caps et après l’étape clutch éventuelle (§4, point 4) :

- `p1_true_odd = 1 / p1_prob`, `p2_true_odd = 1 / p2_prob` (si probas > 0).

---

## 5. Détection « Value Bet » (EV)

Classe `ValueDetector` (`value_detector.py`), paramètre `min_value_threshold` (défaut Live configurable, ex. **0.05** = 5 % d’espérance).

Pour une cote book `O_b` et une cote modèle `O_t` (juste) :

- `p* = 1 / O_t` — probabilité « modèle ».
- Espérance de gain par unité misée (yield) :  
  `EV = p* * (O_b - 1) - (1 - p*) * 1`.

Si `confidence` est fournie, le seuil effectif devient  
`threshold = min_value_threshold * (2 - confidence)` (plus strict si faible confiance).

**Value** si `EV >= threshold`.  
`value_pct = EV * 100`.

---

## 6. Mise recommandée (Live Tracker — Kelly)

Référence implémentation : `app/dashboard.py` (section reco sous chaque opportunité).

### 6.1 Banque utilisée

- **`br_avail`** : **bankroll disponible** (€) = capital théorique libre après engagements et correctifs (voir `compute_live_tracker_bankroll_eur` / snapshot Live).
- La reco est un **pourcentage de `br_avail`**, pas de la bankroll totale « référence » seule.

### 6.2 Probabilité utilisée pour Kelly

On prend la **probabilité implicite de la cote juste** du **côté parié** :

- `p_model_side = 1 / odd_true` (bornée dans [0, 1] en pratique), où `odd_true` est la cote juste du joueur (`true_odd_p1` ou `true_odd_p2`).

### 6.3 Cote utilisée pour Kelly

- **`custom_odd`** : cote **saisie par l’utilisateur** (« Cote réelle »), min 1.01, pas uniquement la cote agrégée du fichier prematch.

### 6.4 Formule Kelly « pleine » puis fractionnée

Soit `O = custom_odd` (cote décimale), `b = O - 1` le gain net pour 1 unité misée en cas de victoire.

Kelly fraction classique pour une seule issue :

`f_full = (p * b - (1 - p)) / b` avec `p = p_model_side`.

Dans le code :  
`b_side = max(0.01, O - 1)`  
`kelly_full = max(0, (b_side * p - (1 - p)) / b_side)`  
puis **`kelly_partial = KELLY_RECO_ADAPTIVE_BASE_FRAC * kelly_full`** avec par défaut **`KELLY_RECO_ADAPTIVE_BASE_FRAC = 0.5`** (Kelly **demi** ; l’UI backtest peut forcer 1/4 adaptatif).

### 6.5 Ajustement Brier segment puis plafond

Le Live réduit la fraction Kelly (1/2 par défaut) avec le Brier de calibration du segment (`resolve_segment_brier_score`) :

`kelly_adj = max(0, 1 - (brier_segment / 0.25))`  
`reco_stake_frac = max(0, min(kelly_partial * kelly_adj, KELLY_RECO_BANKROLL_CAP_FRAC))`

où **`KELLY_RECO_BANKROLL_CAP_FRAC = 0.15`** dans `app/dashboard.py` — soit **maximum 15 %** de la bankroll **disponible** pour ce pari recommandé.

Montant en € :

`reco_eur = br_avail * reco_stake_frac`.

### 6.6 Pré-remplissage du champ « Mise »

Valeur par défaut du `number_input` : approximativement `min(reco_eur, br_avail, …)` avec un plancher (ex. 0.01 €), pour refléter la reco Kelly 1/2 × Brier (ou 1/4 en backtest si sélectionné), capée à 15 % de la BR dispo.

---

## 7. Résumé opérationnel pour reproduire une prédiction

1. Préparer **toutes** les entrées de `TennisMLModel.predict_match` (noms, IDs, rangs, points, stats avancées, tournoi, surface, tour, métadonnées météo / marché si disponibles ; signaux **`p1_tac_*`**, **`p2_tac_*`**, **`p*_travel_penalty_index`**, **`p*_clutch52`** lorsque calculés depuis l’historique, comme dans le Live).
2. Charger le pickle **`models/xgb_model_tml_v45.pkl`** (ou le chemin défini dans le bundle) via `TennisMLModel._load_bundle_if_needed()` — **sans réentraînement après changement du nombre ou de l’ordre des features**, l’étape **`predict_proba`** est incohérente.
3. Construire une seule ligne `X_new` avec les colonnes `self.features` dans l’**ordre exact** (celui du pickle).
4. Appliquer la chaîne **calibration segment → caps (rang, fondamentaux, inactivité) → clutch match tendu si applicable**, comme dans `predict_match`.
5. Dériver `p1_true_odd`, `p2_true_odd`, `confidence`.
6. Pour la value : `ValueDetector.detect_value(O_book, O_true, confidence=...)`.
7. Pour la mise Live : utiliser `p = 1/O_true`, `O = cote utilisateur`, formule §6.

---

## 8. Performance du ML (KPI suivis)

Le fichier est maintenant explicite sur les indicateurs de performance à suivre.  
Ces valeurs sont **dynamiques** (elles dépendent de la période choisie, des CSV backtest chargés et du dernier entraînement du bundle).

### 8.1 KPI principaux

- **Accuracy** (classification binaire gagnant/perdant).
- **Brier score** (calibration probabiliste ; plus bas = mieux).
- **ROI value** (simulation des paris value).
- **Win rate**, **Profit factor**, **Max drawdown**, **Sharpe journalier** (projection bankroll).
- **CLV moyen / médian** (audit qualité des cotes prises vs closing line, indépendant du résultat).

### 8.2 Où les consulter dans l’app

- **`app/dashboard.py` → onglet Diagnostics (`tab4`)** :
  - `compute_model_diagnostics(...)` affiche `Accuracy`, `Brier`, `ROI value`, calibration et ROI par bucket de confiance.
- **`app/dashboard.py` → onglet Backtest (no-leak) (`tab2`)** :
  - simulation de bankroll avec métriques de risque/rendement (`PnL`, `BR finale`, `Max drawdown`, `ROI sur volume`, `Win rate`, `Profit factor`, `Sharpe`).
- **`app/dashboard.py` → onglet Portefeuille (`tab3`)** :
  - suivi réel des paris, ROI live et KPI CLV (moyenne/médiane/couverture).

### 8.3 Recalcul hors UI (référence)

Backtest annuel no-leak (ATP+WTA) :

`python scripts/backtest_2026.py --year <année> --out data/backtest_<année>_bets.csv`

Puis projection de staking dans l’UI (onglet Backtest CSV) avec les mêmes règles de filtre (EV, tournois, mode Kelly / % BR).

### 8.4 Dernier snapshot performance (template)

Renseigner ici la dernière photographie de performance validée :

- **Date du snapshot** : `YYYY-MM-DD HH:MM`
- **Bundle modèle** : `models/xgb_model_tml_v45.pkl` (ou autre)
- **Périmètre** : `ATP/WTA`, année(s), filtres EV, filtres tournois
- **Source éval** : `Diagnostics tab4` / `Backtest CSV tab2` / `Portefeuille tab3`

KPI à compléter :

- **Accuracy** : `... %`
- **Brier** : `...`
- **ROI value** : `... %`
- **Win rate** : `... %`
- **Profit factor** : `...`
- **Max drawdown** : `... %`
- **Sharpe journalier** : `...`
- **CLV moyenne (20 derniers paris)** : `... %`

---

## 9. Limites importantes

- Le bundle et les Elo **dépendent** du dernier entraînement exporté (fichier cible **`models/xgb_model_tml_v45.pkl`** et **`feature_importance_tml_v45.png`** avec le code actuel) ; tout changement du schéma de features impose un **nouvel entraînement** (`scripts/update_model_tml.py`). Les chiffres ne sont pas reproductibles sans ce fichier ni les tables SQLite alignées avec le pipeline d’export.
- Les **cotes book** viennent du scraping prematch (Tennis Explorer / Flashscore selon pipeline) — fichier CSV sous `data/scraped/`.
- La **mise en base** (`save_bet_enriched`) enregistre la mise réelle saisie ; ce document ne décrit que la **reco** affichée, pas une exécution automatique de paris.

---

*Généré à partir du code du dépôt BettingHUD — à tenir à jour si les features (v4.5 Human Factors), les garde-fous, le staking ou les KPI de suivi changent.*
