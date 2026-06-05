#!/usr/bin/env python3
"""Publie sur X le résultat des paris CourtAlphaX réglés + état BR.

Usage :
  py -3 scripts/courtalphax_result_notify.py --dry-run
  py -3 scripts/courtalphax_result_notify.py

À planifier toutes les 30 min en journée (après portfolio_results_daemon).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.bets_db import (  # noqa: E402
    compute_telegram_user_bankroll_eur,
    compute_telegram_user_br_advanced_stats,
)
from scripts.courtalphax_config import (  # noqa: E402
    COURTALPHAX_START_BR_EUR,
    COURTALPHAX_TELEGRAM_USER_ID,
    COURTALPHAX_TRACKER_SOURCE,
)
from scripts.courtalphax_format import format_result_tweet  # noqa: E402
from scripts.courtalphax_post_log import (  # noqa: E402
    fetch_bets_pending_result_post,
    finalize_result_post,
    has_result_post_for_bet,
    release_result_post_claim,
    try_claim_result_post,
)
from scripts.x_client import post_tweet, require_prod_for_x_post, truncate_tweet, x_posting_enabled  # noqa: E402


def run_result_notify(*, dry_run: bool = False, force: bool = False) -> dict:
    require_prod_for_x_post(force=force, dry_run=dry_run)
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        pending = fetch_bets_pending_result_post(
            conn,
            telegram_user_id=COURTALPHAX_TELEGRAM_USER_ID,
            tracker_source=COURTALPHAX_TRACKER_SOURCE,
        )
        if not pending:
            return {"ok": True, "n_posted": 0, "message": "Rien à publier."}

        stats = compute_telegram_user_br_advanced_stats(conn, COURTALPHAX_TELEGRAM_USER_ID)
        snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
        bankroll = float(snap["available_eur"])
        wins = int(stats.get("wins") or 0)
        losses = int(stats.get("losses") or 0)
        roi = stats.get("roi_pct")
        roi_pct = float(roi) if roi is not None else None

        posted_ids: list[str] = []
        for bet in pending:
            bet_id = int(bet["id"])
            if has_result_post_for_bet(conn, bet_id):
                continue

            tweet = format_result_tweet(
                bet,
                bankroll_eur=bankroll,
                start_br_eur=COURTALPHAX_START_BR_EUR,
                wins=wins,
                losses=losses,
                roi_pct=roi_pct,
            )
            tweet = truncate_tweet(tweet)

            if dry_run:
                print(tweet)
                print(f"--- dry-run bet_id={bet_id} ---\n")
                continue

            if not x_posting_enabled():
                raise SystemExit("COURTALPHAX_X_ENABLED=1 requis (ou --dry-run).")

            if not try_claim_result_post(conn, bet_id):
                continue

            try:
                res = post_tweet(tweet, dry_run=False)
                finalize_result_post(
                    conn,
                    bet_id=bet_id,
                    tweet_id=str(res.get("tweet_id") or ""),
                    tweet_text=tweet,
                )
            except Exception:
                release_result_post_claim(conn, bet_id)
                raise

            posted_ids.append(str(res.get("tweet_id") or ""))
            # BR recalculée après chaque pari (profit déjà en base)
            snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
            bankroll = float(snap["available_eur"])
            stats = compute_telegram_user_br_advanced_stats(conn, COURTALPHAX_TELEGRAM_USER_ID)
            wins = int(stats.get("wins") or 0)
            losses = int(stats.get("losses") or 0)
            roi = stats.get("roi_pct")
            roi_pct = float(roi) if roi is not None else None

        return {
            "ok": True,
            "n_posted": len(pending) if dry_run else len(posted_ids),
            "tweet_ids": posted_ids,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="CourtAlphaX — tweets résultat + BR")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Publier hors PROD (déconseillé)")
    args = ap.parse_args()
    res = run_result_notify(dry_run=args.dry_run, force=args.force)
    if not args.dry_run and res.get("n_posted"):
        print(f"OK {res['n_posted']} tweet(s) · ids={res.get('tweet_ids')}")
    elif res.get("n_posted") == 0:
        print(res.get("message") or "Rien à publier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
