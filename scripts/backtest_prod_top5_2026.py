#!/usr/bin/env python3
"""Backtest production Top 5 (pick_modes.TOP5 / collect_hybrid_proba_picks).

Mirrors live PROD hybrid selection (Telegram morning /top5), not Pack1/2 portfolio wrappers:
  - majors ATP/WTA 250+ only
  - model favorite, proba >= 77 %, EV tier1 15-35 % + tier2 30-55 %, max 6/day, rel >= 85
  - sort proba favori ↓, exclusion duplicate_model_prob
  - no book_gap cap, no surface cap, no confidence filter

2026: hybrid CSV no-leak (< LIVE_CUTOFF) + daily_top_proba_picks live replay (>= cutoff).
2025: CSV only (data/backtest_2025_bets.csv), cadre favori modèle via enrich_favorite_rows.
Kelly: 0,65 × Brier segment, 15 % liquidity cap, BR start 100 €.

Usage:
  python scripts/backtest_prod_top5_2026.py
  python scripts/backtest_prod_top5_2026.py --year 2025
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._report_april_backtest_no_leak import enrich_favorite_rows  # noqa: E402
from scripts.backtest_pack12_global_2026 import (  # noqa: E402
    BR_START,
    FLAT,
    LIVE_CUTOFF,
    MAX_STAKE_PCT,
    PACK1,
    _live_rows,
    _perf_flat,
    _picks_to_kelly_df,
    _tag,
)
from scripts.backtest_staking_sim import simulate_sequential_intraday  # noqa: E402
from scripts.simulate_top10_proba_2026 import KELLY_BASE  # noqa: E402
from scripts.backtest_csv_pick_rows import augment_csv_pick_fields, dataframe_to_pick_rows  # noqa: E402
from scripts.backtest_staking_sim import load_and_filter_bets_csv  # noqa: E402
from scripts.simulate_top10_proba_2026 import DEFAULT_EXTRA_EXCLUDE, DEFAULT_TOURNEY_LEVELS  # noqa: E402
from scripts.backtest_portfolio_improvements import (  # noqa: E402
    Strategy,
    _norm,
    select_pool,
    select_top5_day,
)
from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match  # noqa: E402
from scripts.match_rank_quality import passes_data_reliability_filter  # noqa: E402
from scripts.match_rank_quality import excluded_duplicate_model_prob_from_top5  # noqa: E402
from scripts.match_rank_quality import duplicate_model_prob_keys  # noqa: E402
from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.hybrid_pick_selection import HYBRID_DEFAULT_LIMIT  # noqa: E402
from scripts.pick_modes import DEFAULT_EV_MAX_PCT, DEFAULT_EV_MIN_PCT  # noqa: E402

PROD_EV_MIN_FRAC = DEFAULT_EV_MIN_PCT / 100.0
PROD_EV_MAX_FRAC = DEFAULT_EV_MAX_PCT / 100.0
PROD_MIN_PROBA_FRAC = 0.60
PROD_LIMIT: int | None = None

BASE_TOP5 = Strategy("BASE top5", "top5", rel_min=80, book_gap_max=None, surface_cap=None, top_n=5)
LIVE_REPLAY_CSV_2026 = os.path.join(ROOT, "data", "backtest_2026_live_replay.csv")
LIVE_REPLAY_CSV_2025 = os.path.join(ROOT, "data", "backtest_2025_live_replay.csv")


def _resolve_csv_path(year: int, csv_override: str | None = None) -> str:
    if csv_override:
        return csv_override
    if year == 2026 and os.path.isfile(LIVE_REPLAY_CSV_2026):
        return LIVE_REPLAY_CSV_2026
    if year == 2025 and os.path.isfile(LIVE_REPLAY_CSV_2025):
        return LIVE_REPLAY_CSV_2025
    return os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")


def _is_live_replay_dataframe(df: pd.DataFrame) -> bool:
    if "replay_source" not in df.columns:
        return False
    return (df["replay_source"].astype(str) == "live_replay_v1").any()


def _csv_rows_for_year(year: int, *, csv_path: str | None = None) -> list[dict]:
    """Pool CSV en cadre favori modèle (aligné live / collect_top5_proba_picks)."""
    path = _resolve_csv_path(year, csv_path)
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
    if year == 2026:
        df = df[df["date"].dt.strftime("%Y-%m-%d") < LIVE_CUTOFF].copy()
    if _is_live_replay_dataframe(df):
        # One row per match, favorite-frame pre-computed (live_replay_v1).
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
        return dataframe_to_pick_rows(df)
    # Legacy CSV: dedupe WINNER/LOSER sides then enrich favorite frame.
    df["_match_key"] = (
        df["date"].dt.strftime("%Y-%m-%d")
        + "|"
        + df["winner_name"].astype(str)
        + "|"
        + df["loser_name"].astype(str)
    )
    df = df.sort_values("ev", ascending=False).drop_duplicates("_match_key", keep="first").drop(columns=["_match_key"])
    fav = enrich_favorite_rows(df)
    fav = fav[
        (fav["p_model_fav"].astype(float) > PROD_MIN_PROBA_FRAC)
        & (fav["ev_fav_pct"].astype(float) >= DEFAULT_EV_MIN_PCT)
        & (fav["ev_fav_pct"].astype(float) <= DEFAULT_EV_MAX_PCT)
    ].copy()
    parts = fav["match_name"].str.split(" vs ", n=1, expand=True)
    fav["winner_name"] = parts[0]
    fav["loser_name"] = parts[1]
    fav["book_gap_pp"] = (
        fav["p_model_fav"].astype(float) - (1.0 / fav["odd_fav"].astype(float).clip(lower=1.01))
    ).abs() * 100.0
    fav["settled"] = True
    fav = augment_csv_pick_fields(fav)
    return dataframe_to_pick_rows(fav)


def _norm_pick_row(r: dict) -> dict:
    """Normalize pick fields; map CSV ``settled``/``won`` to French status when missing."""
    out = _norm(r)
    if "settled" in r:
        out["settled"] = bool(r.get("settled"))
    if "won" in r:
        out["won"] = bool(r.get("won"))
    if out.get("settled") and str(out.get("status") or "").strip() not in ("Gagné", "Perdu", "Annulé"):
        out["status"] = "Gagné" if out.get("won") else "Perdu"
    return out


def _candidate_passes_prod_pool(row: dict) -> bool:
    """Same gates as collect_top5_proba_picks (before sort/limit)."""
    if not passes_data_reliability_filter(row):
        return False
    p_fav = float(row.get("p_model_fav") or 0.0)
    if p_fav <= PROD_MIN_PROBA_FRAC:
        return False
    ev_pct = row.get("ev_fav_pct")
    if ev_pct is not None:
        ev_f = float(ev_pct) / 100.0
    else:
        ev_f = float(row.get("ev_fav") or 0.0)
    if ev_f < PROD_EV_MIN_FRAC or ev_f > PROD_EV_MAX_FRAC:
        return False
    if excluded_duplicate_model_prob_from_top5(row):
        return False
    return True


def select_prod_top5_day(candidates: list[dict], *, limit: int | None = PROD_LIMIT) -> list[dict]:
    """Top picks du jour — HYB P75+P80-all (union complète par défaut)."""
    from scripts.hybrid_pick_selection import select_hybrid_picks

    dup = duplicate_model_prob_keys(candidates)
    return select_hybrid_picks(candidates, limit=limit, duplicate_keys=dup)


def picks_for_rows(rows: list[dict], *, limit: int = PROD_LIMIT) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    out: list[dict] = []
    for day in sorted(by_day):
        out.extend(select_prod_top5_day(by_day[day], limit=limit))
    return out


def picks_pack_style(rows: list[dict], strat: Strategy, *, strict_rel: bool = False) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    out: list[dict] = []
    for day in sorted(by_day):
        pool = select_pool(
            by_day[day],
            rel_min=strat.rel_min,
            strict_rel=strict_rel or strat.strict_rel,
            book_gap_max=strat.book_gap_max,
            proba_floor=strat.proba_floor,
            ev_floor=strat.ev_floor,
        )
        out.extend(select_top5_day(pool, surface_cap=strat.surface_cap, limit=strat.top_n))
    return out


def _kelly_sim(
    picks: list[dict],
    ml: TennisMLModel,
    *,
    br_start: float = BR_START,
    kelly_frac: float = KELLY_BASE,
) -> dict:
    df = _picks_to_kelly_df(picks, ml)
    if df.empty:
        return {
            "n_bets": 0,
            "net_profit_eur": 0.0,
            "bankroll_final": br_start,
            "roi_on_staked_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_daily": 0.0,
            "profit_factor": 0.0,
        }
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    return simulate_sequential_intraday(
        df,
        bankroll_start=br_start,
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=float(kelly_frac),
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )


def report_block(label: str, picks: list[dict], ml: TennisMLModel, *, kelly_frac: float = KELLY_BASE) -> dict:
    n, ns, w, flat = _perf_flat(picks)
    hit = (w / ns * 100.0) if ns else 0.0
    roi = (flat / (ns * FLAT) * 100.0) if ns else 0.0
    k = _kelly_sim(_tag(picks, "top5"), ml, kelly_frac=kelly_frac)
    days = len({str(p.get("calendar_date") or "")[:10] for p in picks})
    return {
        "label": label,
        "n": ns,
        "days": days,
        "hit": hit,
        "flat": flat,
        "roi": roi,
        "k": k,
    }


def _print_block(r: dict) -> None:
    k = r["k"]
    print(f"--- {r['label']} ---")
    print(
        f"Picks: {r['n']} | Days: {r['days']} | Hit: {r['hit']:.1f}% | "
        f"Flat PnL: {r['flat']:+.0f} EUR | Flat ROI: {r['roi']:+.1f}%"
    )
    print(
        f"Kelly PnL: {float(k.get('net_profit_eur') or 0):+.1f} EUR | "
        f"BR final: {float(k.get('bankroll_final') or BR_START):.1f} EUR | "
        f"ROI vol: {float(k.get('roi_on_staked_pct') or 0):+.1f}% | "
        f"Max DD: {float(k.get('max_drawdown_pct') or 0):.1f}% | "
        f"Sharpe: {float(k.get('sharpe_daily') or 0):.2f} | "
        f"PF: {float(k.get('profit_factor') or 0):.2f}"
    )
    print()


def _pick_keys(picks: list[dict]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for p in picks:
        keys.add((str(p.get("calendar_date") or "")[:10], str(p.get("match_name") or "").lower()))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest production Top 5 (real prod logic)")
    ap.add_argument("--year", type=int, default=2026, choices=(2025, 2026))
    ap.add_argument("--limit", type=int, default=PROD_LIMIT, help="Max picks per day (default: unlimited HYB union)")
    ap.add_argument("--csv", default="", help="Override CSV path (default: live_replay for 2026 if present)")
    ap.add_argument(
        "--kelly-frac",
        type=float,
        default=None,
        help="Fraction Kelly base (0.65=prod). Sans arg: compare 0.65 et 1.0",
    )
    args = ap.parse_args()
    year = int(args.year)
    day_limit = max(1, int(args.limit))
    csv_override = str(args.csv).strip() or None
    kelly_fracs = [float(args.kelly_frac)] if args.kelly_frac is not None else [0.65, 1.0]

    csv_rows = _csv_rows_for_year(year, csv_path=csv_override)
    live_rows = _live_rows() if year == 2026 else []
    live_total = len(live_rows)
    live_with_rel = sum(1 for r in live_rows if r.get("data_reliability_score") is not None)
    csv_with_rel = sum(1 for r in csv_rows if r.get("data_reliability_score") is not None)

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    p_csv = picks_for_rows(csv_rows, limit=day_limit)
    p_live = picks_for_rows(live_rows, limit=day_limit)
    p_all = p_csv + p_live

    base_all = picks_pack_style(csv_rows, BASE_TOP5) + picks_pack_style(live_rows, BASE_TOP5)
    pack1_all = picks_pack_style(csv_rows, PACK1) + picks_pack_style(live_rows, PACK1)

    print(f"=== PROD Top {day_limit} — backtest {year} (real production logic) ===")
    print("Code path: pick_modes.TOP5 → collect_daily_ev_band_picks → select_hyb_p75_p80_all (HYB pur)")
    print(
        "Filters: majors 250+, proba>60%, EV +15%→+100%, reliability>=80 (null excluded), "
        f"sort proba↓, max {day_limit}/day"
    )
    print("NOT applied: book_gap cap, surface cap, confidence, strict_rel null-pass")
    print(f"Cap mise: {MAX_STAKE_PCT:.0f}% liquidite matin, BR depart {BR_START:.0f} EUR, facteur Brier segment")
    if year == 2026:
        csv_used = _resolve_csv_path(year, csv_override)
        print(
            f"Hybrid: CSV < {LIVE_CUTOFF} ({len(csv_rows)} pool rows from {os.path.basename(csv_used)}, "
            f"rel scored {csv_with_rel}) + "
            f"live >= {LIVE_CUTOFF} ({live_total} pool rows, rel scored {live_with_rel})"
        )
    else:
        print(f"Source: CSV only — {len(csv_rows)} pool rows, rel scored {csv_with_rel}")
    print()

    for kf in kelly_fracs:
        klabel = "Kelly 1/2" if abs(kf - 0.5) < 1e-9 else ("Kelly 1" if abs(kf - 1.0) < 1e-9 else f"Kelly {kf}")
        print(f"========== {klabel} (fraction base {kf}) ==========")
        blocks: list[tuple[str, list[dict]]] = [("GLOBAL", p_all), ("CSV", p_csv)]
        if year == 2026:
            blocks.append((f"LIVE >={LIVE_CUTOFF}", p_live))
        for block, picks in blocks:
            _print_block(report_block(block, picks, ml, kelly_frac=kf))
        print()

    if len(kelly_fracs) != 1:
        return 0

    kf = kelly_fracs[0]
    print("--- Monthly (global, sequential Kelly BR) ---")
    by_m: dict[str, list[dict]] = defaultdict(list)
    for p in p_all:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)
    br = BR_START
    cum_flat = 0.0
    hdr = (
        f"{'Month':<8} {'Picks':>5} {'Hit%':>6} {'FlatEUR':>8} {'CumFlat':>8} "
        f"{'KellyEUR':>9} {'BR':>8} {'DD%':>6}"
    )
    print(hdr)
    for m in sorted(by_m):
        mp = by_m[m]
        _, ns, w, flat = _perf_flat(mp)
        hit = (w / ns * 100.0) if ns else 0.0
        cum_flat += flat
        km = _kelly_sim(_tag(mp, "top5"), ml, br_start=br, kelly_frac=kf)
        br = float(km.get("bankroll_final") or br)
        print(
            f"{m:<8} {ns:>5} {hit:>5.1f}% {flat:>+8.0f} {cum_flat:>+8.0f} "
            f"{float(km.get('net_profit_eur') or 0):>+9.1f} {br:>8.1f} "
            f"{float(km.get('max_drawdown_pct') or 0):>5.1f}"
        )

    r = report_block("GLOBAL", p_all, ml, kelly_frac=kf)
    k = r["k"]
    print()
    print("=== vs BASE top5 / Pack1 (pick count delta) ===")
    print(f"PROD (this script) : {r['n']} settled picks over {r['days']} days")
    print(f"BASE top5          : {len(base_all)} picks (rel>=80, no gap/cap)")
    print(f"Pack1 Top5         : {len(pack1_all)} picks (rel>=85, gap<=15, cap2/surface)")
    print(f"PROD − BASE        : {r['n'] - len(base_all):+d} picks")
    print(f"PROD − Pack1       : {r['n'] - len(pack1_all):+d} picks")
    prod_keys = _pick_keys(p_all)
    base_only = len(_pick_keys(base_all) - prod_keys)
    pack1_only = len(_pick_keys(pack1_all) - prod_keys)
    prod_only_vs_base = len(prod_keys - _pick_keys(base_all))
    print(f"Days/matches only in BASE (not PROD): {base_only}")
    print(f"Days/matches only in Pack1 (not PROD): {pack1_only}")
    print(f"Days/matches only in PROD (not BASE): {prod_only_vs_base}")
    print()
    print("=== SUMMARY ===")
    print(f"Total picks: {r['n']} over {r['days']} days")
    print(f"Hit rate: {r['hit']:.1f}%")
    print(f"Flat 5 EUR: PnL {r['flat']:+.0f} EUR, ROI {r['roi']:+.1f}%")
    print(
        f"Kelly 1/2+Brier: PnL {float(k.get('net_profit_eur') or 0):+.1f} EUR, "
        f"BR {float(k.get('bankroll_final') or BR_START):.1f} EUR, "
        f"ROI vol {float(k.get('roi_on_staked_pct') or 0):+.1f}%, "
        f"Max DD {float(k.get('max_drawdown_pct') or 0):.1f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
