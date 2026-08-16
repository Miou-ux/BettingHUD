#!/usr/bin/env python3
"""Flag / report aberrant future WTA tourney_date rows (ex. 2029)."""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "bettinghud.db")


def main() -> int:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """
        SELECT tourney_date, tourney_name, winner_name, loser_name, tourney_level
        FROM wta_matches
        WHERE CAST(substr(CAST(tourney_date AS TEXT), 1, 4) AS INTEGER)
              > CAST(strftime('%Y', 'now') AS INTEGER) + 1
        ORDER BY tourney_date DESC
        LIMIT 50
        """
    ).fetchall()
    print(f"future/aberrant WTA rows: {len(rows)}")
    for r in rows:
        print(r)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
