#!/usr/bin/env python3
"""Compare Top 5 proba/jour — bandes EV 10-100 % vs 10-200 % (majeurs 2026)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_major_ev_2026 import (  # noqa: E402
    CSV_DEFAULT,
    YEAR_DEFAULT,
    load_major_ev_pool,
    run_scenario,
)
from scripts.ml_model import TennisMLModel  # noqa: E402

BANDS = (
    ("Top 5 · EV 10-100 %", 10.0, 100.0),
    ("Top 5 · EV 10-200 %", 10.0, 200.0),
)


def main() -> None:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    csv_path = CSV_DEFAULT
    year = YEAR_DEFAULT
    rows: list[dict] = []

    print("=" * 72)
    print(f"BACKTEST {year} — majeurs 250+ · Top 5 proba / jour")
    print(f"  Source : {csv_path}")
    print("=" * 72)

    for label, ev_min, ev_max in BANDS:
        _, df_pool = load_major_ev_pool(
            csv_path, year=year, ev_min_pct=ev_min, ev_max_pct=ev_max
        )
        res = run_scenario(
            df_pool,
            label=label,
            mode="top_proba",
            top_n=5,
            ml=ml,
            br_start=100.0,
        )
        rows.append(res)
        print(f"\n--- {label} ---")
        print(
            f"  Pool : {res['pool_bets']} paris / {res['pool_days']} jours "
            f"-> sélection : {res['bets']} paris / {res['days']} jours"
        )
        print(
            f"  1u   : hit {res['hit_pct']:.1f} % · ROI {res['roi_1u_pct']:+.2f} % · "
            f"profit {res['profit_1u']:+.2f} u · DD {res['dd_1u_pct']:.1f} % · Brier {res['brier']:.4f}"
        )
        print(
            f"  Kelly: ROI {res['roi_kelly_pct']:+.2f} % · BR x{res['br_kelly_mult']:.1f} · "
            f"DD {res['dd_kelly_pct']:.1f} % · Sharpe j. {res['sharpe_daily']:.2f}"
        )

    a, b = rows[0], rows[1]
    print("\n" + "=" * 72)
    print("DELTA (10-200 % vs 10-100 %)")
    print(
        f"  Pool   : {b['pool_bets'] - a['pool_bets']:+d} paris "
        f"({b['pool_bets']} vs {a['pool_bets']})"
    )
    print(
        f"  Top 5  : {b['bets'] - a['bets']:+d} paris "
        f"({b['bets']} vs {a['bets']})"
    )
    print(f"  Hit %  : {b['hit_pct'] - a['hit_pct']:+.2f} pts")
    print(f"  ROI 1u : {b['roi_1u_pct'] - a['roi_1u_pct']:+.2f} pts")
    print(f"  Profit : {b['profit_1u'] - a['profit_1u']:+.2f} u")

    out = os.path.join(ROOT, "data", "reports", "compare_top5_ev_10_100_vs_200_2026.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import pandas as pd

    pd.DataFrame(
        [
            {k: v for k, v in r.items() if k != "df_selected"}
            for r in rows
        ]
    ).to_csv(out, index=False)
    print(f"\nExport : {out}")


if __name__ == "__main__":
    main()
