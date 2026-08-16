#!/usr/bin/env python3
"""Nettoyage WTA prod : dates aberrantes, dedup C1, re-ingest, QC."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PARIS = ZoneInfo("Europe/Paris")
DEFAULT_RAW = ROOT / "data" / "raw" / "tennis_wta"
DEFAULT_DB = ROOT / "data" / "bettinghud.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="Nettoyage données WTA (dedup + dates + ingest)")
    ap.add_argument("--work-dir", default=str(DEFAULT_RAW))
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--skip-ingest", action="store_true")
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    db_path = args.db_path

    print("=== 1/4 enrich + dedup CSV ===")
    from scripts.enrich_wta_delta_metadata import enrich_metadata
    from scripts.wta_sackmann_common import DEFAULT_CUTOFF

    stats = enrich_metadata(work_dir, cutoff=DEFAULT_CUTOFF, dedup=True, write_players=True)
    print(stats)
    reorganize = stats.get("reorganize") or {}
    main_d = (reorganize.get("main") or {}).get("dupes_removed", 0)
    qual_d = (reorganize.get("qual_itf") or {}).get("dupes_removed", 0)
    print(f"  doublons retirés: main={main_d} qual_itf={qual_d}")
    print(f"  dates aberrantes retirées: {stats.get('aberrant_dates_removed', 0)}")

    if not args.skip_ingest:
        print("\n=== 2/4 re-ingest SQLite ===")
        import subprocess

        rc = subprocess.call(
            [sys.executable, str(ROOT / "scripts" / "ingest_sackmann_wta.py"), "--db-path", db_path],
        )
        if rc != 0:
            print("ingest_sackmann_wta.py FAILED")
            return rc
        subprocess.call([sys.executable, str(ROOT / "scripts" / "apply_sqlite_indexes.py")])

    print("\n=== 3/4 QC gates ===")
    from scripts.wta_delta_qc_gates import run_wta_delta_qc_gates

    qc = run_wta_delta_qc_gates(raw_dir=work_dir, db_path=db_path)
    for line in qc.summary_lines():
        print(line)

    print("\n=== 4/4 meta sync + vérif DB ===")
    conn = sqlite3.connect(db_path)
    try:
        from scripts.bets_db import ensure_bets_meta, set_meta

        ensure_bets_meta(conn)
        ts = datetime.now(PARIS).isoformat(timespec="seconds")
        set_meta(conn, "last_tours_sync_ts", ts)
        set_meta(conn, "last_sackmann_sync_ts", ts)
        max_d = conn.execute("SELECT MAX(tourney_date) FROM wta_matches").fetchone()[0]
        future = conn.execute(
            """
            SELECT COUNT(*) FROM wta_matches
            WHERE CAST(substr(CAST(tourney_date AS TEXT), 1, 4) AS INTEGER)
                  > CAST(strftime('%Y', 'now') AS INTEGER) + 1
            """
        ).fetchone()[0]
        print(f"  max tourney_date: {max_d}")
        print(f"  lignes futures aberrantes: {future}")
    finally:
        conn.close()

    return 0 if qc.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
