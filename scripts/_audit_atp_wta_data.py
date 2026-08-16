#!/usr/bin/env python3
"""Audit rapide fraîcheur / couverture données ATP+WTA (prod ou local)."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PARIS = ZoneInfo("Europe/Paris")
DB = os.path.join(ROOT, "data", "bettinghud.db")


def main() -> int:
    from scripts.bets_db import get_data_freshness_snapshot
    from scripts.wta_delta_qc_gates import _rank_rate_post_cutoff_sqlite, run_wta_delta_qc_gates
    from scripts.wta_sackmann_common import DEFAULT_CUTOFF

    today = datetime.now(PARIS).date()
    cutoff5 = int((today - timedelta(days=5)).strftime("%Y%m%d"))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=== FRAÎCHEUR (meta + derniers matchs) ===")
    fresh = get_data_freshness_snapshot(DB)
    for k in (
        "last_tours_sync_iso",
        "last_tml_sync_iso",
        "last_sackmann_sync_iso",
        "last_atp_match",
        "last_wta_match",
    ):
        print(f"  {k}: {fresh.get(k)}")

    print("\n=== ATP matches_recent ===")
    atp = conn.execute(
        "SELECT COUNT(*) n, MAX(tourney_date) max_d, MIN(tourney_date) min_d FROM matches_recent"
    ).fetchone()
    print(f"  total={atp['n']} | max={atp['max_d']} | min={atp['min_d']}")
    aug = conn.execute(
        "SELECT COUNT(*) FROM matches_recent WHERE tourney_date >= ?", (int(today.strftime("%Y%m") + "01"),)
    ).fetchone()[0]
    print(f"  mois courant: {aug} matchs")
    stale = atp["max_d"] is None or int(str(atp["max_d"]).replace("-", "")[:8]) < cutoff5
    print(f"  verdict lag>5j: {'OUI (stale)' if stale else 'NON (OK)'}")

    print("\n=== WTA wta_matches ===")
    wta = conn.execute(
        "SELECT COUNT(*) n, MAX(tourney_date) max_d FROM wta_matches"
    ).fetchone()
    print(f"  total={wta['n']} | max brut={wta['max_d']}")
    sane_max = conn.execute(
        """
        SELECT MAX(tourney_date) FROM wta_matches
        WHERE tourney_date <= date('now', '+30 days')
        """
    ).fetchone()[0]
    print(f"  max sans dates futures aberrantes: {sane_max}")
    future = conn.execute(
        "SELECT COUNT(*) FROM wta_matches WHERE tourney_date > '2026-12-31'"
    ).fetchone()[0]
    print(f"  lignes date > 2026: {future}")
    aug_w = conn.execute(
        "SELECT COUNT(*) FROM wta_matches WHERE tourney_date >= '2026-08-01'"
    ).fetchone()[0]
    print(f"  août 2026: {aug_w} matchs")

    print("\n=== WTA delta (post-cutoff) ===")
    for label, main_only in (("main tour", True), ("all", False)):
        rate, ok_n, tot = _rank_rate_post_cutoff_sqlite(DB, DEFAULT_CUTOFF, main_tour_only=main_only)
        print(f"  rangs {label}: {rate:.1f}% ({ok_n}/{tot})")
    null_sv = conn.execute(
        """
        SELECT COUNT(*) FROM wta_matches
        WHERE CAST(REPLACE(SUBSTR(tourney_date, 1, 10), '-', '') AS INTEGER) > ?
          AND tourney_level NOT IN ('15', 'I')
          AND w_svpt IS NULL
        """,
        (int(DEFAULT_CUTOFF),),
    ).fetchone()[0]
    print(f"  main tour w_svpt NULL post-cutoff: {null_sv}")

    qc = run_wta_delta_qc_gates(db_path=DB)
    print("  QC gates:")
    for line in qc.summary_lines():
        print(f"    {line}")

    print("\n=== Feature store ===")
    fs = os.path.join(ROOT, "data", "cache", "player_feature_store.joblib")
    if os.path.isfile(fs):
        age_h = (time.time() - os.path.getmtime(fs)) / 3600
        print(f"  age={age_h:.1f}h | mtime={datetime.fromtimestamp(os.path.getmtime(fs), PARIS)}")
    else:
        print("  ABSENT")

    print("\n=== TML recent ===")
    try:
        tml_n = conn.execute("SELECT COUNT(*) FROM tml_recent_matches").fetchone()[0]
        tml_max = conn.execute("SELECT MAX(match_date) FROM tml_recent_matches").fetchone()[0]
        print(f"  rows={tml_n} | max_date={tml_max}")
    except sqlite3.OperationalError as exc:
        print(f"  table absente ou erreur: {exc}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
