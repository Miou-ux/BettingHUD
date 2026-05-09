"""Micro-Elo (service / return) with Tennis Abstract surface-speed weighting.

Train-time scan (no leakage): updates global + per-surface parallel ratings;
pre-match effective ratings blend surface tracks with α = n/(n+30).
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from surface_speed import lookup_surface_speed  # noqa: E402


def _pid(pid):
    if pd.isna(pid):
        return None
    return str(pid).strip()


def _decay(rating, last_date, current_date, base_elo, tau_days):
    if last_date is None:
        return rating
    delta_days = (current_date - last_date).days
    if delta_days <= 14:
        return rating
    factor = float(np.exp(-delta_days / float(tau_days)))
    return base_elo + (rating - base_elo) * factor


def _expected_pt(serve_elo, return_elo, scale):
    return 1.0 / (1.0 + (10.0 ** ((return_elo - serve_elo) / scale)))


def _stat(row, camel, lower):
    v = getattr(row, camel, None)
    if v is None:
        v = getattr(row, lower, None)
    return v


def run_micro_elo_scan(
    matches_df: pd.DataFrame,
    name_key_fn,
    base_elo: float = 1500.0,
    decay_tau_days: float = 365.0,
    micro_elo_scale: float = 200.0,
    micro_elo_k_serve: float = 0.45,
    speed_baseline: float = 0.75,
    speed_alpha: float = 0.35,
) -> dict:
    serve_g: dict = {}
    ret_g: dict = {}
    serve_sf: dict = {}
    ret_sf: dict = {}
    n_sf: dict = {}
    last_seen: dict = {}

    w_elo, l_elo, w_surf, l_surf = [], [], [], []
    w_sv, w_rt, l_sv, l_rt = [], [], [], []

    n_ok = 0
    for row in matches_df.itertuples(index=False):
        wid = _pid(getattr(row, "winner_id", None))
        lid = _pid(getattr(row, "loser_id", None))
        wname = name_key_fn(getattr(row, "winner_name", None))
        lname = name_key_fn(getattr(row, "loser_name", None))
        tour = str(getattr(row, "tour", "ATP"))
        wname_key = f"{tour}::{wname}" if wname else None
        lname_key = f"{tour}::{lname}" if lname else None
        surface = str(getattr(row, "surface", "") or "")
        dt = getattr(row, "tourney_date")

        spd_raw = getattr(row, "surface_speed", None)
        if spd_raw is None or (isinstance(spd_raw, float) and np.isnan(spd_raw)):
            speed = lookup_surface_speed(getattr(row, "tourney_name", None), surface)
        else:
            speed = float(spd_raw)

        if (not wid or not lid) and not (wname_key and lname_key):
            for lst in (w_elo, l_elo, w_surf, l_surf):
                lst.append(base_elo)
            for lst in (w_sv, w_rt, l_sv, l_rt):
                lst.append(base_elo)
            continue

        def rg(pid, nk, use_id):
            sg = serve_g.get(pid, serve_g.get(nk, base_elo)) if use_id else serve_g.get(nk, base_elo)
            rg_ = ret_g.get(pid, ret_g.get(nk, base_elo)) if use_id else ret_g.get(nk, base_elo)
            return sg, rg_

        def rsf(pid, nk, use_id):
            key = (pid if use_id else nk, surface)
            return (
                serve_sf.get(key, base_elo),
                ret_sf.get(key, base_elo),
                n_sf.get(key, 0),
            )

        wg_sg, wg_rg = rg(wid, wname_key, bool(wid))
        lg_sg, lg_rg = rg(lid, lname_key, bool(lid))

        w_last = last_seen.get(wid) if wid else last_seen.get(wname_key)
        l_last = last_seen.get(lid) if lid else last_seen.get(lname_key)

        wg_sg = _decay(wg_sg, w_last, dt, base_elo, decay_tau_days)
        wg_rg = _decay(wg_rg, w_last, dt, base_elo, decay_tau_days)
        lg_sg = _decay(lg_sg, l_last, dt, base_elo, decay_tau_days)
        lg_rg = _decay(lg_rg, l_last, dt, base_elo, decay_tau_days)

        w_ss, w_rs, w_n = rsf(wid, wname_key, bool(wid))
        l_ss, l_rs, l_n = rsf(lid, lname_key, bool(lid))

        w_ss = _decay(w_ss, w_last, dt, base_elo, decay_tau_days)
        w_rs = _decay(w_rs, w_last, dt, base_elo, decay_tau_days)
        l_ss = _decay(l_ss, l_last, dt, base_elo, decay_tau_days)
        l_rs = _decay(l_rs, l_last, dt, base_elo, decay_tau_days)

        n0 = 30.0
        aw = w_n / (w_n + n0) if w_n > 0 else 0.0
        al = l_n / (l_n + n0) if l_n > 0 else 0.0

        eff_w_sv = aw * w_ss + (1.0 - aw) * wg_sg
        eff_w_rt = aw * w_rs + (1.0 - aw) * wg_rg
        eff_l_sv = al * l_ss + (1.0 - al) * lg_sg
        eff_l_rt = al * l_rs + (1.0 - al) * lg_rg

        w_elo.append((wg_sg + wg_rg) / 2.0)
        l_elo.append((lg_sg + lg_rg) / 2.0)
        w_surf.append((eff_w_sv + eff_w_rt) / 2.0)
        l_surf.append((eff_l_sv + eff_l_rt) / 2.0)
        w_sv.append(eff_w_sv)
        w_rt.append(eff_w_rt)
        l_sv.append(eff_l_sv)
        l_rt.append(eff_l_rt)

        w_svpt = _stat(row, "w_svpt", "w_svpt")
        w_1stwon = _stat(row, "w_1stWon", "w_1stwon")
        w_2ndwon = _stat(row, "w_2ndWon", "w_2ndwon")
        l_svpt = _stat(row, "l_svpt", "l_svpt")
        l_1stwon = _stat(row, "l_1stWon", "l_1stwon")
        l_2ndwon = _stat(row, "l_2ndWon", "l_2ndwon")

        stats_ok = (
            w_svpt is not None and not pd.isna(w_svpt) and float(w_svpt) > 5
            and w_1stwon is not None and not pd.isna(w_1stwon)
            and w_2ndwon is not None and not pd.isna(w_2ndwon)
            and l_svpt is not None and not pd.isna(l_svpt) and float(l_svpt) > 5
            and l_1stwon is not None and not pd.isna(l_1stwon)
            and l_2ndwon is not None and not pd.isna(l_2ndwon)
        )

        if not stats_ok:
            for pid in [wid, lid]:
                if pid:
                    last_seen[pid] = dt
            for nk in [wname_key, lname_key]:
                if nk:
                    last_seen[nk] = dt
            continue

        n_ok += 1
        slow_pen = max(0.0, speed_baseline - speed)
        slow_amp_loss = 1.0 + speed_alpha * slow_pen
        slow_amp_gain = 1.0 + 0.5 * speed_alpha * slow_pen

        level = str(getattr(row, "tourney_level", "A"))
        k_level = 1.30 if level == "G" else 1.15 if level == "M" else 1.0 if level == "A" else 0.85
        # WTA: + 15 % volatility on Masters-1000 (form swings react faster than ATP).
        if tour == "WTA" and level == "M":
            k_level *= 1.15

        ws_g, wr_g = wg_sg, wg_rg
        ls_g, lr_g = lg_sg, lg_rg

        # --- Step 1: winner serves ---
        ew_pct = _expected_pt(ws_g, lr_g, micro_elo_scale)
        ew_count = ew_pct * float(w_svpt)
        actual_w = float(w_1stwon) + float(w_2ndwon)
        diff_w = actual_w - ew_count
        k_eff_w = micro_elo_k_serve * k_level * (slow_amp_loss if diff_w < 0 else slow_amp_gain)
        delta_w = k_eff_w * diff_w

        ws_g_1 = ws_g + delta_w
        lr_g_1 = lr_g - delta_w

        w_ss0, w_rs0 = w_ss, w_rs
        l_ss0, l_rs0 = l_ss, l_rs
        w_ss_1 = w_ss0 + delta_w
        l_rs_1 = l_rs0 - delta_w

        # --- Step 2: loser serves (uses WR global before loser-step = wr_g, not yet updated) ---
        el_pct = _expected_pt(ls_g, wr_g, micro_elo_scale)
        el_count = el_pct * float(l_svpt)
        actual_l = float(l_1stwon) + float(l_2ndwon)
        diff_l = actual_l - el_count
        k_eff_l = micro_elo_k_serve * k_level * (slow_amp_loss if diff_l < 0 else slow_amp_gain)
        delta_l = k_eff_l * diff_l

        ls_g_2 = ls_g + delta_l
        wr_g_2 = wr_g - delta_l

        l_ss_2 = l_ss0 + delta_l
        w_rs_2 = w_rs0 - delta_l

        def put_one(key, sg, rg, ss, rs):
            if not key:
                return
            serve_g[key] = sg
            ret_g[key] = rg
            k = (key, surface)
            serve_sf[k] = ss
            ret_sf[k] = rs
            n_sf[k] = n_sf.get(k, 0) + 1
            last_seen[key] = dt

        w_key = wid if wid else wname_key
        l_key = lid if lid else lname_key
        put_one(w_key, ws_g_1, wr_g_2, w_ss_1, w_rs_2)
        put_one(l_key, ls_g_2, lr_g_1, l_ss_2, l_rs_1)

    return {
        "winner_elo_pre": pd.Series(w_elo),
        "loser_elo_pre": pd.Series(l_elo),
        "winner_surf_elo_pre": pd.Series(w_surf),
        "loser_surf_elo_pre": pd.Series(l_surf),
        "winner_service_elo_pre": pd.Series(w_sv),
        "winner_return_elo_pre": pd.Series(w_rt),
        "loser_service_elo_pre": pd.Series(l_sv),
        "loser_return_elo_pre": pd.Series(l_rt),
        "serve_g": serve_g,
        "ret_g": ret_g,
        "serve_sf": serve_sf,
        "ret_sf": ret_sf,
        "n_sf": n_sf,
        "last_seen": last_seen,
        "matches_with_stats": n_ok,
    }
