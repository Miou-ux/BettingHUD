# Bot Telegram BettingHUD — documentation complète

Bot **@BettingHUDbot** : notifications et commandes sur le **serveur PROD uniquement**.  
PREPROD (PC local) : prévisualisation `--dry-run` seulement, pas d’envoi réel.

> Voir aussi : [[ENVIRONNEMENTS]] · [[DEPLOY_SERVEUR]] · [[OPS_PROD_DEPANNAGE]] · [[ARCHITECTURE_ACTUELLE_ET_MISES]]

---

## 1. Vue d’ensemble

| Fonction | Déclencheur | Contenu |
|--------|-------------|---------|
| **Top 5 matinal** | Fin du pipeline matin (`TELEGRAM_TOP5_AFTER_MORNING=1`) | Top 5 proba · EV favori **+15 % → +100 %** · tri proba ↓ (onglet **Paris du jour**) |
| **`/jour`** | Commande Telegram | Matchs **Aujourd’hui** avec **EV+** uniquement (seuil min défaut 0 %, tri priorité) |
| **`/jourchallenger`** | Commande Telegram | Tournois **Challenger** ATP/WTA du jour · EV **+15 % → +100 %** · tri **proba** ↓ |
| **`/jourmajor`** | Commande Telegram | Tournois **main draw 250+** du jour · EV **+15 % → +100 %** · tri **proba** ↓ |
| **`/top5`** | Commande Telegram | Même logique que le Top 5 matinal, à la demande |
| **`/start`**, **`/help`** | Commandes Telegram | Bienvenue et aide |
| **`/strategie`** | Commande Telegram | Résumé stratégie sélection + mise (Kelly) |

```mermaid
flowchart LR
  subgraph prod [PROD serveur]
    Cron[Crontab 02:00 Paris]
    Pipe[morning_live_pipeline.py]
    Snap[Snapshot live]
    T5[telegram_top5_notify.py]
    Daemon[telegram_bot_daemon.py]
    TG[API Telegram]
  end
  Cron --> Pipe
  Pipe --> Snap
  Pipe -->|TELEGRAM_TOP5_AFTER_MORNING=1| T5
  Daemon -->|/jour /top5| T5
  Daemon --> Snap
  T5 --> TG
  User[Utilisateur Telegram] --> Daemon
  TG --> User
```

---

## 2. Fichiers du dépôt

| Fichier | Rôle |
|---------|------|
| `scripts/telegram_top5_notify.py` | Formatage HTML, envoi messages, `run_notify`, `run_daily_picks_notify` |
| `scripts/telegram_bot_daemon.py` | Long polling `getUpdates`, commandes `/jour`, `/top5`, `/help` |
| `scripts/live_tracker_picks.py` | Collecte headless des picks **Live Tracker (Aujourd’hui)** pour `/jour` |
| `scripts/daily_top_proba_store.py` | `collect_top5_proba_picks` — Top 5 Paris du jour (EV 15–100 %) |
| `scripts/morning_live_pipeline.py` | Pipeline matin ; envoi Top 5 si `TELEGRAM_TOP5_AFTER_MORNING=1` |
| `deploy/systemd/bettinghud-telegram-bot.service` | Service systemd daemon commandes |
| `deploy/install_ubuntu.sh` | Installe et active le service Telegram |

**Logs PROD**

| Log | Contenu |
|-----|---------|
| `data/logs/telegram_bot_daemon.log` | Polling, commandes reçues, erreurs |
| `data/logs/morning_pipeline_cron.log` | Cron pipeline matin |
| `data/cache/logs/morning_pipeline_*.log` | Détail par exécution pipeline |

**État daemon**

| Fichier | Rôle |
|---------|------|
| `data/cache/.telegram_bot_offset` | Dernier `update_id` Telegram (reprise après redémarrage) |

---

## 3. Logique métier : `/jour` vs `/top5`

### 3.1 `/jour` — Live Tracker (Aujourd’hui)

Même pool de matchs que le Live Tracker (**Aujourd’hui**), mais **uniquement les paris EV+** (`collect_live_tracker_value_picks`).

**Matchs scannés** (via `scripts/daily_top_proba_store.collect_top5_proba_picks`) :

1. Snapshot live du jour (`load_today_matches_for_daily_top_proba`)
2. Cotes valides (`odd_p1`, `odd_p2` > 1)
3. Rang/points fiables sur les deux joueurs
4. **Gros tournois ATP/WTA uniquement** (`is_major_atp_wta_match` — hors Challenger, ITF, UTR, futures)
5. Calendrier **aujourd’hui** (Europe/Paris)
6. Match à venir ou démarré depuis moins de `BETTINGHUD_LIVE_STARTED_GRACE_MINUTES` (défaut **90** min) — pour `/jour` via `live_tracker_picks`

