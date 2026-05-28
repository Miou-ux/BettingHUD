#!/usr/bin/env python3
"""Backfill `daily_top_proba_picks` depuis JSONL + algo_opportunities historiques."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import run_backfill_daily_top_proba


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Récupère l'historique top probas depuis JSONL et algo_opportunities."
    )
    parser.add_argument("--start-date", help="YYYY-MM-DD (inclus)")
    parser.add_argument("--end-date", help="YYYY-MM-DD (inclus)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Écrase aussi les jours déjà capturés en live/daemon",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("BETTINGHUD_DB_PATH", "data/bettinghud.db"),
    )
    args = parser.parse_args()

    db_path = args.db if os.path.isabs(args.db) else os.path.join(ROOT, args.db)
    result = run_backfill_daily_top_proba(
        db_path=db_path,
        start_date=args.start_date,
        end_date=args.end_date,
        force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
