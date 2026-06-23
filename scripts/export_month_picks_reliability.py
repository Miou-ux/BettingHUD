#!/usr/bin/env python3
"""Export picks daily_top_proba (N derniers jours) + score fiabilité recomputé + focus perdus."""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import open_db, sync_daily_top_proba_from_results
from scripts.match_rank_quality import match_data_reliability_score
from scripts.stats_engine import TennisStatsEngine

PARIS_TZ = ZoneInfo("Europe/Paris")


def _prediction_contradicts_rank_points(match: dict) -> bool:
    try:
        p1_odd = float(match.get("true_odd_p1") or 0.0)
        if p1_odd <= 1.0:
            return False
        p1_prob = 1.0 / p1_odd
        ps1 = match.get("p1_stats") if isinstance(match.get("p1_stats"), dict) else {}
        ps2 = match.get("p2_stats") if isinstance(match.get("p2_stats"), dict) else {}
        r1 = int(ps1.get("rank") or 0)
        r2 = int(ps2.get("rank") or 0)
        if r1 <= 0 or r2 <= 0:
            return False
        gap = 30
        if r1 > r2 + gap and p1_prob > 0.55:
            return True
        if r2 > r1 + gap and p1_prob < 0.45:
            return True
        if r1 > r2 and p1_prob > 0.70 and (r1 - r2) >= 25:
            return True
        if r2 > r1 and p1_prob < 0.30 and (r2 - r1) >= 25:
            return True
    except Exception:
        pass
    return False


def _pick_row_to_match(row: dict, engine: TennisStatsEngine) -> dict:
    fav_side = int(row.get("fav_side") or 1)
    if fav_side == 1:
        true_odd_p1 = float(row.get("true_odd_fav") or 2.0)
        true_odd_p2 = float(row.get("true_odd_underdog") or 2.0)
        odd_p1 = float(row.get("odd_fav") or 0.0)
        odd_p2 = float(row.get("odd_underdog") or 0.0)
    else:
        true_odd_p1 = float(row.get("true_odd_underdog") or 2.0)
        true_odd_p2 = float(row.get("true_odd_fav") or 2.0)
        odd_p1 = float(row.get("odd_underdog") or 0.0)
        odd_p2 = float(row.get("odd_fav") or 0.0)

    tour = str(row.get("tour") or "ATP").upper()
    p1_name = str(row.get("player1") or "").strip()
    p2_name = str(row.get("player2") or "").strip()
    p1_meta = engine.get_player_id_meta(p1_name, tour_hint=tour)
    p2_meta = engine.get_player_id_meta(p2_name, tour_hint=tour)
    p1_id = p1_meta.get("player_id")
    p2_id = p2_meta.get("player_id")
    p1_stats = dict(engine.get_player_stats(p1_id, p1_name, tour_hint=tour))
    p2_stats = dict(engine.get_player_stats(p2_id, p2_name, tour_hint=tour))

    match = {
        "date": str(row.get("match_date") or row.get("calendar_date") or "")[:10],
        "player1": p1_name,
        "player2": p2_name,
        "p1_player_id": p1_id,
        "p2_player_id": p2_id,
        "p1_stats": p1_stats,
        "p2_stats": p2_stats,
        "true_odd_p1": true_odd_p1,
        "true_odd_p2": true_odd_p2,
        "odd_p1": odd_p1,
        "odd_p2": odd_p2,
        "book_gap_pp": row.get("book_gap_pp"),
        "snapshot_tier": row.get("snapshot_tier") or "full",
    }
    match["unreliable"] = _prediction_contradicts_rank_points(match)
    return match


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument(
        "--out",
        default="",
        help="CSV (defaut: data/exports/month_picks_reliability_YYYYMMDD.csv)",
    )
    args = ap.parse_args()

    since = (datetime.now(PARIS_TZ).date() - timedelta(days=max(1, args.days))).isoformat()
    out = args.out.strip() or os.path.join(
        ROOT,
        "data",
        "exports",
        f"month_picks_reliability_{datetime.now(PARIS_TZ).strftime('%Y%m%d')}.csv",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)

    conn = open_db(args.db)
    try:
        sync_daily_top_proba_from_results(conn)
        conn.row_factory = None
        cur = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            WHERE calendar_date >= ?
            ORDER BY calendar_date, tour, rank
            """,
            (since,),
        )
        cols = [d[0] for d in cur.description]
        raw_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    engine = TennisStatsEngine(db_path=args.db)
    export_rows: list[dict] = []
    for row in raw_rows:
        match = _pick_row_to_match(row, engine)
        score, flags = match_data_reliability_score(match)
        export_rows.append(
            {
                "calendar_date": row.get("calendar_date"),
                "match_date": row.get("match_date"),
                "tour": row.get("tour"),
                "rank": row.get("rank"),
                "match_name": row.get("match_name"),
                "fav_player": row.get("fav_player"),
                "p_model_fav_pct": round(float(row.get("p_model_fav") or 0) * 100, 2),
                "odd_fav": row.get("odd_fav"),
                "ev_fav_pct": row.get("ev_fav_pct"),
                "book_gap_pp": row.get("book_gap_pp"),
                "snapshot_tier": row.get("snapshot_tier"),
                "status": row.get("status"),
                "winner_resolved": row.get("winner_resolved"),
                "score_final": row.get("score_final"),
                "theoretical_profit": row.get("theoretical_profit"),
                "p1_rank": match["p1_stats"].get("rank"),
                "p2_rank": match["p2_stats"].get("rank"),
                "p1_stats_source": match["p1_stats"].get("stats_source"),
                "p2_stats_source": match["p2_stats"].get("stats_source"),
                "unreliable_recomputed": match.get("unreliable"),
                "data_reliability_score": score,
                "data_reliability_flags": "|".join(flags),
                "score_note": "recomputed_at_export",
            }
        )

    fieldnames = list(export_rows[0].keys()) if export_rows else []
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(export_rows)

    lost = [r for r in export_rows if str(r.get("status") or "") == "Perdu"]
    won = [r for r in export_rows if str(r.get("status") or "") == "Gagné"]
    pending = [r for r in export_rows if str(r.get("status") or "") not in ("Gagné", "Perdu", "Annulé")]

    print(f"since={since} picks={len(export_rows)} -> {out}")
    print(f"settled: gagné={len(won)} perdu={len(lost)} autre/en_cours={len(pending)}")
    if lost:
        print("\n=== PERDUS (score fiabilité recomputé) ===")
        for r in sorted(lost, key=lambda x: (x.get("calendar_date"), x.get("tour"), x.get("rank"))):
            print(
                f"{r['calendar_date']} {r['tour']}#{r['rank']} | {r['fav_player']} | "
                f"p={r['p_model_fav_pct']}% odd={r['odd_fav']} | score={r['data_reliability_score']} | "
                f"flags={r['data_reliability_flags'] or '-'}"
            )
        avg_lost = sum(int(r["data_reliability_score"]) for r in lost) / len(lost)
        avg_won = sum(int(r["data_reliability_score"]) for r in won) / len(won) if won else 0
        print(f"\nscore moyen perdus={avg_lost:.1f}  gagnés={avg_won:.1f}")
        under80 = sum(1 for r in lost if int(r["data_reliability_score"]) < 80)
        print(f"perdus avec score < 80: {under80}/{len(lost)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
