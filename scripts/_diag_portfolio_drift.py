#!/usr/bin/env python3
"""Diag écarts reconcile ledger vs kelly_replay_metrics."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.bets_db import open_db
from scripts.live_replay_engine import load_ml
from scripts.ml_model import resolve_match_brier_segment_key
from scripts.portfolio_tracking_store import (
    MODE_TOP5,
    _pick_reconcile_key,
    portfolio_bet_to_pick_row,
    recompute_portfolio_ledger,
    reconcile_portfolio_ledger,
)


def main() -> int:
    conn = open_db()
    ml = load_ml()
    mode = MODE_TOP5
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    recompute_portfolio_ledger(conn, mode, ml=ml)
    rep = reconcile_portfolio_ledger(conn, mode, ml=ml, tol_eur=0.02)
    mm = [m for m in (rep.get("mismatches") or []) if m.get("field") == "profit_eur"]
    led = {
        _pick_reconcile_key(portfolio_bet_to_pick_row(dict(r))): dict(r)
        for r in conn.execute(
            "SELECT * FROM portfolio_daily_bets WHERE mode = ?", (mode,)
        ).fetchall()
    }

    brier_diffs = 0
    sort_issues = 0
    for m in mm[:30]:
        row = led.get(m["key"])
        if not row:
            continue
        fresh = float(seg.get(str(resolve_match_brier_segment_key(row)), glob_b))
        stored = row.get("segment_brier")
        stored_f = float(stored) if stored is not None else fresh
        if abs(stored_f - fresh) > 1e-5:
            brier_diffs += 1

    print("mismatches", len(mm), "brier_diff_among_first30", brier_diffs)
    print("summary_diff", rep.get("summary_diff"))

    # ordering: same-day ranks
    days_multi = conn.execute(
        """
        SELECT calendar_date, COUNT(*) c FROM portfolio_daily_bets
        WHERE mode=? GROUP BY calendar_date HAVING c>1 ORDER BY c DESC LIMIT 3
        """,
        (mode,),
    ).fetchall()
    for cal, c in days_multi:
        rows = conn.execute(
            """
            SELECT bet_rank, p_model_fav, match_name, stake_eur FROM portfolio_daily_bets
            WHERE mode=? AND calendar_date=? ORDER BY bet_rank
            """,
            (mode, cal),
        ).fetchall()
        print(f"day {cal} n={c}")
        for r in rows:
            print(f"  rank={r[0]} p={r[1]} stake={r[3]} {str(r[2])[:40]}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
