#!/usr/bin/env python3
"""Diagnostic Waltert display vs DB — snapshot runtime vs persistence."""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from datetime import datetime
from zoneinfo import ZoneInfo

from app.dashboard import (
    _collect_top_favorite_action_cards,
    _load_today_tracked_matches_for_inplay,
    _match_favorite_model_metrics,
)
from scripts.daily_top_proba_store import (
    collect_hybrid_proba_picks,
    collect_top5_proba_picks,
    filter_matches_for_daily_top_proba,
    load_today_matches_for_daily_top_proba,
)
from scripts.discord_1d1p_core import load_1d1p_today_pick
from scripts.live_snapshot import load_latest_live_snapshot
from scripts.match_rank_quality import (
    ensure_match_reliability_scored,
    passes_data_reliability_filter,
)
from scripts.pick_modes import PickMode, load_picks

PARIS = ZoneInfo("Europe/Paris")
today = datetime.now(PARIS).date().isoformat()


def main() -> None:
    allm, meta = load_latest_live_snapshot(max_age_sec=86400)
    print(f"=== SNAPSHOT {len(allm)} matchs built_at={meta.get('built_at')} ===")
    w = [m for m in allm if "waltert" in str(m).lower()]
    for m in w:
        ensure_match_reliability_scored(m)
        met = _match_favorite_model_metrics(m)
        print(
            f"  {m.get('player1')} vs {m.get('player2')} | "
            f"fav={met['fav'] if met else '?'} "
            f"p_fav={(met['fav_p']*100):.1f}% "
            f"rel={m.get('data_reliability_score')} flags={m.get('data_reliability_flags')}"
        )
        if met:
            print(
                f"    p1={met['p1']*100:.1f}% true_odd=({m.get('true_odd_p1')},{m.get('true_odd_p2')}) "
                f"ev={met.get('ev_fav_pct')}%"
            )

    filtered, _ = load_today_matches_for_daily_top_proba()
    print(f"\n=== APRÈS filter_matches_for_daily_top_proba: {len(filtered)} (Waltert dedans? ", end="")
    print(any("waltert" in str(m).lower() for m in filtered), ")")

    raw_filt = filter_matches_for_daily_top_proba(
        [dict(m) for m in allm if isinstance(m, dict)]
    )
    w2 = [m for m in raw_filt if "waltert" in str(m).lower()]
    print(f"=== filter seul (sans today): Waltert count={len(w2)} passes_rel=", end="")
    if w:
        print(passes_data_reliability_filter(w[0]))

    top5 = collect_top5_proba_picks(filtered, limit=10, calendar_date=today)
    hyb = collect_hybrid_proba_picks(filtered, limit=5, calendar_date=today)
    pick, _, pool, _ = load_1d1p_today_pick(db_path="data/bettinghud.db", calendar_date=today)
    print(f"\n=== RUNTIME PICKS (snapshot, pas DB) ===")
    print(f"top5={[r.get('fav_player') for r in top5]}")
    print(f"hybrid={[r.get('fav_player') for r in hyb]}")
    print(f"1d1p={pick.get('fav_player') if pick else None} pool={pool}")

    for mode in (PickMode.TOP5, PickMode.ONE_PICK_ONE_DAY):
        r = load_picks(mode)
        print(f"pick_modes {mode.value}: {[p.get('fav_player') for p in r.picks]}")

    cards = _collect_top_favorite_action_cards(_load_today_tracked_matches_for_inplay(), limit=10)
    wcards = [
        c for c in cards if "waltert" in str(c.get("match", {})).lower()
    ]
    print(f"\n=== DASHBOARD Top5 Action (snapshot, SANS filtre fiabilité) ===")
    print(f"cards={len(cards)} waltert_cards={len(wcards)}")
    for c in cards[:5]:
        met = c["metrics"]
        print(f"  {met['fav']} {met['fav_p']*100:.1f}% EV={met.get('ev_fav_pct')}")

    conn = sqlite3.connect("data/bettinghud.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT fav_player, p_model_fav, ev_fav_pct, data_reliability_score, rank, capture_source
        FROM daily_top_proba_picks
        WHERE calendar_date = ?
        ORDER BY rank
        """,
        (today,),
    ).fetchall()
    print(f"\n=== DB daily_top_proba_picks ({len(rows)} lignes) ===")
    for r in rows:
        print(dict(r))
    hist = conn.execute(
        """
        SELECT calendar_date, fav_player, p_model_fav
        FROM daily_top_proba_picks
        WHERE lower(fav_player) LIKE '%waltert%'
           OR lower(player1) LIKE '%waltert%'
           OR lower(player2) LIKE '%waltert%'
        ORDER BY calendar_date DESC LIMIT 5
        """
    ).fetchall()
    print(f"\n=== Historique DB Waltert: {len(hist)} ===")
    for r in hist:
        print(dict(r))
    conn.close()

    print("\n=== TOP fav_p > 93% (snapshot brut, tous matchs) ===")
    hi = []
    for m in allm:
        met = _match_favorite_model_metrics(m)
        if met and met["fav_p"] > 0.93:
            hi.append((met["fav_p"], met["fav"], met["und"], m.get("tournament")))
    hi.sort(reverse=True)
    for row in hi:
        print(f"  {row[0]*100:.1f}% {row[1]} vs {row[2]} ({row[3]})")


if __name__ == "__main__":
    main()
