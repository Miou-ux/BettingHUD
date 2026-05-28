#!/usr/bin/env python3
"""Build a lightweight player feature store for live inference.

The store captures the latest causal player state from historical ATP/WTA rows so
the Live Tracker can use O(1) lookups instead of repeatedly scanning SQLite
history for every live match.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import joblib
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.surface_speed import lookup_surface_speed  # noqa: E402
from scripts.tournament_geo import tournament_site_lon_lat_tz  # noqa: E402

DEFAULT_OUTPUT = os.path.join(ROOT, "data", "cache", "player_feature_store.joblib")


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _ratio(num: object, den: object, default: float) -> float:
    try:
        n = float(num)
        d = float(den)
        if d <= 0 or np.isnan(n) or np.isnan(d):
            return float(default)
        return float(n / d)
    except Exception:
        return float(default)


def _nanmean(values: list[object], default: float = np.nan) -> float:
    vals = np.asarray([_safe_float(v, np.nan) for v in values], dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return float(default)
    return float(np.mean(vals))


def _read_history(db_path: str, min_year: int) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        atp = pd.read_sql(
            "SELECT * FROM matches_recent WHERE source='tennismylife' "
            "AND CAST(substr(tourney_date,1,4) AS INTEGER) >= ?",
            conn,
            params=(int(min_year),),
        )
        try:
            wta = pd.read_sql(
                "SELECT * FROM wta_matches "
                "WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= ?",
                conn,
                params=(int(min_year),),
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
        if "surface" in wta.columns:
            wta["surface"] = wta["surface"].astype(str).str.title().replace({"Nan": pd.NA})

    df = pd.concat([atp, wta], ignore_index=True, sort=False)
    df = df.dropna(subset=["winner_name", "loser_name", "surface", "tourney_date"])
    return df.sort_values("tourney_date").reset_index(drop=True)


def _keys_for_player(model: TennisMLModel, tour: str, pid: object, name: object) -> list[str]:
    keys: list[str] = []
    pid_key = model._pid_key(pid)
    if pid_key:
        keys.append(pid_key if pid_key.startswith(("ATP::", "WTA::")) else f"{tour}::{pid_key}")
    nk = model._name_key(name)
    if nk:
        keys.append(f"{tour}::{nk}")
    return list(dict.fromkeys(keys))


def _tb_won_lost_for_player(model: TennisMLModel, score: object, is_winner: bool) -> tuple[float, float]:
    w_tb, l_tb, _played = model._infer_tiebreaks_from_score(score)
    return (float(w_tb), float(l_tb)) if is_winner else (float(l_tb), float(w_tb))


def _is_three_plus_setter(model: TennisMLModel, score: object, best_of: object) -> bool:
    try:
        return bool(model._is_three_plus_setter(score, best_of))
    except Exception:
        return False


def _last_round_depth(model: TennisMLModel, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    tail = rows[-20:]
    last_tid = tail[-1].get("tourney_id")
    if last_tid is not None and str(last_tid).strip():
        same = [r for r in tail if str(r.get("tourney_id") or "") == str(last_tid)]
    else:
        same = [r for r in tail if r.get("date") == tail[-1].get("date")]
    depths = [int(model._round_depth(r.get("round"))) for r in same]
    if any(str(r.get("round") or "").upper() == "F" and r.get("won") for r in same):
        depths.append(8)
    return int(max(depths or [0]))


def _build_player_state(model: TennisMLModel, recs: list[dict[str, Any]], as_of: pd.Timestamp) -> dict[str, Any]:
    recs = sorted(recs, key=lambda r: r["date"])
    cutoff7 = as_of - pd.Timedelta(days=7)
    cutoff14 = as_of - pd.Timedelta(days=14)
    cutoff52 = as_of - pd.Timedelta(days=364)
    cutoff365 = as_of - pd.Timedelta(days=365)
    last10 = recs[-10:]
    last20 = recs[-20:]
    r7 = [r for r in recs if r["date"] >= cutoff7]
    r14 = [r for r in recs if r["date"] >= cutoff14]
    r52 = [r for r in recs if r["date"] >= cutoff52]
    r365 = [r for r in recs if r["date"] >= cutoff365]

    minutes7 = float(sum(max(0.0, _safe_float(r.get("minutes"), 0.0)) for r in r7))
    wins7 = int(sum(1 for r in r7 if r.get("won")))
    three14 = int(sum(1 for r in r14 if r.get("three_plus")))
    last_match_date = recs[-1]["date"] if recs else None
    days_since = int(max(0, (as_of - last_match_date).days)) if last_match_date is not None else 7

    tb_w = float(sum(_safe_float(r.get("tb_won"), 0.0) for r in r52))
    tb_l = float(sum(_safe_float(r.get("tb_lost"), 0.0) for r in r52))
    tb_pct = float(tb_w / (tb_w + tb_l)) if (tb_w + tb_l) > 0 else 0.5

    first_srv10 = _nanmean([r.get("first_srv_win") for r in last10])
    bp_conv10 = _nanmean([r.get("bp_conv") for r in last10])
    dom10 = _nanmean([r.get("dominance_ratio") for r in last10])

    tac_recs = r365 if len(r365) >= 5 else recs[-20:]
    tac_ace = _nanmean([r.get("ace_rate") for r in tac_recs])
    tac_f1 = _nanmean([r.get("first_in_win") for r in tac_recs])
    tac_bp = _nanmean([r.get("bp_saved") for r in tac_recs])
    tac_hold = _nanmean([r.get("hold_rate") for r in tac_recs])

    clutch_bp_saved = _nanmean([r.get("bp_saved") for r in r365])
    clutch_bp_conv = _nanmean([r.get("bp_conv") for r in r365])
    clutch52 = float(np.nanmean([
        0.5 if np.isnan(clutch_bp_saved) else clutch_bp_saved,
        0.5 if np.isnan(clutch_bp_conv) else clutch_bp_conv,
        tb_pct,
    ]))

    speeds = np.array([_safe_float(r.get("surface_speed"), np.nan) for r in r365], dtype=float)
    wins = np.array([1.0 if r.get("won") else 0.0 for r in r365], dtype=float)
    speed_corr = 0.0
    speed_affinity = 0.0
    if len(speeds) >= 5 and np.nanstd(speeds) > 1e-9 and np.nanstd(wins) > 1e-9:
        c = np.corrcoef(speeds, wins)[0, 1]
        speed_corr = 0.0 if np.isnan(c) else float(c)
    if len(speeds) >= 5:
        fast = wins[speeds >= 0.75]
        slow = wins[speeds <= 0.65]
        if len(fast) >= 2 and len(slow) >= 2:
            speed_affinity = float(np.mean(fast) - np.mean(slow))

    ace20 = _nanmean([r.get("ace_rate") for r in last20])
    serve20 = _nanmean([r.get("serve_win") for r in last20])
    break20 = _nanmean([r.get("break_rate") for r in last20])
    style_cluster = model._infer_style_cluster(
        0.06 if np.isnan(ace20) else float(ace20),
        0.60 if np.isnan(serve20) else float(serve20),
        0.20 if np.isnan(break20) else float(break20),
    )

    last = recs[-1] if recs else {}
    return {
        "last_match_date": None if last_match_date is None else pd.Timestamp(last_match_date).date().isoformat(),
        "days_since_last_match": days_since,
        "minutes_played_last7d": minutes7,
        "wins_last7d": wins7,
        "three_setters_last14d": three14,
        "last_round_reached": _last_round_depth(model, recs),
        "tb_win_pct_52w": float(np.clip(tb_pct, 0.0, 1.0)),
        "first_srv_win10": float(0.68 if np.isnan(first_srv10) else np.clip(first_srv10, 0.0, 1.0)),
        "bp_conv10": float(0.38 if np.isnan(bp_conv10) else np.clip(bp_conv10, 0.0, 1.0)),
        "dominance_ratio": float(1.0 if np.isnan(dom10) else max(0.0, dom10)),
        "tac_ace": float(0.08 if np.isnan(tac_ace) else max(0.0, tac_ace)),
        "tac_f1_pct": float(0.62 if np.isnan(tac_f1) else np.clip(tac_f1, 0.0, 1.0)),
        "tac_bp_saved_pct": float(0.58 if np.isnan(tac_bp) else np.clip(tac_bp, 0.0, 1.0)),
        "tac_hold_pct": float(0.75 if np.isnan(tac_hold) else np.clip(tac_hold, 0.0, 1.0)),
        "clutch52": float(np.clip(clutch52, 0.0, 1.0)),
        "bp_resilience": float(0.5 if np.isnan(clutch_bp_saved) else np.clip(clutch_bp_saved, 0.0, 1.0)),
        "speed_affinity": float(speed_affinity),
        "speed_performance_delta": float(speed_corr),
        "style_cluster": style_cluster,
        "form20": float(np.mean([1.0 if r.get("won") else 0.0 for r in last20])) if last20 else 0.5,
        "last_geo": last.get("geo"),
        "last_tz": last.get("tz"),
    }


def build_feature_store(db_path: str, min_year: int, output_path: str = DEFAULT_OUTPUT) -> dict[str, Any]:
    model = TennisMLModel(db_path=db_path)
    t0 = time.time()
    df = _read_history(db_path, min_year)
    as_of = pd.Timestamp(df["tourney_date"].max()).normalize() if not df.empty else pd.Timestamp.utcnow().normalize()
    per_key: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in df.itertuples(index=False):
        dt = pd.Timestamp(getattr(row, "tourney_date")).normalize()
        tour = str(getattr(row, "tour", "ATP") or "ATP").upper()
        surface = str(getattr(row, "surface", "") or "")
        tname = getattr(row, "tourney_name", "")
        speed = _safe_float(getattr(row, "surface_speed", None), lookup_surface_speed(tname, surface))
        geo = tournament_site_lon_lat_tz(tname)
        for is_winner, id_attr, name_attr in (
            (True, "winner_id", "winner_name"),
            (False, "loser_id", "loser_name"),
        ):
            keys = _keys_for_player(model, tour, getattr(row, id_attr, None), getattr(row, name_attr, None))
            svpt = _safe_float(getattr(row, "w_svpt" if is_winner else "l_svpt", None), 0.0)
            ace = _safe_float(getattr(row, "w_ace" if is_winner else "l_ace", None), 0.0)
            first_in = _safe_float(getattr(row, "w_1stIn" if is_winner else "l_1stIn", None), 0.0)
            first_won = _safe_float(getattr(row, "w_1stWon" if is_winner else "l_1stWon", None), 0.0)
            second_won = _safe_float(getattr(row, "w_2ndWon" if is_winner else "l_2ndWon", None), 0.0)
            bp_saved = _safe_float(getattr(row, "w_bpSaved" if is_winner else "l_bpSaved", None), np.nan)
            bp_faced = _safe_float(getattr(row, "w_bpFaced" if is_winner else "l_bpFaced", None), np.nan)
            sv_gms = _safe_float(getattr(row, "w_SvGms" if is_winner else "l_SvGms", None), 0.0)
            opp_svpt = _safe_float(getattr(row, "l_svpt" if is_winner else "w_svpt", None), 0.0)
            opp_first_won = _safe_float(getattr(row, "l_1stWon" if is_winner else "w_1stWon", None), 0.0)
            opp_second_won = _safe_float(getattr(row, "l_2ndWon" if is_winner else "w_2ndWon", None), 0.0)
            opp_bp_saved = _safe_float(getattr(row, "l_bpSaved" if is_winner else "w_bpSaved", None), np.nan)
            opp_bp_faced = _safe_float(getattr(row, "l_bpFaced" if is_winner else "w_bpFaced", None), np.nan)
            opp_sv_gms = _safe_float(getattr(row, "l_SvGms" if is_winner else "w_SvGms", None), 0.0)
            breaks_suffered = max(0.0, bp_faced - bp_saved) if not np.isnan(bp_faced) and not np.isnan(bp_saved) else 0.0
            breaks_made = max(0.0, opp_bp_faced - opp_bp_saved) if not np.isnan(opp_bp_faced) and not np.isnan(opp_bp_saved) else 0.0
            hold_rate = (sv_gms - breaks_suffered) / sv_gms if sv_gms > 0 else 0.75
            break_rate = breaks_made / opp_sv_gms if opp_sv_gms > 0 else 0.20
            return_win = _ratio(opp_svpt - (opp_first_won + opp_second_won), opp_svpt, np.nan)
            serve_win = _ratio(first_won + second_won, svpt, np.nan)
            service_lost = np.nan if np.isnan(serve_win) else max(0.01, 1.0 - serve_win)
            dom = np.nan if np.isnan(return_win) or np.isnan(service_lost) else return_win / service_lost
            tb_won, tb_lost = _tb_won_lost_for_player(model, getattr(row, "score", None), is_winner)
            rec = {
                "date": dt,
                "won": bool(is_winner),
                "minutes": _safe_float(getattr(row, "minutes", None), 0.0),
                "three_plus": _is_three_plus_setter(model, getattr(row, "score", None), getattr(row, "best_of", 3)),
                "round": getattr(row, "round", None),
                "tourney_id": getattr(row, "tourney_id", None),
                "tb_won": tb_won,
                "tb_lost": tb_lost,
                "first_srv_win": _ratio(first_won, first_in, np.nan),
                "bp_conv": _ratio(breaks_made, opp_bp_faced, np.nan),
                "dominance_ratio": dom,
                "ace_rate": _ratio(ace, svpt, np.nan),
                "serve_win": serve_win,
                "break_rate": break_rate,
                "first_in_win": _ratio(first_won, first_in, np.nan),
                "bp_saved": _ratio(bp_saved, bp_faced, np.nan),
                "hold_rate": hold_rate,
                "surface_speed": speed,
                "geo": (float(geo[0]), float(geo[1])) if geo and geo[0] is not None and geo[1] is not None else None,
                "tz": float(geo[2]) if geo and geo[2] is not None else 0.0,
            }
            for key in keys:
                per_key[key].append(rec)

    players = {
        key: _build_player_state(model, recs, as_of)
        for key, recs in per_key.items()
        if recs
    }
    payload = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of.date().isoformat(),
            "min_year": int(min_year),
            "n_players": len(players),
            "n_history_rows": int(len(df)),
            "elapsed_sec": round(time.time() - t0, 3),
        },
        "players": players,
    }
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp = output_path + ".part"
    joblib.dump(payload, tmp)
    os.replace(tmp, output_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build live player feature store.")
    parser.add_argument("--db-path", default=os.path.join(ROOT, "data", "bettinghud.db"))
    parser.add_argument("--min-year", type=int, default=int(os.getenv("BETTINGHUD_FEATURE_STORE_MIN_YEAR", "2020")))
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build_feature_store(args.db_path, args.min_year, args.output)
    meta = payload.get("meta", {})
    print(
        f"[feature-store] saved={args.output} players={meta.get('n_players')} "
        f"rows={meta.get('n_history_rows')} as_of={meta.get('as_of')} "
        f"elapsed={meta.get('elapsed_sec')}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
