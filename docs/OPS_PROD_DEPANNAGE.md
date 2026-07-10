# Ops production & dépannage (PROD)

Dernière mise à jour : **18 juin 2026**.

Guide opérationnel : ce qui est déployé, ce qui ne l’est pas, variables d’environnement, incidents rencontrés en mise en production et procédures de correction.

**Voir aussi** : [[ENVIRONNEMENTS]], [[DEPLOY_SERVEUR]], [[CHANGELOG_RECENT]] § 0.17.

---

## 0. Incident CourtAlpha replay (8 juillet 2026)

**Symptôme** : CourtAlpha premium affiche une **500** sur :

- `Best pick` historique (`/api/picks/one-day-one-pick`)
- `Top 5 probas > historique` (`/api/picks/top5-replay`)

### Cause racine

1. **Fichier script absent sur PROD** : `api/services/top5_replay.py` dépend de `scripts/backtest_prod_top5_2026.py`, absent de `/opt/bettinghud/scripts/`.
2. **Chemin bundle ML relatif** : `scripts/ml_model.py` résolvait `models/xgb_model_tml_v47.pkl` relativement au `cwd`. Depuis `/opt/courtalpha`, cela pointait hors de `/opt/bettinghud/models/`.

### Symptômes observables

```bash
ssh bettinghud "journalctl -u courtalpha-api -n 80 --no-pager"
```

Erreurs typiques :

- `ModuleNotFoundError: No module named 'scripts.backtest_prod_top5_2026'`
- `Exception: Modèle non entraîné et non trouvé.`

### Correctif

```bash
scp O:\Miouppy\Documents\BettingHUD\scripts\backtest_prod_top5_2026.py bettinghud:/opt/bettinghud/scripts/
scp O:\Miouppy\Documents\BettingHUD\scripts\ml_model.py bettinghud:/opt/bettinghud/scripts/
ssh bettinghud "sudo systemctl restart courtalpha-api"
```

### Vérification post-fix

```bash
ssh bettinghud "cd /opt/courtalpha && /opt/bettinghud/venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '/opt/courtalpha')
sys.path.insert(0, '/opt/bettinghud')
from api.services.top5_replay import build_top5_replay
from api.services.one_day_one_pick import build_one_day_one_pick_replay
print('top5', build_top5_replay(db_path='/opt/bettinghud/data/bettinghud.db')['period'])
print('1d1p', build_one_day_one_pick_replay(db_path='/opt/bettinghud/data/bettinghud.db')['period'])
PY"
```

---

## 0bis. Incident CourtAlpha historique intraday écrasé (8 juillet 2026)

**Symptôme** : un pick publié (ex. Kostyuk 06/07) apparaît sur Telegram/Discord mais disparaît de l'historique web (`/api/picks/top5-replay`, `/api/picks/one-day-one-pick`).

### Cause

- `daily_top_proba_picks` est upserté par clé `pick_key = {date|tour|rank}`.
- Les captures intraday tardives remplacent les lignes publiées le matin.
- Le replay lisait cette table SQL comme source unique => historique “état final du jour”, pas “publication”.

### Correctif appliqué (final)

- Replay historique : **source SQL `daily_top_proba_picks`** (comportement d'origine — ne pas remplacer globalement par JSONL, risque de tronquer l'historique fin mai).
- Jour corrompu (`2026-07-06`) : **backfill ciblé** depuis `data/exports/daily_top_proba/2026-07-06.jsonl` (capture publication matin `>= 05:00` Paris).
- **Prévention durable** (`scripts/bets_db.py`) : verrou publication — si `first_captured_ts >= 05:00` Paris pour un `pick_key`, les sources intraday (`portfolio_results_daemon`, `live_data_daemon`, `live_snapshot`, …) ne remplacent plus `match_name` / favori / proba / EV à ce rang.
- Archive JSONL append-only inchangée (`data/exports/daily_top_proba/*.jsonl`) pour audit et backfill ponctuel.
- Fallback robustesse si module `scripts.reliability_pick_match` indisponible (pas de 500).

### Régression évitée

