#!/usr/bin/env python3
"""Audit projection du jour : ecarts modele vs book + causes."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import ml_model
from scripts.diagnose_live_incoherence import _implied, _predict_full, _predict_minimal


def _load_snapshot():
    for name in (
        "live_matches_snapshot.full.joblib",
        "live_matches_snapshot.joblib",
    ):
        path = os.path.join(ROOT, "data", "cache", name)
        if os.path.isfile(path):
            data = joblib.load(path)
            matches = data.get("matches") if isinstance(data, dict) else data
            meta_path = path + ".meta.json"
            meta = {}
            if os.path.isfile(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            return list(matches or []), path, meta
    return [], None, {}


def _predict_with_adv(ml, m, match_date: str):
    os.environ.setdefault("BETTINGHUD_HEADLESS", "1")
    from app.dashboard import (
        _compute_live_advanced_signals,
        _defending_ratio_cached,
    )

    p1s, p2s = m["p1_stats"], m["p2_stats"]
    mq1, mq2 = m.get("p1_match_quality") or {}, m.get("p2_match_quality") or {}
    f1, f2 = m.get("p1_form") or {}, m.get("p2_form") or {}
    fs = m.get("feature_snapshot") or {}
    tour = m.get("tour") or "ATP"
    adv = _compute_live_advanced_signals(
        m["player1"],
        m["player2"],
        m.get("surface") or "Clay",
        tour,
        str(m.get("tournament") or ""),
        match_date,
    )
    r1 = r2 = 0.0
    try:
        r1 = float(
            _defending_ratio_cached(
                ml.db_path,
                m.get("p1_player_id"),
                m.get("tournament"),
                match_date,
                float(p1s.get("pts") or 0),
                tour,
            )
        )
        r2 = float(
            _defending_ratio_cached(
                ml.db_path,
                m.get("p2_player_id"),
                m.get("tournament"),
                match_date,
                float(p2s.get("pts") or 0),
                tour,
            )
        )
    except Exception:
        pass
    h = m.get("h2h") or {}
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
            tour=tour,
            tournament_name=m.get("tournament"),
            match_date=match_date,
            p1_form_win_pct_90=f1.get("win_pct", 50),
            p2_form_win_pct_90=f2.get("win_pct", 50),
            p1_wins_last7d=mq1.get("wins_last7d", 0),
            p2_wins_last7d=mq2.get("wins_last7d", 0),
            p1_three_setters_last14d=mq1.get("three_setters_last14d", 0),
            p2_three_setters_last14d=mq2.get("three_setters_last14d", 0),
            p1_last_round_reached=mq1.get("last_round_reached", 0),
            p2_last_round_reached=mq2.get("last_round_reached", 0),
            h2h_p1_wins=h.get("p1_wins", 0),
            h2h_p2_wins=h.get("p2_wins", 0),
            p1_style_advantage_score=adv.get("style_advantage_score"),
            p1_clutch_index=adv.get("p1_clutch_index"),
            p2_clutch_index=adv.get("p2_clutch_index"),
            p1_first_srv_win10=adv.get("p1_first_srv_win10"),
            p2_first_srv_win10=adv.get("p2_first_srv_win10"),
            p1_bp_conv10=adv.get("p1_bp_conv10"),
            p2_bp_conv10=adv.get("p2_bp_conv10"),
            p1_dominance_ratio=adv.get("p1_dominance_ratio"),
            p2_dominance_ratio=adv.get("p2_dominance_ratio"),
            p1_days_since_last_match=fs.get("p1_days_since_last_match"),
            p2_days_since_last_match=fs.get("p2_days_since_last_match"),
            p1_points_def_ratio=r1,
            p2_points_def_ratio=r2,
            p1_tac_ace=adv.get("p1_tac_ace"),
            p2_tac_ace=adv.get("p2_tac_ace"),
            p1_tac_f1_pct=adv.get("p1_tac_f1_pct"),
            p2_tac_f1_pct=adv.get("p2_tac_f1_pct"),
            p1_tac_bp_saved_pct=adv.get("p1_tac_bp_saved_pct"),
            p2_tac_bp_saved_pct=adv.get("p2_tac_bp_saved_pct"),
            p1_tac_hold_pct=adv.get("p1_tac_hold_pct"),
            p2_tac_hold_pct=adv.get("p2_tac_hold_pct"),
            p1_travel_penalty_index=adv.get("p1_travel_penalty_index"),
            p2_travel_penalty_index=adv.get("p2_travel_penalty_index"),
            p1_clutch52=adv.get("p1_clutch52"),
            p2_clutch52=adv.get("p2_clutch52"),
            p1_minutes_played_last7d=adv.get("p1_minutes_played_last7d"),
            p2_minutes_played_last7d=adv.get("p2_minutes_played_last7d"),
            p1_tb_win_pct_52w=adv.get("p1_tb_win_pct_52w"),
            p2_tb_win_pct_52w=adv.get("p2_tb_win_pct_52w"),
        )["p1_win_prob"]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap-pp", type=float, default=25.0)
    ap.add_argument("--deep", action="store_true", help="Rejoue signaux tactiques (lent)")
    args = ap.parse_args()

    matches, path, meta = _load_snapshot()
    if not matches:
        print("Aucun snapshot trouve.")
        return 1

    built = datetime.fromtimestamp(meta["built_at"]) if meta.get("built_at") else None
    print(f"Snapshot: {path}")
    print(f"Construit: {built} | Matchs: {len(matches)}")

    ml = ml_model.TennisMLModel()
    ml._load_bundle_if_needed()

    rows = []
    for m in matches:
        snap_p = (
            1.0 / float(m["true_odd_p1"])
            if float(m.get("true_odd_p1") or 0) > 1
            else None
        )
        book_p = _implied(float(m.get("odd_p1") or 0))
        gap = abs(snap_p - book_p) * 100 if snap_p and book_p else None
        p_min = p_full = p_adv = None
        try:
            p_min = _predict_minimal(ml, m)
            p_full = _predict_full(ml, m)
        except Exception:
            pass
        if args.deep:
            try:
                md = str(m.get("date") or "")[:10] or datetime.now().date().isoformat()
                p_adv = _predict_with_adv(ml, m, md)
            except Exception:
                p_adv = None

        stale_min = abs(snap_p - p_min) * 100 if snap_p and p_min is not None else None
        stale_adv = abs(snap_p - p_adv) * 100 if snap_p and p_adv is not None else None

        rows.append(
            {
                "match": f"{m.get('player1')} vs {m.get('player2')}",
                "tour": m.get("tour"),
                "tournament": m.get("tournament"),
                "snap_pct": round(snap_p * 100, 1) if snap_p else None,
                "book_pct": round(book_p * 100, 1) if book_p else None,
                "gap_pp": round(gap, 1) if gap is not None else None,
                "core_pct": round(p_min * 100, 1) if p_min is not None else None,
                "full_pct": round(p_full * 100, 1) if p_full is not None else None,
                "adv_pct": round(p_adv * 100, 1) if p_adv is not None else None,
                "stale_vs_core_pp": round(stale_min, 1) if stale_min is not None else None,
                "stale_vs_adv_pp": round(stale_adv, 1) if stale_adv is not None else None,
                "stats_ref": (m.get("p1_stats") or {}).get("stats_reference_date"),
                "stats_src": (m.get("p1_stats") or {}).get("stats_source"),
            }
        )

    rows.sort(key=lambda r: -(r["gap_pp"] or 0))
    big = [r for r in rows if (r["gap_pp"] or 0) >= args.gap_pp]
    warn = [r for r in rows if 15 <= (r["gap_pp"] or 0) < args.gap_pp]
    ok = [r for r in rows if (r["gap_pp"] or 0) < 15]

    print(
        f"\nEcarts modele/book: >= {args.gap_pp}pp: {len(big)} | "
        f"15-{args.gap_pp}pp: {len(warn)} | <15pp: {len(ok)}"
    )

    by_tour: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_tour[str(r["tour"])].append(r["gap_pp"] or 0)
    print("\nPar tour:")
    for t, gaps in sorted(by_tour.items()):
        gaps_s = sorted(gaps)
        med = gaps_s[len(gaps_s) // 2]
        print(
            f"  {t}: n={len(gaps)} mediane={med:.1f}pp "
            f"max={max(gaps):.1f}pp count>={args.gap_pp}="
            f"{sum(1 for x in gaps if x >= args.gap_pp)}"
        )

    print(f"\nTop ecarts (>={args.gap_pp}pp):")
    for r in big[:12]:
        extra = ""
        if r.get("core_pct") is not None:
            extra = f" | core {r['core_pct']}%"
        if r.get("adv_pct") is not None:
            extra += f" adv_replay {r['adv_pct']}%"
        if (r.get("stale_vs_core_pp") or 0) > 10:
            extra += f" | snap!=core {r['stale_vs_core_pp']}pp"
        print(
            f"  {r['gap_pp']:5.1f}pp {r['tour']} {r['match'][:50]} "
            f"| mod {r['snap_pct']}% book {r['book_pct']}%{extra}"
        )

  # Verdict
    pct_bad = 100.0 * len(big) / max(1, len(rows))
    print(f"\nVerdict: {pct_bad:.0f}% des matchs avec ecart >= {args.gap_pp}pp")
    if pct_bad > 30:
        print("  -> Trop d'incoherences pour parier sans filtre.")
    elif pct_bad > 15:
        print("  -> Incohérences modérées; filtrer les gros ecarts.")
    else:
        print("  -> Niveau acceptable.")

    if args.deep and big:
        print("\nDecomposition (core vs tactique) sur les 3 pires:")
        for r in big[:3]:
            core = r.get("core_pct")
            adv = r.get("adv_pct")
            snap = r.get("snap_pct")
            if core is not None and adv is not None:
                tac_push = adv - core
                print(
                    f"  {r['match'][:40]}: core {core}% +tactique ~{tac_push:+.0f}pp "
                    f"= {adv}% (snap {snap}%)"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
