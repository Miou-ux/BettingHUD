# Chart « Top probas jour »

Spécification du graphique et du **toggle EV partagé** (onglet **📈 Top probas jour** + **🎯 Live Tracker**).

## Objectif

Visualiser en un coup d’œil les **15 matchs du jour** (calendrier **Europe/Paris**) où le modèle accorde la **plus forte probabilité au favori**, et comparer cette proba à celle **implicite du bookmaker** sur le même joueur.

Le **même toggle EV** est partagé avec le **Live Tracker** : quand il est actif, les tuiles value bets y sont filtrées et triées comme ce top 15 (proba favori modèle ↓, côté favori uniquement, max 15 tuiles). Le graphique + tableau restent **uniquement** dans l’onglet Top probas jour (pas de bloc dupliqué dans le Live Tracker).

## Source de données

| Élément | Détail |
|---------|--------|
| Fichier UI | `app/dashboard.py` — `_match_favorite_model_metrics`, `_collect_top_model_prob_rows`, `_build_top_probas_day_chart`, `_build_top_model_probs_df`, `_render_top_model_probs_panel`, `_render_top_model_probs_tab`, `_render_favorite_ev_band_toggle`, `_filter_matches_favorite_ev_band` |
| Constantes | `TOP_PROBAS_DISPLAY_LIMIT = 15`, `FAVORITE_EV_BAND_MIN_FRAC = 0.15`, `FAVORITE_EV_BAND_MAX_FRAC = 1.0` |
| Snapshot | `data/cache/live_matches_snapshot.full.joblib` (ou variante active) |
| Filtre jour | `_is_today_calendar_match()` — date `match.date` = jour courant (onglet Top probas) |
| Filtre tournoi | Défaut : **main draw 250+** (`is_major_tournament_match`). Toggle **Inclure les Challengers** = même logique que Live Tracker. |
| Cotes | `odd_p1` / `odd_p2` > 1.0 obligatoire |

**Fréquence de mise à jour** : identique au snapshot live (pipeline matin, `rebuild_live_projection.py`, bouton **Actualiser le Live Tracker**).

## Métriques

### Proba modèle (barres horizontales)

- Champ snapshot : `feature_snapshot.capped_p1_prob` (proba capée côté P1).
- **Favori modèle** : joueur avec `max(p1, 1 − p1)`.
- Affichage : `proba_modele_pct = fav_p × 100` (%).

### Proba book implicite (trait vertical jaune)

- Cote du **même favori** que le modèle : `odd_fav` = `odd_p1` ou `odd_p2` selon le côté favori.
- Formule : `proba_book_pct = 100 / odd_fav` (proba implicite brute, **sans** retrait de marge book).

### Gap book (tableau + tooltip)

- Champ snapshot : `book_gap_pp` (écart max modèle vs book sur les deux côtés, en points de pourcentage).
- Alerte visuelle tableau : gap ≥ **25 pp** (orange) — constante `_TOP_PROBAS_GAP_WARN_PP`.

### Tri et limite

- Tri décroissant sur `proba_modele_pct`.
- **Top 15** lignes (`TOP_PROBAS_DISPLAY_LIMIT = 15`).

### Filtre EV (toggle UI partagé)

**Libellé widget** : `Top 15 · EV favori +15 % à +100 % (tri proba favori ↓)`  
(Live Tracker : variante avec mention « tri tuiles par proba favori ↓ ».)

| Clé session | Rôle |
|-------------|------|
| `favorite_ev_band_filter` | **État canonique** (on/off) — lu par filtres, tri et graphique |
| `favorite_ev_band_filter_live` | Widget Streamlit dans l’onglet Live Tracker |
| `favorite_ev_band_filter_topprobas` | Widget Streamlit dans l’onglet Top probas jour |

Les deux widgets synchronisent l’état canonique (`on_change` + init au render).

Quand activé :
- **Top probas jour** : le top 15 ne garde que les lignes avec **EV favori** dans la bande ci-dessous ;
- **Live Tracker** : filtre les matchs sur la même bande EV favori ; affiche jusqu’à **15 tuiles** value bets triées par **proba favori modèle** ↓ (**côté favori uniquement**) ; masque le sélecteur Composite / Sharpe / EV.

Quand désactivé :
- Top probas : top 15 sans filtre EV ;
- Live Tracker : tri habituel des value bets (Composite par défaut).

Bande **EV favori** :
- **EV min** : **+15%**
- **EV max** : **+100%**

Définition :
- `EV = p_fav * cote_fav - 1`
- `p_fav = max(p1, 1-p1)` issu du snapshot `feature_snapshot.capped_p1_prob`
- `cote_fav = odd_p1` ou `odd_p2` selon le favori côté modèle

## Encodage visuel (Altair)

| Élément | Type | Style |
|---------|------|--------|
| Axe Y | Nominal | **`favori_label`** = nom du **favori modèle** seul (sans adversaire) |
| Tri Y | Défaut | Liste explicite `label_order` (proba croissante → **plus forte en haut**), pas tri alphabétique |
| Axe X | Quantitatif | 0–100 %, titre « Probabilité (%) » |
| Barres | `mark_bar` | Couleur par **circuit** : ATP `#00B0FF`, WTA `#c75b9a`, autre `#8A8D98` (`_TOP_PROBAS_CHART_TOUR_COLORS`) |
| Book | `mark_tick` | Jaune `#f0d78f`, épaisseur 3 |
| Références | `mark_rule` pointillés | **50 %**, **70 %**, **80 %** (`_TOP_PROBAS_CHART_REF_LINES`) |
| Hauteur | Dynamique | `max(360, 26 × n_lignes + 48)` px |

Constantes couleur modèle (référence tableau) : `_TOP_PROBAS_CHART_MODEL_COLOR = #294c86` (surbrillance colonne « Favori modèle » dans le dataframe stylé).

## Interprétation

1. **Barre longue + trait jaune proche** : modèle et marché alignés sur le favori.
2. **Barre bien au-delà du trait** : modèle plus confiant que le book (vérifier `gap_pp` avant toute décision).
3. **Trait jaune au-delà de la barre** : marché plus confiant que le modèle sur ce favori.
4. **Pointillés 70 / 80 %** : repères de confiance « forte » / « très forte » (indicatif, pas un seuil de pari).

## Dépendances

- **Altair** 6.x (`requirements.txt`) — rendu via `st.altair_chart`.
- Pas de retrain ML requis pour modifier ce graphique.

## Évolutions possibles

- Courbe historique jour / jour (nécessite persistance des tops quotidiens).
- Deuxième série « proba book normalisée » (retrait marge overround).
- Filtre circuit (ATP / WTA) dans l’onglet.
