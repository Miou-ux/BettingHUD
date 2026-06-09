#!/usr/bin/env python3
"""Diagnostic PROD: top5 vs live, Demain labels, telegram config."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

import joblib

PARIS = ZoneInfo("Europe/Paris")
today = datetime.now(PARIS).date()


def main() -> int:
    print("=" * 72)
    print(f"PROD DIAG — {today.isoformat()} (Europe/Paris)")
    print("=" * 72)

    # 1) Demain vs date
    for path in (
        "data/cache/live_matches_snapshot.full.joblib",
        "data/cache/live_matches_snapshot.joblib",
    ):
        if os.path.isfile(path):
            payload = joblib.load(path)
            break
    else:
        print("NO SNAPSHOT")
        return 1

    if isinstance(payload, dict):
        ms = list(payload.get("matches") or payload.get("data") or [])
        if not ms and "built_at" in payload:
            ms = list(payload.get("rows") or [])
    elif isinstance(payload, list):
        ms = payload
    else:
        ms = []
    demain_today_date = 0
    demain_tomorrow_date = 0
    hhmm_today = 0
    for m in ms:
        d = str(m.get("date") or "")[:10]
        t = str(m.get("time") or "")
        if t.startswith("Demain"):
            if d == str(today):
                demain_today_date += 1
            else:
                demain_tomorrow_date += 1
        elif d == str(today):
            hhmm_today += 1

    print("\n--- 1) Libellé Demain vs champ date ---")
    print(f"  Snapshot: {len(ms)} matchs")
    print(f"  date={today} + time HH:MM (OK): {hhmm_today}")
    print(f"  date={today} + time 'Demain …' (label stale): {demain_today_date}")
    print(f"  date≠today + 'Demain …': {demain_tomorrow_date}")
    if demain_today_date:
        print("  >> Cause: scrape TE garde le préfixe Demain alors que date=aujourd'hui.")
        print("  >> UI: filtre Aujourd'hui utilise la date réelle (OK) mais affiche time tel quel.")

    # 2) Top5 vs Live Tracker pool
    from scripts.daily_top_proba_store import (
        collect_top5_proba_picks,
        is_today_paris_match,
        load_today_matches_for_daily_top_proba,
    )
    from scripts.live_tracker_picks import filter_live_tracker_day_matches

    pool, _meta = load_today_matches_for_daily_top_proba()
    lt = filter_live_tracker_day_matches(pool, today_only=True)

    # Paris du jour extra filter (major ATP/WTA) — mimic dashboard
    def _is_major(cat, tourney):
        from app.dashboard import _is_major_atp_wta

        return _is_major_atp_wta(cat, tourney)

    paris_pool = [
        m
        for m in pool
        if _is_major(m.get("category"), m.get("tournament"))
        and is_today_paris_match(m)
    ]

    top5 = collect_top5_proba_picks(pool, limit=5)

    print("\n--- 2) Top 5 proba vs Live Tracker ---")
    print(f"  Pool snapshot (jour+cotes+rang): {len(pool)}")
    print(f"  Live Tracker /jour (sans filtre EV): {len(lt)}")
    print(f"  Paris du jour (+ ATP/WTA majeur): {len(paris_pool)}")
    print(f"  Top 5 (EV 15-100%, tri proba): {len(top5)}")
    print("  Différences attendues:")
    print("    - /top5: EV bande + top 5 proba seulement")
    print("    - /jour: tous matchs value, tri priorité composite, pas EV bande")
    print("    - Live Tracker UI: filtres circuit/tournoi/EV toggle/segments")
    for p in top5:
        print(
            f"    #{p.get('rank')} {p.get('fav_player')} "
            f"p={float(p.get('p_model_fav',0))*100:.1f}% "
            f"ev={float(p.get('ev_fav_pct',0)):+.1f}%"
        )

    # 3) Telegram env
    print("\n--- 3) Telegram ---")
    env = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    token = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    after = os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip()
    allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    print(f"  BETTINGHUD_ENV={env}")
    print(f"  TELEGRAM_BOT_TOKEN set: {token}")
    print(f"  TELEGRAM_CHAT_ID set: {bool(chat)} (len={len(chat)})")
    print(f"  TELEGRAM_TOP5_AFTER_MORNING={after!r}")
    print(f"  TELEGRAM_ALLOWED_CHAT_IDS: {allowed or '(chat_id only)'}")

    mp = "data/logs/morning_pipeline_cron.log"
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-2000:]
        if "Telegram Top 5" in tail or "telegram" in tail.lower():
            print("  morning_pipeline_cron.log: mention Telegram (voir tail serveur)")
        elif tail.strip():
            print("  morning_pipeline_cron.log: present, check Telegram lines")
        else:
            print("  morning_pipeline_cron.log: empty")
    else:
        print("  morning_pipeline_cron.log: MISSING")

    tglog = "data/logs/telegram_bot_daemon.log"
    if os.path.isfile(tglog):
        with open(tglog, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-30:]
        sends = [ln for ln in lines if "Top 5" in ln or "top5" in ln.lower() or "envoye" in ln.lower()]
        print(f"  telegram_bot_daemon.log: {len(sends)} send-related lines in last 30")
        for ln in sends[-5:]:
            print("   ", ln.strip()[:100])

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
