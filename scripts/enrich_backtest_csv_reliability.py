#!/usr/bin/env python3
"""Ajoute data_reliability_score aux CSV backtest 2025/2026.

Reconstruit un dict match (winner=p1, loser=p2) et calcule
``match_data_reliability_score`` avec rangs historiques TML/Sackmann quand possible.

Usage:
  python scripts/enrich_backtest_csv_reliability.py --year 2025
  python scripts/enrich_backtest_csv_reliability.py --csv data/backtest_2026_bets.csv
  python scripts/enrich_backtest_csv_reliability.py --year 2025 --year 2026 --in-place
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_2026 import _surname_initial  # noqa: E402
from scripts.match_rank_quality import match_data_reliability_score  # noqa: E402


def _norm(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _prediction_contradicts_rank_points(
    p1_prob: float,
    p1_rank: int,
    p2_rank: int,
) -> bool:
    try:
        if p1_rank <= 0 or p2_rank <= 0:
            return False
        gap = 30
        if p1_rank > p2_rank + gap and p1_prob > 0.55:
            return True
        if p2_rank > p1_rank + gap and p1_prob < 0.45:
            return True
        if p1_rank > p2_rank and p1_prob > 0.70 and (p1_rank - p2_rank) >= 25:
            return True
        if p2_rank > p1_rank and p1_prob < 0.30 and (p2_rank - p1_rank) >= 25:
            return True
    except Exception:
        pass
    return False


class HistoricalRankLookup:
    """Index léger nom+date → rangs depuis matches_recent / wta_matches."""

    def __init__(self, db_path: str) -> None:
        self._by_key: dict[tuple[str, str, str], dict] = {}
        conn = sqlite3.connect(db_path)
        try:
            self._load_table(conn, "matches_recent", "tennismylife", "ATP")
            self._load_wta(conn)
        finally:
            conn.close()

    def _load_table(self, conn: sqlite3.Connection, table: str, source: str, tour: str) -> None:
        try:
            rows = conn.execute(
                f"""
                SELECT tourney_date, winner_name, loser_name,
                       winner_rank, loser_rank, winner_rank_points, loser_rank_points
                FROM {table}
                WHERE winner_name IS NOT NULL AND loser_name IS NOT NULL
                """
            ).fetchall()
        except sqlite3.Error:
            return
        for td, wn, ln, wr, lr, wp, lp in rows:
            d = str(td)[:10]
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            key = (
                tour,
                _surname_initial(str(wn)),
                _surname_initial(str(ln)),
            )
            self._by_key[(d, *key)] = {
                "winner_rank": int(wr or 0),
                "loser_rank": int(lr or 0),
                "winner_rank_points": float(wp or 0),
                "loser_rank_points": float(lp or 0),
                "stats_source": source,
                "date": d,
            }

    def _load_wta(self, conn: sqlite3.Connection) -> None:
        try:
            rows = conn.execute(
                """
                SELECT tourney_date, winner_name, loser_name,
                       winner_rank, loser_rank, winner_rank_points, loser_rank_points
                FROM wta_matches
                WHERE winner_name IS NOT NULL AND loser_name IS NOT NULL
                """
            ).fetchall()
        except sqlite3.Error:
            return
        for td, wn, ln, wr, lr, wp, lp in rows:
            d = pd.Timestamp(td).strftime("%Y-%m-%d") if td else ""
            if not d:
                continue
            key = ("WTA", _surname_initial(str(wn)), _surname_initial(str(ln)))
            self._by_key[(d, *key)] = {
                "winner_rank": int(wr or 0),
                "loser_rank": int(lr or 0),
                "winner_rank_points": float(wp or 0),
                "loser_rank_points": float(lp or 0),
                "stats_source": "sackmann_wta",
                "date": d,
            }

    def lookup(self, *, tour: str, winner: str, loser: str, date_iso: str) -> dict | None:
        tour_u = str(tour or "ATP").upper()
        wk = _surname_initial(winner)
        lk = _surname_initial(loser)
        base = pd.Timestamp(date_iso)
        for delta in range(0, 15):
            d = (base + pd.Timedelta(days=delta)).strftime("%Y-%m-%d")
            hit = self._by_key.get((d, tour_u, wk, lk))
            if hit:
                return hit
        for delta in (-1, -2, -3):
            d = (base + pd.Timedelta(days=delta)).strftime("%Y-%m-%d")
            hit = self._by_key.get((d, tour_u, wk, lk))
            if hit:
                return hit
        return None


def _row_to_match(row: pd.Series, lookup: HistoricalRankLookup) -> dict:
    winner = str(row.get("winner_name") or "")
    loser = str(row.get("loser_name") or "")
    tour = str(row.get("tour") or "ATP").upper()
    date_iso = str(row.get("date") or "")[:10]
    hist = lookup.lookup(tour=tour, winner=winner, loser=loser, date_iso=date_iso)

    p1_prob_raw = row.get("global_p1_prob")
    if p1_prob_raw is not None and not pd.isna(p1_prob_raw):
        p1_prob = float(p1_prob_raw)
    else:
        side = str(row.get("side") or "WINNER").upper()
        pm = float(row.get("p_model") or 0.5)
        p1_prob = pm if side == "WINNER" else 1.0 - pm

    if hist:
        p1_stats = {
            "rank": hist["winner_rank"],
            "pts": hist["winner_rank_points"],
            "stats_source": hist["stats_source"],
            "stats_reference_date": hist["date"],
        }
        p2_stats = {
            "rank": hist["loser_rank"],
            "pts": hist["loser_rank_points"],
            "stats_source": hist["stats_source"],
            "stats_reference_date": hist["date"],
        }
        p1_id = f"{tour}::hist::{_norm(winner)}"
        p2_id = f"{tour}::hist::{_norm(loser)}"
    else:
        p1_stats = {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}
        p2_stats = {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}
        p1_id = None
        p2_id = None

    try:
        gap_pp = abs(float(row.get("p_model") or 0) - float(row.get("p_implied") or 0)) * 100.0
    except (TypeError, ValueError):
        gap_pp = None

    match = {
        "date": date_iso,
        "player1": winner,
        "player2": loser,
        "p1_player_id": p1_id,
        "p2_player_id": p2_id,
        "p1_stats": p1_stats,
        "p2_stats": p2_stats,
        "true_odd_p1": 1.0 / p1_prob if p1_prob > 0 else 2.0,
        "true_odd_p2": 1.0 / (1.0 - p1_prob) if p1_prob < 1 else 2.0,
        "book_gap_pp": gap_pp,
        "snapshot_tier": "full",
    }
    match["unreliable"] = _prediction_contradicts_rank_points(
        p1_prob,
        int(p1_stats.get("rank") or 0),
        int(p2_stats.get("rank") or 0),
    )
    return match


def augment_backtest_dataframe(
    df: pd.DataFrame,
    *,
    db_path: str | None = None,
    recompute_missing: bool = True,
) -> pd.DataFrame:
    """Complète book_gap, reliability, confidence sur un DataFrame backtest."""
    out = df.copy()
    if "book_gap_pp" not in out.columns and "p_model" in out.columns and "p_implied" in out.columns:
        out["book_gap_pp"] = (out["p_model"].astype(float) - out["p_implied"].astype(float)).abs() * 100.0
    if "p_model_fav" not in out.columns and "p_model" in out.columns:
        out["p_model_fav"] = out["p_model"].astype(float)

    need_rel = recompute_missing and (
        "data_reliability_score" not in out.columns
        or out["data_reliability_score"].isna().all()
    )
    if need_rel:
        db = db_path or os.path.join(ROOT, "data", "bettinghud.db")
        lookup = HistoricalRankLookup(db)
        scores: list[int] = []
        flags: list[str] = []
        for _, row in out.iterrows():
            m = _row_to_match(row, lookup)
            score, fl = match_data_reliability_score(m)
            scores.append(score)
            flags.append("|".join(fl) if fl else "")
        out["data_reliability_score"] = scores
        out["data_reliability_flags"] = flags
    elif "data_reliability_score" in out.columns:
        out["data_reliability_score"] = pd.to_numeric(out["data_reliability_score"], errors="coerce")

    if "confidence" not in out.columns or out["confidence"].isna().all():
        pm = out["p_model"].astype(float) if "p_model" in out.columns else out["p_model_fav"].astype(float)
        out["confidence"] = ((pm - 0.5).abs() * 2.0).clip(0.0, 1.0)
    return out


def enrich_csv(path: str, *, db_path: str, out_path: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    lookup = HistoricalRankLookup(db_path)
    hist_hit = sum(
        1
        for _, row in df.iterrows()
        if _row_to_match(row, lookup).get("p1_stats", {}).get("stats_source")
    )
    df = augment_backtest_dataframe(df, db_path=db_path, recompute_missing=True)

    dest = out_path or path
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    df.to_csv(dest, index=False)

    ge80 = int((df["data_reliability_score"] >= 80).sum())
    ge85 = int((df["data_reliability_score"] >= 85).sum())
    print(
        f"{os.path.basename(path)}: {len(df)} lignes | rangs historiques: {hist_hit}/{len(df)} "
        f"({100*hist_hit/len(df):.0f}%) | score≥80: {ge80} | score≥85: {ge85} | "
        f"moy={df['data_reliability_score'].mean():.1f} -> {dest}"
    )
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrichit CSV backtest avec data_reliability_score")
    ap.add_argument("--csv", action="append", help="Chemin CSV (répétable)")
    ap.add_argument("--year", type=int, action="append", help="2025 ou 2026 → data/backtest_YEAR_bets.csv")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--out", default="", help="Fichier sortie (défaut: écrase --csv)")
    ap.add_argument("--in-place", action="store_true", help="Écrase les CSV sources")
    args = ap.parse_args()

    paths: list[str] = list(args.csv or [])
    for y in args.year or []:
        paths.append(os.path.join(ROOT, "data", f"backtest_{y}_bets.csv"))
    if not paths:
        paths = [
            os.path.join(ROOT, "data", "backtest_2025_bets.csv"),
            os.path.join(ROOT, "data", "backtest_2026_bets.csv"),
        ]

    for i, path in enumerate(paths):
        if not os.path.isfile(path):
            print(f"[!] absent: {path}")
            continue
        out = path if args.in_place or not args.out else (args.out if len(paths) == 1 else args.out.replace(".csv", f"_{i}.csv"))
        if args.out and not args.in_place and len(paths) == 1:
            out = args.out
        enrich_csv(path, db_path=args.db, out_path=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
