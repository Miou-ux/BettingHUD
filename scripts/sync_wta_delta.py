#!/usr/bin/env python3
"""Construit le delta WTA depuis tennis-data.co.uk → format Sackmann.

Écrit uniquement dans ``--work-dir`` (preprod). Ne touche jamais au socle.

Usage :
  python scripts/sync_wta_delta.py --work-dir data/preprod/wta_work/tennis_wta
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.tennis_data_results import _refresh_if_stale  # noqa: E402
from scripts.wta_sackmann_common import (  # noqa: E402
    DEFAULT_CUTOFF,
    SACKMANN_COLUMNS,
    build_age_lookup,
    build_score,
    dedup_key,
    empty_row,
    estimate_age,
    norm_name_key,
    next_synthetic_player_id,
    parse_yyyymmdd,
    round_to_sackmann,
    surface_norm,
    tier_to_level,
    tourney_id_from,
)

_YEAR_RE = re.compile(r"(\d{4})\.csv$", re.I)


def _load_existing_keys(raw_dir: Path) -> set[tuple]:
    keys: set[tuple] = set()
    for p in sorted(raw_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            df = pd.read_csv(p, usecols=["tourney_date", "tourney_name", "winner_name", "loser_name"], low_memory=False)
        except Exception:
            continue
        for _, row in df.iterrows():
            keys.add(dedup_key(row))
    return keys


def _load_socle_matches_for_ages(raw_dir: Path) -> pd.DataFrame:
    frames = []
    for p in sorted(raw_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_tennis_data_year(year: int) -> pd.DataFrame:
    path = _refresh_if_stale("WTA", year)
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"tennis-data WTA {year} introuvable")
    sheets = pd.read_excel(path, sheet_name=None)
    frames = [f for f in sheets.values() if isinstance(f, pd.DataFrame) and len(f)]
    if not frames:
        raise RuntimeError(f"xlsx vide: {path}")
    return pd.concat(frames, ignore_index=True)


def _is_qual_itf(tier: str) -> bool:
    t = str(tier or "").upper()
    return "ITF" in t or t.startswith("Q") or "QUAL" in t


def _row_from_tennis_data(row: pd.Series, *, age_lookup: dict, player_ids: set[int], cutoff: int) -> dict | None:
    td = parse_yyyymmdd(row.get("Date"))
    if td is None or td < cutoff:
        return None
    winner = str(row.get("Winner", "") or "").strip()
    loser = str(row.get("Loser", "") or "").strip()
    if not winner or not loser:
        return None
    tournament = str(row.get("Tournament", "") or "").strip()
    location = str(row.get("Location", "") or "").strip()
    tourney_name = tournament if not location else f"{tournament} {location}"
    year = int(str(td)[:4])
    out = empty_row()
    out["tourney_id"] = tourney_id_from(tourney_name, year)
    out["tourney_name"] = tourney_name
    out["surface"] = surface_norm(row.get("Surface"))
    out["draw_size"] = pd.NA
    tier = str(row.get("Tier", "") or "")
    lvl = tier_to_level(tier)
    if lvl in {"G", "PM", "P", "I"}:
        out["tourney_level"] = lvl
    else:
        out["tourney_level"] = TennisMLModel._infer_tourney_level_from_name(tourney_name)
    out["tourney_date"] = td
    out["match_num"] = pd.NA
    wid = next_synthetic_player_id(player_ids)
    lid = next_synthetic_player_id(player_ids)
    out["winner_id"] = wid
    out["loser_id"] = lid
    out["winner_name"] = winner
    out["loser_name"] = loser
    out["winner_age"] = estimate_age(winner, td, age_lookup)
    out["loser_age"] = estimate_age(loser, td, age_lookup)
    out["score"] = build_score(row)
    bo = row.get("Best of")
    out["best_of"] = int(bo) if pd.notna(bo) else 3
    out["round"] = round_to_sackmann(row.get("Round"))
    out["minutes"] = pd.NA
    out["winner_rank"] = row.get("WRank")
    out["loser_rank"] = row.get("LRank")
    out["winner_rank_points"] = row.get("WPts")
    out["loser_rank_points"] = row.get("LPts")
    return out


def sync_delta(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    dry_run: bool = False,
) -> dict:
    work_dir = Path(work_dir)
    if not work_dir.is_dir():
        raise FileNotFoundError(work_dir)

    existing_keys = _load_existing_keys(work_dir)
    age_lookup = build_age_lookup(_load_socle_matches_for_ages(work_dir))
    player_ids: set[int] = set()
    for p in work_dir.glob("*.csv"):
        try:
            df = pd.read_csv(p, usecols=["winner_id", "loser_id"], low_memory=False)
            for col in ("winner_id", "loser_id"):
                for v in df[col].dropna():
                    try:
                        player_ids.add(int(float(v)))
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

    y0 = cutoff // 10000
    years = list(range(y0, datetime.now().year + 1))
    new_rows: list[dict] = []
    skipped_dup = 0
    for year in years:
        try:
            tdf = _load_tennis_data_year(year)
        except FileNotFoundError:
            continue
        for _, row in tdf.iterrows():
            rec = _row_from_tennis_data(row, age_lookup=age_lookup, player_ids=player_ids, cutoff=cutoff)
            if rec is None:
                continue
            k = dedup_key(rec)
            if k in existing_keys:
                skipped_dup += 1
                continue
            existing_keys.add(k)
            rec["_qual_itf"] = _is_qual_itf(str(row.get("Tier", "")))
            new_rows.append(rec)

    by_file: dict[int, list[dict]] = {}
    qual_by_file: dict[int, list[dict]] = {}
    for rec in new_rows:
        y = int(str(rec["tourney_date"])[:4])
        target = qual_by_file if rec.pop("_qual_itf", False) else by_file
        target.setdefault(y, []).append(rec)

    appended = 0
    if not dry_run:
        for bucket, pattern in ((by_file, "wta_matches_{year}.csv"), (qual_by_file, "wta_matches_qual_itf_{year}.csv")):
            for year, rows in bucket.items():
                if not rows:
                    continue
                path = work_dir / pattern.format(year=year)
                df_new = pd.DataFrame(rows)[list(SACKMANN_COLUMNS)]
                if path.is_file():
                    df_old = pd.read_csv(path, low_memory=False)
                    df_out = pd.concat([df_old, df_new], ignore_index=True)
                else:
                    df_out = df_new
                df_out.to_csv(path, index=False)
                appended += len(rows)

    return {
        "cutoff": cutoff,
        "candidates": len(new_rows),
        "appended": appended,
        "skipped_duplicates": skipped_dup,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync delta WTA tennis-data -> Sackmann work dir.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "preprod" / "wta_work" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    stats = sync_delta(Path(args.work_dir), cutoff=args.cutoff_date, dry_run=args.dry_run)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
