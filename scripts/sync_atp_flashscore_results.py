#!/usr/bin/env python3
"""Append recent ATP singles results from Flashscore (bridge TennisMyLife lag).

TML yearly CSV often trails ~1 week on grass swingers (Eastbourne, Mallorca).
Finished ATP matches are discovered in Flashscore ``f_2_*`` feeds (same as WTA)
and appended to ``matches_recent`` with ``source='flashscore'``.

Run after ``sync_tml_recent.py`` (TML rebuild drops the table each run).

Usage:
  python scripts/sync_atp_flashscore_results.py
  python scripts/sync_atp_flashscore_results.py --dry-run
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.surface_speed import effective_surface_speed_cpi, infer_outdoor, lookup_surface_speed  # noqa: E402
from scripts.wta_flashscore_serve_stats import (  # noqa: E402
    FlashscoreIndex,
    fetch_match_service_stats,
    map_service_to_sackmann,
)
from scripts.wta_sackmann_common import (  # noqa: E402
    DEFAULT_CUTOFF,
    build_age_lookup,
    build_name_to_player_id,
    estimate_age,
    norm_name_key,
    resolve_player_id,
    surface_norm,
    tourney_id_from,
)

_SEED_RE = re.compile(r"\s*\(\d+\)\s*$")
_SKIP_TOURNEY = re.compile(r"utr|itf|futures", re.I)
DB_DEFAULT = ROOT / "data" / "bettinghud.db"


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
    if ag < 0 or ah < 0 or ag == ah:
        return False
    mx = max(ag, ah)
    return mx >= 2 and (ag + ah) <= 3


def _score_from_game(game: dict, *, home_won: bool) -> str:
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
    return " ".join(sets) if sets else ""


def _dedup_key(row: dict) -> tuple:
    td = str(row.get("tourney_date", "")).replace("-", "")[:8]
    return (
        int(td) if td.isdigit() else 0,
        str(row.get("tourney_name", "") or "").strip(),
        str(row.get("winner_name", "") or "").strip(),
        str(row.get("loser_name", "") or "").strip(),
    )


def _tml_level(tier_raw: object, tourney_name: str) -> str:
    try:
        pts = float(str(tier_raw or "").replace(",", "."))
        if pts >= 900:
            return "G"
        if pts >= 450:
            return "500"
        if pts >= 200:
            return "250"
        if pts >= 90:
            return "125"
    except (TypeError, ValueError):
        pass
    n = str(tourney_name or "").lower()
    if any(k in n for k in ("wimbledon", "roland", "australian open", "us open")):
        return "G"
    if any(k in n for k in ("masters", "miami", "indian wells", "monte carlo", "madrid", "rome", "cincinnati", "shanghai", "paris")):
        return "M"
    return "250"


def _infer_surface(tourney_name: str) -> str:
    n = str(tourney_name or "").lower()
    if any(k in n for k in ("wimbledon", "eastbourne", "mallorca", "halle", "queen", "nottingham", "stuttgart")):
        return "Grass"
    if any(k in n for k in ("roland", "barcelona", "rome", "madrid", "monte carlo")):
        return "Clay"
    return "Hard"


def _load_socle(conn: sqlite3.Connection) -> pd.DataFrame:
    try:
        return pd.read_sql("SELECT * FROM matches_recent", conn)
    except Exception:
        return pd.DataFrame()


def _load_existing_keys(conn: sqlite3.Connection) -> set[tuple]:
    keys: set[tuple] = set()
    rows = conn.execute(
        "SELECT tourney_date, tourney_name, winner_name, loser_name FROM matches_recent"
    ).fetchall()
    for td, tn, w, l in rows:
        keys.add(
            (
                int(str(td).replace("-", "")[:8]),
                str(tn or "").strip(),
                str(w or "").strip(),
                str(l or "").strip(),
            )
        )
    return keys


def _build_id_to_name(socle: pd.DataFrame) -> dict[int, str]:
    out: dict[int, str] = {}
    if socle.empty:
        return out
    for _, row in socle.iterrows():
        for ncol, icol in (("winner_name", "winner_id"), ("loser_name", "loser_id")):
            try:
                pid = int(float(row.get(icol)))
            except (TypeError, ValueError):
                continue
            name = str(row.get(ncol) or "").strip()
            if name:
                out[pid] = name
    return out


def _display_name(te_name: str, pid: int, id_to_name: dict[int, str]) -> str:
    if pid in id_to_name:
        return id_to_name[pid]
    return te_name


def _load_prematch_context(*, lookback_files: int = 80) -> dict[tuple[frozenset[str], int], dict]:
    scraped = ROOT / "data" / "scraped"
    paths = sorted(glob.glob(str(scraped / "prematch_odds_*.csv")))[-lookback_files:]
    out: dict[tuple[frozenset[str], int], dict] = {}
    for path in paths:
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if df.empty or "category" not in df.columns:
            continue
        atp = df[df["category"].astype(str).str.upper() == "ATP"]
        for _, row in atp.iterrows():
            if "double" in str(row.get("tournament_url", "")).lower():
                continue
            tname = str(row.get("tournament") or "").strip()
            if not tname or _SKIP_TOURNEY.search(tname):
                continue
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
            tier_raw = row.get("tourney_winner_points")
            surf = _infer_surface(tname)
            meta = {
                "tourney_name": tname,
                "surface": surf,
                "tier_raw": tier_raw,
            }
            out[(pair, td)] = meta
            for delta in (-1, 1):
                out.setdefault((pair, td + delta), meta)
    return out


def _prematch_meta(
    ctx: dict[tuple[frozenset[str], int], dict],
    winner: str,
    loser: str,
    tourney_date: int,
) -> dict | None:
    pair = frozenset({norm_name_key(winner), norm_name_key(loser)})
    return ctx.get((pair, tourney_date))


def _empty_atp_row() -> dict:
    return {
        "tourney_id": None,
        "tourney_name": None,
        "surface": None,
        "draw_size": None,
        "tourney_level": None,
        "indoor": None,
        "tourney_date": None,
        "match_num": 0,
        "winner_id": None,
        "winner_seed": None,
        "winner_entry": None,
        "winner_name": None,
        "winner_hand": None,
        "winner_ht": None,
        "winner_ioc": None,
        "winner_age": None,
        "winner_rank": None,
        "winner_rank_points": None,
        "loser_id": None,
        "loser_seed": None,
        "loser_entry": None,
        "loser_name": None,
        "loser_hand": None,
        "loser_ht": None,
        "loser_ioc": None,
        "loser_age": None,
        "loser_rank": None,
        "loser_rank_points": None,
        "score": None,
        "best_of": 3,
        "round": "R32",
        "minutes": None,
        "w_ace": None,
        "w_df": None,
        "w_svpt": None,
        "w_1stIn": None,
        "w_1stWon": None,
        "w_2ndWon": None,
        "w_SvGms": None,
        "w_bpSaved": None,
        "w_bpFaced": None,
        "l_ace": None,
        "l_df": None,
        "l_svpt": None,
        "l_1stIn": None,
        "l_1stWon": None,
        "l_2ndWon": None,
        "l_SvGms": None,
        "l_bpSaved": None,
        "l_bpFaced": None,
        "source": "flashscore",
        "source_updated_at": None,
        "surface_speed": None,
    }


def _row_from_game(
    game: dict,
    *,
    ctx: dict,
    age_lookup: dict,
    name_to_id: dict[str, int],
    id_to_name: dict[int, str],
    player_ids: set[int],
    cutoff: int,
    fetch_stats: bool,
    session,
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
    winner_te, loser_te = (home, away) if home_won else (away, home)
    meta = _prematch_meta(ctx, winner_te, loser_te, td)
    if meta is None:
        return None

    tourney_name = str(meta["tourney_name"])
    year = int(str(td)[:4])
    wid = resolve_player_id(winner_te, name_to_id, player_ids)
    lid = resolve_player_id(loser_te, name_to_id, player_ids)
    winner = _display_name(winner_te, wid, id_to_name)
    loser = _display_name(loser_te, lid, id_to_name)

    surf = surface_norm(meta.get("surface") or _infer_surface(tourney_name))
    outdoor = infer_outdoor(None, tourney_name)
    base_spd = float(lookup_surface_speed(tourney_name, surf))
    eff_spd = float(effective_surface_speed_cpi(base_spd, outdoor, None, None))

    out = _empty_atp_row()
    out["tourney_id"] = tourney_id_from(tourney_name, year)
    out["tourney_name"] = tourney_name
    out["surface"] = surf
    out["tourney_level"] = _tml_level(meta.get("tier_raw"), tourney_name)
    out["indoor"] = "O" if outdoor else "I"
    out["tourney_date"] = str(td)
    out["winner_id"] = wid
    out["loser_id"] = lid
    out["winner_name"] = winner
    out["loser_name"] = loser
    out["winner_age"] = estimate_age(winner, td, age_lookup)
    out["loser_age"] = estimate_age(loser, td, age_lookup)
    out["score"] = _score_from_game(game, home_won=home_won)
    out["surface_speed"] = eff_spd
    out["source_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

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


def sync_atp_flashscore_results(
    db_path: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    dry_run: bool = False,
    fetch_stats: bool = True,
) -> dict:
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    socle = _load_socle(conn)
    existing = _load_existing_keys(conn)
    player_ids: set[int] = set()
    if not socle.empty:
        for col in ("winner_id", "loser_id"):
            for v in socle[col].dropna():
                try:
                    player_ids.add(int(float(v)))
                except (TypeError, ValueError):
                    pass
    age_lookup = build_age_lookup(socle)
    name_to_id = build_name_to_player_id(socle)
    id_to_name = _build_id_to_name(socle)
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
            id_to_name=id_to_name,
            player_ids=player_ids,
            cutoff=cutoff,
            fetch_stats=fetch_stats and not dry_run,
            session=session,
        )
        if row is None:
            if _finished_bo3(game) and _game_date_int(game) and _game_date_int(game) >= cutoff:
                home = str(game.get("AE") or "")
                away = str(game.get("AF") or "")
                if home and away and "/" not in home and "/" not in away:
                    skipped_no_ctx += 1
            continue
        k = _dedup_key(row)
        if k in existing:
            skipped_dup += 1
            continue
        existing.add(k)
        candidates.append(row)

    appended = 0
    if not dry_run and candidates:
        table_cols = [r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()]
        df_new = pd.DataFrame(candidates)
        for c in table_cols:
            if c not in df_new.columns:
                df_new[c] = pd.NA
        df_new = df_new[table_cols]
        df_new.to_sql("matches_recent", conn, if_exists="append", index=False)
        conn.commit()
        appended = len(candidates)

    conn.close()
    return {
        "cutoff": cutoff,
        "candidates": len(candidates),
        "appended": appended,
        "skipped_duplicates": skipped_dup,
        "skipped_no_prematch_context": skipped_no_ctx,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync recent ATP results from Flashscore into matches_recent.")
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fetch-stats", action="store_true")
    args = ap.parse_args(argv)
    stats = sync_atp_flashscore_results(
        Path(args.db),
        cutoff=args.cutoff_date,
        dry_run=args.dry_run,
        fetch_stats=not args.no_fetch_stats,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
