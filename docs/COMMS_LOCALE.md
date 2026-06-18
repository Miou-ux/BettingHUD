# Telegram & Discord — langue des communications

## Décision (juin 2026)

**Toutes les communications publiques sur Telegram et Discord sont en anglais.**

| Canal | Langue | Exemples |
|-------|--------|----------|
| **Telegram bot** (display **CourtAlpha**, username `@CourtAlphabot`) | **EN** | `/top5`, `/today`, `/1pick1day`, `/help`, picks matinaux, boutons Bet |
| **Telegram canal public** (1D1P acquisition) | **EN** | Pick du jour, résultats, recap |
| **Discord** (webhook 1D1P) | **EN** | Embeds pick / no-pick / résultat |

## Hors périmètre (inchangé)

| Surface | Langue |
|---------|--------|
| **Web CourtAlpha** | FR + EN (i18n React, choix utilisateur) |
| **Docs internes** (`docs/`) | Français (référence ops) |
| **Statuts API / DB** | Français (`Gagné`, `Perdu`, `Annulé`, `En cours`) — filtrage portefeuille |

## Implémentation

| Fichier | Rôle |
|---------|------|
| `scripts/comms_locale.py` | Locale outbound (`COMMS_LOCALE`, dates EN, disclaimers) |
| `scripts/telegram_top5_notify.py` | Messages bot + picks |
| `scripts/telegram_channel_notify.py` | Canal public |
| `scripts/discord_1d1p_format.py` | Embeds Discord + track record |
| `scripts/telegram_1d1p_notify.py` | Publication auto 1D1P |
| `scripts/telegram_bet_flow.py` | Flux Bet (EN) |

Variable d'environnement (prod, défaut **en**) :

```env
COMMS_LOCALE=en
```

Valeur `fr` : repli legacy pour tests locaux (`--dry-run`) uniquement — **ne pas utiliser en prod**.

## Vérification

```bash
# Telegram (aperçu)
/opt/bettinghud/venv/bin/python scripts/telegram_top5_notify.py --dry-run
/opt/bettinghud/venv/bin/python scripts/discord_1d1p_notify.py --dry-run
```

Les en-têtes doivent être en anglais (`Today · …`, `Model proba`, `No value pick today`, etc.).

## Liens

- [[ONE_DAY_ONE_PICK]] — référence complète 1D1P
- [[TELEGRAM_TOP5]] — commandes bot
- [[DISCORD_1D1P]] — webhook Discord
- [[TELEGRAM_CHANNEL_ACQUISITION]] — canal public
