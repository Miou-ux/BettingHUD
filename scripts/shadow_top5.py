#!/usr/bin/env python3
"""Shadow test runner for Top5 candidate strategy.

Current candidate:
  - majors ATP/WTA 250+ main draw
  - model favorite proba >= 80%
  - EV >= 25%
  - data_reliability_score >= 85
  - max 5/day, ranked by proba desc

This script stores daily picks and can sync results against match_results.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.bets_db import (
    DB_PATH_DEFAULT,
    _algo_kelly_stake_frac,
    _algo_profit_for_status,
    _lookup_match_result_for_players,
    _resolve_bet_status_from_match_result,
    ensure_match_results_cache,
    open_db,
)
from scripts.daily_top_proba_store import collect_top5_proba_picks, load_today_matches_for_daily_top_proba
from scripts.scraper_results import canonical_player

PARIS_TZ = ZoneInfo("Europe/Paris")
STRATEGY_KEY = "top5_ev25_rel85_p80"
TABLE = "shadow_top5_picks"


def ensure_shadow_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_key TEXT NOT NULL,
            calendar_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            match_name TEXT NOT NULL,
            player1 TEXT,
            player2 TEXT,
            fav_player TEXT,
            p_model_fav REAL,
            odd_fav REAL,
            ev_fav_pct REAL,
            data_reliability_score INTEGER,
            tournament TEXT,
            surface TEXT,
            status TEXT DEFAULT 'En cours',
            fav_won INTEGER,
            winner_resolved TEXT,
            score_final TEXT,
            result_source TEXT,
            theoretical_stake_frac REAL DEFAULT 0.0,
            theoretical_profit REAL DEFAULT 0.0,
            snapshot_built_at REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(strategy_key, calendar_date, rank)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_date ON {TABLE}(calendar_date)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_status ON {TABLE}(status)"
    )
    conn.commit()


def _today_paris() -> str:
    return datetime.now(PARIS_TZ).date().isoformat()


def select_candidate_picks(matches: list[dict], *, calendar_date: str) -> list[dict]:
    pool = collect_top5_proba_picks(
        matches,
        limit=None,
        ev_min_frac=0.25,
        ev_max_frac=1.0,
        today_only=True,
        major_only=True,
        min_proba_frac=0.80,
        calendar_date=calendar_date,
    )
    filtered = [
        p
        for p in pool
        if int(float(p.get("data_reliability_score") or 0)) >= 85
    ]
    filtered.sort(
        key=lambda r: (
            -float(r.get("p_model_fav") or 0.0),
            str(r.get("match_name") or "").lower(),
        )
    )
    out: list[dict] = []
    for i, row in enumerate(filtered[:5], start=1):
        pick = dict(row)
        pick["rank"] = i
        out.append(pick)
    return out


def capture_shadow_picks(
    *,
    db_path: str = DB_PATH_DEFAULT,
    strategy_key: str = STRATEGY_KEY,
    calendar_date: str | None = None,
) -> dict[str, Any]:
    cal_day = calendar_date or _today_paris()
    matches, meta = load_today_matches_for_daily_top_proba()
    picks = select_candidate_picks(matches, calendar_date=cal_day)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    built_at = float((meta or {}).get("built_at") or 0.0) or None

    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_shadow_schema(conn)
        for pick in picks:
            conn.execute(
                f"""
                INSERT INTO {TABLE}(
                    strategy_key, calendar_date, rank, match_name, player1, player2,
                    fav_player, p_model_fav, odd_fav, ev_fav_pct, data_reliability_score,
                    tournament, surface, snapshot_built_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_key, calendar_date, rank) DO UPDATE SET
                    match_name=excluded.match_name,
                    player1=excluded.player1,
                    player2=excluded.player2,
                    fav_player=excluded.fav_player,
                    p_model_fav=excluded.p_model_fav,
                    odd_fav=excluded.odd_fav,
                    ev_fav_pct=excluded.ev_fav_pct,
                    data_reliability_score=excluded.data_reliability_score,
                    tournament=excluded.tournament,
                    surface=excluded.surface,
                    snapshot_built_at=excluded.snapshot_built_at,
                    updated_at=excluded.updated_at
                """,
                (
                    strategy_key,
                    cal_day,
                    int(pick.get("rank") or 0),
                    str(pick.get("match_name") or ""),
                    pick.get("player1"),
                    pick.get("player2"),
                    pick.get("fav_player"),
                    float(pick.get("p_model_fav") or 0.0),
                    float(pick.get("odd_fav") or 0.0),
                    float(pick.get("ev_fav_pct") or 0.0),
                    int(float(pick.get("data_reliability_score") or 0)),
                    pick.get("tournament"),
                    pick.get("surface"),
                    built_at,
                    now_iso,
                    now_iso,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "strategy_key": strategy_key,
        "calendar_date": cal_day,
        "n_picks": len(picks),
        "snapshot_built_at": built_at,
    }


def sync_shadow_results(*, db_path: str = DB_PATH_DEFAULT, strategy_key: str = STRATEGY_KEY) -> int:
    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_shadow_schema(conn)
        ensure_match_results_cache(conn)
        rows = conn.execute(
            f"""
            SELECT id, calendar_date, player1, player2, fav_player, odd_fav, p_model_fav
            FROM {TABLE}
            WHERE strategy_key = ?
              AND COALESCE(status, 'En cours') = 'En cours'
            """,
            (strategy_key,),
        ).fetchall()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n = 0
        for r in rows:
            p1 = canonical_player(r["player1"])
            p2 = canonical_player(r["player2"])
            hit = _lookup_match_result_for_players(
                conn,
                str(r["calendar_date"] or "")[:10],
                p1,
                p2,
                calendar_date=str(r["calendar_date"] or "")[:10],
            )
            if not hit:
                continue
            winner, score, walkover, retired, source, _ = hit
            status, fav_won = _resolve_bet_status_from_match_result(
                str(r["fav_player"] or ""),
                winner,
                walkover=bool(walkover),
                retired=bool(retired),
            )
            if status == "En cours":
                continue
            p = float(r["p_model_fav"] or 0.0)
            o = float(r["odd_fav"] or 0.0)
            stake_frac = _algo_kelly_stake_frac(p, o, None)
            profit = 0.0 if status == "Annulé" else _algo_profit_for_status(status, o, stake_frac)
            conn.execute(
                f"""
                UPDATE {TABLE}
                SET status = ?, fav_won = ?, winner_resolved = ?, score_final = ?, result_source = ?,
                    theoretical_stake_frac = ?, theoretical_profit = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    fav_won,
                    winner,
                    score,
                    source,
                    stake_frac,
                    profit,
                    now_iso,
                    int(r["id"]),
                ),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def report_shadow(*, db_path: str = DB_PATH_DEFAULT, strategy_key: str = STRATEGY_KEY) -> dict[str, Any]:
    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_shadow_schema(conn)
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS n_total,
                SUM(CASE WHEN status IN ('Gagné','Perdu','Annulé') THEN 1 ELSE 0 END) AS n_settled,
                SUM(CASE WHEN status='Gagné' THEN 1 ELSE 0 END) AS n_wins,
                SUM(CASE WHEN status='Perdu' THEN 1 ELSE 0 END) AS n_losses,
                SUM(CASE WHEN status='Annulé' THEN 1 ELSE 0 END) AS n_void,
                SUM(COALESCE(theoretical_profit,0)) AS kelly_profit
            FROM {TABLE}
            WHERE strategy_key = ?
            """,
            (strategy_key,),
        ).fetchone()
    finally:
        conn.close()

    n_settled = int(row["n_settled"] or 0)
    n_wins = int(row["n_wins"] or 0)
    hit = (n_wins / n_settled * 100.0) if n_settled else 0.0
    return {
        "strategy_key": strategy_key,
        "n_total": int(row["n_total"] or 0),
        "n_settled": n_settled,
        "hit_pct": hit,
        "kelly_profit": float(row["kelly_profit"] or 0.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow test capture/sync/report for candidate Top5.")
    ap.add_argument("--capture", action="store_true", help="Capture today's shadow picks")
    ap.add_argument("--sync-results", action="store_true", help="Sync settled results from match_results")
    ap.add_argument("--report", action="store_true", help="Print shadow performance summary")
    ap.add_argument("--strategy-key", default=STRATEGY_KEY)
    ap.add_argument("--db-path", default=DB_PATH_DEFAULT)
    args = ap.parse_args()

    acted = False
    if args.capture:
        acted = True
        out = capture_shadow_picks(db_path=args.db_path, strategy_key=args.strategy_key)
        print(out)
    if args.sync_results:
        acted = True
        n = sync_shadow_results(db_path=args.db_path, strategy_key=args.strategy_key)
        print({"synced": n, "strategy_key": args.strategy_key})
    if args.report:
        acted = True
        print(report_shadow(db_path=args.db_path, strategy_key=args.strategy_key))
    if not acted:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