**Lignes affichées** :

- Uniquement les côtés avec **EV strictement positive** (`ValueDetector`, seuil min défaut **0 %**)
- Variable optionnelle : `TELEGRAM_JOUR_EV_MIN_PCT=15` pour exiger au moins +15 % (comme le Live Tracker UI)

**Pas de ligne** sur le favori modèle si EV ≤ 0.

**Tri** : score **priorité composite** (Sharpe / Brier × qualité segment).

**Champs affichés** (message Telegram HTML) :

| Champ | Source |
|-------|--------|
| Joueur parié vs adversaire | Côté retenu |
| Circuit · tournoi · heure | Match snapshot |
| Proba modèle | `1 / true_odd` |
| EV | `ValueDetector` |
| Cote | Cote book du côté parié |
| Kelly reco | `_algo_kelly_stake_frac` (½ × Brier adaptatif) |

Messages longs : découpés automatiquement (~3900 caractères / message) avec en-tête « Partie 1/2 ».

### 3.2 `/jourchallenger` — Challengers du jour

Alias : `/challengers`.

| Critère | Valeur |
|---------|--------|
| Tournois | `is_challenger_tier_match` : `category=Challenger`, nom/url **challenger**, ou **points vainqueur &lt; 250** (ex. Foggia WTA 125) |
| Jour | **Aujourd’hui** (même hygiène que `/jour`) |
| EV | **+15 % → +100 %** (`TELEGRAM_JOURCHALLENGER_EV_MIN_PCT` / `_MAX_PCT`) |
| Tri | **Proba modèle** décroissante (pas priorité composite) |

Fonction : `load_live_tracker_challenger_day_picks` dans `scripts/live_tracker_picks.py`.

Prérequis : snapshot live incluant les Challengers (build matin ou rebuild). Voir **`docs/CHALLENGERS_ET_TOURNOIS.md`**.

### 3.3 `/jourmajor` — Majors 250+ du jour

Alias : `/majors`.

| Critère | Valeur |
|---------|--------|
| Tournois | `is_major_tournament_match` : main draw ATP/WTA **250+** (hors Challenger, WTA 125, ITF) |
| Jour | **Aujourd’hui** (même hygiène que `/jour`) |
| EV | **+15 % → +100 %** (`TELEGRAM_JOURMAJOR_EV_MIN_PCT` / `_MAX_PCT`) |
| Tri | **Proba modèle** décroissante |

Fonction : `load_live_tracker_major_day_picks` dans `scripts/live_tracker_picks.py`.

### 3.4 `/top5` et envoi matinal — Paris du jour

Aligné onglet **Paris du jour** / stratégie backtest validée :

| Critère | Valeur |
|---------|--------|
| Tournois | **Majors ATP/WTA** uniquement (`is_major_atp_wta_match`) |
| Filtre | EV **favori modèle** entre **+15 %** et **+100 %** |
| Tri | Proba favori modèle ↓ |
| Limite | **5** matchs (`TELEGRAM_TOP5_LIMIT`) |

Fonction : `scripts/daily_top_proba_store.collect_top5_proba_picks`.

---

## 4. Commandes Telegram

| Commande | Alias | Action |
|----------|-------|--------|
| `/start` | — | Message de bienvenue + liste des commandes |
| `/help` | — | Aide détaillée |
| `/jour` | `/picks`, `/picksdujour` | Matchs **Aujourd’hui** EV+ (tri priorité composite) |
| `/jourchallenger` | `/challengers` | Challengers + WTA 125 · EV 15–100 % · tri proba ↓ |
| `/jourmajor` | `/majors` | Main draw 250+ · EV 15–100 % · tri proba ↓ |
| `/top5` | `/top` | Top 5 proba main draw (EV favori 15–100 %) |
| `/strategie` | `/strategy` | Stratégie BettingHUD + mise Kelly (synthèse) |

**Sécurité** : seuls les `chat_id` listés dans `TELEGRAM_CHAT_ID` ou `TELEGRAM_ALLOWED_CHAT_IDS` peuvent déclencher les commandes. Les autres reçoivent « Chat non autorisé ».

### 4.1 `/strategie` — contenu

Message statique (`format_bot_strategy_message` dans `telegram_top5_notify.py`) :

1. **Principe** — modèle ML ATP/WTA, edge vs book (EV &gt; 0)
2. **Sélection** — jour courant, favori modèle, EV +15 % → +100 %, Top 5 proba ↓
3. **Mise** — ½ Kelly, facteur Brier segment, plafond 15 % BR
4. **Pratique** — vérifier cote réelle, miser ≤ reco Kelly

Aperçu PREPROD : `py -3 scripts/telegram_top5_notify.py --strategy`

---

## 5. Configuration PROD

### 5.1 Créer le bot

