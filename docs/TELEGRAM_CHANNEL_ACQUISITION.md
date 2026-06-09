# Canal Telegram public — acquisition CourtAlpha

Canal de diffusion **gratuit** du pick quotidien 1 Day 1 Pick, distinct du bot privé `@BettingHUDbot` (whitelist).

> Voir aussi : [[TELEGRAM_TOP5]] · [[Home]]

---

## 1. Création du canal (ops, ~15 min)

1. Telegram → **Nouveau canal** → nom : `CourtAlpha — 1 Day 1 Pick`
2. Type : **Public** — choisir un lien permanent, ex. `t.me/courtalpha_1day1pick`
3. Bio : `Pick tennis quotidien · historique public · value bets modèle`
4. Ajouter **@BettingHUDbot** (ou le bot PROD) comme **administrateur** avec droit **Publier des messages**
5. Récupérer l’id du canal :
   - `@courtalpha_1day1pick` ou
   - `-100xxxxxxxxxx` (via `getUpdates` après un post test)

---

## 2. Message épinglé (copier-coller)

```
🎾 CourtAlpha — 1 Day 1 Pick

Un seul pick tennis par jour sur les tournois majeurs ATP/WTA.
Historique 100 % public et vérifiable — aucun pick effacé après perte.

📊 Track record live :
https://courtalpha.tech/1-day-1-pick?utm_source=telegram&utm_medium=channel&utm_campaign=pinned

📖 Methodo (stratégie & backtests) :
https://courtalpha.tech/methodo?utm_source=telegram&utm_medium=channel&utm_campaign=pinned

⚠️ Information statistique — pas un conseil financier. Paris sportifs : risque de perte, jouez responsablement (18+).
```

Épingler ce message en haut du canal.

---

## 3. Variables `.env` PROD

```bash
TELEGRAM_CHANNEL_ID=@courtalpha_1day1pick
# ou TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx
TELEGRAM_CHANNEL_ENABLED=1
COURTALPHA_PUBLIC_URL=https://courtalpha.tech
COURTALPHA_ROOT=/opt/courtalpha
```

Le bot utilise le même `TELEGRAM_BOT_TOKEN` que le bot privé.

---

## 4. Automatisation

| Script | Rôle |
|--------|------|
| `scripts/telegram_channel_notify.py` | Pick du jour + résultat J-1 + récap hebdo |
| `deploy/cron/acquisition-traffic` | Cron unifié acquisition (TG, X, OG, rapports) |

```bash
# Test local / PREPROD
py -3 scripts/telegram_channel_notify.py --dry-run
py -3 scripts/telegram_channel_notify.py --dry-run --weekly
```

**Dédup** : métadonnées SQLite `tg_channel_daily_*`, `tg_channel_result_*`, `tg_channel_weekly_*` — pas de double post le même jour (sauf `--dry-run`).

---

## 5. Frontend

- Sidebar : lien « Canal Telegram » si `VITE_TELEGRAM_CHANNEL_URL` est défini au build
- Fréquentation admin : filtrer `utm_source=telegram`

---

## 6. KPIs (30 jours)

- Abonnés canal
- Sessions web `utm_source=telegram`
- Pages vues `/1-day-1-pick`
