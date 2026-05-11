"""
Remplit humidity_pct / temp_c via Open-Meteo (archive) pour les matchs déjà en base.

Usage:
  py -3 scripts/backfill_match_weather.py --db data/bettinghud.db --limit 2000
  py -3 scripts/backfill_match_weather.py --wta-only --limit 500

Les matchs indoor (colonne indoor = I sur ATP) sont ignorés.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Optional

import pandas as pd

_scripts = os.path.dirname(os.path.abspath(__file__))
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

from weather_open_meteo import (  # noqa: E402
    ensure_weather_schema,
    fetch_archive_daytime_means,
    geocode_tournament,
)
from surface_speed import infer_outdoor  # noqa: E402


def _parse_tourney_date(val) -> Optional[pd.Timestamp]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if len(s) == 8 and s.isdigit():
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    return pd.to_datetime(val, errors="coerce")


def backfill_table(
    conn: sqlite3.Connection,
    table: str,
    source_filter: Optional[str],
    limit: int,
    dry_run: bool,
) -> int:
    if table == "matches_recent":
        if source_filter:
            q = (
                "SELECT rowid, tourney_name, tourney_date, indoor FROM matches_recent "
                "WHERE (humidity_pct IS NULL OR temp_c IS NULL) AND source = ? "
                f"LIMIT {int(limit)}"
            )
            rows = conn.execute(q, (source_filter,)).fetchall()
        else:
            q = (
                "SELECT rowid, tourney_name, tourney_date, indoor FROM matches_recent "
                f"WHERE (humidity_pct IS NULL OR temp_c IS NULL) LIMIT {int(limit)}"
            )
            rows = conn.execute(q).fetchall()
    else:
        rows = conn.execute(
            f"SELECT rowid, tourney_name, tourney_date FROM {table} "
            f"WHERE (humidity_pct IS NULL OR temp_c IS NULL) LIMIT {int(limit)}"
        ).fetchall()

    updated = 0
    for row in rows:
        if table == "matches_recent":
            rid, tname, tdate, indoor = row
        else:
            rid, tname, tdate = row
            indoor = None

        if not infer_outdoor(indoor, tname):
            continue

        ts = _parse_tourney_date(tdate)
        if ts is None or pd.isna(ts):
            continue
        d = ts.date()

        g = geocode_tournament(tname, conn=conn)
        if g is None:
            print(f"[skip geocode] {tname!r}", flush=True)
            continue
        lat, lon, _ = g
        tc, rh = fetch_archive_daytime_means(lat, lon, d, conn=conn)
        if tc is None or rh is None:
            print(f"[skip wx] {tname} {d}", flush=True)
            continue

        if not dry_run:
            conn.execute(
                f"UPDATE {table} SET humidity_pct = ?, temp_c = ? WHERE rowid = ?",
                (rh, tc, rid),
            )
            conn.commit()
        updated += 1
        if updated % 50 == 0:
            print(f"  … {updated} lignes", flush=True)
        time.sleep(0.1)

    return updated


def main():
    ap = argparse.ArgumentParser(description="Backfill météo Open-Meteo (archive)")
    ap.add_argument("--db", default="data/bettinghud.db")
    ap.add_argument("--limit", type=int, default=500, help="Taille du lot SQL par passe (voir aussi --until-complete).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--atp-only", action="store_true")
    ap.add_argument("--wta-only", action="store_true")
    ap.add_argument(
        "--until-complete",
        action="store_true",
        help="Réémet des passes jusqu'à ce qu'il n'y ait plus de lignes outdoor remplissables dans la limite (0 mis à jour).",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_weather_schema(conn)

    grand = 0
    round_ix = 0
    while True:
        round_ix += 1
        batch = int(args.limit)
        total = 0
        if not args.wta_only:
            n = backfill_table(
                conn,
                "matches_recent",
                "tennismylife",
                batch,
                args.dry_run,
            )
            print(f"matches_recent (TML): {n} mis à jour", flush=True)
            total += n
        if not args.atp_only:
            try:
                n2 = backfill_table(conn, "wta_matches", None, batch, args.dry_run)
                print(f"wta_matches: {n2} mis à jour", flush=True)
                total += n2
            except sqlite3.OperationalError as e:
                print(f"wta_matches: skip ({e})", flush=True)
                n2 = 0

        print(f"Passe #{round_ix} — sous-total {total} — cumul {grand + total}", flush=True)
        grand += total
        if args.dry_run or not getattr(args, "until_complete", False):
            break
        if total == 0:
            break

    conn.close()
    print(f"TOTAL {grand}", flush=True)


if __name__ == "__main__":
    main()
