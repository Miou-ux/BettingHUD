# Sélection hybride Top 5 / 1 Day 1 Pick

Référence unique pour la logique **hybride** déployée en prod (juillet 2026).

## Périmètre

| Canal / mode | Fichier d’entrée | Sélection |
|--------------|------------------|-----------|
| **Top 5** Telegram matin, `/top5`, API `/api/picks/top5` | `pick_modes.TOP5` → `collect_hybrid_proba_picks` | Jusqu’à **5** picks/jour |
| **1 Day 1 Pick** TG, Discord, web live | `pick_modes.ONE_PICK_ONE_DAY` → `load_1d1p_today_pick` | **Rang 1** de la même sélection hybride |
| **Paris du jour** `/jour`, Live Tracker | `pick_modes.TODAY` | **Inchangé** (value bets EV ≥ 15 %, pas hybride) |

## Règles

| Étape | Règle |
|-------|--------|
| Pool | Matchs du jour (Europe/Paris), tournois **majors 250+** main draw |
| Proba | Favori modèle **≥ 77 %** |
| Fiabilité | `data_reliability_score ≥ 75` |
| Gap book | **≤ 30 pp** (écart proba modèle vs cote book) |
| Exclusion | Pas de publication si flag **`duplicate_model_prob`** |
| **Tier 1** | EV favori **15–30 %** (inclus) — remplissage prioritaire |
| **Tier 2** | EV favori **30–50 %** (30 exclus, 50 inclus) — complément si &lt; 5 picks tier 1 |
| Tri | **EV favori** ↓ (tie-break proba, puis nom match) |
| Dédup | `dedupe_top_proba_rows_by_match` (doublons snapshot TE) |
| Cap | **5** picks/jour (Top 5) ; 1D1P = **pick #1** |

## Mise (Kelly)

| Paramètre | Valeur prod |
|-----------|-------------|
| Fraction Kelly | **0,65** (`scripts/kelly_policy.py`) |
| Ajustement | × `max(0, 1 − Brier_segment / 0,25)` |
| Plafond | **15 %** de la liquidité / BR disponible par pari |
| Code live | `bets_db._algo_kelly_stake_frac` |

## Code

| Module | Rôle |
|--------|------|
| `scripts/hybrid_pick_selection.py` | `select_hybrid_picks()`, constantes, ligne critères Telegram |
| `scripts/daily_top_proba_store.py` | `collect_hybrid_proba_picks()` — construction pool + appel hybride |
| `scripts/discord_1d1p_core.py` | `load_1d1p_today_pick()` — rang 1 hybride |
| `scripts/pick_modes.py` | Point d’entrée unifié web / TG / Discord |
| `scripts/backtest_prod_top5_2026.py` | Backtest aligné prod (`select_prod_top5_day` → hybride) |
| `scripts/backtest_top5_hybrid_p80_2026.py` | Comparatifs exploratoires (cap 5 vs sans cap) |

## Backtest 2026 (référence juillet 2026)

| Période | Paris | Hit | Flat 5 € | vs Top5 prod (ancien) |
|---------|------:|----:|---------:|----------------------|
| Année 2026 | 192 | 87,5 % | +296 € | prod +265 €, hit 65,8 % |
| Live ≥ 18/05 | 53 | 94,3 % | +100 € | prod +36 €, hit 62,0 % |

Sans plafond journalier : +9 picks / +3 € sur 2026 — le cap 5/jour est conservé.

## Déploiement prod

```bash
# Depuis le poste (rsync / deploy habituel)
scp scripts/hybrid_pick_selection.py scripts/daily_top_proba_store.py \
    scripts/discord_1d1p_core.py scripts/pick_modes.py \
    scripts/telegram_top5_notify.py scripts/backtest_prod_top5_2026.py \
    bettinghud:/opt/bettinghud/scripts/

ssh bettinghud "sudo systemctl restart courtalpha-api bettinghud-telegram-bot bettinghud-dashboard"
```

**Republication 1D1P** (si changement en cours de journée) :

```bash
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/repost_1d1p_today.py --apply"
```

## CourtAlpha (replay historique)

Le replay web (`CourtAlpha/api/services/one_day_one_pick.py`) utilise `select_1d1p_pick` (hybride). **UI** : i18n, SEO, méthodo et `selection.description` API alignés juillet 2026.

## Liens

- [[TELEGRAM_TOP5]] — bot et canaux
- [[ONE_DAY_ONE_PICK]] — 1D1P publication
- [[DATA_RELIABILITY]] — fiabilité et `duplicate_model_prob`
- [[BACKTEST_PROD_TOP5_2025_2026]] — méthodo backtest
