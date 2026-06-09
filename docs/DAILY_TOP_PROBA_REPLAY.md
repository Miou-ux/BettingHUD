# Replay réel — top probas journalier (ATP / WTA)

Stockage propre du **top 15 probas favori modèle** par jour et par circuit, pour rejouer la stratégie sur les cotes/prédictions **capturées au moment du snapshot live** (pas le backtest tennis-data).

---

## Objectif

| Besoin | Solution |
|--------|----------|
| Replay « réel » plus tard | SQLite + archive JSONL append-only |
| ATP et WTA séparés | 15 lignes max par `(calendar_date, tour)` |
| Données complètes | Cotes, probas, EV favori, Brier, segment, snapshot |
| Résultats matchs | Sync auto via `match_results` |

---

## Où c’est stocké

### 1. SQLite — état canonique du jour

**Table** : `daily_top_proba_picks` dans `data/bettinghud.db`

**Clé** : `pick_key = {calendar_date}|{ATP|WTA}|{rank:02d}`  
Ex. `2026-05-27|WTA|03`

**Une ligne** = un rang (1–15) pour un circuit un jour calendaire **Europe/Paris**.

Colonnes principales :

| Groupe | Champs |
|--------|--------|
| Identité | `calendar_date`, `match_date`, `tour`, `rank`, `top_limit`, `match_id`, `match_name`, `player1`, `player2` |
| Favori modèle | `fav_side`, `fav_player`, `underdog_player`, `p1_prob`, `p_model_fav` |
| Marché | `odd_fav`, `odd_underdog`, `true_odd_fav`, `true_odd_underdog`, `ev_fav`, `ev_fav_pct`, `p_implicit_fav`, `book_gap_pp` |
| Contexte | `tournament`, `surface`, `match_time`, `tourney_level`, `confidence` |
| Mise théorique | `segment_key`, `segment_brier`, `theoretical_stake_frac` (Kelly ½ × Brier) |
| Capture | `snapshot_built_at`, `snapshot_tier`, `capture_source`, `first_captured_ts`, `last_captured_ts` |
| Résultat | `status`, `fav_won`, `winner_resolved`, `score_final`, `theoretical_profit`, `settled_ts` |

**Upsert** : à chaque capture, le rang est mis à jour (dernier snapshot) ; `first_captured_ts` est conservé.

### 2. JSONL — historique replay (append-only)

**Dossier** : `data/exports/daily_top_proba/`

**Fichier** : `{calendar_date}.jsonl` — une ligne JSON par capture (matin, rebuild, etc.)

Contenu d’une ligne :

```json
{
  "captured_ts": "2026-05-27T09:15:00+02:00",
  "calendar_date": "2026-05-27",
  "snapshot_built_at": 1716798900.0,
  "capture_source": "live_snapshot",
  "n_picks": 30,
  "picks": [ /* 15 WTA + 15 ATP */ ]
}
```

→ Permet de rejouer **exactement** ce qui était affiché à 9h vs 18h si le snapshot change.

---

## Quand c’est enregistré

Automatiquement en **mode daemon** (sans ouvrir Streamlit) :

| Source | Rôle |
|--------|------|
| **`portfolio_results_daemon`** | Passe toutes les **10 min** : capture snapshot + sync résultats |
| **`live_data_daemon`** (dashboard ouvert) | Passe toutes les **15 min** après refresh snapshot |

Autres déclencheurs :

- sync report algo depuis snapshot (`sync_algo_report_from_snapshot`) ;
- rebuild live (`rebuild_live_projection.py`) ;
- pipeline matin (`morning_live_pipeline.py`) ;
- ouverture Live Tracker (sync UI, anti-spam 120 s).

**Anti-spam** (fichier `data/cache/.daily_top_proba_daemon.json`) :

- upsert SQLite : max 1× / 10 min si snapshot inchangé ;
- JSONL append : snapshot rebuild **ou** toutes les **60 min** (`BETTINGHUD_DAILY_TOP_PROBA_JSONL_INTERVAL_SEC`).

Lancer le daemon portefeuille (Windows) :

```powershell
py -3 -m scripts.portfolio_results_daemon
# ou scripts\run_portfolio_daemon.bat
```

Manuel (force une capture immédiate) :