1. Telegram → **@BotFather**
2. `/newbot` → nom + username (ex. `@BettingHUDbot`)
3. Copier le **token** (ne jamais le committer)

### 5.2 Obtenir le chat ID

1. Envoyer `/start` au bot
2. `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Repérer `"chat":{"id":123456789}`

### 5.3 Fichier `.env` sur le serveur

Chemin : **`/opt/bettinghud/.env`** (permissions `600`, dans `.gitignore`)

```env
BETTINGHUD_ENV=prod

# Obligatoire
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789

# Envoi Top 5 après pipeline matin
TELEGRAM_TOP5_AFTER_MORNING=1

# Top 5 (Paris du jour)
TELEGRAM_TOP5_LIMIT=5
TELEGRAM_TOP5_EV_MIN_PCT=15
TELEGRAM_TOP5_EV_MAX_PCT=100

# /jour — limite optionnelle (0 = tous les picks EV+)
TELEGRAM_JOUR_EV_MIN_PCT=0
TELEGRAM_DAILY_PICKS_LIMIT=0

# Chats autorisés pour /jour et /top5 (optionnel, virgules)
# TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321

# Polling daemon (optionnel)
# TELEGRAM_BOT_POLL_TIMEOUT_SEC=25
```

### 5.4 Variables d’environnement (référence)

| Variable | Défaut | Usage |
|----------|--------|--------|
| `TELEGRAM_BOT_TOKEN` | — | Token @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat principal (notifications + commandes) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | — | Liste additionnelle de chats autorisés |
| `TELEGRAM_TOP5_AFTER_MORNING` | `0` | `1` = Top 5 en fin de `morning_live_pipeline.py` |
| `TELEGRAM_TOP5_LIMIT` | `5` | Nombre de picks Top 5 |
| `TELEGRAM_TOP5_EV_MIN_PCT` | `15` | EV min favori (Top 5) |
| `TELEGRAM_TOP5_EV_MAX_PCT` | `100` | EV max favori (Top 5) |
| `TELEGRAM_DAILY_PICKS_LIMIT` | `0` | Max lignes `/jour` (`0` = illimité) |
| `TELEGRAM_BOT_POLL_TIMEOUT_SEC` | `25` | Timeout long polling |
| `BETTINGHUD_LIVE_STARTED_GRACE_MINUTES` | `90` | Matchs démarrés encore inclus (/jour) |
| `BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC` | `86400` | TTL chargement snapshot |

---

## 6. Déploiement et services PROD

### 6.1 Pipeline matin + Top 5 automatique

Cron : `deploy/cron/morning-pipeline` → `/etc/cron.d/bettinghud-morning`

- **02:00 Europe/Paris** : `scripts/morning_live_pipeline.py`
- Charge `.env` au démarrage
- Si `TELEGRAM_TOP5_AFTER_MORNING=1` et `BETTINGHUD_ENV=prod` → `run_notify(source="morning")`

### 6.2 Service daemon commandes

```bash
sudo cp /opt/bettinghud/deploy/systemd/bettinghud-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bettinghud-telegram-bot
sudo systemctl status bettinghud-telegram-bot
```

Le service charge `EnvironmentFile=-/opt/bettinghud/.env` et force `BETTINGHUD_ENV=prod`.

**Après mise à jour du code** :

```bash
ssh bettinghud "cd /opt/bettinghud && git pull"
ssh bettinghud "sudo systemctl restart bettinghud-telegram-bot"
# Si scripts modifiés sans git :
# scp scripts/telegram_*.py scripts/live_tracker_picks.py bettinghud:/opt/bettinghud/scripts/
```

### 6.3 Mise à jour complète (checklist)

- [ ] `git pull` sur `/opt/bettinghud`
- [ ] Vérifier `/opt/bettinghud/.env` (token, chat_id, `TELEGRAM_TOP5_AFTER_MORNING`)
- [ ] `sudo systemctl restart bettinghud-telegram-bot`
- [ ] Tester `/help` et `/jour` sur Telegram
- [ ] Vérifier `tail -20 /opt/bettinghud/data/logs/telegram_bot_daemon.log`

---

## 7. PREPROD (PC local)

### 7.1 Règles

- **Pas** de token dans un `.env` local commité (inutile pour l’usage normal)
- **Pas** de tâche planifiée Windows pour Telegram
- Envoi réel **bloqué** sauf `--force` (déconseillé)

### 7.2 Prévisualiser les messages

```powershell
cd O:\Miouppy\Documents\BettingHUD

# Top 5 (Paris du jour)
py -3.11 scripts/telegram_top5_notify.py --dry-run

# Live Tracker /jour
py -3.11 scripts/telegram_top5_notify.py --dry-run --daily
```

Sortie console = rendu HTML (émojis, format Telegram).

---

## 8. CLI (scripts)

### `telegram_top5_notify.py`

```bash
# Top 5 — envoi PROD (avec .env chargé)
python scripts/telegram_top5_notify.py

