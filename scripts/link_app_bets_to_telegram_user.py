#!/usr/bin/env python3
"""Rattache les paris app à un utilisateur Telegram + aligne la BR Kelly."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (  # noqa: E402
    compute_live_tracker_bankroll_eur,
    compute_telegram_user_bankroll_eur,
    copy_app_kelly_meta_to_telegram_user,
    get_live_tracker_manual_adjust_eur,
    link_unassigned_app_bets_to_telegram_user,
)


def _app_kelly_available(conn) -> float:
    snap = compute_live_tracker_bankroll_eur(conn)
    cur = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM user_bets
        WHERE COALESCE(TRIM(tracker_source), '') = ''
          AND COALESCE(TRIM(status), '') = 'En cours'
        """
    )
    legacy_open = float(cur.fetchone()[0] or 0.0)
    cur2 = conn.execute(
        """
        SELECT COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE COALESCE(TRIM(tracker_source), '') = ''
          AND COALESCE(TRIM(status), '') != 'En cours'
        """
    )
    legacy_profit = float(cur2.fetchone()[0] or 0.0)
    manual_adj = get_live_tracker_manual_adjust_eur(conn)
    avail_raw = (
        float(snap["start_eur"])
        + float(snap["settled_profit_eur"])
        + legacy_profit
        - float(snap["committed_open_eur"])
        - legacy_open
    )
    return float(avail_raw + manual_adj)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rattache paris app (sans owner) + copie BR Kelly vers Telegram user",
    )
    ap.add_argument("--user-id", required=True, help="Telegram user id (ex. 7113749284)")
    ap.add_argument("--db", default="data/bettinghud.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    uid = str(args.user_id).strip()

    conn = __import__("sqlite3").connect(args.db)
    try:
        app_avail = _app_kelly_available(conn)
        tg_before = compute_telegram_user_bankroll_eur(conn, uid)
        if args.dry_run:
            n = conn.execute(
                """
                SELECT COUNT(*) FROM user_bets
                WHERE (telegram_user_id IS NULL OR TRIM(telegram_user_id) = '')
                  AND (
                    tracker_source IN ('live_tracker', 'top5_proba_action', 'live_inplay_manual')
                    OR COALESCE(TRIM(tracker_source), '') = ''
                  )
                """
            ).fetchone()[0]
            print(f"dry-run: {n} pari(s) seraient rattachés à {uid}")
            print(f"app BR: {app_avail:.2f} | tg BR actuelle: {float(tg_before['available_eur']):.2f}")
            return 0

        n_linked = link_unassigned_app_bets_to_telegram_user(conn, uid)
        meta = copy_app_kelly_meta_to_telegram_user(conn, uid)
        tg_after = compute_telegram_user_bankroll_eur(conn, uid)
        print(f"linked_bets: {n_linked}")
        print(f"meta: {meta}")
        print(f"app_available_eur: {app_avail:.4f}")
        print(f"tg_before_available_eur: {float(tg_before['available_eur']):.4f}")
        print(f"tg_after_available_eur: {float(tg_after['available_eur']):.4f}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
