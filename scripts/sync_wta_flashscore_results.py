#!/usr/bin/env python3
"""Append recent WTA singles results from Flashscore (bridge tennis-data lag).

tennis-data.co.uk WTA xlsx often trails ~1 week on grass swingers (e.g. Bad Homburg).
This script discovers finished WTA matches in Flashscore feeds and appends missing rows
to the Sackmann work-dir CSVs (same dedup keys as sync_wta_delta.py).

Tournament/surface context comes from recent prematch CSVs (TE scrape).

Usage:
  python scripts/sync_wta_flashscore_results.py --work-dir data/raw/tennis_wta
  python scripts/sync_wta_flashscore_results.py --dry-run
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.wta_flashscore_serve_stats import FlashscoreIndex, map_service_to_sackmann, fetch_match_service_stats  # noqa: E402
from scripts.wta_sackmann_common import (  # noqa: E402
    DEFAULT_CUTOFF,
    SACKMANN_COLUMNS,
    build_age_lookup,
    build_rank_history,
    build_te_cache_rank_maps,
    dedup_key,
    empty_row,
    estimate_age,
    fill_ranks_if_missing,
    is_qual_itf_route,
    load_current_rankings_map,
    next_synthetic_player_id,
    norm_name_key,
    surface_norm,
    tier_to_level,
    build_name_to_player_id,
    resolve_player_id,
    tourney_id_from,
)
from scripts.surface_speed import infer_surface_category, resolve_tournament_surface  # noqa: E402

_SEED_RE = re.compile(r"\s*\(\d+\)\s*$")


def _clean_player_name(name: str) -> str:
    return _SEED_RE.sub("", str(name or "").strip())


def _game_date_int(game: dict) -> int | None:
    try:
        ts = int(game.get("AD") or 0)
        if ts <= 0:
            return None
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return int(d.strftime("%Y%m%d"))
    except (TypeError, ValueError, OSError):
        return None


def _finished_bo3(game: dict) -> bool:
    try:
        ag = int(game.get("AG") or -1)
        ah = int(game.get("AH") or -1)
    except (TypeError, ValueError):
        return False
    if ag < 0 or ah < 0:
        return False
    if ag == ah:
        return False
    mx = max(ag, ah)
    return mx >= 2 and (ag + ah) <= 3


def _score_from_game(game: dict, *, home_won: bool) -> str:
    """Build score from winner perspective using BA/BB/BC/BD/BE/BF (home/away games per set)."""
    pairs = [("BA", "BB"), ("BC", "BD"), ("BE", "BF")]
    sets: list[str] = []
    for hk, ak in pairs:
        if hk not in game or ak not in game:
            continue
        try:
            h, a = int(game[hk]), int(game[ak])
        except (TypeError, ValueError):
            continue
        if home_won:
            sets.append(f"{h}-{a}")
        else:
            sets.append(f"{a}-{h}")
    return " ".join(sets) if sets else pd.NA


def _load_existing_keys(raw_dir: Path) -> set[tuple]:
    keys: set[tuple] = set()
    for p in sorted(raw_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            df = pd.read_csv(
                p,
                usecols=["tourney_date", "tourney_name", "winner_name", "loser_name"],
                low_memory=False,
            )
        except Exception:
            continue
        for _, row in df.iterrows():
            keys.add(dedup_key(row))
    return keys


def _load_player_ids(raw_dir: Path) -> set[int]:
    ids: set[int] = set()
    for p in raw_dir.glob("wta_matches*.csv"):
        try:
            df = pd.read_csv(p, usecols=["winner_id", "loser_id"], low_memory=False)
            for col in ("winner_id", "loser_id"):
                for v in df[col].dropna():
                    try:
                        ids.add(int(float(v)))
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    return ids


def _load_socle_matches(raw_dir: Path) -> pd.DataFrame:
    frames = []
    for p in sorted(raw_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _load_prematch_context(*, lookback_days: int = 21) -> dict[tuple[frozenset[str], int], dict]:
    """Map (player pair, yyyymmdd) -> tournament meta from prematch CSVs."""
    scraped = ROOT / "data" / "scraped"
    paths = sorted(glob.glob(str(scraped / "prematch_odds_*.csv")))[-80:]
    out: dict[tuple[frozenset[str], int], dict] = {}
    for path in paths:
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if df.empty or "category" not in df.columns:
            continue
        cats = df["category"].astype(str).str.upper()
        subset = df[cats.isin(["WTA", "ITF"])]
        for _, row in subset.iterrows():
            p1 = _clean_player_name(row.get("player1"))
            p2 = _clean_player_name(row.get("player2"))
            if not p1 or not p2 or "/" in p1 or "/" in p2:
                continue
            try:
                td = int(str(row.get("date") or "").replace("-", "")[:8])
            except ValueError:
                continue
            if td <= 0:
                continue
            pair = frozenset({norm_name_key(p1), norm_name_key(p2)})
            tname = str(row.get("tournament") or "").strip()
            if not tname:
                continue
            tier_raw = str(row.get("tourney_winner_points") or "")
            if str(row.get("category") or "").upper() == "ITF" and not tier_raw:
                tier_raw = "ITF"
            surf = resolve_tournament_surface(tname)
            out[(pair, td)] = {
                "tourney_name": tname,
                "surface": surf,
                "tier_raw": tier_raw,
                "category": str(row.get("category") or "WTA"),
            }
            # Also index ±1 day (timezone / FS date skew)
            for delta in (-1, 1):
                out.setdefault(
                    (pair, td + delta),
                    out[(pair, td)],
                )
    return out


def _prematch_meta(
    ctx: dict[tuple[frozenset[str], int], dict],
    winner: str,
    loser: str,
    tourney_date: int,
) -> dict | None:
    pair = frozenset({norm_name_key(winner), norm_name_key(loser)})
    return ctx.get((pair, tourney_date))


def _row_from_game(
    game: dict,
    *,
    ctx: dict,
    age_lookup: dict,
    name_to_id: dict[str, int],
    player_ids: set[int],
    cutoff: int,
    fetch_stats: bool,
    session,
    rank_history: dict,
    current_rankings: dict,
    te_by_pid: dict,
    te_by_name: dict,
) -> dict | None:
    if not _finished_bo3(game):
        return None
    home = _clean_player_name(str(game.get("AE") or ""))
    away = _clean_player_name(str(game.get("AF") or ""))
    if not home or not away or "/" in home or "/" in away:
        return None
    td = _game_date_int(game)
    if td is None or td < cutoff:
        return None
    try:
        ag, ah = int(game["AG"]), int(game["AH"])
    except (TypeError, ValueError, KeyError):
        return None
    home_won = ag > ah
    winner, loser = (home, away) if home_won else (away, home)
    meta = _prematch_meta(ctx, winner, loser, td)
    if meta is None:
        return None
    tourney_name = str(meta["tourney_name"])
    year = int(str(td)[:4])
    out = empty_row()
    out["tourney_id"] = tourney_id_from(tourney_name, year)
    out["tourney_name"] = tourney_name
    out["surface"] = surface_norm(meta.get("surface") or resolve_tournament_surface(tourney_name))
    out["tourney_level"] = tier_to_level(meta.get("tier_raw") or "") or TennisMLModel._infer_tourney_level_from_name(tourney_name)
    out["tourney_date"] = td
    wid = resolve_player_id(winner, name_to_id, player_ids)
    lid = resolve_player_id(loser, name_to_id, player_ids)
    out["winner_id"] = wid
    out["loser_id"] = lid
    out["winner_name"] = winner
    out["loser_name"] = loser
    out["winner_age"] = estimate_age(winner, td, age_lookup)
    out["loser_age"] = estimate_age(loser, td, age_lookup)
    out["score"] = _score_from_game(game, home_won=home_won)
    out["best_of"] = 3
    out["round"] = "R32"

    fill_ranks_if_missing(
        out,
        tourney_date=td,
        rank_history=rank_history,
        current_rankings=current_rankings,
        name_to_id=name_to_id,
        te_rank_by_pid=te_by_pid,
        te_rank_by_name=te_by_name,
    )
    out["_qual_itf"] = is_qual_itf_route(
        meta.get("category"),
        meta.get("tier_raw"),
        tourney_name,
        out.get("tourney_level"),
    )

    if fetch_stats and game.get("AA"):
        try:
            service = fetch_match_service_stats(str(game["AA"]), session=session)
            if service:
                stats = map_service_to_sackmann(
                    service,
                    winner_name=winner,
                    loser_name=loser,
                    home_name=home,
                    away_name=away,
                )
                if stats:
                    for k, v in stats.items():
                        out[k] = v
        except Exception:
            pass
    return out


def sync_flashscore_results(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    dry_run: bool = False,
    fetch_stats: bool = True,
) -> dict:
    work_dir = Path(work_dir)
    existing = _load_existing_keys(work_dir)
    player_ids = _load_player_ids(work_dir)
    socle = _load_socle_matches(work_dir)
    age_lookup = build_age_lookup(socle)
    name_to_id = build_name_to_player_id(socle)
    rank_history = build_rank_history(socle)
    current_rankings = load_current_rankings_map(work_dir)
    te_by_pid, te_by_name = build_te_cache_rank_maps(work_dir, name_to_id)
    prematch_ctx = _load_prematch_context()

    idx = FlashscoreIndex()
    idx.warm_default(today=date.today())

    import requests

    session = requests.Session()
    candidates: list[dict] = []
    skipped_no_ctx = 0
    skipped_dup = 0
    for game in idx.games.values():
        row = _row_from_game(
            game,
            ctx=prematch_ctx,
            age_lookup=age_lookup,
            name_to_id=name_to_id,
            player_ids=player_ids,
            cutoff=cutoff,
            fetch_stats=fetch_stats and not dry_run,
            session=session,
            rank_history=rank_history,
            current_rankings=current_rankings,
            te_by_pid=te_by_pid,
            te_by_name=te_by_name,
        )
        if row is None:
            if _finished_bo3(game) and _game_date_int(game) and _game_date_int(game) >= cutoff:
                home = str(game.get("AE") or "")
                away = str(game.get("AF") or "")
                if home and away and "/" not in home and "/" not in away:
                    skipped_no_ctx += 1
            continue
        k = dedup_key(row)
        if k in existing:
            skipped_dup += 1
            continue
        existing.add(k)
        candidates.append(row)

    by_year_main: dict[int, list[dict]] = {}
    by_year_qual: dict[int, list[dict]] = {}
    for row in candidates:
        y = int(str(row["tourney_date"])[:4])
        qual = bool(row.pop("_qual_itf", False))
        bucket = by_year_qual if qual else by_year_main
        bucket.setdefault(y, []).append(row)

    appended = 0
    appended_qual = 0
    if not dry_run:

        def _append_rows(year: int, rows: list[dict], *, qual_itf: bool) -> int:
            if not rows:
                return 0
            fname = (
                f"wta_matches_qual_itf_{year}.csv"
                if qual_itf
                else f"wta_matches_{year}.csv"
            )
            path = work_dir / fname
            df_new = pd.DataFrame(rows)[list(SACKMANN_COLUMNS)]
            if path.is_file():
                df_old = pd.read_csv(path, low_memory=False)
                df_out = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_out = df_new
            df_out.to_csv(path, index=False)
            return len(rows)

        for year, rows in by_year_main.items():
            appended += _append_rows(year, rows, qual_itf=False)
        for year, rows in by_year_qual.items():
            appended_qual += _append_rows(year, rows, qual_itf=True)

    return {
        "cutoff": cutoff,
        "candidates": len(candidates),
        "appended": appended,
        "appended_qual_itf": appended_qual,
        "skipped_duplicates": skipped_dup,
        "skipped_no_prematch_context": skipped_no_ctx,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync recent WTA results from Flashscore.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fetch-stats", action="store_true")
    args = ap.parse_args(argv)
    stats = sync_flashscore_results(
        Path(args.work_dir),
        cutoff=args.cutoff_date,
        dry_run=args.dry_run,
        fetch_stats=not args.no_fetch_stats,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