Un patch intermédiaire (lecture JSONL globale ou reselection par rang stocké) avait **réduit** l'historique visible. En cas de doute : vérifier `period.start_date` (doit rester `2026-05-18`) et `n_days` (~26–27).

### Diagnostic rapide

```bash
# Le pick existe-t-il dans les captures append-only du jour ?
ssh bettinghud "python3 - <<'PY'
import json
from pathlib import Path
p=Path('/opt/bettinghud/data/exports/daily_top_proba/2026-07-06.jsonl')
for i,line in enumerate(p.read_text(encoding='utf-8').splitlines(),1):
    o=json.loads(line); picks=o.get('picks') or []
    if any('Kostyuk' in str(x.get('match_name') or '') for x in picks):
        print(i,o.get('captured_ts'),o.get('capture_source'))
PY"
```

---

## 1. Résumé : que pousse-t-on où ?

| Type | Mécanisme | PREPROD → PROD ? |
|------|-----------|------------------|
| **Code Python** (`app/`, `scripts/`, `deploy/`) | `git push` → `git pull` | **Oui** (manuel sur le serveur) |
| **`requirements.txt`** | `pip install -r` après pull | **Oui** (si deps changées) |
| **`data/bettinghud.db`** (paris, BR, meta) | Copie **`scp`** uniquement | **Non** (pas de sync auto) |
| **`models/*.pkl`** | Copie **`scp`** après validation | **Non** (promotion manuelle) |
| **`data/cache/`** (snapshots live) | Rebuild sur chaque env | **Non** (pipeline matin / manuel par env) |
| **Config unités systemd / nginx** | Fichiers `deploy/` + `sudo cp` | **Non** (à appliquer explicitement) |

### Règle métier

- **Paris réels, BR Live Tracker, portefeuille** : source de vérité = **PROD** (`/opt/bettinghud/data/bettinghud.db`).
- **PREPROD** : tests, backtests, essais UI — ne pas supposer que les paris locaux apparaissent sur le serveur.

### Copie manuelle (quand nécessaire)

**Modèle validé PREPROD → PROD** :

```powershell
scp O:\Miouppy\Documents\BettingHUD\models\xgb_model_tml_v47.pkl bettinghud:/opt/bettinghud/models/
ssh bettinghud "cd /opt/bettinghud && ./venv/bin/python scripts/rebuild_live_projection.py"
```

**Base PROD → PREPROD** (debug, reproduire un bug avec les vrais paris) :

```powershell
scp bettinghud:/opt/bettinghud/data/bettinghud.db O:\Miouppy\Documents\BettingHUD\data\bettinghud.db
```

**Base PREPROD → PROD** : à éviter — écrase l’historique réel. Uniquement lors d’une **première** mise en service ou restauration volontaire.

---

## 2. Infrastructure PROD (état de référence)

| Élément | Valeur |
|---------|--------|
| IP / URL | **http://192.95.30.217** |
| OS | Ubuntu 24.04 LTS |
| Utilisateur SSH | `ubuntu` (alias `bettinghud` dans `~/.ssh/config`) |
| Racine app | `/opt/bettinghud` |
| Dépôt Git | `https://github.com/Miou-ux/BettingHUD` (branche `main`) |
| Streamlit | `127.0.0.1:8501` (non exposé directement) |
| Reverse proxy | **nginx** port 80 → Streamlit |

### Services systemd

| Service | Fichier | Rôle |
|---------|---------|------|
| `bettinghud-dashboard` | `deploy/systemd/bettinghud-dashboard.service` | Dashboard Streamlit |
| `bettinghud-daemon` | `deploy/systemd/bettinghud-daemon.service` | Sync résultats paris + top 15 probas/jour |
| `bettinghud-telegram-bot` | `deploy/systemd/bettinghud-telegram-bot.service` | Bot Telegram — `/jour`, `/top5`, polling |

```bash
sudo systemctl status bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
sudo systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
sudo journalctl -u bettinghud-dashboard -f
sudo journalctl -u bettinghud-daemon -f
sudo journalctl -u bettinghud-telegram-bot -f
tail -30 /opt/bettinghud/data/logs/telegram_bot_daemon.log
```

