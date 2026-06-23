#!/usr/bin/env python3
"""Backtest top 1 (1D1P) et top 5 (Telegram) avec / sans filtre fiabilité >= 80."""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match

FLAT_STAKE = 5.0
EV_MIN = 15.0
EV_MAX = 100.0
PROBA_MIN_PCT = 60.0
RELIABILITY_MIN = 80


@dataclass
class Perf:
    label: str
    n_picks: int
    n_settled: int
    n_wins: int
    n_losses: int
    hit_pct: float
    flat_pnl: float
    flat_roi_pct: float
    kelly_pnl: float
    excluded_by_filter: int = 0

    def line(self) -> str:
        return (
            f"{self.label}: picks={self.n_picks} settled={self.n_settled} "
            f"W-L={self.n_wins}-{self.n_losses} hit={self.hit_pct:.1f}% "
            f"flat={self.flat_pnl:+.1f}€ (ROI {self.flat_roi_pct:+.1f}%) "
            f"kelly_th={self.kelly_pnl:+.3f}u"
            + (f" | exclus_filtre={self.excluded_by_filter}" if self.excluded_by_filter else "")
        )


def _row_dict(r: dict) -> dict:
    out = dict(r)
    out["p_model_fav"] = float(r.get("p_model_fav_pct") or 0) / 100.0
    out["ev_fav_pct"] = float(r.get("ev_fav_pct") or 0)
    try:
        out["data_reliability_score"] = int(float(r.get("data_reliability_score") or 0))
    except (TypeError, ValueError):
        out["data_reliability_score"] = 0
    return out


def _ev_ok(r: dict) -> bool:
    ev = float(r.get("ev_fav_pct") or 0)
    return EV_MIN <= ev <= EV_MAX


def _reliability_ok(r: dict, min_score: int) -> bool:
    return int(r.get("data_reliability_score") or 0) >= min_score


def select_1d1p(rows: list[dict]) -> list[dict]:
    """1 pick/jour : 1er rank EV OK par circuit, puis meilleure proba (tie ATP)."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r["calendar_date"])[:10]].append(r)
    out: list[dict] = []
    for day in sorted(by_day):
        group = by_day[day]
        candidates: list[dict] = []
        for tour in ("ATP", "WTA"):
            tour_rows = sorted(
                [r for r in group if str(r.get("tour") or "").upper() == tour],
                key=lambda x: int(x.get("rank") or 999),
            )
            for r in tour_rows:
                if _ev_ok(r):
                    candidates.append(r)
                    break
        if not candidates:
            continue
        pick = max(
            candidates,
            key=lambda r: (
                float(r.get("p_model_fav") or 0),
                str(r.get("tour") or "").upper() == "ATP",
            ),
        )
        out.append(pick)
    return out


def select_top5_telegram(rows: list[dict]) -> list[dict]:
    """5 picks/jour : proba>60%, EV 15-100%, tri proba ↓ (Telegram /top5)."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r["calendar_date"])[:10]].append(r)
    out: list[dict] = []
    for day in sorted(by_day):
        pool = [
            r
            for r in by_day[day]
            if float(r.get("p_model_fav_pct") or 0) > PROBA_MIN_PCT and _ev_ok(r)
        ]
        pool = dedupe_top_proba_rows_by_match(pool)
        pool.sort(
            key=lambda r: (-float(r.get("p_model_fav") or 0), str(r.get("match_name") or "")),
        )
        out.extend(pool[:5])
    return out


def _compute_perf(picks: list[dict], label: str) -> Perf:
    settled = [p for p in picks if str(p.get("status") or "") in ("Gagné", "Perdu")]
    wins = [p for p in settled if p["status"] == "Gagné"]
    losses = [p for p in settled if p["status"] == "Perdu"]
    flat_pnl = 0.0
    kelly_pnl = 0.0
    for p in settled:
        odd = float(p.get("odd_fav") or 0)
        if p["status"] == "Gagné":
            flat_pnl += FLAT_STAKE * (odd - 1.0)
        else:
            flat_pnl -= FLAT_STAKE
        kelly_pnl += float(p.get("theoretical_profit") or 0.0)
    n = len(settled)
    return Perf(
        label=label,
        n_picks=len(picks),
        n_settled=n,
        n_wins=len(wins),
        n_losses=len(losses),
        hit_pct=(len(wins) / n * 100.0) if n else 0.0,
        flat_pnl=flat_pnl,
        flat_roi_pct=(flat_pnl / (n * FLAT_STAKE) * 100.0) if n else 0.0,
        kelly_pnl=kelly_pnl,
    )


def _apply_filter(picks: list[dict], min_score: int) -> tuple[list[dict], int]:
  kept = [p for p in picks if _reliability_ok(p, min_score)]
  return kept, len(picks) - len(kept)


def _lost_detail(picks: list[dict], label: str) -> None:
    for p in picks:
        if p.get("status") != "Perdu":
            continue
        print(
            f"  {p['calendar_date']} {label} | {p.get('fav_player')} | "
            f"p={p.get('p_model_fav_pct')}% score={p.get('data_reliability_score')} | "
            f"flags={p.get('data_reliability_flags') or '-'}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        default=os.path.join(ROOT, "data", "exports", "month_picks_reliability_20260623.csv"),
    )
    ap.add_argument("--reliability-min", type=int, default=RELIABILITY_MIN)
    args = ap.parse_args()

    with open(args.csv, encoding="utf-8") as fh:
        rows = [_row_dict(r) for r in csv.DictReader(fh)]

    top1 = select_1d1p(rows)
    top5 = select_top5_telegram(rows)

    top1_f, ex1 = _apply_filter(top1, args.reliability_min)
    top5_f, ex5 = _apply_filter(top5, args.reliability_min)

    print(f"CSV={args.csv} rows={len(rows)} flat_stake={FLAT_STAKE}€ filter>={args.reliability_min}\n")

    print("=== TOP 1 (1D1P — 1 pick/jour) ===")
    p1 = _compute_perf(top1, "sans filtre")
    p1f = _compute_perf(top1_f, "filtre >=80")
    p1f.excluded_by_filter = ex1
    print(p1.line())
    print(p1f.line())
    print(f"delta flat: {p1f.flat_pnl - p1.flat_pnl:+.1f}€ | delta hit: {p1f.hit_pct - p1.hit_pct:+.1f} pp\n")

    print("=== TOP 5 (Telegram — 5 picks/jour) ===")
    p5 = _compute_perf(top5, "sans filtre")
    p5f = _compute_perf(top5_f, "filtre >=80")
    p5f.excluded_by_filter = ex5
    print(p5.line())
    print(p5f.line())
    print(f"delta flat: {p5f.flat_pnl - p5.flat_pnl:+.1f}€ | delta hit: {p5f.hit_pct - p5.hit_pct:+.1f} pp\n")

    # Impact on losses only
    for name, all_p, filt_p in (
        ("TOP1", top1, top1_f),
        ("TOP5", top5, top5_f),
    ):
        lost_all = [p for p in all_p if p.get("status") == "Perdu"]
        lost_f = [p for p in filt_p if p.get("status") == "Perdu"]
        avoided = [p for p in lost_all if not _reliability_ok(p, args.reliability_min)]
        print(
            f"{name} perdus: {len(lost_all)} -> {len(lost_f)} "
            f"(évités par filtre: {len(avoided)}, faux positifs filtre gagnants: "
            f"{ex1 if name=='TOP1' else ex5} picks retirés dont gagnants)"
        )
        if avoided:
            print(f"  Perdus évités ({name}):")
            _lost_detail(avoided, name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
