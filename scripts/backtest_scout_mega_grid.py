#!/usr/bin/env python3
"""Méga-grille ~10k variantes SCOUT — pool illimité/jour vs P75-TIER.

Usage:
  py -3 scripts/backtest_scout_mega_grid.py
  py -3 scripts/backtest_scout_mega_grid.py --n 10000
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_pack12_global_2026 import BR_START, LIVE_CUTOFF, _perf_flat
from scripts.backtest_prod_top5_2026 import (
    PROD_MIN_PROBA_FRAC,
    _csv_rows_for_year,
    _is_live_replay_dataframe,
    _kelly_sim,
    _norm_pick_row,
    _resolve_csv_path,
    _tag,
)
from scripts.backtest_csv_pick_rows import augment_csv_pick_fields, dataframe_to_pick_rows
from scripts.backtest_staking_sim import load_and_filter_bets_csv
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
from scripts.pick_modes import DEFAULT_EV_MAX_PCT, DEFAULT_EV_MIN_PCT
from scripts.simulate_top10_proba_2026 import DEFAULT_EXTRA_EXCLUDE, DEFAULT_TOURNEY_LEVELS, KELLY_BASE

SCOUT_P75 = replace(EXPERT_SCOUT, min_proba=0.73, max_picks=6)
P75_TIER = replace(SCOUT_P75, tier_fill=True)


def _enrich_kelly(rows: list[dict], ml: TennisMLModel) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        if float(row.get("theoretical_stake_frac") or 0.0) <= 0.0:
            p = float(row.get("p_model_fav") or 0.0)
            odd = float(row.get("odd_fav") or row.get("odd") or 0.0)
            sk = resolve_match_brier_segment_key(row)
            sb = resolve_segment_brier_score(ml, sk)
            row["segment_brier"] = sb
            row["theoretical_stake_frac"] = _algo_kelly_stake_frac(p, odd, sb)
        out.append(row)
    return out


def _live_replay_rows_from_cutoff(year: int) -> list[dict]:
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


def _merge_pool_rows(rows: list[dict]) -> list[dict]:
    """Dédoublonne par match, garde meilleure proba."""
    best: dict[str, dict] = {}
    for row in rows:
        mk = str(row.get("match_name") or "").strip().lower()
        if not mk:
            continue
        prev = best.get(mk)
        if prev is None or float(row.get("p_model_fav") or 0) > float(prev.get("p_model_fav") or 0):
            best[mk] = dict(row)
    return sorted(best.values(), key=lambda r: -float(r.get("p_model_fav") or 0))


def _daily_pools_unlimited(year: int, ml: TennisMLModel) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in _csv_rows_for_year(year):
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))

    if year == 2026:
        jsonl_by: dict[str, list[dict]] = {}
        for path in sorted(glob.glob(os.path.join(JSONL_DIR, "2026-*.jsonl"))):
            day = os.path.basename(path).replace(".jsonl", "")
            if day < LIVE_CUTOFF:
                continue
            pool, _ = _load_morning_pool(path, day)
            if pool:
                jsonl_by[day] = [_norm_pick_row(p) for p in pool]

        lr_by: dict[str, list[dict]] = defaultdict(list)
        for r in _live_replay_rows_from_cutoff(2026):
            lr_by[str(r.get("calendar_date") or "")[:10]].append(r)

        for day in set(jsonl_by) | set(lr_by) | {d for d in by_day if d >= LIVE_CUTOFF}:
            merged = jsonl_by.get(day, []) + lr_by.get(day, []) + by_day.get(day, [])
            if merged:
                by_day[day] = _merge_pool_rows(merged)

    return {d: _enrich_kelly(_merge_pool_rows(rows), ml) for d, rows in by_day.items()}


def _gen_configs(n: int, rng: random.Random) -> list[tuple[str, ExpertCfg]]:
    out: list[tuple[str, ExpertCfg]] = [("P75-TIER", P75_TIER)]
    seen: set[tuple] = set()
    sorts = ("score", "proba", "ev", "kelly")
    attempts = 0
    while len(out) < n + 1 and attempts < n * 20:
        attempts += 1
        min_ev = rng.choice([4, 5, 6, 7, 8, 9, 10, 12, 15])
        max_ev = rng.choice([35, 38, 40, 42, 45, 48, 50, 52, 55, 58, 60])
        if min_ev >= max_ev:
            continue
        cfg = ExpertCfg(
            max_picks=rng.choice([2, 3, 4, 5, 6, 7, 8, 10]),
            min_proba=rng.choice([0.70, 0.71, 0.72, 0.73, 0.74, 0.75, 0.76, 0.77, 0.78, 0.79]),
            min_rel=rng.choice([78, 80, 82, 85]),
            min_ev_pct=float(min_ev),
            max_ev_pct=float(max_ev),
            max_book_gap_pp=float(rng.choice([22, 25, 28, 30, 32, 35, 38, 40])),
            min_kelly_frac=rng.choice([0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]),
            block_book_gap_flag=rng.choice([True, False]),
            block_rank_default=True,
            block_te_both=True,
            block_dup_prob=True,
            sort_mode=rng.choice(sorts),
            tier_fill=rng.choice([True, False]),
            rel_fallback=rng.choice([True, False]),
        )
        key = (
            cfg.max_picks,
            cfg.min_proba,
            cfg.min_rel,
            cfg.min_ev_pct,
            cfg.max_ev_pct,
            cfg.max_book_gap_pp,
            cfg.min_kelly_frac,
            cfg.block_book_gap_flag,
            cfg.sort_mode,
            cfg.tier_fill,
            cfg.rel_fallback,
        )
        if key in seen:
            continue
        seen.add(key)
        label = f"v{len(out):05d}"
        out.append((label, cfg))
    return out


def _run_cfg(
    cfg: ExpertCfg,
    day_pools: dict[int, dict[str, tuple[list[dict], set]]],
    *,
    conn=None,
    smap: dict | None = None,
) -> list[dict]:
    picks: list[dict] = []
    for year, by_day in day_pools.items():
        for day, (pool, dup) in by_day.items():
            for p in select_expert_picks(pool, duplicate_keys=dup, cfg=cfg):
                row = dict(p)
                if year == 2026 and day >= LIVE_CUTOFF:
                    row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
    return picks


def _summarize(picks: list[dict], ml: TennisMLModel) -> dict:
    def _seg(year_prefix: str | None, live_only: bool = False) -> dict:
        if live_only:
            chunk = [p for p in picks if str(p.get("calendar_date") or "")[:10] >= LIVE_CUTOFF]
        elif year_prefix:
            chunk = [p for p in picks if str(p.get("calendar_date") or "")[:4] == year_prefix]
        else:
            chunk = picks
        _, ns, w, flat = _perf_flat(chunk)
        k = _kelly_sim(_tag(chunk, "x"), ml) if chunk else {"net_profit_eur": 0.0}
        by_m: dict[str, list[dict]] = defaultdict(list)
        for p in chunk:
            by_m[str(p.get("calendar_date") or "")[:7]].append(p)
        k_m = sum(
            float(_kelly_sim(_tag(c, "m"), ml, br_start=BR_START).get("net_profit_eur") or 0)
            for c in by_m.values()
            if c
        )
        return {
            "picks": len(chunk),
            "settled": ns,
            "hit": (w / ns * 100.0) if ns else 0.0,
            "kelly": float(k.get("net_profit_eur") or 0),
            "kelly_m": k_m,
            "flat": flat,
        }

    return {"2025": _seg("2025"), "2026": _seg("2026"), "live": _seg(None, live_only=True)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10000, help="Nombre de variantes aléatoires (+ P75-TIER)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    args = ap.parse_args()

    t0 = time.time()
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    smap = _settlement_map(args.db)

    pools = {2025: _daily_pools_unlimited(2025, ml), 2026: _daily_pools_unlimited(2026, ml)}
    day_pools: dict[int, dict[str, tuple[list[dict], set]]] = {}
    for year, by_day in pools.items():
        day_pools[year] = {}
        for day, pool in by_day.items():
            day_pools[year][day] = (pool, duplicate_model_prob_keys(pool))

    avg25 = sum(len(p[0]) for p in day_pools[2025].values()) / max(1, len(day_pools[2025]))
    avg26 = sum(len(p[0]) for p in day_pools[2026].values()) / max(1, len(day_pools[2026]))
    live_days = [d for d in day_pools[2026] if d >= LIVE_CUTOFF]
    avg_live = sum(len(day_pools[2026][d][0]) for d in live_days) / max(1, len(live_days))

    configs = _gen_configs(args.n, random.Random(args.seed))
    print(f"Mega-grid: {len(configs)} variantes | pool ILLIMITÉ | settlement match_results | seed {args.seed}")
    print(f"Pool moy/j: 2025={avg25:.1f} | 2026={avg26:.1f} | live={avg_live:.1f} ({len(live_days)} j)")

    ref: dict | None = None
    beat25: list[tuple[str, ExpertCfg, dict]] = []
    beat26: list[tuple[str, ExpertCfg, dict]] = []
    beatlive: list[tuple[str, ExpertCfg, dict]] = []
    triple: list[tuple[str, ExpertCfg, dict]] = []

    conn = open_db(args.db)
    try:
        for i, (label, cfg) in enumerate(configs):
            picks = _run_cfg(cfg, day_pools, conn=conn, smap=smap)
            m = _summarize(picks, ml)
            if label == "P75-TIER":
                ref = m
                ref_cfg = cfg
                continue
            if ref is None:
                continue
            b25 = m["2025"]["kelly_m"] > ref["2025"]["kelly_m"] + 0.5
            b26 = m["2026"]["kelly"] > ref["2026"]["kelly"] + 0.5
            bl = m["live"]["kelly"] > ref["live"]["kelly"] + 0.5
            if b25:
                beat25.append((label, cfg, m))
            if b26:
                beat26.append((label, cfg, m))
            if bl:
                beatlive.append((label, cfg, m))
            if b25 and b26 and bl:
                triple.append((label, cfg, m))
            if (i + 1) % 1000 == 0:
                print(f"  … {i+1}/{len(configs)} variantes", flush=True)

        if ref is None:
            picks = _run_cfg(P75_TIER, day_pools, conn=conn, smap=smap)
            ref = _summarize(picks, ml)
    finally:
        conn.close()

    print(f"\n=== RÉFÉRENCE P75-TIER ===")
    print(
        f"  2025 ΣKellyM {ref['2025']['kelly_m']:+.0f}€ hit {ref['2025']['hit']:.1f}% "
        f"{ref['2025']['settled']}/{ref['2025']['picks']} pk"
    )
    print(
        f"  2026 Kelly   {ref['2026']['kelly']:+.0f}€ hit {ref['2026']['hit']:.1f}% "
        f"{ref['2026']['settled']}/{ref['2026']['picks']} pk"
    )
    print(
        f"  live Kelly   {ref['live']['kelly']:+.1f}€ hit {ref['live']['hit']:.1f}% "
        f"{ref['live']['settled']}/{ref['live']['picks']} pk"
    )

    def _print_beat(title: str, items: list, *, seg: str, metric: str) -> None:
        print(f"\n=== {title}: {len(items)} variantes battent P75-TIER ===")
        key = metric
        items.sort(key=lambda t: -t[2][seg][key])
        for label, cfg, m in items[:25]:
            d = m[seg][key] - ref[seg][key]
            print(
                f"  {label} {cfg.label} | {seg} {m[seg][key]:+.1f} (Δ{d:+.1f}) "
                f"hit {m[seg]['hit']:.0f}% pk {m[seg]['picks']}"
            )
        if len(items) > 25:
            print(f"  … +{len(items)-25} autres")

    _print_beat("2025", beat25, seg="2025", metric="kelly_m")
    _print_beat("2026 GLOBAL", beat26, seg="2026", metric="kelly")
    _print_beat("LIVE replay", beatlive, seg="live", metric="kelly")

    print(f"\n=== TRIPLE (2025 + 2026 + live): {len(triple)} variantes ===")
    triple.sort(key=lambda t: -(t[2]["2025"]["kelly_m"] + t[2]["2026"]["kelly_m"] + t[2]["live"]["kelly"]))
    for label, cfg, m in triple[:20]:
        print(
            f"  {label} | {cfg.label}\n"
            f"    2025 ΣKM {m['2025']['kelly_m']:+.0f}€ | 2026 {m['2026']['kelly']:+.0f}€ | "
            f"live {m['live']['kelly']:+.1f}€ | hit live {m['live']['hit']:.0f}%"
        )
    if not triple:
        print("  Aucune variante ne bat P75-TIER sur les 3 segments simultanément.")

    print(f"\nDurée: {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
