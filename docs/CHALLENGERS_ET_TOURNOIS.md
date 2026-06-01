# Challengers, WTA 125 et filtrage tournois

Dernière mise à jour : **1 juin 2026**.

Référence pour le **Live Tracker**, **Paris du jour**, **Telegram** et le **build snapshot** après les correctifs TE (Foggia, catégorie `Challenger`, points vainqueur).

> Voir aussi : [[ARCHITECTURE_ACTUELLE_ET_MISES]] § 3 · [[TELEGRAM_TOP5]] · [[CHANGELOG_RECENT]] § 0.22

---

## 1. Problème Tennis Explorer

Le scrape (`scripts/scraper_prematch.py`, URL `type=all`) ramène **tous** les matchs du jour : ATP/WTA tour principal, **Challenger**, **WTA 125**, ITF, UTR, etc.

TE ne nomme pas toujours les tournois de façon explicite :

| Exemple TE | `category` scrape | Niveau réel |
|------------|-------------------|-------------|
| `Birmingham challenger` | `Challenger` | ATP Challenger |
| `Perugia challenger` | `Challenger` | ATP Challenger |
| `Foggia` | `WTA` | **WTA 125** (125 pts vainqueur) |
| `Birmingham` (WTA) | `WTA` | **WTA 125** |
| `Roland Garros` | `ATP` | Grand Chelem (2000 pts) |

Un filtre basé uniquement sur le mot **« challenger »** dans le nom du tournoi laisse passer Foggia/Birmingham WTA et **exclut** la majorité des ATP Challengers (`category = Challenger`).

---

## 2. Module `scripts/tournament_tier.py`

Classification centralisée (snapshot, Paris du jour, Telegram, tests).

### 2.1 Points vainqueur TE

Lors du scrape, pour chaque tournoi (URL `td.t-name a`), le script ouvre la fiche TE et lit la ligne **`winner`** du tableau « Tournament details » → colonne **Ranking points**.

Champ stocké : **`tourney_winner_points`** (CSV + snapshot).

| Points | Interprétation |
|--------|----------------|
| **125** | ATP Challenger ou WTA 125 |
| **250+** | Main draw (250, 500, 1000, GS) |
| **2000** | Grand Chelem (détection URL : `french-open`, `wimbledon`, etc.) |

Variables d’environnement : aucune (logique fixe, seuil **250** = `MIN_MAIN_DRAW_WINNER_POINTS`).

### 2.2 Fonctions

| Fonction | Rôle |
|----------|------|
| `is_main_draw_tournament_match(m)` | Paris du jour, `/top5`, persistance top 15 — **250+ pts** ou nom sans token mineur |
| `is_challenger_tier_match(m)` | `/jourchallenger`, toggle Live Tracker — `category=Challenger`, nom/url challenger, ou **pts &lt; 250** |
| `is_major_tournament_match(m)` | Alias de `is_main_draw_tournament_match` |

Exclusions communes (nom ou URL) : `itf`, `utr`, `futures`, `m15`/`w25`/…, etc.

---

## 3. Build snapshot live

Fichier : `app/dashboard.py` — `_load_prematch_df_for_live()`.

| Étape | Comportement |
|-------|----------------|
| Lecture CSV prematch | Jour + demain |
| Circuit | `include_challengers=True` → garde **ATP**, **WTA**, **`Challenger`** (exclut ITF/UTR par nom) |
| Calendrier | Aujourd’hui + demain |
| Enrichissement ML | Profils, rangs, features |

Le snapshot contient donc **Challengers ATP + WTA 125** ; l’UI les masque par défaut.

**Rebuild** : `py -3 scripts/rebuild_live_projection.py` (PROD : après `git pull`).

---

## 4. Live Tracker (UI)

### 4.1 Toggle « Inclure les Challengers »

- **Défaut** : masqué (`_is_atp_wta_circuit_match`, `include_challengers=False`) — équivalent **main draw** par nom (sans mot challenger).
- **Coché** : `include_challengers=True` + logique `is_challenger_tier_match` via filtre jour.

Clé Streamlit : `live_include_challengers`.

### 4.2 Garde-fous qui limitent encore l’affichage

Même avec le toggle, un match peut être absent si :

