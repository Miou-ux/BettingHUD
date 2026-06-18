# Discord — salon #general

Message de bienvenue **CourtAlpha** (overview : ML, Top 5, Today's Picks, 1D1P, backtest 2025). **Anglais** ([[COMMS_LOCALE]]).

> Le salon **#1pick1day** a son propre welcome + picks : [[DISCORD_1D1P]].

## Configuration

1. Créer un webhook sur **#general** (Paramètres du salon → Intégrations → Webhooks).
2. Dans `/opt/bettinghud/.env` :

```env
DISCORD_GENERAL_WEBHOOK_URL=https://discord.com/api/webhooks/…
DISCORD_GENERAL_USERNAME=CourtAlpha
```

3. Publier (puis épingler manuellement) :

```bash
/opt/bettinghud/venv/bin/python scripts/discord_general_notify.py --welcome
/opt/bettinghud/venv/bin/python scripts/discord_general_notify.py --welcome --dry-run
```

## Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/discord_general_format.py` | Embed welcome #general |
| `scripts/discord_general_notify.py` | CLI `--welcome` |
| `scripts/discord_client.py` | `discord_general_webhook_url()` |
