#!/usr/bin/env python3
"""Diagnostic Moeller vs Mikrut — identity + snapshot."""
from __future__ import annotations

import glob
import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.player_identity import canonical_name, to_lastname_initial
from scripts.stats_engine import TennisStatsEngine, _canonical_player_index_key


def main() -> None:
    print("=== Canonical keys ===")
    for name in ["Moeller M.", "Mikrut L.", "Moeller", "Mikrut"]:
        print(
            f"  {name!r} -> last_init={to_lastname_initial(name)!r} "
            f"canon={canonical_name(to_lastname_initial(name))!r} "
            f"index_key={_canonical_player_index_key(name)!r}"
        )

    files = glob.glob("data/scraped/prematch_odds_*.csv")
    if files:
        csv = max(files, key=os.path.getmtime)
        df = pd.read_csv(csv)
        print(f"\n=== Prematch CSV: {csv} ({len(df)} rows) ===")
        for term in ["Moeller", "Mikrut", "Heilbronn"]:
            mask = df.astype(str).apply(lambda col: col.str.contains(term, case=False, na=False)).any(axis=1)
            hits = df.loc[mask]
            if len(hits):
                cols = [c for c in df.columns if c in (
                    "player1", "player2", "p1_url", "p2_url", "tournament", "time",
                    "category", "odd_p1", "odd_p2", "tourney_winner_points",
                )]
                print(f"\n-- {term} ({len(hits)} rows) --")
                print(hits[cols].to_string(index=False))
    else:
        print("\n=== No prematch CSV found ===")

    print("\n=== StatsEngine identity ===")
    se = TennisStatsEngine()
    urls = {}
    if files:
        csv = max(files, key=os.path.getmtime)
        df = pd.read_csv(csv)
        for _, row in df.iterrows():
            for pn, u in ((row.get("player1"), row.get("p1_url")), (row.get("player2"), row.get("p2_url"))):
                if pd.notna(pn) and pd.notna(u):
                    urls[str(pn).strip()] = str(u).strip()

    for name in ["Moeller M.", "Mikrut L."]:
        url = urls.get(name)
        for tour in ["ATP", "WTA", None]:
            meta = se.get_player_id_meta(name, source_name="flashscore", source_url=url, tour_hint=tour)
            print(f"  {name} tour={tour} url={url!r} -> {meta}")

    print("\n=== DB name search ===")
    conn = sqlite3.connect("data/bettinghud.db")
    for q in ["%Moeller%", "%Mikrut%", "%moeller%", "%mikrut%"]:
        for table, col in [
            ("matches_recent", "winner_name"),
            ("matches_recent", "loser_name"),
            ("wta_matches", "winner_name"),
            ("wta_matches", "loser_name"),
        ]:
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} LIKE ? LIMIT 20",
                    (q,),
                ).fetchall()
                if rows:
                    print(f"  {table}.{col} {q}: {[r[0] for r in rows]}")
            except Exception:
                pass
    print("\n=== All Moeller / Mikrut in matches_recent ===")
    for q, label in [("%Moeller%", "Moeller"), ("%Mikrut%", "Mikrut")]:
        rows = conn.execute(
            """
            SELECT DISTINCT id, name FROM (
              SELECT winner_id AS id, winner_name AS name FROM matches_recent WHERE winner_name LIKE ?
              UNION
              SELECT loser_id, loser_name FROM matches_recent WHERE loser_name LIKE ?
            )
            """,
            (q, q),
        ).fetchall()
        print(f"  {label}: {rows}")

    print("\n=== Player stats (ATP) ===")
    se = TennisStatsEngine()
    for name in ["Moeller M.", "Mikrut L."]:
        pid = se.get_player_id(name, tour_hint="ATP")
        st = se.get_player_stats(pid, name, tour_hint="ATP")
        print(f"  {name} -> pid={pid}")
        print(f"    {st}")

    conn.close()

    # snapshot
    try:
        import joblib
        for path in [
            "data/cache/live_matches_snapshot.joblib",
            "data/cache/live_matches_snapshot.full.joblib",
        ]:
            if os.path.isfile(path):
                matches = joblib.load(path)
                for m in matches:
                    blob = f"{m.get('player1')} {m.get('player2')} {m.get('tournament')}"
                    if "moeller" in blob.lower() or "mikrut" in blob.lower() or "heilbronn" in blob.lower():
                        print(f"\n=== Snapshot {path} ===")
                        print(
                            f"  {m.get('player1')} vs {m.get('player2')} | {m.get('tournament')} | {m.get('time')}"
                        )
                        print(f"  p1_stats={m.get('p1_stats')} p2_stats={m.get('p2_stats')}")
                        print(f"  p1_profile={m.get('p1_profile_loaded')} p2_profile={m.get('p2_profile_loaded')}")
                        print(f"  rank_source={m.get('rank_source_quality')}")
    except Exception as exc:
        print(f"\nSnapshot read error: {exc}")


if __name__ == "__main__":
    main()