- pas de **rang/points** fiables sur les deux joueurs (`_match_has_rank_points_source`) ;
- cotes invalides ;
- filtre **jour** / match déjà commencé (grâce 90 min) ;
- pas encore dans le snapshot (rebuild requis après changement de code).

C’est normal qu’un tournoi soit au CSV (ex. Centurion 192 lignes) mais peu de cartes en UI (ex. 3 matchs).

---

## 5. Paris du jour & Telegram `/top5`

**Uniquement main draw** (`is_major_tournament_match` dans `collect_top5_proba_picks`).

- Exclut **Challenger**, **WTA 125** (Foggia), ITF, UTR.
- Bande EV **+15 % → +100 %**, tri **proba** ↓, top **5**.

Aligné entre dashboard **Paris du jour** et bot **`/top5`** / envoi matinal.

---

## 6. Telegram `/jourchallenger`

| Critère | Valeur |
|---------|--------|
| Tournois | `is_challenger_tier_match` (Challenger ATP, WTA 125, nom/url) |
| Jour | Aujourd’hui (Europe/Paris) |
| EV | **+15 % → +100 %** |
| Tri | **Proba modèle** ↓ |

| Variable | Défaut |
|----------|--------|
| `TELEGRAM_JOURCHALLENGER_EV_MIN_PCT` | 15 |
| `TELEGRAM_JOURCHALLENGER_EV_MAX_PCT` | 100 |
| `TELEGRAM_CHALLENGER_PICKS_LIMIT` | 0 (= tous) |

Alias : `/challengers`.  
Aperçu PREPROD : `py -3 scripts/telegram_top5_notify.py --challenger --dry-run --force`

Fichiers : `scripts/live_tracker_picks.py` (`load_live_tracker_challenger_day_picks`), `scripts/telegram_bot_daemon.py`.

---

## 7. Telegram `/jour` (rappel)

Tous circuits **EV+** (seuil défaut 0 %), tri **priorité composite** — **pas** limité aux Challengers ni au main draw.

---

## 8. Commandes ops (PROD)

```bash
ssh bettinghud
cd /opt/bettinghud
git pull origin main

# Scrape + points vainqueur (~1–2 min)
./venv/bin/python scripts/scraper_prematch.py

# Rebuild snapshot (~10–15 min)
export BETTINGHUD_HEADLESS=1
./venv/bin/python scripts/rebuild_live_projection.py

sudo systemctl restart bettinghud-dashboard
```

Logs : `data/logs/morning_pipeline_cron.log`, `data/logs/manual_rebuild_*.log`.

---

## 9. Fichiers modifiés (juin 2026)

| Fichier | Changement |
|---------|------------|
| `scripts/tournament_tier.py` | **Nouveau** — classification main draw vs challenger tier |
| `scripts/scraper_prematch.py` | `tournament_url`, `tourney_winner_points`, enrichissement TE |
| `app/dashboard.py` | Toggle Challengers, build `category=Challenger`, filtre vectorisé |
| `scripts/daily_top_proba_store.py` | Top 5 / top 15 : `is_major_tournament_match` |
| `scripts/live_tracker_picks.py` | `/jourchallenger`, `is_challenger_tier_match` |
| `scripts/telegram_top5_notify.py` | `run_challenger_daily_picks_notify` |
| `scripts/telegram_bot_daemon.py` | Commande `/jourchallenger` |
| `tests/test_tournament_tier.py` | Tests Foggia, Challenger category, RG |

Commits principaux : `54cf276`, `bb3911b`, `28e8ee6`, `5d63936`, `5cc9e83`.

---

## 10. FAQ

**Pourquoi seulement Foggia/Birmingham avant le correctif ?**  
Le build ne gardait que `ATP`/`WTA` ; les tournois `Challenger` étaient supprimés. Seuls les WTA 125 sans le mot « challenger » passaient.

**Les Challengers sont-ils dans le pipeline matin ?**  
Oui, dans le **snapshot** (build). Paris du jour et `/top5` les **excluent**. `/jourchallenger` et le toggle Live Tracker les **affichent**.

**PREPROD vs PROD** : voir [[ENVIRONNEMENTS]] — doc et code via Git ; données via scrape/rebuild sur chaque machine.