**Telegram** : config `/opt/bettinghud/.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TOP5_AFTER_MORNING`). Doc : **`docs/TELEGRAM_TOP5.md`**.

### Cron

**Vue complète** : [[CRONS_SEMAINE]] (tableau hebdomadaire).

| Créneau (Paris) | Fichier cron | Rôle |
|-----------------|--------------|------|
| **02:00** · **05:00** | `bettinghud-morning-pipeline` | Build snapshot · publications TG/Discord (wrapper alertes) |
| **03:30** quotidien | `bettinghud-data-sync` | Sync ATP + **WTA delta** + ingest |
| **04:00** dimanche | `bettinghud-data-sync` | Retrain ML hebdo |
| **04:15** quotidien | `bettinghud-ops-p0` | Backup DB serveur |
| ***/5 min** | `bettinghud-ops-p0` | Watchdog santé + restart auto |
| **02:15** dimanche | `bettinghud-wta-backup` | Backup archive WTA |
| **08:00** lundi | `bettinghud-ml-weekly` | Rapport Brier WTA → admin Telegram |
| **04:55** quotidien | `bettinghud-acquisition-traffic` | Image OG stats |
| ***/2 min** | `bettinghud-billing` | Indexeur ETH |

Logs : `morning_build_cron.log`, `morning_publish_cron.log`, `tours_auto_sync.log`, `ml_train_cron.log`, `ml_weekly_telegram.log`.

**Déploiement** : fichiers cron en **LF** (pas CRLF). `sudo sed -i 's/\r$//' /etc/cron.d/bettinghud-<nom>`

**Doublon cron matin (corrigé 10 juil. 2026)** : l’ancien fichier `/etc/cron.d/bettinghud-morning` (sans `cron_run_with_alert`) cohabitait avec `bettinghud-morning-pipeline` → deux jobs à 02:00 et 05:00, course sur le verrou snapshot (`Build snapshot échec ou verrou`). **Ne garder que** `bettinghud-morning-pipeline`. Suppression : `sudo rm -f /etc/cron.d/bettinghud-morning` (fait en prod le 10/07).

**CourtAlphaX (X)** — **pause juin 2026** (crons commentés) · [[COURTALPHAX_X]]

---

## 3. Variables d’environnement

### `BETTINGHUD_ENV`

| Valeur | Où | Effet |
|--------|-----|--------|
| `preprod` (défaut) | PC local | Bandeau **PREPROD**, titre navigateur `[PREPROD]` |
| `prod` | systemd dashboard + daemon | Bandeau **PROD** dans onglet Paramètres |

### `BETTINGHUD_HEADLESS` — piège critique

| Contexte | Valeur | Effet |
|----------|--------|--------|
| **Service `bettinghud-dashboard`** | **Ne pas définir** | L’UI complète (onglets) s’affiche |
| Scripts CLI (`rebuild_live_projection.py`, `morning_live_pipeline.py`, `sync_algo_report.py`, …) | `1` | Importe les moteurs **sans** dessiner l’UI Streamlit |

**Ne pas confondre** avec l’option Streamlit **`--server.headless=true`** : elle signifie seulement « pas de navigateur sur le serveur » — elle est **correcte** et **obligatoire** en PROD.

Si `BETTINGHUD_HEADLESS=1` est mis sur le service dashboard, le script `app/dashboard.py` saute tout le bloc `if not HEADLESS_APP:` : seul le bandeau de chargement (« Chargement… » / « Prêt. ») reste visible, **sans onglets**.

### Autres variables (dashboard PROD)

Définies dans `deploy/systemd/bettinghud-dashboard.service` :

| Variable | Rôle |
|----------|------|
| `BETTINGHUD_LIVE_DATA_DAEMON=1` | Thread sync données live en arrière-plan |
| `BETTINGHUD_AUTO_SYNC_TOURS=1` | Sync tours ATP/WTA automatique |

Daemon : `PYTHONPATH=/opt/bettinghud` (obligatoire pour `import scripts.*`).

Liste complète des flags `BETTINGHUD_*` : `docs/CHANGELOG_RECENT.md` et commentaires en tête de `app/dashboard.py`.

