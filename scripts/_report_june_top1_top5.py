#!/usr/bin/env python3
"""Rapport mensuel Top 1 / Top 5 — perf + BR Kelly (usage interne)."""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.bilan_top5_1d1p import (  # noqa: E402
    BANKROLL_START,
    _profit_frac,
    _stake_frac,
    _status_flags,
    load_1d1p_picks,
    load_top5_picks,
    summarize_picks,
)


def _in_month(cal: str, year: int, month: int) -> bool:
    s = str(cal or "")[:10]
    return s.startswith(f"{year:04d}-{month:02d}-")


def _filter_month(picks: list[dict], year: int, month: int) -> list[dict]:
    return [p for p in picks if _in_month(p.get("calendar_date"), year, month)]


def load_top1_picks(db_path: str, since: str) -> list[dict[str, Any]]:
    """Top 1 = rank 1 brut (meilleure proba capturée) par jour et circuit."""
    from scripts.bets_db import ensure_daily_top_proba_schema, open_db, sync_daily_top_proba_from_results

    conn = open_db(db_path)
    try:
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            WHERE calendar_date >= ?
              AND rank = 1
            ORDER BY calendar_date ASC, tour ASC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def summarize_by_tour(picks: list[dict], *, label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tour in ("ATP", "WTA", "ALL"):
        sub = picks if tour == "ALL" else [p for p in picks if str(p.get("tour") or "").upper() == tour]
        if not sub and tour != "ALL":
            continue
        out[tour] = summarize_picks(sub, label=f"{label} ({tour})")
    return out


def daily_aggregate(s: dict) -> list[dict]:
    by_day: dict[str, dict] = {}
    for d in s.get("daily", []):
        day = d["date"]
        if day not in by_day:
            by_day[day] = {"wins": 0, "losses": 0, "void": 0, "open": 0, "profit": 0.0, "n": 0, "br_end": d["bankroll"]}
        b = by_day[day]
        b["n"] += 1
        b["profit"] += float(d["profit_eur"])
        b["br_end"] = d["bankroll"]
        if d["icon"] == "✅":
            b["wins"] += 1
        elif d["icon"] == "❌":
            b["losses"] += 1
        elif d["icon"] == "⏸️":
            b["void"] += 1
        else:
            b["open"] += 1
    return [{"date": k, **v} for k, v in sorted(by_day.items())]


def print_summary(s: dict) -> None:
    print(
        f"  Picks: {s['n_picks']} | "
        f"G {s['n_won']} · P {s['n_lost']} · void {s['n_void']} · open {s['n_open']}"
    )
    print(
        f"  Hit: {s['hit_pct']}% | BR {s['bankroll_final']:.2f} € "
        f"({s['growth_pct']:+.1f}%) | ROI mises {s['roi_staked_pct']:+.1f}%"
    )
    print(f"  Max DD: {s['max_drawdown_pct']}% | Mises cumulées: {s['total_staked_eur']:.2f} €")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=6)
    ap.add_argument("--db", default="data/bettinghud.db")
    ap.add_argument("--br-start", type=float, default=BANKROLL_START)
    args = ap.parse_args()

    since = f"{args.year:04d}-{args.month:02d}-01"
    y, m = args.year, args.month

    top5_all = _filter_month(load_top5_picks(args.db, since), y, m)
    top1_all = _filter_month(load_top1_picks(args.db, since), y, m)
    d1_all = _filter_month(load_1d1p_picks(args.db, since), y, m)

    s_top5 = summarize_picks(top5_all, bankroll_start=args.br_start, label="Top 5 Telegram")
    s_top1 = summarize_picks(top1_all, bankroll_start=args.br_start, label="Top 1 proba (rank 1)")
    s_d1 = summarize_picks(d1_all, bankroll_start=args.br_start, label="1 Day 1 Pick")

    month_name = ["", "jan", "fév", "mar", "avr", "mai", "juin", "juil", "aoû", "sep", "oct", "nov", "déc"][m]
    print(f"=== Rapport {month_name} {y} — BR Kelly départ {args.br_start:.0f} € ===")
    print(f"DB: {args.db}\n")

    for title, s in [("Top 1 (meilleure proba / jour / circuit)", s_top1), ("Top 5 Telegram (EV+proba)", s_top5), ("1 Day 1 Pick", s_d1)]:
        print(f"## {title}")
        print_summary(s)
        for tour, st in summarize_by_tour(
            top1_all if "Top 1" in title else (top5_all if "Top 5" in title else d1_all),
            label=title.split("(")[0].strip(),
        ).items():
            if tour == "ALL":
                continue
            print(f"  [{tour}] BR {st['bankroll_final']:.2f} € · hit {st['hit_pct']}% · {st['n_won']}G/{st['n_lost']}P")
        print()

    print("## Top 1 — détail")
    for d in s_top1["daily"]:
        print(f"  {d['date']} {d['icon']} {d['match']} @{float(d['odd'] or 0):.2f} → {d['status']} ({d['profit_eur']:+.2f} €, BR {d['bankroll']:.2f} €)")

    print("\n## Top 5 — par jour")
    for row in daily_aggregate(s_top5):
        print(
            f"  {row['date']} — {row['wins']}G/{row['losses']}P/{row['n']} picks · "
            f"{row['profit']:+.2f} € · BR {row['br_end']:.2f} €"
        )

    print("\n## 1 Day 1 Pick — détail")
    for d in s_d1["daily"]:
        odd = d.get("odd")
        odd_s = f" @{float(odd):.2f}" if odd else ""
        print(f"  {d['date']} {d['icon']} {d['match']}{odd_s} → {d['status']} ({d['profit_eur']:+.2f} €, BR {d['bankroll']:.2f} €)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
