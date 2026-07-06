# Discord — 1 Day 1 Pick

Publication automatique du **pick du jour** (même logique que [/1-day-1-pick](https://courtalpha.tech/1-day-1-pick)) sur un salon Discord via **webhook**.

> **Référence complète** (web + TG + Discord + cron) : [[ONE_DAY_ONE_PICK]]  
> **Langue** : embeds et textes en **anglais**. Voir [[COMMS_LOCALE]].

## Configuration Discord

1. Créer un webhook sur le **salon dédié 1pick1day** (pas le salon général).
2. Serveur Discord → **Paramètres du salon** → **Intégrations** → **Webhooks** → copier l’URL
3. Ajouter dans `/opt/bettinghud/.env` (prod) :

> **Séparation** : `DISCORD_1D1P_WEBHOOK_URL` = salon **1pick1day** uniquement. Le salon **#general** utilise `DISCORD_GENERAL_WEBHOOK_URL` — voir [[DISCORD_GENERAL]].

```env
DISCORD_1D1P_WEBHOOK_URL=https://discord.com/api/webhooks/…
DISCORD_1D1P_ENABLED=1
DISCORD_1D1P_USERNAME=CourtAlpha
DISCORD_1D1P_SITE_URL=https://courtalpha.tech/1-day-1-pick
COMMS_LOCALE=en

# Optionnel — pin automatique du message track record (sinon pin manuel)
DISCORD_BOT_TOKEN=…
DISCORD_1D1P_CHANNEL_ID=1514541672061599754
```

Les webhooks **ne peuvent pas épingler** : un seul message « 📊 Track Record » est créé puis **édité chaque jour** (même `message_id`). Avec `DISCORD_BOT_TOKEN` + `DISCORD_1D1P_CHANNEL_ID`, le bot épingle ce message à la première création.

## Contenu publié

| Moment | Script | Détail |
|--------|--------|--------|
| **05:00** (publications matin) | `od1p_publish.py` → Discord + Telegram | Pick du jour (ou « no value ») + track record Discord |
| **Toutes les ~10 min** (daemon) | `od1p_publish.py` → résultats | Résultat Gagné / Perdu / Annulé sur **Discord et Telegram** |
| **Manuel** | `discord_1d1p_notify.py --performance-board` | Crée ou met à jour le message track record |

Sélection : même logique que `/1pick1day` web (`scripts/pick_modes.py` → `discord_1d1p_core.select_1d1p_pick`).

| Règle | Détail |
|-------|--------|
| Standard | EV **15–100 %**, proba ↓ par circuit, max proba ATP vs WTA |
| Repli | Si **p &lt; 70 %** → **EV &gt; 0** (cap 100 %) |
| Code Discord | `discord_1d1p_notify.run_daily_pick` → `load_1d1p_today_pick` — pas de filtre séparé |

Republication : `scripts/repost_1d1p_today.py --apply` (supprime posts du jour Discord/TG puis reposte).

Les modes **top5** et **today** restent Telegram/web uniquement.

## Commandes

```bash
# Aperçu sans envoi
/opt/bettinghud/venv/bin/python scripts/discord_1d1p_notify.py --dry-run

# Pick du jour (manuel)
/opt/bettinghud/venv/bin/python scripts/discord_1d1p_notify.py

# Résultats en attente
/opt/bettinghud/venv/bin/python scripts/discord_1d1p_notify.py --results --dry-run

# Track record (pin / mise à jour)
/opt/bettinghud/venv/bin/python scripts/discord_1d1p_notify.py --performance-board
```

## Anti-doublon

Table SQLite `discord_1d1p_posts` : un seul post pick / jour, un seul post résultat / `pick_key`.

## Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/discord_1d1p_notify.py` | CLI pick + résultats Discord |
| `scripts/telegram_1d1p_notify.py` | CLI pick + résultats Telegram |
| `scripts/od1p_publish.py` | Orchestration matin + daemon (TG + Discord) |
| `scripts/discord_1d1p_core.py` | Chargement pick (DB + live) |
| `scripts/discord_1d1p_format.py` | Embeds Discord |
| `scripts/discord_client.py` | POST webhook |
| `scripts/morning_live_pipeline.py` | Hook 05:00 (`--morning-publish`) |
| `scripts/portfolio_results_daemon.py` | Hook résultats |

## Track record (message épinglé)

| Élément | Détail |
|---------|--------|
| Type | Embed « 📊 1 Day 1 Pick — Track Record » |
| Mise à jour | **Edit** du même `message_id` (matin + après chaque résultat) |
| Pin | Manuel dans Discord, ou auto si `DISCORD_BOT_TOKEN` + `DISCORD_1D1P_CHANNEL_ID` |
| Stats | Hit rate, bankroll 100 €, W/L/V, 5 derniers picks (récent → ancien) |
| Journal | `post_type = performance_board` dans `discord_1d1p_posts` |

Webhook POST utilise `?wait=true` pour récupérer le `message_id` (édition / pin).

## Dépannage

- **Rien ne part** : vérifier `DISCORD_1D1P_WEBHOOK_URL`, `DISCORD_1D1P_ENABLED=1`, cron **05:00**, `BETTINGHUD_ENV=prod`.
- **Pick déjà posté** : normal (`already_posted`) — utiliser `--force` en test seulement.
- **Résultat manquant** : le pick du jour doit avoir été posté (journal `daily_pick` avec `pick_key`).
- **Track record non pin** : ajouter `DISCORD_BOT_TOKEN` ou pin manuel une fois.
