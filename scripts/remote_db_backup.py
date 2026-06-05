#!/usr/bin/env python3
"""Export cohérent SQLite (backup API) — exécuté sur le serveur PROD."""
import sqlite3
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: remote_db_backup.py <src.db> <dst.db>", file=sys.stderr)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
