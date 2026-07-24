#!/usr/bin/env python3
"""Backfill daily_published_picks depuis pool matin JSONL + logs TG 1D1P.

Reconstruit ce qui aurait été publié le matin (HYB P75+P80 + filtres TG Top5)
pour chaque jour >= LIVE_CUTOFF. Les jours déjà archivés sont ignorés sauf --force.

Usage:
  python scripts/backfill_published_picks.py --from 2026-05-18 --to 2026-07-23
  python scripts/backfill_published_picks.py --dry-run
  python scripts/backfill_published_picks.py --force --date 2026-07-22
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_pack12_global_2026 import LIVE_CUTOFF
from scripts.backtest_prod_top5_2026 import _norm_pick_row
from scripts.backtest_scout_mega_grid import _daily_pools_unlimited
from scripts.bets_db import DB_PATH_DEFAULT, ensure_daily_top_proba_schema, open_db, read_daily_top_proba_picks
from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match, matchup_players_key
from scripts.discord_1d1p_core import select_1d1p_pick
from scripts.experiment_july_expert_kelly import _settlement_map
from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel
from scripts.published_picks_store import (
    MODE_1D1P,
    MODE_TOP5,
    has_published_for_date,
    mark_published_no_picks,
    save_published_picks,
)
from scripts.telegram_top5_notify import filter_telegram_display_picks
from scripts.tournament_tier import is_major_atp_wta_by_name


def _date_range(d0: str, d1: str) -> list[str]:
    start = date.fromisoformat(d0[:10])
    end = date.fromisoformat(d1[:10])
    if end < start:
        start, end = end, start
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _pool_by_day(ml: TennisMLModel) -> dict[str, list[dict]]:
    pools = _daily_pools_unlimited(2026, ml)
    return {d: [_norm_pick_row(dict(r)) for r in rows] for d, rows in pools.items()}


def _pick_by_key(db_path: str, pick_key: str, cal: str) -> dict | None:
    smap = _settlement_map(db_path)
    if pick_key in smap:
        return dict(smap[pick_key])
    for row in read_daily_top_proba_picks(db_path=db_path, calendar_date=cal):
        if str(row.get("pick_key") or "") == pick_key:
            return dict(row)
    return None


def _resolve_1d1p_from_tg_log(conn, cal: str, db_path: str) -> dict | None:
    from scripts.od1p_pick_key import resolve_od1p_pick_for_result
    from scripts.telegram_1d1p_post_log import ensure_telegram_1d1p_schema

    ensure_telegram_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT pick_key, message_preview FROM telegram_1d1p_posts
        WHERE post_type = 'daily_pick' AND calendar_date = ?
        LIMIT 1
        """,
        (cal,),
    ).fetchone()
    if not row:
        return None
    pk, preview = str(row[0] or ""), row[1]
    if pk and "|1D1P" not in pk:
        found = _pick_by_key(db_path, pk, cal)
        if found:
            return found
    resolved = resolve_od1p_pick_for_result(conn, calendar_date=cal, message_preview=preview)
    return dict(resolved) if resolved else None


def _select_top5_for_day(pool: list[dict], *, limit: int | None) -> list[dict]:
    dup = duplicate_model_prob_keys(pool)
    picks = select_hyb_p75_p80_all(pool, duplicate_keys=dup, limit=limit)
    return filter_telegram_display_picks(picks)


def _select_1d1p_for_day(
    pool: list[dict],
    *,
    used_matchups: set[str],
) -> dict | None:
    day_rows = dedupe_top_proba_rows_by_match(pool)

    def _row_ok(row: dict) -> bool:
        if not is_major_atp_wta_by_name(str(row.get("tour") or ""), str(row.get("tournament") or "")):
            return False
        mk = matchup_players_key(row)
        return not mk or mk not in used_matchups

    pick = select_1d1p_pick(day_rows, row_ok=_row_ok)
    return dict(pick) if pick else None


