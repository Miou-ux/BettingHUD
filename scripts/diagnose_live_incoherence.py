#!/usr/bin/env python3
"""Diagnose live prediction vs book/market gaps for snapshot matches."""
from __future__ import annotations

import argparse
import json
import os
import sys

import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import ml_model


def _load_snapshot():
    path = os.path.join(ROOT, "data", "cache", "live_matches_snapshot.full.joblib")
    data = joblib.load(path)
    return data.get("matches") or [], path


def _implied(odd: float) -> float | None:
    return 1.0 / odd if odd and odd > 1.0 else None


def _predict_minimal(ml: ml_model.TennisMLModel, m: dict) -> float:
    p1s, p2s = m["p1_stats"], m["p2_stats"]
    return float(
        ml.predict_match(
            surface=m.get("surface", "Clay"),
            p1_name=m["player1"],
            p2_name=m["player2"],
            p1_rank=p1s["rank"],
            p2_rank=p2s["rank"],
            p1_age=p1s["age"],
            p2_age=p2s["age"],
            p1_ht=p1s["ht"],
            p2_ht=p2s["ht"],
            p1_pts=p1s["pts"],
            p2_pts=p2s["pts"],
            p1_id=m.get("p1_player_id"),
            p2_id=m.get("p2_player_id"),
            tour=m.get("tour") or "WTA",
            tournament_name=m.get("tournament"),
            match_date=m.get("date"),
            h2h_p1_wins=(m.get("h2h") or {}).get("p1_wins", 0),
            h2h_p2_wins=(m.get("h2h") or {}).get("p2_wins", 0),
        )["p1_win_prob"]
    )


