#!/usr/bin/env python3
from __future__ import annotations
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from scripts.backtest_hybrid_p75_p80 import _run_hybrid
from scripts.backtest_hyb_yearly_kelly import _run_prod_hybrid
from scripts.backtest_pack12_global_2026 import BR_START, _perf_flat
from scripts.backtest_prod_top5_2026 import _kelly_sim, _tag
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _settlement_map
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel

start = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
ml = TennisMLModel()
ml._load_bundle_if_needed()
smap = _settlement_map(os.path.join(ROOT, "data", "bettinghud.db"))
pools = {2026: _daily_pools_unlimited(2026, ml)}
day_pools = {y: {d: (p, duplicate_model_prob_keys(p)) for d, p in by.items() if d >= start} for y, by in pools.items()}
conn = open_db(os.path.join(ROOT, "data", "bettinghud.db"))
hyb = [p for p in _run_hybrid(day_pools, base_cfg=P75_TIER, min_proba=0.80, min_rel=80, min_ev_pct=None, max_ev_pct=None, conn=conn, smap=smap) if str(p.get("calendar_date", ""))[:10] >= start]
prod = [p for p in _run_prod_hybrid(day_pools, conn=conn, smap=smap) if str(p.get("calendar_date", ""))[:10] >= start]
conn.close()
for name, picks in [("HYB P75+P80-all", hyb), ("HYBRID prod (P77)", prod)]:
    n, ns, w, flat = _perf_flat(picks)
    k = _kelly_sim(_tag(picks, name), ml, br_start=BR_START)
    print(f"{name}: {n} picks | hit {100*w/ns:.1f}% | flat {flat:+.0f}€ | Kelly {float(k.get('net_profit_eur') or 0):+.1f}€ | BR {float(k.get('bankroll_final') or 100):.1f}€")
