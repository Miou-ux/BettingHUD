#!/usr/bin/env python3
"""Backtest comparatif des améliorations portfolio (avant implémentation).

Stratégies simulées sur daily_top_proba_picks (live replay).
Métriques : flat 5€, Kelly théorique (theoretical_profit), hit%, DD Kelly jour.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import open_db, read_daily_top_proba_picks, sync_daily_top_proba_from_results
from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match

FLAT = 5.0
EV_MIN, EV_MAX = 15.0, 100.0
PROBA_MIN = 60.0
BOOK_GAP_MAX_PP = 15.0
CONF_MIN_1D1P = 0.55
MAX_PER_SURFACE = 2
KELLY_DAILY_BUDGET_PCT = 30.0  # variante


@dataclass
class Perf:
    label: str = ""
    n_picks: int = 0
    n_settled: int = 0
    n_wins: int = 0
    flat_pnl: float = 0.0
    kelly_pnl: float = 0.0
    max_dd_kelly: float = 0.0
    excluded: int = 0
    days: int = 0

    @property
    def hit_pct(self) -> float:
        return (self.n_wins / self.n_settled * 100.0) if self.n_settled else 0.0

    @property
    def flat_roi(self) -> float:
        return (self.flat_pnl / (self.n_settled * FLAT) * 100.0) if self.n_settled else 0.0


def _norm(r: dict) -> dict:
    out = dict(r)
    p = r.get("p_model_fav")
    if p is None or float(p or 0) <= 0:
        out["p_model_fav"] = float(r.get("p_model_fav_pct") or 0) / 100.0
    else:
        out["p_model_fav"] = float(p)
    out["ev_fav_pct"] = float(r.get("ev_fav_pct") or 0)
    try:
        out["data_reliability_score"] = int(float(r["data_reliability_score"])) if r.get("data_reliability_score") is not None else None
    except (TypeError, ValueError):
        out["data_reliability_score"] = None
    out["book_gap_pp"] = float(r["book_gap_pp"]) if r.get("book_gap_pp") is not None else None
    try:
        out["confidence"] = float(r["confidence"]) if r.get("confidence") is not None else None
    except (TypeError, ValueError):
        out["confidence"] = None
    out["surface"] = str(r.get("surface") or "unknown").strip().lower() or "unknown"
    flags = str(r.get("data_reliability_flags") or "")
    out["_has_book_gap_flag"] = "book_gap_high" in flags
    out["settled"] = str(r.get("status") or "") in ("Gagné", "Perdu")
    out["won"] = str(r.get("status") or "") == "Gagné"
    out["odd"] = float(r.get("odd_fav") or 0)
    out["kelly_profit"] = float(r.get("theoretical_profit") or 0)
    return out


def _ev_ok(r: dict) -> bool:
    return EV_MIN <= float(r.get("ev_fav_pct") or 0) <= EV_MAX


def _proba_ok(r: dict) -> bool:
    return float(r.get("p_model_fav") or 0) * 100 > PROBA_MIN


def _rel_ok(r: dict, min_score: int, *, strict_null: bool = False) -> bool:
    s = r.get("data_reliability_score")
    if s is None:
        return not strict_null
    return int(s) >= min_score


def _book_gap_ok(r: dict, max_pp: float | None) -> bool:
    if max_pp is None:
        return True
    if r.get("_has_book_gap_flag"):
        return False
    gap = r.get("book_gap_pp")
    if gap is not None and float(gap) > max_pp:
        return False
    return True


def _conf_ok(r: dict, min_c: float | None) -> bool:
    if min_c is None:
        return True
    c = r.get("confidence")
    if c is None:
        return True
    return float(c) >= min_c


def _apply_surface_cap(picks: list[dict], max_per: int | None) -> list[dict]:
    if not max_per or max_per <= 0:
        return picks
    counts: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    for p in picks:
        s = p.get("surface") or "unknown"
        if counts[s] >= max_per:
            continue
        counts[s] += 1
        out.append(p)
    return out


def select_pool(
    rows: list[dict],
    *,
    rel_min: int = 80,
    strict_rel: bool = False,
    book_gap_max: float | None = None,
    proba_min_pct: float = PROBA_MIN,
    ev_min: float = EV_MIN,
    ev_max: float = EV_MAX,
    proba_floor: float | None = None,
    ev_floor: float | None = None,
) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if not _rel_ok(r, rel_min, strict_null=strict_rel):
            continue
        if not _book_gap_ok(r, book_gap_max):
            continue
        ev = float(r.get("ev_fav_pct") or 0)
        if ev < (ev_floor if ev_floor is not None else ev_min) or ev > ev_max:
            continue
        p_pct = float(r.get("p_model_fav") or 0) * 100
        if p_pct <= (proba_floor if proba_floor is not None else proba_min_pct):
            continue
        out.append(r)
    return out


def select_top5_day(pool: list[dict], *, surface_cap: int | None = None, limit: int = 5) -> list[dict]:
    ranked = sorted(pool, key=lambda r: (-float(r.get("p_model_fav") or 0), str(r.get("match_name") or "")))
    ranked = dedupe_top_proba_rows_by_match(ranked)
    return _apply_surface_cap(ranked[:limit], surface_cap)


def select_1d1p_day(group: list[dict], *, rel_min: int, conf_min: float | None, book_gap_max: float | None) -> dict | None:
    pool = select_pool(group, rel_min=rel_min, book_gap_max=book_gap_max)
    candidates: list[dict] = []
    for tour in ("ATP", "WTA"):
        tour_rows = sorted(
            [r for r in pool if str(r.get("tour") or "").upper() == tour],
            key=lambda x: int(x.get("rank") or 999),
        )
        for r in tour_rows:
            if _ev_ok(r):
                candidates.append(r)
                break
    if not candidates:
        return None
    pick = max(candidates, key=lambda r: (float(r.get("p_model_fav") or 0), str(r.get("tour") or "").upper() == "ATP"))
    if not _conf_ok(pick, conf_min):
        return None
    return pick


def select_today_value_day(pool: list[dict], *, limit: int = 10, surface_cap: int | None = None) -> list[dict]:
    ranked = sorted(pool, key=lambda r: (-float(r.get("ev_fav_pct") or 0), -float(r.get("p_model_fav") or 0)))
    ranked = dedupe_top_proba_rows_by_match(ranked)
    return _apply_surface_cap(ranked[:limit], surface_cap)


def _simulate_picks(picks: list[dict], *, kelly_budget_pct: float | None = None) -> Perf:
    settled = [p for p in picks if p.get("settled")]
    wins = [p for p in settled if p.get("won")]
    flat = 0.0
    kelly = 0.0
    by_day: dict[str, list[dict]] = defaultdict(list)
    for p in settled:
        by_day[str(p.get("calendar_date") or "")[:10]].append(p)

    for day, day_picks in sorted(by_day.items()):
        day_k = sum(float(p.get("kelly_profit") or 0) for p in day_picks)
        if kelly_budget_pct is not None and kelly_budget_pct < 100:
            raw_abs = sum(abs(float(p.get("theoretical_stake_frac") or 0)) for p in day_picks)
            if raw_abs > 0:
                scale = min(1.0, (kelly_budget_pct / 100.0) / raw_abs) if raw_abs > kelly_budget_pct / 100.0 else 1.0
                day_k = sum(float(p.get("kelly_profit") or 0) * scale for p in day_picks)
        kelly += day_k

    for p in settled:
        if p.get("won"):
            flat += FLAT * (float(p["odd"]) - 1.0)
        else:
            flat -= FLAT

    # DD sur cumul kelly par jour
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for day in sorted(by_day):
        day_k = sum(float(p.get("kelly_profit") or 0) for p in by_day[day])
        cum += day_k
        peak = max(peak, cum)
        if peak > 0:
            max_dd = max(max_dd, (peak - cum))

    return Perf(
        n_picks=len(picks),
        n_settled=len(settled),
        n_wins=len(wins),
        flat_pnl=flat,
        kelly_pnl=kelly,
        max_dd_kelly=max_dd,
        days=len(by_day),
    )


@dataclass
class Strategy:
    name: str
    product: str  # top5 | 1d1p | today
    rel_min: int = 80
    strict_rel: bool = False
    book_gap_max: float | None = None
    surface_cap: int | None = None
    conf_min_1d1p: float | None = None
    proba_floor: float | None = None
    ev_floor: float | None = None
    today_limit: int = 10
    top_n: int = 5
    kelly_budget_pct: float | None = None


STRATEGIES: list[Strategy] = [
    Strategy("BASE top5 rel≥80", "top5", rel_min=80),
    Strategy("BASE 1d1p rel≥80", "1d1p", rel_min=80),
    Strategy("BASE today rel≥80", "today", rel_min=80),
    Strategy("P0 top5 rel≥85 + cap2 surf", "top5", rel_min=85, surface_cap=2),
    Strategy("P0 1d1p rel≥90 + conf≥0.55", "1d1p", rel_min=90, conf_min_1d1p=CONF_MIN_1D1P),
    Strategy("P0 today rel≥80 + cap2 surf", "today", rel_min=80, surface_cap=2),
    Strategy("GAP top5 sans book_gap>15", "top5", rel_min=80, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("GAP 1d1p sans book_gap>15", "1d1p", rel_min=80, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("GAP today sans book_gap>15", "today", rel_min=80, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("COMBO top5 P0+GAP", "top5", rel_min=85, surface_cap=2, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("COMBO 1d1p P0+GAP", "1d1p", rel_min=90, conf_min_1d1p=CONF_MIN_1D1P, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("COMBO today P0+GAP", "today", rel_min=80, surface_cap=2, book_gap_max=BOOK_GAP_MAX_PP),
    Strategy("SECURITE top3 proba≥75 rel≥90", "top5", rel_min=90, proba_floor=75.0, surface_cap=2, top_n=3),
    Strategy("VALUE top5 ev≥25 rel≥85", "top5", rel_min=85, ev_floor=25.0),
    Strategy("Kelly budget 30% top5 base", "top5", rel_min=80, kelly_budget_pct=KELLY_DAILY_BUDGET_PCT),
    Strategy("Kelly budget 30% COMBO top5", "top5", rel_min=85, surface_cap=2, book_gap_max=BOOK_GAP_MAX_PP, kelly_budget_pct=KELLY_DAILY_BUDGET_PCT),
]


def run_strategy(rows: list[dict], strat: Strategy) -> Perf:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(r)

    picks: list[dict] = []
    for day in sorted(by_day):
        group = by_day[day]
        pool = select_pool(
            group,
            rel_min=strat.rel_min,
            strict_rel=strat.strict_rel,
            book_gap_max=strat.book_gap_max,
            proba_floor=strat.proba_floor,
            ev_floor=strat.ev_floor,
        )
        if strat.product == "top5":
            picks.extend(select_top5_day(pool, surface_cap=strat.surface_cap, limit=strat.top_n))
        elif strat.product == "1d1p":
            p = select_1d1p_day(group, rel_min=strat.rel_min, conf_min=strat.conf_min_1d1p, book_gap_max=strat.book_gap_max)
            if p and _rel_ok(p, strat.rel_min) and _book_gap_ok(p, strat.book_gap_max):
                if strat.conf_min_1d1p is None or _conf_ok(p, strat.conf_min_1d1p):
                    if strat.proba_floor is None or float(p.get("p_model_fav") or 0) * 100 > strat.proba_floor:
                        picks.append(p)
        elif strat.product == "today":
            picks.extend(select_today_value_day(pool, limit=strat.today_limit, surface_cap=strat.surface_cap))

    perf = _simulate_picks(picks, kelly_budget_pct=strat.kelly_budget_pct)
    perf.label = strat.name
    return perf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--end", default="")
    ap.add_argument("--strict-rel-only", action="store_true", help="Ne garder que les picks avec score fiabilité capturé")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = end - timedelta(days=max(1, int(args.days)) - 1)

    conn = open_db(args.db)
    try:
        sync_daily_top_proba_from_results(conn)
    finally:
        conn.close()

    raw = read_daily_top_proba_picks(db_path=args.db)
    rows = [_norm(r) for r in raw if start.isoformat() <= str(r.get("calendar_date") or "")[:10] <= end.isoformat()]
    if args.strict_rel_only:
        rows = [r for r in rows if r.get("data_reliability_score") is not None]
    settled_n = sum(1 for r in rows if r.get("settled"))
    rel_n = sum(1 for r in rows if r.get("data_reliability_score") is not None)
    gap_n = sum(1 for r in rows if r.get("book_gap_pp") is not None or r.get("_has_book_gap_flag"))

    print(f"Période {start} → {end} | lignes={len(rows)} réglées={settled_n} avec_rel={rel_n} avec_gap={gap_n}")
    print(f"Note: rel null = pass (historique sans score capturé)\n")
    print(f"{'Stratégie':<36} {'Prd':>5} {'Pick':>5} {'Hit%':>6} {'Flat€':>8} {'ROI%':>7} {'Kelly':>8} {'DDk':>6}")
    print("-" * 88)

    results: list[Perf] = []
    for strat in STRATEGIES:
        perf = run_strategy(rows, strat)
        results.append(perf)
        prd = strat.product
        print(
            f"{perf.label:<36} {prd:>5} {perf.n_settled:>5} {perf.hit_pct:>5.1f}% "
            f"{perf.flat_pnl:>+7.0f}€ {perf.flat_roi:>+6.1f}% {perf.kelly_pnl:>+7.2f} {perf.max_dd_kelly:>6.2f}"
        )

    print("\n--- Meilleur flat ROI (min 20 picks) ---")
    eligible = [p for p in results if p.n_settled >= 20]
    if eligible:
        best = max(eligible, key=lambda p: p.flat_roi)
        print(f"{best.label}: {best.flat_pnl:+.0f}€ ROI {best.flat_roi:+.1f}% hit {best.hit_pct:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
