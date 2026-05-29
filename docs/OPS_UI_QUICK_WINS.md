# Quick wins ops & UI (#1 · #2 · #3)

Trois améliorations à **faible risque** pour diagnostiquer le live et sécuriser les picks avant de miser.

---

## #1 — Audit parité picks du jour

**Script** : `scripts/audit_daily_picks_parity.py`

Compare sur le snapshot du jour (Top 5 proba, EV favori +15 % → +100 %) :

| Source | Logique |
|--------|---------|
| **Paris du jour** | `_collect_top_favorite_action_cards()` + filtre ATP/WTA majeur |
| **Telegram /top5** | `collect_top5_proba_picks()` |
| **DB daemon** | `daily_top_proba_picks` (top 5 / circuit + recomposition global top 5) |

```bash
py -3 scripts/audit_daily_picks_parity.py
py -3 scripts/audit_daily_picks_parity.py --date 2026-05-28
py -3 scripts/audit_daily_picks_parity.py --export data/reports/audit_picks_parity.csv
```

- **Exit 0** : Paris du jour ≡ Telegram (matchs + ordre).
- **Exit 1** : écart détecté (détail dans la sortie console).
- Écart **DB global top 5** vs Paris : souvent **attendu** (daemon = top 15 par circuit, sans filtre EV).

---

## #2 — Bandeau « État système » (Paramètres)

**UI** : onglet **Paramètres** → bloc **État système** (juste sous le bandeau PREPROD/PROD).

**Code** : `app/dashboard.py` — `_collect_system_status()`, `_render_system_status_banner()`.

| Indicateur | Source |
|------------|--------|
| CSV prematch | Dernier `data/scraped/prematch_odds_*.csv` (mtime) |
| Snapshot live | `snapshot_meta()` + `data/cache/live_matches_snapshot*.joblib` |
| Daemon résultats | `data/cache/.portfolio_results_daemon.heartbeat` (< 11 min = actif) |
| Pipeline matin | Dernier `data/cache/logs/morning_pipeline_*.log` ou `data/logs/morning_pipeline_cron.log` |
| Paris en cours | `COUNT(*)` sur `user_bets` statut « En cours » |

**Niveaux** :

- **Vert (Prêt à jouer)** : toutes les sources récentes.
- **Orange** : CSV > 30 min, snapshot > 6 h, daemon absent, pipeline sans « Pipeline terminé ».
- **Rouge** : CSV absent / > 3 h, snapshot absent / > 24 h, daemon mort, pipeline en erreur.

L’expander **Détail fichiers & commandes** liste les chemins et les scripts de relance.

---

## #3 — Empty states explicites (entonnoir EV)

**Code** : `app/dashboard.py` — `_compute_favorite_ev_funnel_stats()`, `_format_favorite_ev_funnel_caption()`.

Affiché quand un onglet est vide :

| Onglet | Message type |
|--------|----------------|
| **Paris du jour** | `17 pool → 17 jour → 17 cotes/probas → 1 EV 15–100 % → Top 5` |
| **Top probas jour** | Entonnoir + hint toggle EV |
| **Live Tracker** | `24 disque → 22 cotes → 20 rang → 18 horaire → 12 jour+ATP/WTA → 0 filtres UI` |

Permet de distinguer **filtre EV vide** vs **snapshot absent** vs **filtres UI trop stricts**.

---

## Vérification rapide (PREPROD)

```bash
py -3 -m py_compile app/dashboard.py scripts/audit_daily_picks_parity.py
py -3 scripts/audit_daily_picks_parity.py
```

Puis ouvrir le dashboard → **Paramètres** (bandeau 5 métriques) et **Paris du jour** (vérifier caption entonnoir si 0 pick).

---

## Liens

- [[SCHEDULE_MISES_A_JOUR]] — horaires scrape / pipeline / daemon
- [[OPS_PROD_DEPANNAGE]] — incidents PROD
- [[TELEGRAM_TOP5]] — bot et commande `/top5`
