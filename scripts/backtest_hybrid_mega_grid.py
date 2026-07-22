#!/usr/bin/env python3
"""Mega-grille hybrid (~10k configs) — 2025+2026 flat & Kelly vs prod."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from multiprocessing import Pool, cpu_count

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_pack12_global_2026 import (  # noqa: E402
    BR_START,
    _live_rows,
    _perf_flat,
    _picks_to_kelly_df,
    _tag,
)
from scripts.backtest_prod_top5_2026 import (  # noqa: E402
    _csv_rows_for_year,
    _norm_pick_row,
)
from scripts.backtest_staking_sim import simulate_sequential_intraday  # noqa: E402
from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match  # noqa: E402
from scripts.hybrid_pick_selection import ev_fav_pct  # noqa: E402
from scripts.match_rank_quality import (  # noqa: E402
    duplicate_model_prob_keys,
    passes_public_pick_gates,
)
from scripts.simulate_top10_proba_2026 import KELLY_BASE, MAX_STAKE_PCT  # noqa: E402

_KELLY_CTX: dict = {}


@dataclass(frozen=True)
class HybridCfg:
    p_min: float
    rel_min: int
    t1_ev_min: float
    t1_ev_max: float
    t2_ev_min: float
    t2_ev_max: float
    gap_max: float
    sort: str
    limit: int
    tg_ev_min: float
    use_tier2: bool


def prod_baseline_cfg() -> HybridCfg:
    return HybridCfg(
        p_min=0.77,
        rel_min=85,
        t1_ev_min=15.0,
        t1_ev_max=35.0,
        t2_ev_min=30.0,
        t2_ev_max=55.0,
        gap_max=30.0,
        sort="proba",
        limit=6,
        tg_ev_min=0.0,
        use_tier2=True,
    )


def _book_gap_ok(row: dict, gap_max: float) -> bool:
    gap = row.get("book_gap_pp")
    if gap is None:
        return True
    try:
        return float(gap) <= float(gap_max)
    except (TypeError, ValueError):
        return True


def _base_ok(row: dict, cfg: HybridCfg, *, duplicate_keys: set | None) -> bool:
    if float(row.get("p_model_fav") or 0) < cfg.p_min:
        return False
    if not passes_public_pick_gates(row, duplicate_keys=duplicate_keys, min_score=cfg.rel_min):
        return False
    if not _book_gap_ok(row, cfg.gap_max):
        return False
    return True


def _in_t1(row: dict, cfg: HybridCfg) -> bool:
    ev = ev_fav_pct(row)
    return cfg.t1_ev_min <= ev <= cfg.t1_ev_max


def _in_t2(row: dict, cfg: HybridCfg) -> bool:
    ev = ev_fav_pct(row)
    return cfg.t2_ev_min < ev <= cfg.t2_ev_max


def _sort_key(row: dict, sort: str) -> tuple:
    p = float(row.get("p_model_fav") or 0.0)
    ev = ev_fav_pct(row) / 100.0
    name = str(row.get("match_name") or "").lower()
    if sort == "proba":
        return (-p, -ev, name)
    if sort == "edge":
        return (-(ev * p), -p, name)
    return (-ev, -p, name)


def _tg_filter(rows: list[dict], cfg: HybridCfg) -> list[dict]:
    if cfg.tg_ev_min <= 0:
        return rows
    out: list[dict] = []
    for r in rows:
        if ev_fav_pct(r) < cfg.tg_ev_min:
            continue
        if float(r.get("p_model_fav") or 0) * 100.0 <= 60.0:
            continue
        out.append(r)
    return out


def select_hybrid_cfg(
    candidates: list[dict],
    cfg: HybridCfg,
    *,
    duplicate_keys: set | None = None,
) -> list[dict]:
    pool = [r for r in candidates if _base_ok(r, cfg, duplicate_keys=duplicate_keys)]
    tier1 = [r for r in pool if _in_t1(r, cfg)]
    tier2 = [r for r in pool if _in_t2(r, cfg)] if cfg.use_tier2 else []

    def rank(rows: list[dict]) -> list[dict]:
        ranked = sorted(rows, key=lambda r: _sort_key(r, cfg.sort))
        ranked = dedupe_top_proba_rows_by_match(ranked)
        return _tg_filter(ranked, cfg)

    picked: list[dict] = []
    seen: set[str] = set()
    cap = max(0, int(cfg.limit))
    for row in rank(tier1):
        key = str(row.get("match_name") or "").lower()
        if key in seen:
            continue
        picked.append(row)
        seen.add(key)
        if len(picked) >= cap:
            return picked
    if len(picked) < cap:
        for row in rank(tier2):
            key = str(row.get("match_name") or "").lower()
            if key in seen:
                continue
            picked.append(row)
            seen.add(key)
            if len(picked) >= cap:
                break
    return picked


def _prepare_days(rows: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    return dict(by_day)


def picks_for_cfg(days: dict[str, list[dict]], cfg: HybridCfg) -> list[dict]:
    out: list[dict] = []
    for day in sorted(days):
        cands = days[day]
        dup = duplicate_model_prob_keys(cands)
        out.extend(select_hybrid_cfg(cands, cfg, duplicate_keys=dup))
    return out


def _kelly_pnl(picks: list[dict], *, br_start: float = BR_START) -> float:
    ml = _KELLY_CTX["ml"]
    kf = _KELLY_CTX["kelly_frac"]
    df = _picks_to_kelly_df(_tag(picks, "g"), ml)
    if df.empty:
        return 0.0
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    res = simulate_sequential_intraday(
        df,
        bankroll_start=br_start,
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=float(kf),
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )
    return float(res.get("net_profit_eur") or 0.0)


def eval_cfg(cfg: HybridCfg, *, with_kelly: bool = True) -> dict:
    days25 = _KELLY_CTX["days25"]
    days26 = _KELLY_CTX["days26"]
    p25 = picks_for_cfg(days25, cfg)
    p26 = picks_for_cfg(days26, cfg)
    p_all = p25 + p26

    _, ns25, _, flat25 = _perf_flat(p25)
    _, ns26, _, flat26 = _perf_flat(p26)
    _, ns, w, flat_all = _perf_flat(p_all)

    k25 = k26 = k_all = 0.0
    if with_kelly:
        k25 = _kelly_pnl(p25)
        br = BR_START + k25
        k26 = _kelly_pnl(p26, br_start=br)
        k_all = k25 + k26

    hit = (w / ns * 100.0) if ns else 0.0
    return {
        **asdict(cfg),
        "n25": ns25,
        "n26": ns26,
        "n_all": ns,
        "hit_all": hit,
        "flat25": flat25,
        "flat26": flat26,
        "flat_all": flat_all,
        "kelly25": k25,
        "kelly26": k26,
        "kelly_all": k_all,
    }


def _worker_flat(cfg_dict: dict) -> dict:
    return eval_cfg(HybridCfg(**cfg_dict), with_kelly=False)


def _init_worker(days25: dict, days26: dict, kelly_frac: float) -> None:
    global _KELLY_CTX
    from scripts.ml_model import TennisMLModel

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    _KELLY_CTX = {
        "ml": ml,
        "days25": days25,
        "days26": days26,
        "kelly_frac": kelly_frac,
    }


def _random_cfg(rng: random.Random) -> HybridCfg:
    p_min = rng.choice([0.65, 0.68, 0.70, 0.72, 0.75, 0.77, 0.80, 0.82, 0.85])
    rel = rng.choice([70, 75, 80, 85, 90])
    t1_min = rng.choice([5, 8, 10, 12, 15, 18, 20, 22])
    t1_max = rng.choice([25, 30, 35, 40, 45, 50, 55])
    if t1_max <= t1_min:
        t1_max = t1_min + rng.choice([5, 10, 15])
    t2_min = rng.choice([25, 28, 30, 35, 40, 45])
    t2_max = rng.choice([45, 50, 55, 60, 70, 80, 100])
    if t2_max <= t2_min:
        t2_max = t2_min + rng.choice([5, 10, 15])
    return HybridCfg(
        p_min=p_min,
        rel_min=rel,
        t1_ev_min=float(t1_min),
        t1_ev_max=float(t1_max),
        t2_ev_min=float(t2_min),
        t2_ev_max=float(t2_max),
        gap_max=float(rng.choice([15, 20, 25, 30, 35, 40, 50, 999])),
        sort=rng.choice(["ev", "proba", "edge"]),
        limit=int(rng.choice([3, 4, 5, 6, 7, 8])),
        tg_ev_min=float(rng.choice([0, 5, 8, 10, 12, 15, 18])),
        use_tier2=rng.choice([True, False]),
    )


def _print_row(i: int, r: dict, base: dict) -> None:
    print(
        f"{i:>3}. flat={r['flat_all']:+.0f}€ ({r['flat_all']-base['flat_all']:+.0f}) | "
        f"Kelly={r['kelly_all']:+.0f}€ ({r['kelly_all']-base['kelly_all']:+.0f}) | "
        f"flat25={r['flat25']:+.0f} k25={r['kelly25']:+.0f} | flat26={r['flat26']:+.0f} k26={r['kelly26']:+.0f} | "
        f"n={r['n_all']} hit={r['hit_all']:.1f}% | "
        f"P≥{r['p_min']*100:.0f}% rel≥{r['rel_min']} T1={r['t1_ev_min']:.0f}-{r['t1_ev_max']:.0f} "
        f"T2={'on' if r['use_tier2'] else 'off'} {r['t2_ev_min']:.0f}-{r['t2_ev_max']:.0f} "
        f"gap≤{r['gap_max']:.0f} sort={r['sort']} lim={r['limit']} tgEV≥{r['tg_ev_min']:.0f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-configs", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=max(1, min(8, cpu_count() - 1)))
    ap.add_argument("--kelly-frac", type=float, default=0.65)
    ap.add_argument("--min-picks", type=int, default=400)
    args = ap.parse_args()

    from scripts.ml_model import TennisMLModel

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    rows25 = _csv_rows_for_year(2025)
    rows26 = _csv_rows_for_year(2026) + _live_rows()
    _KELLY_CTX["ml"] = ml
    _KELLY_CTX["kelly_frac"] = float(args.kelly_frac)
    _KELLY_CTX["days25"] = _prepare_days(rows25)
    _KELLY_CTX["days26"] = _prepare_days(rows26)

    base_cfg = prod_baseline_cfg()
    base = eval_cfg(base_cfg)
    print("=== BASELINE HYBRID PROD (2025+2026) ===")
    print(
        f"flat25={base['flat25']:+.0f}€ flat26={base['flat26']:+.0f}€ flat_all={base['flat_all']:+.0f}€ | "
        f"Kelly25={base['kelly25']:+.0f}€ Kelly26={base['kelly26']:+.0f}€ Kelly_all={base['kelly_all']:+.0f}€ | "
        f"n={base['n_all']} hit={base['hit_all']:.1f}%"
    )
    print()

    rng = random.Random(int(args.seed))
    cfg_dicts: list[dict] = []
    seen = {json.dumps(asdict(base_cfg), sort_keys=True)}
    while len(cfg_dicts) < int(args.n_configs):
        c = _random_cfg(rng)
        key = json.dumps(asdict(c), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        cfg_dicts.append(asdict(c))

    t0 = time.time()
    print(f"Phase 1 — flat only, {len(cfg_dicts)} configs, {args.workers} workers...")
    results: list[dict] = [base]
    chunksize = max(32, len(cfg_dicts) // (int(args.workers) * 4))
    with Pool(
        processes=int(args.workers),
        initializer=_init_worker,
        initargs=(_KELLY_CTX["days25"], _KELLY_CTX["days26"], float(args.kelly_frac)),
    ) as pool:
        for i, row in enumerate(pool.imap_unordered(_worker_flat, cfg_dicts, chunksize=chunksize), 1):
            results.append(row)
            if i % 2500 == 0:
                print(f"  ... {i}/{len(cfg_dicts)} ({time.time()-t0:.0f}s)")
    print(f"Phase 1 done in {time.time()-t0:.0f}s")

    eligible = [r for r in results if r["n_all"] >= int(args.min_picks)]
    kelly_candidates: dict[str, dict] = {}
    kelly_candidates[json.dumps(asdict(base_cfg), sort_keys=True)] = base
    for r in sorted(eligible, key=lambda x: -x["flat_all"])[:400]:
        kelly_candidates[json.dumps({k: r[k] for k in asdict(base_cfg)}, sort_keys=True)] = r
    for r in eligible:
        if r["flat_all"] > base["flat_all"]:
            kelly_candidates[json.dumps({k: r[k] for k in asdict(base_cfg)}, sort_keys=True)] = r

    print(f"Phase 2 — Kelly on {len(kelly_candidates)} shortlisted configs...")
    t1 = time.time()
    final: list[dict] = []
    for key, r in kelly_candidates.items():
        cfg = HybridCfg(**{k: r[k] for k in asdict(base_cfg)})
        final.append(eval_cfg(cfg, with_kelly=True))
    for r in eligible:
        key = json.dumps({k: r[k] for k in asdict(base_cfg)}, sort_keys=True)
        if key not in kelly_candidates:
            final.append(r)
    results = final
    base = next(r for r in results if r["p_min"] == base_cfg.p_min and r["t1_ev_min"] == base_cfg.t1_ev_min)
    eligible = [r for r in results if r["n_all"] >= int(args.min_picks)]
    print(f"Phase 2 done in {time.time()-t1:.0f}s | total {time.time()-t0:.0f}s\n")
    beat_both = [
        r for r in eligible if r["flat_all"] > base["flat_all"] and r["kelly_all"] > base["kelly_all"]
    ]

    print(f"Configs testées: {len(results)} | éligibles n≥{args.min_picks}: {len(eligible)}")
    print(
        f"Battent baseline flat: {sum(1 for r in eligible if r['flat_all'] > base['flat_all'])} | "
        f"Kelly: {sum(1 for r in eligible if r['kelly_all'] > base['kelly_all'])} | "
        f"les deux: {len(beat_both)}"
    )
    print()

    if beat_both:
        beat_both.sort(key=lambda r: (-(r["flat_all"] + r["kelly_all"] / 50.0), -r["hit_all"]))
        print("=== TOP 15 — battent hybrid prod en FLAT **et** KELLY (2025+2026) ===")
        for i, r in enumerate(beat_both[:15], 1):
            _print_row(i, r, base)
    else:
        print("=== Aucune config ne bat flat ET kelly simultanément ===")
        _print_row(1, max(eligible, key=lambda r: r["flat_all"]), base)
        print("Meilleur Kelly:")
        _print_row(1, max(eligible, key=lambda r: r["kelly_all"]), base)

    print("\n=== TOP 10 FLAT ===")
    for i, r in enumerate(sorted(eligible, key=lambda x: -x["flat_all"])[:10], 1):
        _print_row(i, r, base)

    print("\n=== TOP 10 KELLY ===")
    for i, r in enumerate(sorted(eligible, key=lambda x: -x["kelly_all"])[:10], 1):
        _print_row(i, r, base)

    out = os.path.join(ROOT, "data", "reports", "hybrid_mega_grid_2025_2026.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in sorted(eligible, key=lambda x: -(x["flat_all"] + x["kelly_all"] / 50)):
            f.write(json.dumps(r) + "\n")
    print(f"\nExport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
