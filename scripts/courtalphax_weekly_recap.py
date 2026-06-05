#!/usr/bin/env python3
"""Récap hebdomadaire CourtAlphaX — tweet du dimanche soir.

Semaine calendaire lun–dim (Europe/Paris).

Usage :
  py -3 scripts/courtalphax_weekly_recap.py --dry-run
  py -3 scripts/courtalphax_weekly_recap.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

PARIS_TZ = ZoneInfo("Europe/Paris")

from scripts.bets_db import compute_telegram_user_bankroll_eur, ensure_bets_meta  # noqa: E402
from scripts.courtalphax_config import (  # noqa: E402
    COURTALPHAX_START_BR_EUR,
    COURTALPHAX_TELEGRAM_USER_ID,
    COURTALPHAX_TRACKER_SOURCE,
)
from scripts.courtalphax_format import format_weekly_recap_tweet  # noqa: E402
from scripts.courtalphax_post_log import (  # noqa: E402
    fetch_weekly_courtalphax_bets,
    fetch_weekly_no_pick_dates,
    has_weekly_recap_for_week,
    log_post,
)
from scripts.x_client import post_tweet, require_prod_for_x_post, truncate_tweet, x_posting_enabled  # noqa: E402


def week_bounds_sunday(*, ref: datetime | None = None) -> tuple[str, str, str]:
    """Semaine lun–dim (Paris). week_end = dimanche de la semaine courante."""
    today = (ref or datetime.now(PARIS_TZ)).date()
    week_end = today + timedelta(days=(6 - today.weekday()))
    week_start = week_end - timedelta(days=6)
    week_key = f"{week_start.isoformat()}_{week_end.isoformat()}"
    return week_start.isoformat(), week_end.isoformat(), week_key


def run_weekly_recap(*, dry_run: bool = False, force: bool = False) -> dict:
    require_prod_for_x_post(force=force, dry_run=dry_run)
    week_start, week_end, week_key = week_bounds_sunday()

    conn = sqlite3.connect("data/bettinghud.db")
    try:
        ensure_bets_meta(conn)
        if not force and has_weekly_recap_for_week(conn, week_key):
            return {
                "ok": False,
                "reason": "already_posted",
                "week_key": week_key,
                "message": "Récap semaine déjà publié.",
            }

        bets = fetch_weekly_courtalphax_bets(
            conn,
            telegram_user_id=COURTALPHAX_TELEGRAM_USER_ID,
            tracker_source=COURTALPHAX_TRACKER_SOURCE,
            week_start=week_start,
            week_end=week_end,
        )
        no_pick_dates = fetch_weekly_no_pick_dates(
            conn, week_start=week_start, week_end=week_end
        )
        snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
        bankroll = float(snap["available_eur"])

        tweet = truncate_tweet(
            format_weekly_recap_tweet(
                week_start=week_start,
                week_end=week_end,
                bets=bets,
                no_pick_dates=no_pick_dates,
                bankroll_eur=bankroll,
                start_br_eur=COURTALPHAX_START_BR_EUR,
            )
        )

        if dry_run:
            print(tweet)
            print(f"\n--- dry-run · semaine {week_key} · {len(bets)} pari(s) ---")
            return {
                "ok": True,
                "dry_run": True,
                "week_key": week_key,
                "n_bets": len(bets),
                "tweet": tweet,
            }

        if not x_posting_enabled():
            raise SystemExit("COURTALPHAX_X_ENABLED=1 requis (ou --dry-run).")

        posted = post_tweet(tweet, dry_run=False)
        log_post(
            conn,
            post_type="weekly_recap",
            calendar_date=week_key,
            tweet_id=posted.get("tweet_id"),
            tweet_text=tweet,
        )
        return {
            "ok": True,
            "week_key": week_key,
            "n_bets": len(bets),
            "tweet_id": posted.get("tweet_id"),
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="CourtAlphaX — récap hebdo (dimanche)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Republier / hors garde-fou")
    args = ap.parse_args()
    res = run_weekly_recap(dry_run=args.dry_run, force=args.force)
    if not res.get("ok"):
        print(res.get("message") or res, file=sys.stderr)
        return 1 if res.get("reason") == "already_posted" else 0
    if not args.dry_run:
        print(f"OK récap semaine · {res.get('week_key')} · tweet={res.get('tweet_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
