#!/usr/bin/env python3
"""Passe les BR auto-initialisées (55 €) à 100 € — sans toucher les comptes personnalisés."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.bets_db import DB_PATH_DEFAULT, migrate_legacy_default_start_br  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB_PATH_DEFAULT)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        stats = migrate_legacy_default_start_br(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    mode = "dry-run" if args.dry_run else "applied"
    print(f"{mode}: {stats}")


if __name__ == "__main__":
    main()
