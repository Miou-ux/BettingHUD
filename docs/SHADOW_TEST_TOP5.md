# Shadow Test Top 5 (prod)

Objectif : comparer une stratégie candidate au Top 5 prod **sans impacter les notifications ni les mises réelles**.

## 1. Stratégie candidate actuelle

- `strategy_key`: `top5_ev25_rel85_p80`
- périmètre : majors ATP/WTA 250+ main draw
- proba favori modèle `>= 80%`
- EV favori `>= 25%`
- `data_reliability_score >= 85`
- tri proba desc, cap `5/jour`

Code : `scripts/shadow_top5.py`

## 2. Ce qui est capturé

Table SQLite: `shadow_top5_picks` (dans `data/bettinghud.db`)

Champs principaux:
- date, rang, match, proba, EV, score de fiabilité
- statut (`En cours`, `Gagné`, `Perdu`, `Annulé`)
- profit théorique (`theoretical_profit`) selon la logique Kelly existante

## 3. Intégration prod

Le shadow capture est exécuté **automatiquement** après la phase publications du cron 05:00 via:
- `scripts/morning_orchestrator.py` → `run_shadow_step()`

Comportement:
- non bloquant (si erreur shadow, la chaîne matinale reste OK)
- sans impact sur les messages Telegram/Discord

## 4. Commandes manuelles

Capture du jour :

```bash
/opt/bettinghud/venv/bin/python scripts/shadow_top5.py --capture
```

Sync des résultats :

```bash
/opt/bettinghud/venv/bin/python scripts/shadow_top5.py --sync-results
```

Rapport synthèse :

```bash
/opt/bettinghud/venv/bin/python scripts/shadow_top5.py --report
```

Tout en un :

```bash
/opt/bettinghud/venv/bin/python scripts/shadow_top5.py --capture --sync-results --report
```

## 5. Runbook d'évaluation (2 à 3 semaines)

Comparer **prod Top5** vs **shadow** sur la même fenêtre:

- `ROI flat`
- `ROI Kelly sur volume`
- hit rate
- max drawdown
- volume de picks (`n`)

### Critère go/no-go conseillé

Go si, sur la fenêtre:

1. ROI Kelly volume shadow > prod (écart significatif),
2. drawdown shadow non dégradé de façon majeure,
3. performance stable semaine par semaine (pas uniquement sur un seul pic).

Sinon: garder prod et itérer une autre variante (`strategy_key` différente).

## 6. Rapport hebdo Telegram admin

Script:

```bash
/opt/bettinghud/venv/bin/python scripts/shadow_weekly_telegram_notify.py --dry-run
/opt/bettinghud/venv/bin/python scripts/shadow_weekly_telegram_notify.py
```

Contenu:
- période glissante J-7 à J-1
- métriques `Prod` vs `Shadow` (n, hit, flat PnL/ROI, Kelly profit)
- verdict automatique `GO` / `NO-GO`

Cron prod:
- fichier source: `deploy/cron/shadow-weekly-telegram`
- log: `/opt/bettinghud/data/logs/shadow_weekly_telegram.log`
