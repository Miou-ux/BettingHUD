#!/usr/bin/env python3
"""Diagnostique les algo_opportunities / daily_top_proba encore « En cours »."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (
    _lookup_match_result_for_players,
    ensure_algo_opportunities_schema,
    ensure_daily_top_proba_schema,
    ensure_match_results_cache,
    sync_algo_opportunities_from_results,
    sync_daily_top_proba_from_results,
)
from scripts.scraper_results import canonical_player

DB = os.path.join(ROOT, "data", "bettinghud.db")
PARIS = ZoneInfo("Europe/Paris")
RG_START = "2026-05-18"


def _classify_open_algo(conn: sqlite3.Connection, *, since: str) -> None:
    ensure_match_results_cache(conn)
    today = datetime.now(PARIS).date().isoformat()
    rows = conn.execute(
        """
        SELECT opportunity_key, match_date, detected_date, tournament, bet_on,
               player1, player2, status
        FROM algo_opportunities
        WHERE COALESCE(status, 'En cours') = 'En cours'
          AND match_date >= ?
        ORDER BY match_date, opportunity_key
        """,
        (since,),
    ).fetchall()
    print(f"\n=== algo_opportunities « En cours » depuis {since} : {len(rows)} ===")
    if not rows:
        return

    buckets: dict[str, list] = {
        "future": [],
        "missing_players": [],
        "no_result_cache": [],
        "result_in_window": [],
        "result_exact_date": [],
    }
    for row in rows:
        key, md, dd, tour, bet, p1, p2, st = row
        md_s = str(md or "")[:10]
        if md_s > today:
            buckets["future"].append(row)
            continue
        if not (p1 and p2):
            buckets["missing_players"].append(row)
            continue
        p1c, p2c = canonical_player(p1), canonical_player(p2)
        exact = conn.execute(
            """
            SELECT 1 FROM match_results
            WHERE match_date = ?
              AND ((p1_canonical=? AND p2_canonical=?) OR (p1_canonical=? AND p2_canonical=?))
              AND winner_canonical IS NOT NULL AND TRIM(winner_canonical) != ''
            LIMIT 1
            """,
            (md_s, p1c, p2c, p2c, p1c),
        ).fetchone()
        if exact:
            buckets["result_exact_date"].append(row)
            continue
        hit = _lookup_match_result_for_players(
            conn, md_s, p1c, p2c, calendar_date=str(dd or md_s)[:10]
        )
        if hit:
            buckets["result_in_window"].append(row)
        else:
            buckets["no_result_cache"].append(row)

    for name, items in buckets.items():
        print(f"  {name}: {len(items)}")
    print("\n--- Échantillon « result_in_window » (résultat TE à ±3 j du jour book) ---")
    for row in buckets["result_in_window"][:8]:
        key, md, dd, tour, bet, p1, p2, _ = row
        print(f"  {md} {bet} · {p1} vs {p2} · {str(tour or '')[:40]}")

    print("\n--- Échantillon « no_result_cache » ---")
    for row in buckets["no_result_cache"][:8]:
        key, md, dd, tour, bet, p1, p2, _ = row
        print(f"  {md} {bet} · {p1} vs {p2} · {str(tour or '')[:40]}")


def _sync_counts(conn: sqlite3.Connection) -> None:
    n_algo = sync_algo_opportunities_from_results(conn)
    n_dtp = sync_daily_top_proba_from_results(conn)
    print(f"\nSync après diagnostic : algo={n_algo} daily_top_proba={n_dtp}")


def main() -> None:
    since = sys.argv[1] if len(sys.argv) > 1 else RG_START
    conn = sqlite3.connect(DB)
    ensure_algo_opportunities_schema(conn)
    ensure_daily_top_proba_schema(conn)

    open_algo = conn.execute(
        "SELECT COUNT(*) FROM algo_opportunities WHERE COALESCE(status,'En cours')='En cours' AND match_date>=?",
        (since,),
    ).fetchone()[0]
    open_dtp = conn.execute(
        "SELECT COUNT(*) FROM daily_top_proba_picks WHERE COALESCE(status,'En cours')='En cours' AND calendar_date>=?",
        (since,),
    ).fetchone()[0]
    total_algo = conn.execute(
        "SELECT COUNT(*) FROM algo_opportunities WHERE match_date>=?", (since,)
    ).fetchone()[0]
    print(f"DB: {DB}")
    print(f"Depuis {since}: algo {open_algo}/{total_algo} en cours · daily_top_proba {open_dtp} en cours")

    _classify_open_algo(conn, since=since)
    if "--fix" in sys.argv:
        _sync_counts(conn)
        open_algo2 = conn.execute(
            "SELECT COUNT(*) FROM algo_opportunities WHERE COALESCE(status,'En cours')='En cours' AND match_date>=?",
            (since,),
        ).fetchone()[0]
        print(f"Reste en cours algo: {open_algo2}")
    conn.close()


if __name__ == "__main__":
    main()