---

## 4. Configuration Streamlit derrière nginx

Fichier : **`.streamlit/config.toml`** (copier sur le serveur avec l’app).

```toml
[server]
headless = true
enableCORS = false
enableXsrfProtection = false
enableWebsocketCompression = false
```

Thème sombre (`backgroundColor = #0B0C10`) : un chargement long **sans** widget de statut peut ressembler à un **écran noir** — le dashboard affiche désormais « Chargement du dashboard… » aussi en PROD.

---

## 5. Configuration nginx (WebSocket)

Fichier : **`deploy/nginx/bettinghud.conf`**.

Points importants pour Streamlit :

- `map $http_upgrade $connection_upgrade` pour gérer correctement Upgrade / Connection
- `proxy_http_version 1.1`
- `proxy_buffering off`
- Timeouts longs (`proxy_read_timeout 86400`)

Après modification :

```bash
sudo cp /opt/bettinghud/deploy/nginx/bettinghud.conf /etc/nginx/sites-available/bettinghud
sudo ln -sf /etc/nginx/sites-available/bettinghud /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Sans WebSocket fonctionnel, la page HTML se charge mais **aucun contenu Streamlit** n’apparaît (écran sombre ou vide).

---

## 6. Incidents PROD documentés (mai 2026)

### 6.1 Écran noir / page vide

| Cause | Symptôme | Correction |
|-------|----------|------------|
| Module Python manquant | Erreur dans `journalctl` (`matplotlib`, `bs4`, …) | `pip install -r requirements.txt` dans `/opt/bettinghud/venv` |
| WebSocket nginx | HTML OK, pas de rendu | Appliquer `deploy/nginx/bettinghud.conf` (§ 5) |
| Chargement long + thème sombre | Fond noir quelques secondes | Normal ; bandeau « Chargement… » ; attendre fin `load_engines` |
| `BETTINGHUD_HEADLESS=1` sur dashboard | Uniquement « Prêt. » + « Modèle ML + stats… » | Retirer la variable du service systemd (§ 3) |

Vérifications :

```bash
curl -s http://127.0.0.1:8501/_stcore/health    # doit répondre ok
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/   # 200 via nginx
sudo journalctl -u bettinghud-dashboard --since "10 min ago" | grep -iE 'error|traceback|ModuleNotFound'
```

### 6.2 Seul le bandeau « Prêt. » visible

**Cause** : `Environment=BETTINGHUD_HEADLESS=1` dans `bettinghud-dashboard.service`.

**Correction** :

```bash
# Vérifier qu’HEADLESS n’apparaît pas
systemctl show bettinghud-dashboard -p Environment

