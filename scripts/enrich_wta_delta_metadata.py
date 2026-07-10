#!/usr/bin/env python3
"""Enrichit le delta WTA : IDs Sackmann, âges, hand/ht/ioc, dédup optionnelle.

Complète ``sync_wta_delta.py`` (qui ne créait que des IDs synthétiques 920000+).
Génère aussi ``wta_players.csv`` dérivé de l'historique des matchs.

Usage :
  python scripts/enrich_wta_delta_metadata.py --work-dir data/preprod/wta_work/tennis_wta
  python scripts/enrich_wta_delta_metadata.py --work-dir data/raw/tennis_wta --dedup
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
    SACKMANN_COLUMNS,
    apply_player_enrichment,
    build_age_lookup,
    build_player_profile_lookup,
    build_rank_history,
    build_wta_players_table,
    dedup_key,
    fill_ranks_if_missing,
    parse_yyyymmdd,
    row_completeness_score,
)


def _qual_match_paths(work_dir: Path) -> list[Path]:
    return [
        p
        for p in sorted(work_dir.glob("wta_matches_qual_itf_*.csv"))
        if "doubles" not in p.name.lower()
    ]


def _load_current_rankings(work_dir: Path) -> dict[int, tuple[float, float]]:
    """player_id -> (rank, points) depuis wta_rankings_current.csv si présent."""
    path = work_dir / "wta_rankings_current.csv"
    if not path.is_file():
        return {}
    try:
        rk = pd.read_csv(path, low_memory=False)
    except Exception:
        return {}
    id_col = "player_id" if "player_id" in rk.columns else ("player" if "player" in rk.columns else None)
    rank_col = "rank" if "rank" in rk.columns else ("ranking" if "ranking" in rk.columns else None)
    pts_col = "points" if "points" in rk.columns else None
    if not id_col or not rank_col:
        return {}
    out: dict[int, tuple[float, float]] = {}
    for _, row in rk.iterrows():
        try:
            pid = int(float(row[id_col]))
            rank = float(row[rank_col])
        except (TypeError, ValueError):
            continue
        pts = float("nan")
        if pts_col and pd.notna(row.get(pts_col)):
            try:
                pts = float(row[pts_col])
            except (TypeError, ValueError):
                pass
        out[pid] = (rank, pts)
    return out


def reorganize_work_dir_by_year(work_dir: Path) -> dict:
    """Dédup globale + réécriture des CSV par année (corrige C1/C7)."""

    def _repack(paths: list[Path], filename_fmt: str) -> dict:
        if not paths:
            return {"dupes_removed": 0, "files_written": 0, "rows": 0}
        frames = []
        for p in paths:
            try:
                frames.append(pd.read_csv(p, low_memory=False))
            except Exception:
                continue
        if not frames:
            return {"dupes_removed": 0, "files_written": 0, "rows": 0}
        df = pd.concat(frames, ignore_index=True)
        n0 = len(df)
        df["_dedup_key"] = df.apply(dedup_key, axis=1)
        df["_score"] = df.apply(row_completeness_score, axis=1)
        df = (
            df.sort_values("_score", ascending=False)
            .drop_duplicates(subset="_dedup_key", keep="first")
            .drop(columns=["_dedup_key", "_score"])
        )
        dupes_removed = n0 - len(df)
        years = pd.to_datetime(df["tourney_date"].astype(str), format="%Y%m%d", errors="coerce").dt.year
        df["_year"] = years
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass
        files_written = 0
        for yr, grp in df.groupby("_year", dropna=True):
            yr_i = int(yr)
            out_path = work_dir / filename_fmt.format(year=yr_i)
            grp.drop(columns=["_year"]).to_csv(out_path, index=False)
            files_written += 1
        return {"dupes_removed": dupes_removed, "files_written": files_written, "rows": len(df)}

    main_stats = _repack(_match_csv_paths(work_dir), "wta_matches_{year}.csv")
    qual_stats = _repack(_qual_match_paths(work_dir), "wta_matches_qual_itf_{year}.csv")
    return {"main": main_stats, "qual_itf": qual_stats}


def _match_csv_paths(work_dir: Path) -> list[Path]:
    return [
        p
        for p in sorted(work_dir.glob("wta_matches_*.csv"))
        if "qual_itf" not in p.name.lower() and "doubles" not in p.name.lower()
    ]


def _load_all_matches(work_dir: Path) -> pd.DataFrame:
    frames = []
    for p in _match_csv_paths(work_dir):
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=list(SACKMANN_COLUMNS))
    return pd.concat(frames, ignore_index=True)


def _needs_enrichment_row(row: pd.Series, cutoff: int) -> bool:
    td = parse_yyyymmdd(row.get("tourney_date"))
    if td is not None and td >= cutoff:
        return True
    for col in ("winner_id", "loser_id"):
        try:
            if int(float(row.get(col) or 0)) >= 900000:
                return True
        except (TypeError, ValueError):
            pass
    return False


def enrich_metadata(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    dedup: bool = False,
    write_players: bool = True,
) -> dict:
    work_dir = Path(work_dir)
    paths = _match_csv_paths(work_dir)
    if not paths:
        return {"error": "aucun CSV wta_matches"}

    all_df = _load_all_matches(work_dir)
    age_lookup = build_age_lookup(all_df)
    name_to_id, profiles = build_player_profile_lookup(all_df)
    player_ids: set[int] = set(name_to_id.values()) | set(profiles.keys())
    for col in ("winner_id", "loser_id"):
        if col not in all_df.columns:
            continue
        for v in all_df[col].dropna():
            try:
                player_ids.add(int(float(v)))
            except (TypeError, ValueError):
                pass

    rank_history = build_rank_history(all_df)
    current_rankings = _load_current_rankings(work_dir)
    ranks_filled = 0

    changed_cells = 0
    remapped_ids = 0
    files_written = 0
    rows_touched = 0

    for path in paths:
        df = pd.read_csv(path, low_memory=False)
        file_changed = False
        touch_idx = [i for i, row in df.iterrows() if _needs_enrichment_row(row, cutoff)]
        rows_touched += len(touch_idx)

        for i in touch_idx:
            row = df.loc[i]
            td = parse_yyyymmdd(row.get("tourney_date"))
            if td is None:
                continue
            rec = {c: row.get(c) for c in df.columns}
            before_w = rec.get("winner_id")
            before_l = rec.get("loser_id")
            apply_player_enrichment(
                rec,
                tourney_date=td,
                name_to_id=name_to_id,
                profiles=profiles,
                age_lookup=age_lookup,
                player_ids=player_ids,
            )
            ranks_filled += fill_ranks_if_missing(
                rec,
                tourney_date=td,
                rank_history=rank_history,
                current_rankings=current_rankings,
            )
            for col in SACKMANN_COLUMNS:
                if col not in rec:
                    continue
                new_v = rec[col]
                old_v = row.get(col)
                if pd.isna(new_v) and pd.isna(old_v):
                    continue
                if str(new_v) == str(old_v):
                    continue
                if col in ("winner_id", "loser_id") and new_v is not None:
                    try:
                        new_v = int(float(new_v))
                    except (TypeError, ValueError):
                        pass
                df.at[i, col] = new_v
                changed_cells += 1
                file_changed = True
            try:
                if int(float(before_w or 0)) >= 900000 and int(float(rec["winner_id"])) < 900000:
                    remapped_ids += 1
                if int(float(before_l or 0)) >= 900000 and int(float(rec["loser_id"])) < 900000:
                    remapped_ids += 1
            except (TypeError, ValueError):
                pass

        if file_changed:
            df.to_csv(path, index=False)
            files_written += 1

    reorganize_stats = None
    if dedup:
        reorganize_stats = reorganize_work_dir_by_year(work_dir)
        paths = _match_csv_paths(work_dir)

    players_path = work_dir / "wta_players.csv"
    players_count = len(profiles)
    if write_players:
        refreshed = _load_all_matches(work_dir)
        _, profiles = build_player_profile_lookup(refreshed)
        players_count = len(profiles)
        build_wta_players_table(profiles).to_csv(players_path, index=False)

    delta_n = 0
    syn_remaining = 0
    for path in paths:
        df = pd.read_csv(path, low_memory=False)
        df["_td"] = df["tourney_date"].apply(parse_yyyymmdd)
        d = df[df["_td"] >= cutoff]
        delta_n += len(d)
        if len(d):
            syn_remaining += int((d["winner_id"].astype(float) >= 900000).sum())

    return {
        "files_written": files_written,
        "rows_touched": rows_touched,
        "changed_cells": changed_cells,
        "ranks_filled": ranks_filled,
        "remapped_id_events": remapped_ids,
        "delta_rows": delta_n,
        "synthetic_winner_ids_remaining": syn_remaining,
        "reorganize": reorganize_stats,
        "players_csv": str(players_path) if write_players else None,
        "players_count": players_count,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enrichir métadonnées joueuses WTA (IDs, âges, profil).")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "preprod" / "wta_work" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--dedup", action="store_true", help="Supprimer doublons dédup (garde ligne la plus complète).")
    ap.add_argument("--no-players-csv", action="store_true")
    args = ap.parse_args(argv)
    stats = enrich_metadata(
        Path(args.work_dir),
        cutoff=args.cutoff_date,
        dedup=args.dedup,
        write_players=not args.no_players_csv,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
