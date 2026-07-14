#!/usr/bin/env python3
"""Why Waltert (and others) appear or not in Telegram Top5 hybrid."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.daily_top_proba_store import (
    _match_favorite_metrics,
    collect_top5_proba_picks,
    load_today_matches_for_daily_top_proba,
)
from scripts.hybrid_pick_selection import (
    _in_tier1,
    _in_tier2,
    ev_fav_pct,
    hybrid_base_ok,
    hybrid_pool_ok,
    select_hybrid_picks,
)
from scripts.match_rank_quality import (
    duplicate_model_prob_keys,
    match_model_odds_inconsistent,
    passes_public_pick_gates,
)
from scripts.telegram_top5_notify import (
    _pick_ev_pct,
    _pick_proba_pct,
    _telegram_min_ev_pct,
    _telegram_min_proba_pct,
    filter_telegram_display_picks,
)


def main() -> None:
    matches, _ = load_today_matches_for_daily_top_proba()
    dup = duplicate_model_prob_keys(matches)
    print(f"matches_after_filter={len(matches)} dup_keys={len(dup)}")
    print(
        f"tg thresholds: proba>{_telegram_min_proba_pct()}% ev>={_telegram_min_ev_pct()}%"
    )

    w_matches = [m for m in matches if "waltert" in str(m).lower()]
    for m in w_matches:
        row = _match_favorite_metrics(m) or {}
        print("\n=== Waltert (raw match) ===")
        print("passes_public_pick_gates:", passes_public_pick_gates(m, duplicate_keys=dup))
        print("odds_inconsistent:", match_model_odds_inconsistent(m))
        print("rel:", m.get("data_reliability_score"), "flags:", m.get("data_reliability_flags"))
        print("metrics:", {k: row.get(k) for k in ("fav_player", "p_model_fav", "ev_fav_pct", "odd_fav")})
        print("hybrid_base_ok:", hybrid_base_ok(row, duplicate_keys=dup))
        print("hybrid_pool_ok:", hybrid_pool_ok(row, duplicate_keys=dup))
        print("tier1:", _in_tier1(row), "tier2:", _in_tier2(row), "ev:", ev_fav_pct(row))
        tg_row = dict(row)
        tg_row.update(
            {
                "p_model_pct": float(row.get("p_model_fav") or 0) * 100,
                "ev_pct": float(row.get("ev_fav_pct") or 0),
            }
        )
        print("proba_pct:", _pick_proba_pct(tg_row), "ev_pct:", _pick_ev_pct(tg_row))
        print("after filter_telegram_display_picks:", filter_telegram_display_picks([tg_row]))

    rows = collect_top5_proba_picks(matches, limit=None)
    hyb = select_hybrid_picks(rows, limit=5)
    print(f"\n=== hybrid selected ({len(hyb)}) ===")
    for h in hyb:
        print(f"  {h.get('fav_player')} p={float(h.get('p_model_fav') or 0)*100:.1f}% ev={ev_fav_pct(h):.1f}%")

    print("\n=== top proba candidates + hybrid_pool_ok ===")
    for r in rows[:12]:
        ok = hybrid_pool_ok(r, duplicate_keys=dup)
        tg = filter_telegram_display_picks([r])
        print(
            f"  {r.get('fav_player'):20} p={float(r.get('p_model_fav') or 0)*100:5.1f}% "
            f"ev={ev_fav_pct(r):6.1f}% pool={ok} tg={bool(tg)}"
        )


if __name__ == "__main__":
    main()
