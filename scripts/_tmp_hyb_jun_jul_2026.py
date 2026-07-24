#!/usr/bin/env python3
"""HYB illimité 2026 — alignement prod (YTD par défaut)."""
from __future__ import annotations

import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.backtest_prod_top5_2026 import _norm_pick_row
from scripts.backtest_scout_mega_grid import _daily_pools_unlimited
from scripts.bets_db import DB_PATH_DEFAULT, open_db
from scripts.experiment_july_expert_kelly import _attach_settlement, _settlement_map
from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all
from scripts.live_replay_engine import kelly_replay_metrics, load_ml
from scripts.match_rank_quality import duplicate_model_prob_keys

START = os.getenv("HYB_START", "2026-01-01")
END = os.getenv("HYB_END", "2026-07-24")
BR = 100.0


def main() -> None:
    ml = load_ml()
    db = DB_PATH_DEFAULT if os.path.isfile(DB_PATH_DEFAULT) else os.path.join(ROOT, "data", "bettinghud.db")
    conn = open_db(db)
    smap = _settlement_map(db)
    pools = _daily_pools_unlimited(2026, ml)

    picks: list[dict] = []
    by_day: dict[str, int] = defaultdict(int)
    for day in sorted(pools):
        if day < START or day > END:
            continue
        norm = [_norm_pick_row(dict(r)) for r in pools[day]]
        dup = duplicate_model_prob_keys(norm)
        for p in select_hyb_p75_p80_all(norm, duplicate_keys=dup, limit=None):
            row = dict(p)
            row["calendar_date"] = day
            row = _attach_settlement(row, smap, conn=conn)
            picks.append(row)
            by_day[day] += 1
    conn.close()

    _, _, summary = kelly_replay_metrics(picks, ml, bankroll_start=BR)
    settled = [p for p in picks if p.get("settled")]
    won = sum(1 for p in settled if p.get("won"))
    lost = len(settled) - won
    avg = len(picks) / len(by_day) if by_day else 0.0
    br_final = float(summary.get("bankroll_final_eur") or summary.get("bankroll_final") or BR)
    net = float(summary.get("net_profit_eur") or (br_final - BR))

    print(f"=== HYB illimite {START} -> {END} @ {BR:.0f} EUR (Kelly {summary.get('kelly_base_frac', 0.85)}) ===")
    print(f"Picks: {len(picks)} | jours actifs: {len(by_day)} | moy {avg:.1f}/jour")
    if settled:
        print(f"Settled: {len(settled)} | W {won} L {lost} | hit {100 * won / len(settled):.1f}%")
    print(f"BR: {BR:.0f} -> {br_final:.1f} EUR | P/L {net:+.1f} EUR | DD {summary.get('max_drawdown_pct', 0):.1f}%")

    by_m: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)

    br_seq = BR
    print("\nPar mois (Kelly sequentiel):")
    for m in sorted(by_m):
        mp = by_m[m]
        ms = [x for x in mp if x.get("settled")]
        w = sum(1 for x in ms if x.get("won"))
        _, _, sm = kelly_replay_metrics(mp, ml, bankroll_start=br_seq)
        br_end = float(sm.get("bankroll_final_eur") or br_seq)
        pnl = br_end - br_seq
        hit = 100 * w / len(ms) if ms else 0.0
        print(f"  {m}: {len(mp):3} pk | hit {hit:5.1f}% | {br_seq:.0f} -> {br_end:.0f} EUR ({pnl:+.0f})")
        br_seq = br_end


if __name__ == "__main__":
    main()