sudo systemctl daemon-reload
sudo systemctl restart bettinghud-dashboard
```

Le fichier source corrigé : `deploy/systemd/bettinghud-dashboard.service`.

### 6.3 Daemon `portfolio_results_daemon` en échec

| Erreur | Cause | Correction |
|--------|-------|------------|
| `No module named scripts.portfolio_results_daemon` | Lancement avec `-m scripts...` sans package | `ExecStart=.../python .../scripts/portfolio_results_daemon.py` + `PYTHONPATH=/opt/bettinghud` |
| Script absent | `git pull` non fait | `git pull` sur `/opt/bettinghud` |

### 6.4 WTA Sackmann — archive & delta prod

Le dépôt GitHub **JeffSackmann/tennis_wta** est **indisponible** (404, juin 2026). L’archive sous `data/raw/tennis_wta/` sur prod est enrichie par le **pipeline delta** (tennis-data + stats Flashscore).

**Documentation complète :** [[WTA_SACKMANN_ARCHIVE]] · crons : [[CRONS_SEMAINE]]

**État prod (18/06/2026)** : delta déployé · max match WTA **2026-06-14** · Brier global **0.1749** · rollback modèle : `xgb_model_tml_v47_pre_wta_delta.pkl`.

**Backup** :

```powershell
py scripts/backup_wta_sackmann_archive.py --remote bettinghud --retain 12
```

Cron hebdo : `deploy/cron/wta-sackmann-backup` (dimanche **02:15**).

**Sync quotidien (03:30)** : `sync_tours_daily.py` → `sync_wta_delta` + `enrich_wta_delta_te_stats` — **plus** `fetch_wta_sackmann_raw.py`.

**Ingest manuel** (si besoin) :

```bash
cd /opt/bettinghud
./venv/bin/python scripts/ingest_sackmann_wta.py
```

**Santé ML** : rapport admin chaque **lundi 08:00** (`ml_weekly_telegram_notify.py`) · gate Brier : `check_wta_brier_j6.py`.

Checklist delta : `scripts/_wta_delta_acceptance.md` · plan Brier : `scripts/_wta_delta_brier_plan.md`.

### 6.5 Paris du jour vide (« Aucun match du jour »)

**Cause** : l’onglet **Paris du jour** (et le Live Tracker enrichi) lit le **snapshot live** (`data/cache/live_matches_snapshot*.joblib`), pas le CSV prematch brut.

Au premier déploiement, si seuls `bettinghud.db` et le `.pkl` ont été copiés **sans** snapshot ni rebuild, la liste est vide malgré des cotes dans `data/scraped/prematch_odds_*.csv`.

**Correction** :

```bash
ssh bettinghud
cd /opt/bettinghud
./venv/bin/python scripts/rebuild_live_projection.py   # 5–15 min selon le jour
# ou pipeline matin complet :
./venv/bin/python scripts/morning_live_pipeline.py
```

Dans l’UI : bouton **« Actualiser le Live Tracker »** (même effet). Après correction de `BETTINGHUD_HEADLESS`, le dashboard PROD lance aussi un build automatique au chargement si le snapshot manque.

**Après correctif identité WTA / clés snapshot** (juin 2026, voir `docs/DATA_RELIABILITY.md`) : un enrichissement « cotes seules » ne suffit pas — lancer **`rebuild_live_projection.py`** pour régénérer probas et `data_reliability_score`.

**Cause fréquente (refresh sans data)** : un **nouveau CSV prematch** (scrape toutes les ~30 min) change la « signature » (`csv_mtime`). Le snapshot disque reste valide (34 matchs, etc.) mais `_hydrate_live_matches_from_disk` ne le chargeait pas sans signature exacte → onglets vides. Correctif : repli `load_live_snapshot_by_model` (dashboard ≥ 29 mai 2026).

Vérification :

```bash
ls -la /opt/bettinghud/data/cache/live_matches_snapshot*.joblib
```

### 6.6 Données PREPROD absentes en PROD

**Comportement normal** : pas de push automatique de `bettinghud.db`. Au premier déploiement, une copie `scp` a pu aligner les deux bases ; ensuite chaque environnement diverge si les paris ne sont saisis que d’un côté.

### 6.7 CourtAlpha — 403 Forbidden sur https://courtalpha.tech/

**Cause** : déploiement `frontend/dist/` via `scp` Windows → dossier en `700`, nginx (`www-data`) ne peut pas lire.

**Correction** : utiliser `CourtAlpha/deploy/deploy_frontend.ps1` (fix auto) ou :

```bash
find /opt/courtalpha/frontend/dist -type d -exec chmod 755 {} +
find /opt/courtalpha/frontend/dist -type f -exec chmod 644 {} +
```

### 6.8 Streamlit — `ImportError: APP_KELLY_TRACKER_SOURCES`

**Cause** : process Streamlit long-lived garde l’ancien `bets_db.py` en mémoire après `git pull`.

**Correction** :

```bash
sudo systemctl restart bettinghud-dashboard
```

### 6.9 Déploiement fiabilité v3 (juillet 2026)

**Symptôme** : code tiré mais pool live inchangé / snapshot ancien / score non v3.

**Procédure courte** :

```bash
ssh bettinghud
cd /opt/bettinghud
git pull
./venv/bin/pip install -r requirements.txt
./venv/bin/python scripts/rebuild_live_projection.py
sudo systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
./venv/bin/python scripts/diagnose_reliability_funnel.py
```

**Attendu** : ligne `score version: 3` dans le diagnostic.

**Important** : un `git pull` seul ne suffit pas ; le rebuild snapshot est obligatoire pour recalculer `data_reliability_score` live.

---

## 7. Checklist déploiement code (après développement PREPROD)

1. [ ] Tests locaux : Paris du jour, Portefeuille, Live Tracker, Paramètres
2. [ ] `git commit` + `git push` sur `main`
3. [ ] Sur le serveur :
   ```bash
   ssh bettinghud
   cd /opt/bettinghud
   git pull
   ./venv/bin/pip install -r requirements.txt
   sudo systemctl restart bettinghud-dashboard bettinghud-daemon bettinghud-telegram-bot
   ```
4. [ ] Si nouveau modèle : `scp` du `.pkl` + `rebuild_live_projection.py`
5. [ ] Si unit systemd / nginx modifiés :
   ```bash
   sudo cp deploy/systemd/bettinghud-dashboard.service /etc/systemd/system/
   sudo cp deploy/systemd/bettinghud-telegram-bot.service /etc/systemd/system/
   sudo cp deploy/nginx/bettinghud.prod.conf /etc/nginx/sites-available/bettinghud
   sudo systemctl daemon-reload
   sudo systemctl restart bettinghud-dashboard nginx
   ```
   Sous-domaine Streamlit : `bash deploy/nginx/setup_admin_subdomain.sh` (DNS `admin.courtalpha.tech` requis).
6. [ ] Navigateur : https://courtalpha.tech — **Ctrl+Shift+R**
7. [ ] Onglet **Paramètres** : bandeau **PROD** visible
8. [ ] Telegram : `/help` répond ; logs `telegram_bot_daemon.log` OK

---

## 8. Dépendances Python PROD

Toujours installer depuis le venv serveur :

```bash
cd /opt/bettinghud
./venv/bin/pip install -r requirements.txt
```

Packages souvent oubliés lors des premiers déploiements (déjà listés dans `requirements.txt` si le dépôt est à jour) :

- `matplotlib`
- `beautifulsoup4`, `lxml`
- `streamlit-autorefresh`
- `sqlalchemy` (ingest WTA)

Playwright (scrapers) :

```bash
./venv/bin/playwright install chromium
./venv/bin/playwright install-deps
```

---

## 9. Commandes de diagnostic rapide

```bash
# Santé Streamlit
curl -s http://127.0.0.1:8501/_stcore/health

