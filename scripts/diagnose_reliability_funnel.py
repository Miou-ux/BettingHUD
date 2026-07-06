#!/usr/bin/env python3
"""Diagnostic entonnoir fiabilité — matchs du jour vs seuil publication (rel ≥ 80).

Usage:
    py -3 scripts/diagnose_reliability_funnel.py
    py -3 scripts/diagnose_reliability_funnel.py --rescore
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PARIS = ZoneInfo("Europe/Paris")


def main() -> int:
    parser = argparse.ArgumentParser(description="Entonnoir fiabilité data (jour courant)")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Force rescore v2 même si score snapshot présent",
    )
    args = parser.parse_args()

    from scripts.daily_top_proba_store import (
        collect_hybrid_proba_picks,
        filter_matches_for_daily_top_proba,
        is_today_paris_match,
    )
    from scripts.live_snapshot import load_latest_live_snapshot
    from scripts.live_tracker_picks import collect_live_tracker_value_picks
    from scripts.match_rank_quality import (
        MIN_DATA_RELIABILITY_SCORE,
        RELIABILITY_SCORE_VERSION,
        compute_match_reliability,
        duplicate_model_prob_keys,
        ensure_match_reliability_scored,
        passes_data_reliability_filter,
    )
    from scripts.tournament_tier import is_major_tournament_match

    raw, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
    today = [dict(m) for m in raw if isinstance(m, dict) and is_today_paris_match(m)]
    dup = duplicate_model_prob_keys(today)

    if args.rescore:
        for m in today:
            m.pop("data_reliability_score", None)
            m.pop("data_reliability_version", None)

    for m in today:
        ensure_match_reliability_scored(m, duplicate_keys=dup)

    filtered = filter_matches_for_daily_top_proba(today)
    hybrid = collect_hybrid_proba_picks(filtered, limit=5)
    value = collect_live_tracker_value_picks(filtered, ev_threshold_pct=15.0)

    below = []
    for m in today:
        if passes_data_reliability_filter(m):
            continue
        score, flags = compute_match_reliability(m, duplicate_keys=dup)
        below.append(
            {
                "match": f"{m.get('player1')} vs {m.get('player2')}",
                "tournament": m.get("tournament"),
                "score": score,
                "flags": flags,
                "major": is_major_tournament_match(m),
            }
        )

    built = float(meta.get("built_at") or 0.0)
    age_h = (datetime.now(PARIS).timestamp() - built) / 3600.0 if built else -1.0

    print(f"Date (Paris): {datetime.now(PARIS).date().isoformat()}")
    print(f"Snapshot age: {age_h:.1f} h | score version: {RELIABILITY_SCORE_VERSION}")
    print(f"Seuil publication: rel >= {MIN_DATA_RELIABILITY_SCORE}")
    print()
    print(f"  Matchs du jour (raw)     : {len(today)}")
    print(f"  Apres rel >= {MIN_DATA_RELIABILITY_SCORE}       : {len(filtered)}")
    print(f"  Dont majors 250+         : {sum(1 for m in filtered if is_major_tournament_match(m))}")
    print(f"  Value bets EV >= 15 %     : {len(value)}")
    print(f"  Hybride Top 5 (verrouillé): {len(hybrid)}")
    print()

    if below:
        print("Exclus par fiabilité (top 15):")
        for row in sorted(below, key=lambda r: -int(r["score"]))[:15]:
            flags_s = ",".join(row["flags"][:4]) if row["flags"] else "—"
            maj = "M" if row["major"] else "·"
            print(f"  [{maj}] {row['score']:3d}  {row['match'][:45]:45s}  {flags_s}")

    if age_h > 6:
        print()
        print("NOTE: Snapshot > 6 h — lancer: py -3 scripts/rebuild_live_projection.py")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