def backfill(
    *,
    db_path: str,
    date_from: str,
    date_to: str,
    dry_run: bool = False,
    force: bool = False,
    top5_limit: int | None = None,
) -> dict:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    pools = _pool_by_day(ml)
    days = [d for d in _date_range(date_from, date_to) if d >= LIVE_CUTOFF and d in pools]

    conn = open_db(db_path)
    ensure_daily_top_proba_schema(conn)
    stats = {
        "days": len(days),
        "top5_saved": 0,
        "top5_skipped": 0,
        "top5_empty": 0,
        "d1p_saved": 0,
        "d1p_skipped": 0,
        "d1p_empty": 0,
        "d1p_from_tg_log": 0,
    }
    used_matchups: set[str] = set()

    try:
        for cal in days:
            pool = pools[cal]

            # --- Top5 ---
            if not force and has_published_for_date(conn, cal, MODE_TOP5):
                stats["top5_skipped"] += 1
            else:
                top5 = _select_top5_for_day(pool, limit=top5_limit)
                if not top5:
                    stats["top5_empty"] += 1
                    if not dry_run:
                        mark_published_no_picks(
                            conn,
                            mode=MODE_TOP5,
                            calendar_date=cal,
                            source="backfill_morning_pool",
                        )
                        print(f"top5 {cal}: (no pick)")
                elif dry_run:
                    print(f"[dry-run] {cal} top5 ({len(top5)}):", [p.get("fav_player") for p in top5])
                else:
                    n = save_published_picks(
                        conn,
                        mode=MODE_TOP5,
                        calendar_date=cal,
                        picks=top5,
                        source="backfill_morning_pool",
                    )
                    stats["top5_saved"] += int(n > 0)
                    print(f"top5 {cal}: {n} picks", [p.get("fav_player") for p in top5])

            # --- 1D1P ---
            if not force and has_published_for_date(conn, cal, MODE_1D1P):
                stats["d1p_skipped"] += 1
            else:
                pick = _resolve_1d1p_from_tg_log(conn, cal, db_path)
                if pick:
                    stats["d1p_from_tg_log"] += 1
                else:
                    pick = _select_1d1p_for_day(pool, used_matchups=used_matchups)
                if not pick:
                    stats["d1p_empty"] += 1
                    if not dry_run:
                        mark_published_no_picks(
                            conn,
                            mode=MODE_1D1P,
                            calendar_date=cal,
                            source="backfill_morning_pool",
                        )
                        print(f"1d1p {cal}: (no pick)")
                elif dry_run:
                    print(f"[dry-run] {cal} 1d1p:", pick.get("fav_player"))
                else:
                    n = save_published_picks(
                        conn,
                        mode=MODE_1D1P,
                        calendar_date=cal,
                        picks=[pick],
                        source="backfill_morning_pool",
                    )
                    stats["d1p_saved"] += int(n > 0)
                    print(f"1d1p {cal}: {pick.get('fav_player')}")
                if pick:
                    mk = matchup_players_key(pick)
                    if mk:
                        used_matchups.add(mk)
    finally:
        conn.close()

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill daily_published_picks (pool matin JSONL)")
    ap.add_argument("--db", default=DB_PATH_DEFAULT)
    ap.add_argument("--from", dest="date_from", default=LIVE_CUTOFF)
    ap.add_argument("--to", dest="date_to", default=date.today().isoformat())
    ap.add_argument("--date", default="", help="Un seul jour (écrase from/to)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Écrase publications existantes")
    ap.add_argument(
        "--top5-limit",
        type=int,
        default=None,
        help="Cap picks (defaut: union HYB complete, illimite)",
    )
    args = ap.parse_args()

    d0 = str(args.date or args.date_from)[:10]
    d1 = str(args.date or args.date_to)[:10]

    stats = backfill(
        db_path=args.db,
        date_from=d0,
        date_to=d1,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        top5_limit=None if args.top5_limit is None or int(args.top5_limit) <= 0 else int(args.top5_limit),
    )
    print("\n=== backfill summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