# Taille / date base
ls -la /opt/bettinghud/data/bettinghud.db

# Modèle ML présent
ls -la /opt/bettinghud/models/xgb_model_tml_v47.pkl

# Dernières erreurs dashboard
sudo journalctl -u bettinghud-dashboard -n 80 --no-pager | grep -iE 'error|traceback|Uncaught'

# Environnement effectif du service
systemctl show bettinghud-dashboard -p Environment --no-pager
```

---

## 10. Sécurité (rappel)

L’URL **http://192.95.30.217** est **publique sans authentification** par défaut.

Recommandations : restriction IP (ufw / firewall provider), basic auth nginx, ou VPN. Ne pas committer secrets (`.env`, clés API).

---

## 11. Fichiers de référence dans le dépôt

| Fichier | Rôle |
|---------|------|
| `deploy/install_ubuntu.sh` | Installation initiale |
| `deploy/systemd/bettinghud-dashboard.service` | Service Streamlit |
| `deploy/systemd/bettinghud-daemon.service` | Daemon portefeuille |
| `deploy/nginx/bettinghud.conf` | Proxy nginx + WebSocket |
| `deploy/systemd/bettinghud-telegram-bot.service` | Bot Telegram |
| `docs/TELEGRAM_TOP5.md` | Bot Telegram — doc complète |
| `docs/COURTALPHAX_X.md` | Compte public X CourtAlphaX — cron, tweets, runbook |
| `deploy/cron/courtalphax-x` | Cron pick / résultats / récap hebdo X |
| `.streamlit/config.toml` | Thème + options serveur derrière proxy |
| `docs/ENVIRONNEMENTS.md` | Convention PREPROD / PROD |
| `docs/DEPLOY_SERVEUR.md` | Guide d’installation pas à pas |
