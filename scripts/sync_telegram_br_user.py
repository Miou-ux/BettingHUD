#!/usr/bin/env python3
"""Aligne la BR Telegram d'un utilisateur sur la BR Kelly dashboard (app)."""
from __future__ import annotations

import argparse
import sqlite3
import sys

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (  # noqa: E402
    compute_live_tracker_bankroll_eur,
    get_live_tracker_manual_adjust_eur,
    get_meta,
    get_telegram_user_manual_adjust_eur,
    get_telegram_user_start_br,
    set_telegram_user_manual_adjust_eur,
    set_telegram_user_start_br,
    compute_telegram_user_bankroll_eur,
)


def _app_kelly_available(conn: sqlite3.Connection) -> float:
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


def sync_user_br(
    telegram_user_id: str,
    *,
    db_path: str = "data/bettinghud.db",
    dry_run: bool = False,
) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        app_avail = _app_kelly_available(conn)
        tg_before = compute_telegram_user_bankroll_eur(conn, telegram_user_id)

        # start = app_avail + open_tg - profit_tg (sans ajust manuel tg)
        uid = str(telegram_user_id).strip()
        cur = conn.execute(
            """
            SELECT COALESCE(SUM(stake), 0)
            FROM user_bets
            WHERE telegram_user_id = ?
              AND COALESCE(TRIM(status), '') = 'En cours'
            """,
            (uid,),
        )
        tg_open = float(cur.fetchone()[0] or 0.0)
        cur2 = conn.execute(
            """
            SELECT COALESCE(SUM(profit), 0)
            FROM user_bets
            WHERE telegram_user_id = ?
              AND COALESCE(TRIM(status), '') != 'En cours'
            """,
            (uid,),
        )
        tg_profit = float(cur2.fetchone()[0] or 0.0)
        new_start = float(app_avail) + tg_open - tg_profit

        if not dry_run:
            set_telegram_user_start_br(conn, uid, new_start)
            set_telegram_user_manual_adjust_eur(conn, uid, 0.0)
        tg_after = compute_telegram_user_bankroll_eur(conn, uid) if not dry_run else {
            **tg_before,
            "available_eur": new_start + tg_profit - tg_open,
            "start_eur": new_start,
            "manual_adjust_eur": 0.0,
        }
        return {
            "telegram_user_id": uid,
            "app_available_eur": app_avail,
            "tg_before_avail": float(tg_before.get("available_eur") or 0),
            "new_start_eur": new_start,
            "tg_after_avail": float(tg_after.get("available_eur") or 0),
            "tg_open": tg_open,
            "tg_profit": tg_profit,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True, help="Telegram user id (from.id)")
    ap.add_argument("--db", default="data/bettinghud.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = sync_user_br(args.user_id, db_path=args.db, dry_run=bool(args.dry_run))
    for k, v in out.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
