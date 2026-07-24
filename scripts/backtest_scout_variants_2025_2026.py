#!/usr/bin/env python3
"""Grille variantes SCOUT — phase 2 (BGF, tri, tiers, rel fallback).

Usage:
  py -3 scripts/backtest_scout_variants_2025_2026.py
  py -3 scripts/backtest_scout_variants_2025_2026.py --top 10
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from collections import defaultdict
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_pack12_global_2026 import BR_START, LIVE_CUTOFF
from scripts.backtest_prod_top5_2026 import (
    PROD_MIN_PROBA_FRAC,
    _csv_rows_for_year,
    _is_live_replay_dataframe,
    _kelly_sim,
    _norm_pick_row,
    _resolve_csv_path,
    _tag,
    report_block,
    select_prod_top5_day,
)
from scripts.backtest_csv_pick_rows import augment_csv_pick_fields, dataframe_to_pick_rows
from scripts.backtest_staking_sim import load_and_filter_bets_csv
from scripts.pick_modes import DEFAULT_EV_MAX_PCT, DEFAULT_EV_MIN_PCT
from scripts.simulate_top10_proba_2026 import DEFAULT_EXTRA_EXCLUDE, DEFAULT_TOURNEY_LEVELS
from scripts.bets_db import _algo_kelly_stake_frac, open_db
from scripts.experiment_july_expert_kelly import (
    EXPERT_SCOUT,
    ExpertCfg,
    JSONL_DIR,
    _attach_settlement,
    _load_morning_pool,
    _settlement_map,
    select_expert_picks,
)
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key, resolve_segment_brier_score
from scripts.simulate_top10_proba_2026 import KELLY_BASE

SCOUT_P75 = replace(EXPERT_SCOUT, min_proba=0.73, max_picks=6)
BGF = replace(SCOUT_P75, block_book_gap_flag=True)

SCOUT_VARIANTS: dict[str, ExpertCfg | None] = {
    "HYBRID": None,
    # Références
    "P75": SCOUT_P75,
    "BGF": BGF,
    # Proba
    "BGF-P72": replace(BGF, min_proba=0.72),
    "BGF-P71": replace(BGF, min_proba=0.71),
    "BGF-P74": replace(BGF, min_proba=0.74),
    "BGF-P76": replace(BGF, min_proba=0.76),
    "BGF-P77": replace(BGF, min_proba=0.77),
    # EV bands
    "BGF-EV45": replace(BGF, max_ev_pct=45.0),
    "BGF-EV50": replace(BGF, max_ev_pct=50.0),
    "BGF-EV840": replace(BGF, min_ev_pct=8.0, max_ev_pct=40.0),
    "BGF-EV1050": replace(BGF, min_ev_pct=10.0, max_ev_pct=50.0),
    "BGF-EV1250": replace(BGF, min_ev_pct=12.0, max_ev_pct=50.0),
    "BGF-EV1555": replace(BGF, min_ev_pct=15.0, max_ev_pct=55.0),
    "BGF-EV638": replace(BGF, min_ev_pct=6.0, max_ev_pct=38.0),
    # Gap / Kelly / volume
    "BGF-G30": replace(BGF, max_book_gap_pp=30.0),
    "BGF-G28": replace(BGF, max_book_gap_pp=28.0),
    "BGF-K3": replace(BGF, min_kelly_frac=0.03),
    "BGF-K4": replace(BGF, min_kelly_frac=0.04),
    "BGF-M3": replace(BGF, max_picks=3),
    "BGF-M4": replace(BGF, max_picks=4),
    "BGF-M5": replace(BGF, max_picks=5),
    "BGF-M8": replace(BGF, max_picks=8),
    # Rel
    "BGF-R85": replace(BGF, min_rel=85),
    "BGF-R82": replace(BGF, min_rel=82),
    "BGF-REL-FB": replace(BGF, rel_fallback=True),
    # Tri / tiers
    "BGF-SORT-P": replace(BGF, sort_mode="proba"),
    "BGF-SORT-EV": replace(BGF, sort_mode="ev"),
    "BGF-SORT-K": replace(BGF, sort_mode="kelly"),
    "BGF-TIER": replace(BGF, tier_fill=True),
    "BGF-TIER-P": replace(BGF, tier_fill=True, sort_mode="proba"),
    "BGF-TIER-M4": replace(BGF, tier_fill=True, max_picks=4),
    # Combos prometteurs
    "BGF-G30-M4": replace(BGF, max_book_gap_pp=30.0, max_picks=4),
    "BGF-EV45-M4": replace(BGF, max_ev_pct=45.0, max_picks=4),
    "BGF-P76-M4": replace(BGF, min_proba=0.76, max_picks=4),
    "BGF-RELFB-M4": replace(BGF, rel_fallback=True, max_picks=4),
    "P75-TIER": replace(SCOUT_P75, tier_fill=True),
    "P75-SORT-P": replace(SCOUT_P75, sort_mode="proba"),
    # Phase 3 — combos
    "BGF-K4": replace(BGF, min_kelly_frac=0.04),
    "BGF-M3": replace(BGF, max_picks=3),
    "P75-TIER-M4": replace(SCOUT_P75, tier_fill=True, max_picks=4),
    "BGF-EV45-K4": replace(BGF, max_ev_pct=45.0, min_kelly_frac=0.04),
    "BGF-P76-K4": replace(BGF, min_proba=0.76, min_kelly_frac=0.04),
    "BGF-G30-K4": replace(BGF, max_book_gap_pp=30.0, min_kelly_frac=0.04),
    "BGF-TIER-M4": replace(BGF, tier_fill=True, max_picks=4),
    "P75-TIER-BGF": replace(BGF, tier_fill=True),
}


def _enrich_kelly_fields(rows: list[dict], ml: TennisMLModel) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        if float(row.get("theoretical_stake_frac") or 0.0) <= 0.0:
            p = float(row.get("p_model_fav") or 0.0)
            odd = float(row.get("odd_fav") or row.get("odd") or 0.0)
            seg_key = resolve_match_brier_segment_key(row)
            seg_brier = resolve_segment_brier_score(ml, seg_key)
            row["segment_brier"] = seg_brier
            row["theoretical_stake_frac"] = _algo_kelly_stake_frac(p, odd, seg_brier)
        out.append(row)
    return out


def _top_n_by_proba(rows: list[dict], n: int) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda r: (-float(r.get("p_model_fav") or 0.0), str(r.get("match_name") or "").lower()),
    )
    out: list[dict] = []
    seen: set[str] = set()
    for row in ranked:
        mk = str(row.get("match_name") or "").strip().lower()
        if not mk or mk in seen:
            continue
        seen.add(mk)
        out.append(row)
        if len(out) >= n:
            break
    return out


def _live_replay_rows_from_cutoff(year: int) -> list[dict]:
    """Candidats live_replay_v1 ≥ LIVE_CUTOFF (pool élargi vs JSONL top 15)."""
    path = _resolve_csv_path(year)
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df = load_and_filter_bets_csv(
        path,
        year=year,
        ev_min_pct=15.0,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df = df[df["ev"].astype(float) <= 1.0].copy()
    df = df[df["date"].dt.strftime("%Y-%m-%d") >= LIVE_CUTOFF].copy()
    if df.empty or not _is_live_replay_dataframe(df):
        return []
    df = df[
        (df["p_model_fav"].astype(float) > PROD_MIN_PROBA_FRAC)
        & (df["ev_fav_pct"].astype(float) >= DEFAULT_EV_MIN_PCT)
        & (df["ev_fav_pct"].astype(float) <= DEFAULT_EV_MAX_PCT)
    ].copy()
    if "match_name" not in df.columns:
        df["match_name"] = df.apply(
            lambda r: f"{r.get('player1', r.get('winner_name'))} vs {r.get('player2', r.get('loser_name'))}",
            axis=1,
        )
    df["settled"] = True
    df = augment_csv_pick_fields(df)
    return [_norm_pick_row(r) for r in dataframe_to_pick_rows(df)]


def _daily_pools(year: int, ml: TennisMLModel, *, pool_top_n: int = 20) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in _csv_rows_for_year(year):
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))

    if year == 2026:
        jsonl_by_day: dict[str, list[dict]] = {}
        for path in sorted(glob.glob(os.path.join(JSONL_DIR, "2026-*.jsonl"))):
            day = os.path.basename(path).replace(".jsonl", "")
            if day < LIVE_CUTOFF:
                continue
            pool, _ = _load_morning_pool(path, day)
            if pool:
                jsonl_by_day[day] = [_norm_pick_row(p) for p in pool]

        lr_by_day: dict[str, list[dict]] = defaultdict(list)
        for r in _live_replay_rows_from_cutoff(2026):
            lr_by_day[str(r.get("calendar_date") or "")[:10]].append(r)

        live_days = set(jsonl_by_day) | set(lr_by_day) | {d for d in by_day if d >= LIVE_CUTOFF}
        for day in live_days:
            merged: dict[str, dict] = {}
            for row in jsonl_by_day.get(day, []):
                mk = str(row.get("match_name") or "").strip().lower()
                if mk:
                    merged[mk] = dict(row)
            for row in lr_by_day.get(day, []):
                mk = str(row.get("match_name") or "").strip().lower()
                if mk and mk not in merged:
                    merged[mk] = dict(row)
            if merged:
                by_day[day] = _top_n_by_proba(list(merged.values()), pool_top_n)

    capped: dict[str, list[dict]] = {}
    for day, rows in by_day.items():
        capped[day] = _enrich_kelly_fields(_top_n_by_proba(rows, pool_top_n), ml)
    return capped


def _run_all(
    pools_by_year: dict[int, dict[str, list[dict]]],
    smap: dict[str, dict],
    names: list[str],
    *,
    conn=None,
) -> dict[str, list[dict]]:
    all_picks: dict[str, list[dict]] = {n: [] for n in names}
    for year, by_day in pools_by_year.items():
        for day in sorted(by_day):
            pool = by_day[day]
            dup = duplicate_model_prob_keys(pool)
            for name in names:
                cfg = SCOUT_VARIANTS[name]
                picks = select_prod_top5_day(pool) if cfg is None else select_expert_picks(pool, duplicate_keys=dup, cfg=cfg)
                for p in picks:
                    row = dict(p)
                    if year == 2026 and day >= LIVE_CUTOFF:
                        row = _attach_settlement(row, smap, conn=conn)
                    all_picks[name].append(row)
    return all_picks


def _slice(picks: list[dict], segment: str) -> list[dict]:
    if segment == "all":
        return picks
    if segment == "2025":
        return [p for p in picks if str(p.get("calendar_date") or "")[:4] == "2025"]
    if segment == "2026":
        return [p for p in picks if str(p.get("calendar_date") or "")[:4] == "2026"]
    if segment == "live":
        return [p for p in picks if str(p.get("calendar_date") or "")[:10] >= LIVE_CUTOFF]
    return picks


def _monthly_kelly_sum(picks: list[dict], ml: TennisMLModel, kelly_frac: float) -> float:
    by_m: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)
    return sum(
        float(_kelly_sim(_tag(c, "m"), ml, br_start=BR_START, kelly_frac=kelly_frac).get("net_profit_eur") or 0)
        for c in by_m.values() if c
    )


def _metrics(name: str, picks: list[dict], ml: TennisMLModel, kf: float) -> dict:
    r = report_block(name, picks, ml, kelly_frac=kf)
    k = r["k"]
    return {
        "name": name,
        "picks": r["n"],
        "hit": r["hit"],
        "flat": r["flat"],
        "kelly": float(k.get("net_profit_eur") or 0),
        "kelly_m": _monthly_kelly_sum(picks, ml, kf),
        "max_dd": float(k.get("max_drawdown_pct") or 0),
        "sharpe": float(k.get("sharpe_daily") or 0),
        "pf": float(k.get("profit_factor") or 0),
    }


def _print_top(title: str, rows: list[dict], *, sort: str, n: int) -> None:
    ranked = sorted(rows, key=lambda x: float(x.get(sort) or 0), reverse=True)[:n]
    print(f"\n{'='*80}")
    print(f"{title} (top {n} par {sort})")
    print(f"{'='*80}")
    hdr = f"{'Var':<16} {'Pk':>4} {'Hit%':>5} {'Flat':>6} {'Kelly':>8} {'ΣKM':>6} {'DD':>5} {'Sh':>5} {'PF':>4}"
    print(hdr)
    for r in ranked:
        print(
            f"{r['name']:<16} {r['picks']:>4} {r['hit']:>4.0f}% {r['flat']:>+6.0f} "
            f"{r['kelly']:>+8.1f} {r['kelly_m']:>+6.0f} {r['max_dd']:>4.0f}% {r['sharpe']:>5.2f} {r['pf']:>4.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--kelly-frac", type=float, default=KELLY_BASE)
    ap.add_argument("--top", type=int, default=12, help="Top N variantes affichées")
    ap.add_argument("--pool-top", type=int, default=20, help="Top N proba/jour dans le pool")
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    pools = {
        2025: _daily_pools(2025, ml, pool_top_n=args.pool_top),
        2026: _daily_pools(2026, ml, pool_top_n=args.pool_top),
    }
    smap = _settlement_map(args.db)
    names = list(SCOUT_VARIANTS.keys())
    conn = open_db(args.db)
    try:
        all_picks = _run_all(pools, smap, names, conn=conn)
    finally:
        conn.close()

    segments = [
        ("2026 LIVE replay", "live", "kelly"),
        ("2026 GLOBAL", "2026", "kelly"),
        ("2025", "2025", "kelly_m"),
        ("2025+2026", "all", "kelly_m"),
    ]

    live_sizes = [len(pools[2026][d]) for d in pools[2026] if d >= LIVE_CUTOFF]
    avg_live = sum(live_sizes) / max(1, len(live_sizes))
    print(
        f"SCOUT variants — {len(names)} profils | pool top {args.pool_top}/j | Kelly {args.kelly_frac} | "
        f"live JSONL+replay avg {avg_live:.1f} cand/j (≥ {LIVE_CUTOFF})"
    )

    for title, seg, sort in segments:
        rows = [_metrics(n, _slice(all_picks[n], seg), ml, args.kelly_frac) for n in names]
        _print_top(title, rows, sort=sort, n=args.top)

    ref = _metrics("BGF", _slice(all_picks["BGF"], "live"), ml, args.kelly_frac)
    print(f"\n--- LIVE replay — variantes ≠ BGF (Kelly ref {ref['kelly']:+.1f}€, {ref['picks']} picks) ---")
    live_rows = {n: _metrics(n, _slice(all_picks[n], "live"), ml, args.kelly_frac) for n in names}
    for n in sorted(names, key=lambda x: -live_rows[x]["kelly"]):
        m = live_rows[n]
        if (
            m["picks"] != ref["picks"]
            or abs(m["kelly"] - ref["kelly"]) > 0.05
            or abs(m["hit"] - ref["hit"]) > 0.05
        ):
            print(
                f"  {n:<16} pk={m['picks']:>2} hit={m['hit']:>4.0f}% Kelly={m['kelly']:>+7.1f}€ "
                f"Δk={m['kelly']-ref['kelly']:>+6.1f} Sharpe={m['sharpe']:.2f} PF={m['pf']:.2f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
