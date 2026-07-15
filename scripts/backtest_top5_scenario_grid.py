#!/usr/bin/env python3
"""Grille de scénarios Top5 prod (hybrid réel) — 2025 + 2026.

Usage:
  py -3 scripts/backtest_top5_scenario_grid.py
  py -3 scripts/backtest_top5_scenario_grid.py --years 2025,2026 --top 20
  py -3 scripts/backtest_top5_scenario_grid.py --export data/reports/top5_scenario_grid.csv

Chaque scénario varie :
  - filtres pool (proba, EV, rel, gap, dup)
  - paramètres hybrid (p min, EV tier1/tier2)
  - cap journalier (fixe ou smart)
  - fraction Kelly (optionnel)

Critère recommandé : Kelly 2025+2026 combiné ET stabilité (pas de gain 2025 / perte 2026).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_pack12_global_2026 import _live_rows  # noqa: E402
from scripts.backtest_prod_top5_2026 import (  # noqa: E402
    _csv_rows_for_year,
    _norm_pick_row,
    report_block,
    select_prod_top5_day,
)
from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match  # noqa: E402
from scripts.match_rank_quality import excluded_duplicate_model_prob_from_top5  # noqa: E402
from scripts.ml_model import TennisMLModel  # noqa: E402

UNIVERSE = {
    2025: os.path.join(ROOT, "data", "reports", "backtest_universe_250plus_2025.from_prod.csv"),
    2026: os.path.join(ROOT, "data", "reports", "backtest_universe_250plus_2026.from_prod.csv"),
}


def _gap_pp(row: dict) -> float | None:
    g = row.get("book_gap_pp")
    if g is not None:
        try:
            return float(g)
        except (TypeError, ValueError):
            return None
    pi = row.get("p_implicit_fav")
    if pi is None:
        return None
    try:
        return abs((float(row.get("p_model_fav") or 0.0) - float(pi)) * 100.0)
    except (TypeError, ValueError):
        return None


@dataclass
class Scenario:
    name: str
    # pool filters (before hybrid)
    pmin: float = 0.60
    evmin: float = 15.0
    evmax: float = 100.0
    relmin: int = 80
    gapmax: float | None = None
    no_dup: bool = True
    # hybrid knobs (patched at runtime)
    hybrid_pmin: float = 0.80
    hybrid_t1_max: float = 35.0
    hybrid_t2_max: float = 55.0
    # daily cap
    limit: int = 5
    smart_cap: bool = False
    smart_cap_fn: Callable[[list[dict]], int] | None = None
    # staking
    kelly_frac: float = 0.65
    tags: list[str] = field(default_factory=list)


def default_smart_cap(candidates: list[dict]) -> int:
    """5/6/7 selon qualité moyenne du pool hybrid-éligible."""
    import scripts.hybrid_pick_selection as h

    pool = [r for r in candidates if h.hybrid_pool_ok(r)]
    if not pool:
        return 5
    mean_p = sum(float(r.get("p_model_fav") or 0.0) for r in pool) / len(pool)
    rels = [float(r.get("data_reliability_score") or 0.0) for r in pool]
    mean_rel = sum(rels) / len(rels)
    gaps = [_gap_pp(r) for r in pool]
    gaps = [g for g in gaps if g is not None]
    mean_gap = sum(gaps) / len(gaps) if gaps else 99.0
    if mean_p >= 0.82 and mean_rel >= 86 and mean_gap <= 24:
        return 7
    if mean_p >= 0.79 and mean_rel >= 83 and mean_gap <= 28:
        return 6
    return 5


@contextlib.contextmanager
def _hybrid_patch(sc: Scenario):
    import scripts.hybrid_pick_selection as h

    old = (
        h.HYBRID_MIN_PROBA_FRAC,
        h.HYBRID_TIER1_EV_MAX_PCT,
        h.HYBRID_TIER2_EV_MAX_PCT,
        h.HYBRID_POOL_EV_MAX_PCT,
    )
    h.HYBRID_MIN_PROBA_FRAC = float(sc.hybrid_pmin)
    h.HYBRID_TIER1_EV_MAX_PCT = float(sc.hybrid_t1_max)
    h.HYBRID_TIER2_EV_MAX_PCT = float(sc.hybrid_t2_max)
    h.HYBRID_POOL_EV_MAX_PCT = float(sc.hybrid_t2_max)
    try:
        yield
    finally:
        (
            h.HYBRID_MIN_PROBA_FRAC,
            h.HYBRID_TIER1_EV_MAX_PCT,
            h.HYBRID_TIER2_EV_MAX_PCT,
            h.HYBRID_POOL_EV_MAX_PCT,
        ) = old


def pool_filter(row: dict, sc: Scenario) -> bool:
    p = float(row.get("p_model_fav") or 0.0)
    evp = float(row.get("ev_fav_pct") or (float(row.get("ev_fav") or 0.0) * 100.0))
    rel = row.get("data_reliability_score")
    relv = float(rel) if rel is not None else -1.0
    if p < sc.pmin or evp < sc.evmin or evp > sc.evmax or relv < sc.relmin:
        return False
    if sc.gapmax is not None:
        g = _gap_pp(row)
        if g is not None and g > sc.gapmax:
            return False
    if sc.no_dup and excluded_duplicate_model_prob_from_top5(row):
        return False
    return True


def picks_for_scenario(rows: list[dict], sc: Scenario) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        row = _norm_pick_row(r)
        if pool_filter(row, sc):
            by_day[str(row.get("calendar_date") or "")[:10]].append(row)

    out: list[dict] = []
    with _hybrid_patch(sc):
        for day in sorted(by_day):
            cands = dedupe_top_proba_rows_by_match(by_day[day])
            if sc.smart_cap:
                fn = sc.smart_cap_fn or default_smart_cap
                lim = int(fn(cands))
            else:
                lim = int(sc.limit)
            out.extend(select_prod_top5_day(cands, limit=lim))
    return out


def eval_year(sc: Scenario, year: int, rows: list[dict], ml: TennisMLModel) -> dict[str, Any]:
    picks = picks_for_scenario(rows, sc)
    r = report_block(sc.name, picks, ml, kelly_frac=sc.kelly_frac)
    k = r["k"]
    return {
        "year": year,
        "n": r["n"],
        "days": r["days"],
        "hit": r["hit"],
        "flat": r["flat"],
        "kelly": float(k.get("net_profit_eur") or 0.0),
        "dd": float(k.get("max_drawdown_pct") or 0.0),
        "sharpe": float(k.get("sharpe_daily") or 0.0),
    }


def build_coarse_grid() -> list[Scenario]:
    """Grille exploratoire — leviers distincts, pas explosion combinatoire."""
    out: list[Scenario] = []

    def add(name: str, **kw) -> None:
        out.append(Scenario(name=name, **kw))

    add("PROD_L5")
    add("PROD_L6", limit=6)
    add("PROD_L7", limit=7)
    add("SMART_CAP", smart_cap=True)

    # hybrid tuning (vrai goulot volume à cap fixe)
    add("HYB_P75_L5", hybrid_pmin=0.75, limit=5)
    add("HYB_P78_L5", hybrid_pmin=0.78, limit=5)
    add("HYB_T2_60_L5", hybrid_t2_max=60.0, limit=5)
    add("HYB_T1_35_L5", hybrid_t1_max=35.0, limit=5)
    add("HYB_P75_T2_60_L7", hybrid_pmin=0.75, hybrid_t2_max=60.0, limit=7)

    # filtres pool + cap 7
    add("GAP30_L7", gapmax=30.0, limit=7)
    add("EV_MAX75_L7", evmax=75.0, limit=7)
    add("REL85_L7", relmin=85, limit=7)
    add("Q1_L7", pmin=0.65, evmax=75.0, gapmax=30.0, limit=7)

    # staking (sélection inchangée)
    add("PROD_L7_K50", limit=7, kelly_frac=0.50)
    add("PROD_L7_K75", limit=7, kelly_frac=0.75)

    return out


def build_full_grid() -> list[Scenario]:
    """Grille produit — limit × gap × ev_max (hybrid prod)."""
    out: list[Scenario] = []
    for lim, gap, evmax, pmin in product(
        (5, 6, 7),
        (None, 30.0),
        (75.0, 100.0),
        (0.60, 0.65),
    ):
        gap_s = "none" if gap is None else int(gap)
        name = f"G_l{lim}_g{gap_s}_e{int(evmax)}_p{int(pmin*100)}"
        out.append(
            Scenario(
                name=name,
                limit=lim,
                gapmax=gap,
                evmax=evmax,
                pmin=pmin,
            )
        )
    out.insert(0, Scenario(name="PROD_L5"))
    return out


def build_hybrid_grid() -> list[Scenario]:
    """Grille hybrid tiers — t1_max × t2_max × limit (pool + gap prod inchangés)."""
    out: list[Scenario] = [Scenario(name="PROD_L5")]
    for lim, t1_max, t2_max in product(
        (5, 6, 7),
        (28.0, 30.0, 32.0, 35.0, 38.0),
        (45.0, 50.0, 55.0, 60.0),
    ):
        if t2_max <= 30.0:
            continue
        name = f"H_l{lim}_t1{int(t1_max)}_t2{int(t2_max)}"
        out.append(
            Scenario(
                name=name,
                limit=lim,
                hybrid_t1_max=float(t1_max),
                hybrid_t2_max=float(t2_max),
            )
        )
    return out


def build_combo_grid() -> list[Scenario]:
    """Grille combo — limit × gap × hybrid tiers (autour des gagnants full + hybrid)."""
    out: list[Scenario] = [Scenario(name="PROD_L5")]
    for lim, gap, t1_max, t2_max in product(
        (5, 6, 7),
        (None, 30.0),
        (28.0, 30.0, 35.0),
        (50.0, 55.0, 60.0),
    ):
        gap_s = "none" if gap is None else int(gap)
        name = f"C_l{lim}_g{gap_s}_t1{int(t1_max)}_t2{int(t2_max)}"
        out.append(
            Scenario(
                name=name,
                limit=lim,
                gapmax=gap,
                hybrid_t1_max=float(t1_max),
                hybrid_t2_max=float(t2_max),
            )
        )
    return out


def load_rows(years: list[int]) -> dict[int, list[dict]]:
    data: dict[int, list[dict]] = {}
    for y in years:
        csv_path = UNIVERSE.get(y)
        rows = _csv_rows_for_year(y, csv_path=csv_path) if csv_path else _csv_rows_for_year(y)
        if y == 2026:
            rows = rows + _live_rows()
        data[y] = rows
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Grille scénarios Top5 hybrid prod")
    ap.add_argument("--years", default="2025,2026", help="Années séparées par virgule")
    ap.add_argument("--grid", choices=("coarse", "full", "hybrid", "combo"), default="coarse")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-n", type=int, default=0, help="Filtre n min par année")
    ap.add_argument("--export", default="", help="CSV export path")
    args = ap.parse_args()

    years = [int(x.strip()) for x in str(args.years).split(",") if x.strip()]
    scenarios = {
        "coarse": build_coarse_grid,
        "full": build_full_grid,
        "hybrid": build_hybrid_grid,
        "combo": build_combo_grid,
    }[args.grid]()
    rows_by_year = load_rows(years)

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    results: list[dict[str, Any]] = []
    for sc in scenarios:
        per_year: dict[int, dict] = {}
        ok = True
        for y in years:
            ev = eval_year(sc, y, rows_by_year[y], ml)
            per_year[y] = ev
            if ev["n"] < int(args.min_n):
                ok = False
        if not ok:
            continue
        row: dict[str, Any] = {
            "name": sc.name,
            "limit": sc.limit,
            "smart_cap": sc.smart_cap,
            "pmin": sc.pmin,
            "evmin": sc.evmin,
            "evmax": sc.evmax,
            "relmin": sc.relmin,
            "gapmax": sc.gapmax if sc.gapmax is not None else "",
            "hybrid_pmin": sc.hybrid_pmin,
            "hybrid_t1_max": sc.hybrid_t1_max,
            "hybrid_t2_max": sc.hybrid_t2_max,
            "kelly_frac": sc.kelly_frac,
        }
        kelly_sum = 0.0
        flat_sum = 0.0
        n_sum = 0
        for y in years:
            ev = per_year[y]
            row[f"n_{y}"] = ev["n"]
            row[f"hit_{y}"] = ev["hit"]
            row[f"flat_{y}"] = ev["flat"]
            row[f"kelly_{y}"] = ev["kelly"]
            row[f"dd_{y}"] = ev["dd"]
            kelly_sum += ev["kelly"]
            flat_sum += ev["flat"]
            n_sum += ev["n"]
        row["kelly_sum"] = kelly_sum
        row["flat_sum"] = flat_sum
        row["n_sum"] = n_sum
        # stabilité : les deux années Kelly > baseline serait idéal
        results.append(row)

    results.sort(key=lambda r: (-r["kelly_sum"], -r["flat_sum"]))

    print(f"=== Top5 scenario grid ({args.grid}) — years {years} ===")
    print(f"Scénarios: {len(scenarios)} | résultats: {len(results)}")
    prod = next((r for r in results if r["name"] == "PROD_L5"), None)
    if prod:
        print(
            f"PROD_L5 ref: kelly_sum={prod['kelly_sum']:+.0f}€ "
            f"n_sum={prod['n_sum']} flat_sum={prod['flat_sum']:+.0f}€"
        )
    print()
    hdr = f"{'#':>2} {'Scenario':<22} {'nΣ':>4} {'kellyΣ':>10} {'flatΣ':>8}"
    for y in years:
        hdr += f" {'k'+str(y):>9} {'n'+str(y):>4}"
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(results[: int(args.top)], 1):
        line = f"{i:>2} {r['name']:<22} {r['n_sum']:>4} {r['kelly_sum']:>+10.0f} {r['flat_sum']:>+8.0f}"
        for y in years:
            line += f" {r[f'kelly_{y}']:>+9.0f} {r[f'n_{y}']:>4}"
        print(line)

    if args.export:
        import pandas as pd

        os.makedirs(os.path.dirname(args.export) or ".", exist_ok=True)
        pd.DataFrame(results).to_csv(args.export, index=False)
        print(f"\nExport: {args.export}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
