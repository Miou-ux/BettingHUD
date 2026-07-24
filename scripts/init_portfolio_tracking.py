#!/usr/bin/env python3
"""Initialise le suivi portfolio Top5 / 1D1P (BR théorique 100 €, ledger journalier)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PARIS = ZoneInfo("Europe/Paris")


def main() -> int:
    p = argparse.ArgumentParser(description="Reset portfolio tracking Top5 + 1D1P")
    p.add_argument(
        "--start-date",
        default=datetime.now(PARIS).date().isoformat(),
        help="Date de départ du suivi (YYYY-MM-DD, défaut: aujourd'hui Paris)",
    )
    p.add_argument("--bankroll", type=float, default=100.0, help="Bankroll théorique initiale")
    p.add_argument(
        "--mode",
        choices=("top5", "1d1p", "both"),
        default="both",
        help="Modes à (re)initialiser",
    )
    p.add_argument(
        "--no-backfill",
        action="store_true",
        help="Ne pas importer les publications déjà archivées depuis start-date",
    )
    p.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    args = p.parse_args()

    from scripts.bets_db import open_db, sync_daily_top_proba_from_results
    from scripts.portfolio_tracking_store import (
        MODE_1D1P,
        MODE_TOP5,
        backfill_portfolio_from_published,
        init_portfolio_tracking,
    )

    modes = [MODE_TOP5, MODE_1D1P] if args.mode == "both" else [args.mode]
    conn = open_db(args.db)
    try:
        sync_daily_top_proba_from_results(conn)
        for mode in modes:
            init_portfolio_tracking(
                conn,
                mode=mode,
                start_date=args.start_date,
                bankroll_start_eur=float(args.bankroll),
                clear_ledger=True,
            )
            n = 0
            if not args.no_backfill:
                n = backfill_portfolio_from_published(conn, mode, from_date=args.start_date)
            print(f"{mode}: start={args.start_date} bankroll={args.bankroll:.2f}€ backfill_rows={n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
