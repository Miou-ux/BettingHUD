#!/usr/bin/env python3
"""Refresh Elo maps in the trained bundle without retraining the classifier.

This script is intentionally narrower than ``TennisMLModel.prepare_data``:
- it copies existing micro-Elo service/return ratings from ID keys to name aliases;
- it rebuilds only the classic winner/loser match Elo maps.

The model feature contract is preserved unless a full retrain is run later.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time

import joblib
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402


ID_LIKE_RE = re.compile(r"^(ATP|WTA)::[A-Z0-9]{2,8}$")


def _read_history(db_path: str, min_year: int) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        atp = pd.read_sql(
            """
            SELECT winner_id, loser_id, winner_name, loser_name,
                   tourney_date, surface, tourney_level
            FROM matches_recent
            WHERE source='tennismylife'
              AND CAST(substr(tourney_date,1,4) AS INTEGER) >= ?
            """,
            conn,
            params=(min_year,),
        )
        try:
            wta = pd.read_sql(
                """
                SELECT winner_id, loser_id, winner_name, loser_name,
                       tourney_date, surface, tourney_level
                FROM wta_matches
                WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= ?
                """,
                conn,
                params=(min_year,),
            )
        except Exception:
            wta = pd.DataFrame()
    finally:
        conn.close()

    if not atp.empty:
        atp["tour"] = "ATP"
        atp["winner_id"] = "ATP::" + atp["winner_id"].astype(str).str.replace(r"^(ATP::)+", "", regex=True)
        atp["loser_id"] = "ATP::" + atp["loser_id"].astype(str).str.replace(r"^(ATP::)+", "", regex=True)
        atp["tourney_date"] = pd.to_datetime(atp["tourney_date"], format="%Y%m%d", errors="coerce")
    if not wta.empty:
        wta["tour"] = "WTA"
        wta["winner_id"] = "WTA::" + wta["winner_id"].astype(str).str.replace(r"^(WTA::)+", "", regex=True)
        wta["loser_id"] = "WTA::" + wta["loser_id"].astype(str).str.replace(r"^(WTA::)+", "", regex=True)
        wta["tourney_date"] = pd.to_datetime(wta["tourney_date"], errors="coerce")
        wta["surface"] = wta["surface"].astype(str).str.title().replace({"Nan": pd.NA})

    df = pd.concat([atp, wta], ignore_index=True, sort=False)
    df = df.dropna(subset=["winner_name", "loser_name", "surface", "tourney_date"])
    return df.sort_values("tourney_date").reset_index(drop=True)


def _name_aliases(df: pd.DataFrame, model: TennisMLModel) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for row in df.itertuples(index=False):
        tour = str(getattr(row, "tour", "ATP") or "ATP").upper()
        for id_attr, name_attr in (("winner_id", "winner_name"), ("loser_id", "loser_name")):
            pid = model._pid_key(getattr(row, id_attr, None))
            name_key = model._name_key(getattr(row, name_attr, None))
            if not pid or not name_key:
                continue
            aliases.setdefault(pid, set()).add(f"{tour}::{name_key}")
    return aliases


def _copy_micro_aliases(bundle: dict, aliases: dict[str, set[str]]) -> dict[str, int]:
    stats = {"service": 0, "return": 0, "surface_service": 0, "surface_return": 0, "surface_count": 0, "last_seen": 0}
    service = dict(bundle.get("player_service_elo") or {})
    ret = dict(bundle.get("player_return_elo") or {})
    service_sf = dict(bundle.get("player_service_elo_surface") or {})
    ret_sf = dict(bundle.get("player_return_elo_surface") or {})
    n_sf = dict(bundle.get("player_surface_micro_count") or {})
    last_seen = dict(bundle.get("player_last_seen") or {})

    for pid, name_keys in aliases.items():
        for nk in name_keys:
            if pid in service:
                service[nk] = service[pid]
                stats["service"] += 1
            if pid in ret:
                ret[nk] = ret[pid]
                stats["return"] += 1
            if pid in last_seen:
                last_seen[nk] = last_seen[pid]
                stats["last_seen"] += 1

    for (pid, surface), rating in list(service_sf.items()):
        for nk in aliases.get(pid, ()):
            service_sf[(nk, surface)] = rating
            stats["surface_service"] += 1
    for (pid, surface), rating in list(ret_sf.items()):
        for nk in aliases.get(pid, ()):
            ret_sf[(nk, surface)] = rating
            stats["surface_return"] += 1
    for (pid, surface), count in list(n_sf.items()):
        for nk in aliases.get(pid, ()):
            n_sf[(nk, surface)] = count
            stats["surface_count"] += 1

    bundle["player_service_elo"] = service
    bundle["player_return_elo"] = ret
    bundle["player_service_elo_surface"] = service_sf
    bundle["player_return_elo_surface"] = ret_sf
    bundle["player_surface_micro_count"] = n_sf
    bundle["player_last_seen"] = last_seen
    bundle["player_elo"] = {k: (service[k] + ret[k]) / 2.0 for k in service if k in ret}
    bundle["player_name_service_elo"] = {k: v for k, v in service.items() if "::" in str(k) and not ID_LIKE_RE.fullmatch(str(k))}
    bundle["player_name_return_elo"] = {k: v for k, v in ret.items() if "::" in str(k) and not ID_LIKE_RE.fullmatch(str(k))}
    bundle["player_name_elo"] = {
        k: v for k, v in bundle["player_elo"].items() if "::" in str(k) and not ID_LIKE_RE.fullmatch(str(k))
    }
    bundle["player_last_seen_name"] = {
        k: v for k, v in last_seen.items() if "::" in str(k) and not ID_LIKE_RE.fullmatch(str(k))
    }
    return stats


def main() -> int:
    min_year = int(os.getenv("BETTINGHUD_ELO_REFRESH_MIN_YEAR", "2020"))
    model = TennisMLModel()
    bundle_path = model.model_path
    t0 = time.time()
    print(f"[elo-refresh] read history >= {min_year}", flush=True)
    df = _read_history(model.db_path, min_year)
    print(f"[elo-refresh] rows={len(df)}", flush=True)

    print("[elo-refresh] load bundle", flush=True)
    bundle = joblib.load(bundle_path)
    aliases = _name_aliases(df, model)
    print(f"[elo-refresh] id aliases={len(aliases)}", flush=True)
    micro_stats = _copy_micro_aliases(bundle, aliases)
    print(f"[elo-refresh] micro alias copies={micro_stats}", flush=True)

    print("[elo-refresh] rebuild match Elo", flush=True)
    model._build_match_elo_features(df)
    bundle.update(
        {
            "player_match_elo": model.player_match_elo,
            "player_match_surface_elo": model.player_match_surface_elo,
            "player_match_surface_count": model.player_match_surface_count,
            "player_name_match_elo": model.player_name_match_elo,
            "player_name_match_surface_elo": model.player_name_match_surface_elo,
            "player_name_match_surface_count": model.player_name_match_surface_count,
            "player_match_last_seen": model.player_match_last_seen,
        }
    )

    backup = bundle_path + ".elo_backup"
    if not os.path.exists(backup):
        joblib.dump(joblib.load(bundle_path), backup)
        print(f"[elo-refresh] backup={backup}", flush=True)
    joblib.dump(bundle, bundle_path)
    print(
        "[elo-refresh] saved "
        f"micro={len(bundle.get('player_service_elo') or {})} "
        f"name_micro={len(bundle.get('player_name_service_elo') or {})} "
        f"match={len(model.player_match_elo)} "
        f"name_match={len(model.player_name_match_elo)} "
        f"elapsed={time.time() - t0:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
