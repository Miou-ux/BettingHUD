#!/usr/bin/env python3
"""Backfill publication archivée pour une date (ex. 2026-07-22 Droguet + Hanfmann)."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import DB_PATH_DEFAULT, ensure_daily_top_proba_schema, open_db
from scripts.published_picks_store import MODE_1D1P, MODE_TOP5, save_published_picks


def _row_by_pick_key(conn: sqlite3.Connection, pick_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM daily_top_proba_picks WHERE pick_key = ?",
        (pick_key,),
    ).fetchone()
    return dict(row) if row else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-07-22")
    ap.add_argument("--db", default=DB_PATH_DEFAULT)
    args = ap.parse_args()
    cal = str(args.date)[:10]

    conn = open_db(args.db)
    try:
        ensure_daily_top_proba_schema(conn)
        top5_keys = [
            f"{cal}|ATP|01",  # Droguet
            f"{cal}|ATP|02",  # Hanfmann
        ]
        top5 = []
        for pk in top5_keys:
            row = _row_by_pick_key(conn, pk)
            if row:
                r = dict(row)
                r["hybrid_rel_fallback"] = True
                top5.append(r)
            else:
                print(f"WARN missing pick_key {pk}")
        n5 = save_published_picks(
            conn,
            mode=MODE_TOP5,
            calendar_date=cal,
            picks=top5,
            source="backfill_morning_publish",
        )
        print(f"top5 saved: {n5}", [p.get("fav_player") for p in top5])

        d1 = _row_by_pick_key(conn, f"{cal}|ATP|01")
        n1 = 0
        if d1:
            n1 = save_published_picks(
                conn,
                mode=MODE_1D1P,
                calendar_date=cal,
                picks=[dict(d1)],
                source="backfill_morning_publish",
            )
        print(f"1d1p saved: {n1}", d1.get("fav_player") if d1 else None)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
