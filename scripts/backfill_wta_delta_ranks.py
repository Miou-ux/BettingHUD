#!/usr/bin/env python3
"""Backfill winner/loser ranks on delta WTA rows (post-cutoff) for ML prepare_data.

À lancer après ``refresh_wta_rankings_current.py`` pour exploiter le classement
courant frais + historique matchs + résolution nom→ID.

Usage :
  python scripts/backfill_wta_delta_ranks.py --work-dir data/raw/tennis_wta
  python scripts/backfill_wta_delta_ranks.py --work-dir data/raw/tennis_wta --db-path data/bettinghud.db
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.wta_sackmann_common import (  # noqa: E402
    DEFAULT_CUTOFF,
    build_name_to_player_id,
    build_rank_history,
    build_te_cache_rank_maps,
    build_wta_players_last_index,
    fill_ranks_if_missing,
    load_current_rankings_map,
    parse_yyyymmdd,
    _rank_val_ok,
)


def _match_csv_paths(work_dir: Path) -> list[Path]:
    return [
        p
        for p in sorted(work_dir.glob("wta_matches*.csv"))
        if "doubles" not in p.name.lower()
    ]


def _load_all_matches(work_dir: Path) -> pd.DataFrame:
    frames = []
    for p in _match_csv_paths(work_dir):
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _row_missing_rank(row: pd.Series) -> bool:
    return not _rank_val_ok(row.get("winner_rank")) or not _rank_val_ok(row.get("loser_rank"))


def backfill_ranks(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    db_path: str | None = None,
) -> dict:
    work_dir = Path(work_dir)
    paths = _match_csv_paths(work_dir)
    if not paths:
        return {"error": "aucun CSV wta_matches"}

    all_df = _load_all_matches(work_dir)
    rank_history = build_rank_history(all_df)
    name_to_id = build_name_to_player_id(all_df)
    current_rankings = load_current_rankings_map(work_dir, db_path=db_path)
    te_by_pid, te_by_name = build_te_cache_rank_maps(work_dir, name_to_id)
    wta_last_index = build_wta_players_last_index(work_dir)

    rows_touched = 0
    ranks_filled = 0
    files_written = 0
    post_cutoff = 0
    post_with_both_ranks_after = 0

    for path in paths:
        df = pd.read_csv(path, low_memory=False)
        file_changed = False
        for i, row in df.iterrows():
            td = parse_yyyymmdd(row.get("tourney_date"))
            if td is None or td < cutoff:
                continue
            if not _row_missing_rank(row):
                continue
            rec = {c: row.get(c) for c in df.columns}
            filled = fill_ranks_if_missing(
                rec,
                tourney_date=td,
                rank_history=rank_history,
                current_rankings=current_rankings,
                name_to_id=name_to_id,
                te_rank_by_pid=te_by_pid,
                te_rank_by_name=te_by_name,
                wta_players_last_index=wta_last_index,
            )
            if filled <= 0:
                continue
            ranks_filled += filled
            rows_touched += 1
            for col in rec:
                new_v = rec[col]
                old_v = row.get(col)
                if pd.isna(new_v) and pd.isna(old_v):
                    continue
                if str(new_v) == str(old_v):
                    continue
                df.at[i, col] = new_v
                file_changed = True
        if file_changed:
            df.to_csv(path, index=False)
            files_written += 1

    refreshed = _load_all_matches(work_dir)
    if not refreshed.empty:
        refreshed["_td"] = refreshed["tourney_date"].apply(parse_yyyymmdd)
        post = refreshed[refreshed["_td"] >= cutoff]
        post_cutoff = len(post)
        post_with_both_ranks_after = int(
            (
                post["winner_rank"].apply(_rank_val_ok).astype(bool)
                & post["loser_rank"].apply(_rank_val_ok).astype(bool)
            ).sum()
        )
    return {
        "files_written": files_written,
        "rows_touched": rows_touched,
        "ranks_filled": ranks_filled,
        "post_cutoff_rows": post_cutoff,
        "post_cutoff_both_ranks": post_with_both_ranks_after,
        "current_rankings_players": len(current_rankings),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill rangs delta WTA pour ML.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--db-path", default=os.path.join("data", "bettinghud.db"))
    args = ap.parse_args(argv)
    stats = backfill_ranks(
        Path(args.work_dir),
        cutoff=int(args.cutoff_date),
        db_path=args.db_path if args.db_path else None,
    )
    if stats.get("error"):
        print(f"ERREUR: {stats['error']}", file=sys.stderr)
        return 1
    print(
        f"backfill OK — rows_touched={stats['rows_touched']} "
        f"ranks_filled={stats['ranks_filled']} "
        f"post_cutoff_both_ranks={stats['post_cutoff_both_ranks']}/{stats['post_cutoff_rows']} "
        f"(rankings_map={stats['current_rankings_players']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
