"""Compare PREPROD vs PROD DB stats (safe columns)."""
from __future__ import annotations

import sqlite3
import sys


def stats(db_path: str, label: str) -> None:
    cn = sqlite3.connect(db_path)
    print(f"=== {label} ({db_path}) ===")
    try:
        r = cn.execute(
            "SELECT COUNT(*), MIN(match_date), MAX(match_date) FROM algo_opportunities"
        ).fetchone()
        print(f"algo_opportunities: count={r[0]} min={r[1]} max={r[2]}")
    except Exception as e:
        print(f"algo_opportunities: ERR {e}")
    try:
        rows = cn.execute(
            "SELECT status, COUNT(*) FROM algo_opportunities GROUP BY status"
        ).fetchall()
        print("algo by status:", dict(rows))
    except Exception as e:
        print(f"algo status: ERR {e}")
    try:
        r = cn.execute("SELECT COUNT(*), MAX(placed_ts) FROM user_bets").fetchone()
        print(f"user_bets: count={r[0]} last_placed_ts={r[1]}")
    except Exception as e:
        print(f"user_bets: ERR {e}")
    try:
        r = cn.execute(
            "SELECT COUNT(*), MIN(pick_date), MAX(pick_date) FROM daily_top_proba_picks"
        ).fetchone()
        print(f"daily_top_proba_picks: count={r[0]} min={r[1]} max={r[2]}")
    except Exception as e:
        print(f"daily_top_proba_picks: ERR {e}")
    cn.close()
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: compare_db_env.py DB_PATH [LABEL]")
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else path
    stats(path, label)