def _predict_full(ml: ml_model.TennisMLModel, m: dict) -> float:
    p1s, p2s = m["p1_stats"], m["p2_stats"]
    f1, f2 = m.get("p1_form") or {}, m.get("p2_form") or {}
    fat1, fat2 = m.get("p1_fatigue") or {}, m.get("p2_fatigue") or {}
    mq1, mq2 = m.get("p1_match_quality") or {}, m.get("p2_match_quality") or {}
    fs = m.get("feature_snapshot") or {}
    return float(
        ml.predict_match(
            surface=m.get("surface", "Clay"),
            p1_name=m["player1"],
            p2_name=m["player2"],
            p1_rank=p1s["rank"],
            p2_rank=p2s["rank"],
            p1_age=p1s["age"],
            p2_age=p2s["age"],
            p1_ht=p1s["ht"],
            p2_ht=p2s["ht"],
            p1_pts=p1s["pts"],
            p2_pts=p2s["pts"],
            p1_id=m.get("p1_player_id"),
            p2_id=m.get("p2_player_id"),
            tour=m.get("tour") or "WTA",
            tournament_name=m.get("tournament"),
            match_date=m.get("date"),
            h2h_p1_wins=(m.get("h2h") or {}).get("p1_wins", 0),
            h2h_p2_wins=(m.get("h2h") or {}).get("p2_wins", 0),
            p1_form_win_pct_90=f1.get("win_pct", 50),
            p2_form_win_pct_90=f2.get("win_pct", 50),
            p1_fatigue_minutes_14=fat1.get("minutes_played", 0),
            p2_fatigue_minutes_14=fat2.get("minutes_played", 0),
            p1_fatigue_matches_14=fat1.get("matches", 0),
            p2_fatigue_matches_14=fat2.get("matches", 0),
            p1_hand=p1s.get("hand", "U"),
            p2_hand=p2s.get("hand", "U"),
            p1_wins_last7d=mq1.get("wins_last7d", 0),
            p2_wins_last7d=mq2.get("wins_last7d", 0),
            p1_three_setters_last14d=mq1.get("three_setters_last14d", 0),
            p2_three_setters_last14d=mq2.get("three_setters_last14d", 0),
            p1_last_round_reached=mq1.get("last_round_reached", 0),
            p2_last_round_reached=mq2.get("last_round_reached", 0),
            p1_days_since_last_match=fs.get("p1_days_since_last_match"),
            p2_days_since_last_match=fs.get("p2_days_since_last_match"),
            p1_te_inactivity_blend_used=bool(fs.get("p1_te_inactivity_blend_used")),
            p2_te_inactivity_blend_used=bool(fs.get("p2_te_inactivity_blend_used")),
            p1_days_inactivity_pre_te=fs.get("p1_days_inactivity_pre_te"),
            p2_days_inactivity_pre_te=fs.get("p2_days_inactivity_pre_te"),
            p1_style_advantage_score=fs.get("style_advantage_score"),
            p1_first_srv_win10=fs.get("p1_first_srv_win10") if "p1_first_srv_win10" in fs else None,
            p2_first_srv_win10=fs.get("p2_first_srv_win10") if "p2_first_srv_win10" in fs else None,
            p1_bp_conv10=fs.get("p1_bp_conv10") if "p1_bp_conv10" in fs else None,
            p2_bp_conv10=fs.get("p2_bp_conv10") if "p2_bp_conv10" in fs else None,
            p1_dominance_ratio=fs.get("p1_dominance_ratio") if "p1_dominance_ratio" in fs else None,
            p2_dominance_ratio=fs.get("p2_dominance_ratio") if "p2_dominance_ratio" in fs else None,
        )["p1_win_prob"]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="", help="substring player1")
    ap.add_argument("--rg-wta", action="store_true")
    ap.add_argument("--gap-pp", type=float, default=25.0)
    args = ap.parse_args()

    matches, path = _load_snapshot()
    ml = ml_model.TennisMLModel()
    ml._load_bundle_if_needed()

    rows = []
    for m in matches:
        if args.filter and args.filter.lower() not in str(m.get("player1", "")).lower():
            continue
        if args.rg_wta:
            t = str(m.get("tournament", "")).lower()
            if (m.get("tour") or "").upper() not in ("WTA", "W"):
                continue
            if not any(x in t for x in ("roland", "garros", "french open")):
                continue
        snap_p = 1.0 / float(m["true_odd_p1"]) if float(m.get("true_odd_p1") or 0) > 1 else None
        o1 = float(m.get("odd_p1") or 0)
        imp = _implied(o1)
        try:
            p_min = _predict_minimal(ml, m)
            p_full = _predict_full(ml, m)
        except Exception as exc:
            p_min = p_full = None
            err = str(exc)
        else:
            err = None
        gap_book = abs(snap_p - imp) * 100 if snap_p and imp else None
        gap_replay = abs(snap_p - p_min) * 100 if snap_p and p_min is not None else None
        rows.append(
            {
                "match": f"{m.get('player1')} vs {m.get('player2')}",
                "snap_p1_pct": round(snap_p * 100, 1) if snap_p else None,
                "book_p1_pct": round(imp * 100, 1) if imp else None,
                "replay_min_pct": round(p_min * 100, 1) if p_min is not None else None,
                "replay_full_pct": round(p_full * 100, 1) if p_full is not None else None,
                "snap_vs_book_pp": round(gap_book, 1) if gap_book is not None else None,
                "snap_vs_min_pp": round(gap_replay, 1) if gap_replay is not None else None,
                "stats_ref_p1": (m.get("p1_stats") or {}).get("stats_reference_date"),
                "stats_ref_p2": (m.get("p2_stats") or {}).get("stats_reference_date"),
                "stats_src": (m.get("p1_stats") or {}).get("stats_source"),
                "err": err,
            }
        )

    if args.rg_wta:
        big = [r for r in rows if (r.get("snap_vs_book_pp") or 0) >= args.gap_pp]
        print(f"RG WTA: {len(rows)} matchs, {len(big)} écarts snap/book >= {args.gap_pp} pp")
    print(json.dumps(rows if args.filter else rows[:15], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
