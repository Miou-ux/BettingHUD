# Livres Référence — Betting Quant & Algorithmes

Date: 2026-07-09  
Contexte: notes de travail Obsidian pour guider les choix modèle/sélection/mise BettingHUD.

> Méthode de lecture: synthèse basée sur sources accessibles (sommaires, extraits, chapitres publics, papiers techniques) + connaissances de référence du domaine.  
> Objectif: extraire des principes directement actionnables pour `Top5` / `1D1P`.

---

## Lecture Prioritaire (Top 6)

## 1) Ed Thorp — *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market*
- Pourquoi c'est clé:
  - Base mathématique de la croissance logarithmique.
  - Montre le coût de l'overbetting (erreur la plus destructrice).
- Notes utiles:
  - Le sizing est aussi important que l'edge.
  - Fractional Kelly (0.25-0.65) est souvent optimal en pratique quand les proba sont imparfaites.
  - Sous-estimer son edge est moins dangereux que le surestimer.
- Application BettingHUD:
  - Garder `Kelly 0.85` par défaut (prod `kelly_policy.KELLY_BASE_FRAC`).
  - Réduire automatiquement Kelly par segment selon Brier/ECE (already aligned with current direction).

## 2) Joseph Buchdahl — *Squares & Sharps, Suckers & Sharks*
- Pourquoi c'est clé:
  - Excellente lecture des marchés, du closing line, et des biais de prix.
- Notes utiles:
  - Le bookmaker est un marché d'information, pas seulement un "prix".
  - Les gros outliers EV sont fréquemment des faux positifs (latence, injury/news, mapping).
- Application BettingHUD:
  - Continuer d'encadrer les EV extrêmes (`EV max`).
  - Renforcer garde-fous "market sanity" (`book_gap`, stale odds, mismatch line).

## 3) Steven Roman — *The Logic of Sports Betting*
- Pourquoi c'est clé:
  - Formalise EV, variance, bankroll, ruin probabilities.
- Notes utiles:
  - Hit rate seul n'est pas un KPI suffisant.
  - La distribution des cotes et le sizing déterminent la trajectoire BR.
- Application BettingHUD:
  - Prioriser dashboards avec `ROI on staked`, `max DD`, `profit factor`, `Sharpe` en plus du hit.
  - Evaluer les stratégies sur fenêtres roulantes, pas uniquement global YTD.

## 4) Matthew Davidow — *The Probability of Sports Betting*
- Pourquoi c'est clé:
  - Très orienté pricing/probabilité et fair odds.
- Notes utiles:
  - Une proba "bonne en classification" peut rester mauvaise pour parier si mal calibrée.
  - Le pricing est une discipline de calibration.
- Application BettingHUD:
  - Continuer focus Brier segmenté + calibration isotonic.
  - Ajouter suivi ECE segmenté en monitoring hebdo.

## 5) Stanford Wong — *Sharp Sports Betting*
- Pourquoi c'est clé:
  - Classique pragmatique sur "où est le vrai edge".
- Notes utiles:
  - L'edge réel vient souvent de process discipliné (line shopping, timing, filtrage), pas d'un seul modèle magique.
  - Qualité de l'exécution > sophistication théorique isolée.
- Application BettingHUD:
  - Industrialiser protocole de release: shadow -> gate -> rollout.
  - Séparer lane "core" (qualité) et lane "expansion" (volume contrôlé).

## 6) Hausch/Ziemba/Rubinstein (eds.) — *Efficiency of Racetrack Betting Markets* (et travaux Ziemba)
- Pourquoi c'est clé:
  - Référence académique sur efficience imparfaite des marchés de paris.
- Notes utiles:
  - Les anomalies existent, mais se referment dès qu'elles deviennent triviales.
  - Les gains robustes viennent d'avantages modestes + discipline de risque.
- Application BettingHUD:
  - Ne pas sur-optimiser sur une seule saison.
  - Préférer edges simples, robustes et monitorables.

---

## Compléments Algorithmiques (ML + validation)

## 7) Hastie / Tibshirani / Friedman — *The Elements of Statistical Learning*
- Apports:
  - Bias-variance, régularisation, ensembling.
  - Lecture indispensable pour arbitrer XGBoost vs alternatives.
- Action:
  - Benchmark strict: XGB vs LGBM vs CatBoost avec mêmes splits temporels.

## 8) Kevin Murphy — *Probabilistic Machine Learning*
- Apports:
  - Calibration, incertitude, modèles probabilistes.
- Action:
  - Introduire intervalles d'incertitude pour moduler le sizing (risk-aware Kelly).

## 9) Marcos Lopez de Prado — *Advances in Financial Machine Learning*
- Apports:
  - Purged CV, embargo, robustesse temporelle.
- Action:
  - Renforcer protocoles anti-leakage et sélection de features temporelles.

## 10) Papers bankroll allocation (Kelly vs MPT en betting)
- Exemple utile:
  - *Optimal sports betting strategies in practice: an experimental review*.
- Action:
  - Tester approche portefeuille/jour (contraintes de corrélation des picks), pas seulement pick-by-pick.

---

## Ce qu'il faut retenir pour BettingHUD

## A. "Augmenter le Brier" est une erreur
- Cible: Brier plus bas, surtout sur segments faibles (`ATP_Hard`, etc.).
- Pilotage recommandé:
  - `Brier`, `ECE`, `ROI`, `DD` par segment et par fenêtre roulante.

## B. Changer d'algo n'est pas la priorité #1
- D'abord:
  - quality gates (`EV max`, `book_gap`, reliability),
  - calibration,
  - sizing.
- Ensuite seulement:
  - challenger model (LGBM/CatBoost/stacking) en shadow.

## C. Plus de volume sans sacrifier qualité
- Architecture en 2 lanes:
  - Lane Core: filtres stricts, BR principale.
  - Lane Expansion: seuils plus larges mais caps de risque plus durs.
- Répartition capital:
  - 70-85% Core, 15-30% Expansion.

---

## Plan de lecture concret (2 semaines)

## Semaine 1
- Thorp (Kelly) + Roman (logic EV/variance).
- Sortie attendue:
  - policy staking v2 (fractional + caps + segment multipliers).

## Semaine 2
- Buchdahl + Davidow + 1 papier académique bankroll.
- Sortie attendue:
  - policy sélection v2 (market sanity, EV bands, quality gates).

---

## Checklist d'implémentation issue des lectures

- [ ] Ajouter `ECE` hebdo par segment au monitoring.
- [ ] Ajouter "overbet risk guard" (cap dynamique quand calibration se dégrade).
- [ ] Shadow test 30 jours d'une variante `EV<=50/75 + book_gap<=30`.
- [ ] Benchmark XGB vs LGBM vs CatBoost (mêmes splits temporels).
- [ ] Décision go/no-go basée sur ROI + DD + calibration, pas hit seul.

---

## Références (à prioriser)
- Ed Thorp — *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market*.
- Joseph Buchdahl — *Squares & Sharps, Suckers & Sharks*.
- Steven Roman — *The Logic of Sports Betting*.
- Matthew Davidow — *The Probability of Sports Betting*.
- Stanford Wong — *Sharp Sports Betting*.
- Hausch/Ziemba/Rubinstein — *Efficiency of Racetrack Betting Markets*.
- Hastie/Tibshirani/Friedman — *The Elements of Statistical Learning*.
- Kevin Murphy — *Probabilistic Machine Learning*.
- Lopez de Prado — *Advances in Financial Machine Learning*.

