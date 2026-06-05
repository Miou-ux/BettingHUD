#!/usr/bin/env python3
"""Résumé user_bets par tracker_source (debug portefeuille vs Paris du jour)."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/bettinghud.db")
    p.add_argument("--recent", type=int, default=15)
    args = p.parse_args()
    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"DB introuvable: {db}")
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM user_bets").fetchone()[0]
        print(f"user_bets total: {n}")
        print("\nPar tracker_source:")
        for src, cnt, n_open, pl in conn.execute(
            """
            SELECT COALESCE(tracker_source, ''), COUNT(*),
                   SUM(CASE WHEN status='En cours' THEN 1 ELSE 0 END),
                   ROUND(COALESCE(SUM(profit), 0), 2)
            FROM user_bets GROUP BY 1 ORDER BY 2 DESC
            """
        ):
            print(f"  {src!r:30} n={cnt}  en_cours={n_open}  profit={pl}")
        print("\nDerniers paris:")
        for row in conn.execute(
            """
            SELECT id, date, match_date, match_name, bet_on, stake, status,
                   COALESCE(tracker_source, '')
            FROM user_bets ORDER BY id DESC LIMIT ?
            """,
            (args.recent,),
        ):
            print(" ", row)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
