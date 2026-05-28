"""Diagnostic Garland / Von Deichmann — stats Sackmann vs snapshot live."""
from __future__ import annotations

import glob
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.live_snapshot import load_live_snapshot, snapshot_meta
from scripts.stats_engine import TennisStatsEngine, _canonical_player_index_key

DB = os.path.join(ROOT, "data", "bettinghud.db")


def main() -> None:
    se = TennisStatsEngine(DB)
    names = ["Garland J.", "Von Deichmann K."]
    print("=== Stats engine (DB actuelle) ===")
    for name in names:
        cn = _canonical_player_index_key(name)
        meta = se.get_player_id_meta(name, tour_hint="WTA")
        stats = se.get_player_stats(meta.get("player_id"), name, tour_hint="WTA")
        print(name, "key=", cn, "meta=", meta)
        print("  stats=", stats)

    import sqlite3

    conn = sqlite3.connect(DB)
    print("\n=== Derniers matchs SQL (Garland / Deichmann) ===")
    for pat in ("%Garland%", "%Deichmann%"):
        rows = conn.execute(
            """
            SELECT tourney_date, tourney_level, winner_name, loser_name
            FROM wta_matches
            WHERE winner_name LIKE ? OR loser_name LIKE ?
            ORDER BY tourney_date DESC LIMIT 5
            """,
            (pat, pat),
        ).fetchall()
        print(f"--- {pat} ---")
        for r in rows:
            print(" ", r)
    conn.close()

    print("\n=== Snapshot live ===")
    meta = snapshot_meta()
    print("snapshot_meta:", meta)
    # try load without signature (read file directly)
    snap_path = os.path.join(ROOT, "data", "cache", "live_matches_snapshot.joblib")
    if os.path.isfile(snap_path):
        import joblib

        payload = joblib.load(snap_path)
        matches = payload.get("matches") or []
        for m in matches:
            p1 = str(m.get("player1") or "")
            p2 = str(m.get("player2") or "")
            if "Garland" in p1 or "Garland" in p2:
                if "Deichmann" in p1 or "Deichmann" in p2:
                    print("\nMATCH trouvé dans snapshot:")
                    print(" ", m.get("player1"), "vs", m.get("player2"), m.get("tournament"), m.get("date"))
                    print("  p1_stats", m.get("p1_stats"))
                    print("  p2_stats", m.get("p2_stats"))
                    break
        else:
            for m in matches:
                if "Garland" in str(m.get("player1", "")) + str(m.get("player2", "")):
                    if "Deichmann" in str(m.get("player1", "")) + str(m.get("player2", "")):
                        continue
            garland_only = [
                m
                for m in matches
                if "Garland" in str(m.get("player1", "")) + str(m.get("player2", ""))
            ]
            print(f"matchs avec Garland dans snapshot: {len(garland_only)}")
            for m in garland_only[:3]:
                print(" ", m.get("player1"), "vs", m.get("player2"), m.get("p1_stats", {}).get("stats_reference_date"))

    prematch = sorted(glob.glob(os.path.join(ROOT, "data", "scraped", "prematch_odds_*.csv")), reverse=True)
    if prematch:
        df = pd.read_csv(prematch[0], low_memory=False)
        mask = (
            df["player1"].astype(str).str.contains("Garland", case=False, na=False)
            | df["player2"].astype(str).str.contains("Garland", case=False, na=False)
        ) & (
            df["player1"].astype(str).str.contains("Deichmann", case=False, na=False)
            | df["player2"].astype(str).str.contains("Deichmann", case=False, na=False)
        )
        print("\n=== Prematch CSV ===", prematch[0])
        print(df.loc[mask, ["player1", "player2", "tournament", "date", "category"]].head())


    import sqlite3
    import json

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT cache_key, pid, stats_json FROM live_player_cache "
        "WHERE cache_key LIKE '%Garland%' OR cache_key LIKE '%Deichmann%'"
    ).fetchall()
    print(f"\n=== live_player_cache ({len(rows)} lignes) ===")
    for ck, pid, sj in rows:
        st = json.loads(sj)
        print(ck[:70], pid, st.get("stats_reference_date"), st.get("rank"))


if __name__ == "__main__":
    main()