# Top 5 — aperçu
python scripts/telegram_top5_notify.py --dry-run

# /jour — aperçu (Live Tracker)
python scripts/telegram_top5_notify.py --dry-run --daily

# /jour — envoi PROD
python scripts/telegram_top5_notify.py --daily

# Options
python scripts/telegram_top5_notify.py --limit 5 --ev-min-pct 15 --ev-max-pct 100
python scripts/telegram_top5_notify.py --chat-id 123456789 --force   # debug hors prod
```

### `telegram_bot_daemon.py`

```bash
# PROD (via systemd en pratique)
python scripts/telegram_bot_daemon.py

# Une passe de polling (test)
python scripts/telegram_bot_daemon.py --once
```

---

## 9. Format des messages

### Exemple `/jour` (extrait)

```text
📋 BettingHUD · Live Tracker (Aujourd'hui)

📅 ven. 29 mai 2026 · Europe/Paris
🎯 Live Tracker · tous les matchs scannés ↓
📲 Demande manuelle
✅ 12 match(s) · 16 scanné(s) au total
━━━━━━━━━━━━━━━━━━━━

🥇 Joueur A vs Joueur B
   🏟 WTA · Roland Garros · 🕒 14:30
   📊 Proba 68.2% · EV +22.3% · Cote @1.85
   💰 Kelly reco ~4.2% BR
...
```

### Exemple `/top5` (extrait)

```text
🎾 BettingHUD · Top 5 Proba

📅 ven. 29 mai 2026 · Europe/Paris
⚡ EV +15% → +100% · tri proba modèle
🌅 Envoi matinal automatique
...
```

Pied de page commun : *Info — pas un conseil de pari. Vérifier les cotes avant mise.*

---

## 10. Dépannage

| Symptôme | Cause probable | Action |
|----------|----------------|--------|
| Pas de message matinal | `TELEGRAM_TOP5_AFTER_MORNING` absent | Ajouter `=1` dans `.env` |
| Pipeline OK, pas de Telegram | Erreur silencieuse dans pipeline | Lire `morning_pipeline_*.log` |
| `TELEGRAM_BOT_TOKEN requis` | `.env` non chargé par cron | Vérifier `.env` ; pipeline charge dotenv au démarrage |
| `/top5` ne répond pas | Daemon arrêté | `systemctl status bettinghud-telegram-bot` |
| `403 Forbidden` | Pas de `/start` ou mauvais chat_id | `/start` + vérifier `TELEGRAM_CHAT_ID` |
| Chat non autorisé | Mauvais compte Telegram | Ajouter id dans `TELEGRAM_ALLOWED_CHAT_IDS` |
| `/jour` vide, beaucoup scannés | Normal si peu de value / données | Vérifier snapshot : pipeline matin, dashboard Live |
| Message coupé | Trop de matchs | Normal — parties 1/N ; ou `TELEGRAM_DAILY_PICKS_LIMIT` |
| PREPROD bloqué | `BETTINGHUD_ENV != prod` | Attendu — utiliser `--dry-run` |
| Token exposé | Fuite chat / logs | `/revoke` @BotFather + mettre à jour `.env` |

**Commandes diagnostic PROD** :

```bash
sudo systemctl status bettinghud-telegram-bot
tail -30 /opt/bettinghud/data/logs/telegram_bot_daemon.log
cd /opt/bettinghud && source venv/bin/activate && set -a && source .env && set +a
python scripts/telegram_top5_notify.py --dry-run --daily
python scripts/telegram_top5_notify.py --dry-run
```

---

## 11. Sécurité

- Ne **jamais** committer `TELEGRAM_BOT_TOKEN` ni `.env`
- Fichier `.env` serveur : `chmod 600`
- Limiter les `chat_id` autorisés
- En cas de fuite du token : **@BotFather** → `/revoke` → mettre à jour PROD

---

## 12. Liens documentation

| Note | Sujet |
|------|--------|
| [[ENVIRONNEMENTS]] | PREPROD vs PROD, automatisations |
| [[DEPLOY_SERVEUR]] | Installation Ubuntu, cron, systemd |
| [[OPS_PROD_DEPANNAGE]] | Incidents PROD (dont services) |
| [[PROD_RESILIENCE]] | Redémarrage auto systemd |
| [[CHART_TOP_PROBAS_JOUR]] | Top 15 + toggle EV Live Tracker |
| [[BACKTEST_TOP5_PROBA_VS_EV]] | Stratégie Top 5 proba vs EV |
| [[PREDICTION_ET_MISE]] | Proba, EV, Kelly |
| [[CHANGELOG_RECENT]] | § 0.18 Bot Telegram |
