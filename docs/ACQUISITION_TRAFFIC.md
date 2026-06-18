# Plan acquisition trafic — CourtAlpha

Hub ops + technique pour maximiser le trafic avec automatisation (1–2 h/semaine manuel).

> Voir aussi : [[TELEGRAM_CHANNEL_ACQUISITION]] · [[LLM_VISIBILITY]] · [[COMMUNITY_SEEDING_FR]] · [[GSC_BING_BIO_CHECKLIST]]

---

## KPI cible 60 j

- **Priorité** : sessions / visiteurs uniques sur pages publiques
- **Mesure** : admin `/frequentation` + rapport Telegram hebdo
- **Cible 60 j** : x3 sessions/semaine vs baseline

---

## Statut ops (audit 2026-06-09)

| Élément | Statut | Note |
|---------|--------|------|
| Cron `courtalpha-acquisition` | ✅ installé | `/etc/cron.d/courtalpha-acquisition` (était absent) |
| Canal TG public | ⚠️ à vérifier | `TELEGRAM_CHANNEL_ID` / `TELEGRAM_CHANNEL_ENABLED` absents du `.env` prod repéré — voir [[TELEGRAM_CHANNEL_ACQUISITION]] |
| X auto | ⏸ off | `COURTALPHAX_X_ENABLED=0` |
| OG snapshot | ✅ | `frontend/dist/og-1-day-1-pick.png` présent |
| SEO EN prerender | ✅ | Routes `/en/1-day-1-pick`, `/en/methodo`, `/en/track-record-faq` + hreflang |
| FAQ track record | ✅ | `/track-record-faq` (FR + EN) |
| GSC / Bing | ❌ manuel | [[GSC_BING_BIO_CHECKLIST]] — action utilisateur |
| Baseline 7 j (2026-06-09) | 133 vues | Top : `/1-day-1-pick` (23), sources surtout direct |

Baseline trafic : admin **Fréquentation** ou `web_page_views` (7 j glissants).

---

## Automatisation PROD

| Heure (Paris) | Script | Canal |
|---------------|--------|-------|
| 05:00 | `morning_live_pipeline.py --morning-publish` | Canal TG public (avec Top 5 + 1D1P) |
| ~~04:15–05:00~~ | ~~`courtalphax_daily_pick.py`~~ | ~~X~~ **PAUSE** (juin 2026) |
| 04:20 | `generate_og_snapshot.py` | Image OG stats |
| 04:55 | `generate_og_snapshot.py` | Image OG stats |
| Dim 10h | `telegram_channel_notify.py --weekly` | TG récap |
| Dim 11h | `reddit_draft_notify.py` | TG admin (brouillon) |
| Dim 18h | `traffic_weekly_report.py` | TG admin (stats) |

> **X CourtAlphaX** : tweets auto **désactivés** (`COURTALPHAX_X_ENABLED=0`, crons commentés dans `deploy/cron/courtalphax-x` et `acquisition-traffic`). Réactiver : voir [[COURTALPHAX_X]] § pause.

Cron : [`deploy/cron/acquisition-traffic`](deploy/cron/acquisition-traffic)  
Log unifié : `/opt/bettinghud/data/logs/acquisition.log`

Installation :
```bash
sudo cp /opt/bettinghud/deploy/cron/acquisition-traffic /etc/cron.d/courtalpha-acquisition
```

---

## Variables `.env` PROD

```bash
# Canal Telegram public
TELEGRAM_CHANNEL_ID=@courtalpha_1day1pick
TELEGRAM_CHANNEL_ENABLED=1
COURTALPHA_PUBLIC_URL=https://courtalpha.tech
COURTALPHA_ROOT=/opt/courtalpha

# X CourtAlphaX
COURTALPHAX_X_ENABLED=0
# X_USER_ACCESS_TOKEN=…

# Build frontend (sidebar lien canal)
VITE_TELEGRAM_CHANNEL_URL=https://t.me/courtalpha_1day1pick
```

---

## Scripts

```bash
# Test local
py -3 scripts/telegram_channel_notify.py --dry-run
py -3 scripts/courtalphax_daily_pick.py --dry-run
py -3 scripts/traffic_weekly_report.py --dry-run
py -3 scripts/reddit_draft_notify.py --dry-run
py -3 scripts/generate_og_snapshot.py
py -3 scripts/courtalpha_acquisition_morning.py --dry-run
```

---

## Manuel hebdomadaire (~1–2 h)

1. Lire rapport Telegram dimanche 18h
2. Copier brouillon Reddit (dim 11h) → adapter → poster si pertinent
3. 1 post X optionnel avec screenshot courbe
4. Ajuster 1 levier selon stats UTM

---

## UTM par canal

| Canal | utm_source | utm_medium | utm_campaign |
|-------|------------|------------|--------------|
| Canal TG | telegram | channel | daily / pinned / weekly |
| X auto | twitter | x_auto | daily / result / weekly |
| Reddit | reddit | community | weekly / seeding |
| Bio X | twitter | bio | — |
| Partage site | share | share | share |
