#!/usr/bin/env python3
"""Compare HYB P75+P80 vs ancienne hybride P77 — 2026."""
from __future__ import annotations

import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.backtest_pack12_global_2026 import LIVE_CUTOFF
from scripts.backtest_prod_top5_2026 import _norm_pick_row
from scripts.backtest_scout_mega_grid import _daily_pools_unlimited
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _attach_settlement, _settlement_map
from scripts.hybrid_pick_selection import select_hybrid_picks_legacy
from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all
from scripts.live_replay_engine import kelly_replay_metrics, load_ml
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.telegram_top5_notify import filter_telegram_display_picks

START = os.getenv("HYB_START", "2026-01-01")
END = os.getenv("HYB_END", "2026-07-23")
BR = float(os.getenv("HYB_BR", "100"))

STRATS = [
    ("HYB P75+P80 illimite (prod actuel)", lambda pool, dup: select_hyb_p75_p80_all(pool, duplicate_keys=dup, limit=None)),
    ("HYB P75+P80 cap 6", lambda pool, dup: select_hyb_p75_p80_all(pool, duplicate_keys=dup, limit=6)),
    ("P77 legacy cap 6 (ancien prod)", lambda pool, dup: select_hybrid_picks_legacy(pool, limit=6, duplicate_keys=dup)),
    ("P77 legacy cap 5", lambda pool, dup: select_hybrid_picks_legacy(pool, limit=5, duplicate_keys=dup)),
    ("HYB illim + filtre TG (ancien TG)", lambda pool, dup: filter_telegram_display_picks(
        select_hyb_p75_p80_all(pool, duplicate_keys=dup, limit=None)
    )),
]


def main() -> None:
    ml = load_ml()
    db_path = os.path.join(ROOT, "data", "bettinghud.db")
    conn = open_db(db_path)
    smap = _settlement_map(db_path)
    pools = _daily_pools_unlimited(2026, ml)
    days_avail = sorted(pools.keys())

    print(f"=== Comparaison 2026 ({START} -> {END}) @ {BR:.0f} EUR Kelly 0.85 ===")
    if days_avail:
        print(f"Pool JSONL live: {days_avail[0]} .. {days_avail[-1]} | cutoff prod {LIVE_CUTOFF}\n")

    for name, fn in STRATS:
        picks: list[dict] = []
        by_day: dict[str, int] = defaultdict(int)
        for day in sorted(pools):
            if day < START or day > END:
                continue
            norm = [_norm_pick_row(dict(r)) for r in pools[day]]
            dup = duplicate_model_prob_keys(norm)
            for p in fn(norm, dup):
                row = dict(p)
                row["calendar_date"] = day
                row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
                by_day[day] += 1

        _, _, sm = kelly_replay_metrics(picks, ml, bankroll_start=BR)
        settled = [p for p in picks if p.get("settled")]
        w = sum(1 for p in settled if p.get("won"))
        brf = float(sm.get("bankroll_final_eur") or BR)
        hit = 100 * w / len(settled) if settled else 0.0
        avg = len(picks) / len(by_day) if by_day else 0.0
        dd = float(sm.get("max_drawdown_pct") or 0)
        print(name)
        print(
            f"  {len(picks)} pk | {len(by_day)} j | {avg:.1f}/j | "
            f"hit {hit:.1f}% ({w}/{len(settled)}) | "
            f"{BR:.0f} -> {brf:.0f} EUR ({brf - BR:+.0f}) | DD {dd:.1f}%"
        )
        print()
    conn.close()


if __name__ == "__main__":
    main()
