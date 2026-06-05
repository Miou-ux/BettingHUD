# Backtest 2026 — majeurs 250+, EV +15 % → +200 %

Script : **`scripts/backtest_major_ev_2026.py`**

## Périmètre

| Critère | Valeur |
|---------|--------|
| Année | **2026** (no-leak : modèle entraîné avant `2026-01-01`) |
| Circuits | ATP + WTA |
| Tournois | **Main draw 250+** (hors Challenger, ITF, UTR — aligné Paris du jour / `/jourmajor`) |
| EV favori | **+15 % → +200 %** (inclus) |
| Cotes | tennis-data.co.uk (moyenne bookmakers) |

## Scénarios calculés

1. **Tous les paris** du pool (EV+ majeurs)
2. **Top 5 proba / jour** (stratégie Paris du jour)
3. **Top 10 proba / jour**

Mises : référence **1 unité fixe** + **Kelly ½ × Brier**, plafond **15 %** BR (comme le live).

## Commandes

```bash
# Utilise data/backtest_2026_bets.csv s'il existe
py -3 scripts/backtest_major_ev_2026.py --skip-backtest

# Regénère le CSV source (long : entraînement + prédictions 2026, EV jusqu'à 200 %)
py -3 scripts/backtest_major_ev_2026.py --regen-csv
```

> Si `backtest_2026_bets.csv` a été généré avec EV max 100 %, les buckets 100–200 % seront vides tant que tu n'as pas relancé avec `--regen-csv`.

# EV personnalisée
py -3 scripts/backtest_major_ev_2026.py --ev-min-pct 15 --ev-max-pct 200
```

## Exports

| Fichier | Contenu |
|---------|---------|
| `data/reports/backtest_major_ev_2026_summary.csv` | Métriques par scénario |
| `data/reports/backtest_major_ev_2026_ev_buckets.csv` | ROI / hit par tranche EV |
| `data/reports/backtest_major_ev_2026_bets.csv` | Détail paris Top 5 proba / jour |

## Voir aussi

- `scripts/backtest_2026.py` — génération CSV no-leak
- `scripts/simulate_top10_proba_2026.py` — grille Top N multi-années
- [[CHALLENGERS_ET_TOURNOIS]] — définition tiers tournois