```powershell
py -3 scripts/persist_daily_top_proba.py
```

### Récupérer l’historique déjà enregistré (backfill)

Avant la capture daemon, les données existaient surtout dans **`algo_opportunities`** (value bets détectés) et les **JSONL** du dossier export.

```powershell
py -3 scripts/backfill_daily_top_proba.py
# plage optionnelle :
py -3 scripts/backfill_daily_top_proba.py --start-date 2026-05-18 --end-date 2026-05-26
```

| Source | Fidélité | `capture_source` |
|--------|----------|-------------------|
| JSONL `data/exports/daily_top_proba/` | **Exacte** (captures snapshot) | source d’origine |
| SQLite `algo_opportunities` | **Proxy** — top 15 parmi matchs avec opportunité value, pas le vrai classement snapshot complet | `backfill_algo_opportunities` |

Le backfill **ne remplace pas** les jours déjà capturés en live/daemon (ex. 27/05). Option `--force` pour écraser.

Après import : sync auto des résultats (`match_results` → `status` / `fav_won`).

---

## Règles de sélection

1. Matchs du **jour calendaire** (Paris) avec cotes valides (`odd_p1/p2 > 1`).
2. Même garde-fou qualité que le report algo : rang/points des deux joueurs (`_filter_matches_for_algo_report`).
3. **Favori modèle** = `max(capped_p1_prob, 1 − p1)`.
4. Tri **proba favori décroissante**.
5. **Top 15** séparés pour **ATP** et **WTA**.

*(Pas de filtre EV à l’enregistrement — filtre au replay si besoin via `ev_fav_pct`.)*

---

## Lire / exporter pour replay

```python
from scripts.bets_db import read_daily_top_proba_picks

rows = read_daily_top_proba_picks(calendar_date="2026-05-27", tour="WTA")
```

Script d’audit RG existant : `scripts/audit_rg_top5_portfolio.py` (opportunités value) — **distinct** de cette table.

---

## Sync résultats

Comme `algo_opportunities` :

- daemon portefeuille (`portfolio_results_daemon.py`) ;
- après chaque upsert.

Résolution via cache `match_results` → `status` **Gagné/Perdu** sur le **favori modèle** (`fav_won`).

---

## Fichiers code

| Fichier | Rôle |
|---------|------|
| `scripts/daily_top_proba_store.py` | Collecte + JSONL + orchestration |
| `scripts/bets_db.py` | Schéma SQLite + upsert + sync résultats |
| `scripts/persist_daily_top_proba.py` | CLI manuel |
| `scripts/backfill_daily_top_proba.py` | Récupération historique JSONL + algo_opportunities |
| `tests/test_daily_top_proba_store.py` | Tests collecte |

---

## Différence vs `algo_opportunities`

| | `algo_opportunities` | `daily_top_proba_picks` |
|--|----------------------|-------------------------|
| Sélection | Value bets (EV ≥ seuil) | Top 15 **proba favori** / circuit / jour |
| Côté parié | Côté value | Toujours **favori modèle** |
| Usage | Report opportunités live | **Replay stratégie top probas** |

---

## UI web — 1 Day 1 Pick

Page publique React : **`/1-day-1-pick`** (CourtAlpha, sans login).

| Élément | Détail |
|---------|--------|
| API | `GET /api/picks/one-day-one-pick` |
| Sélection | Chaque jour : meilleur **rank=1** entre ATP et WTA (`p_model_fav` max) |
| Filtres | Tournois majeurs main draw 250+, EV favori 15–100 % |
| Mise | Kelly ½ × Brier (`theoretical_stake_frac`) sur BR 100 € par défaut |
| Résultats | `sync_daily_top_proba_from_results()` à chaque requête API |
| Affichage | **Pick du jour** en carte + tableau historique + courbe bankroll (`curve[]`) |
| Jour courant | Inclus par défaut ; snapshot live si pas encore persisté ; +1 ligne/jour via daemon |

Service API : `CourtAlpha/api/services/one_day_one_pick.py`.

---

## Liens

- [[CHART_TOP_PROBAS_JOUR]] — UI top probas
- [[WEB_REACT]] — pages publiques React
- [[BACKTEST_TOP10_PROBA_SIMULATIONS]] — backtest historique tennis-data (comparatif)
