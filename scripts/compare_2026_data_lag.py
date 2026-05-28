"""Compare top-10 proba simulation 2026 : données à jour vs délai TML/Sackmann."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.simulate_top10_proba_2026 import run_year_projection
from scripts.ml_model import TennisMLModel


def main() -> None:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    scenarios = [
        ("Données à jour (baseline)", "data/backtest_2026_bets.csv"),
        ("Délai 21 jours", "data/backtest_2026_bets_lag21.csv"),
    ]
    print("Comparaison 2026 — top 10 proba/j, EV 15-100%, Kelly 1/2, BR 100 EUR\n")
    for label, rel in scenarios:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            print(f"[SKIP] {label}: {rel} introuvable (lancer backtest_2026.py --data-lag-days 21)")
            continue
        r = run_year_projection(
            2026,
            csv_path=path,
            ev_min_pct=15.0,
            ev_max_pct=100.0,
            ml=ml,
        )
        f, k = r["flat"], r["kelly"]
        print(f"### {label} ({rel})")
        print(f"  Paris top10: {r['n_bets']}  |  Hit: {f['hit_pct']:.1f}%  |  ROI 1u: {f['roi_pct']:+.2f}%")
        print(f"  Profit 1u: {f['net_units']:+.0f} EUR  |  Kelly profit: {k['net_profit_eur']:+.0f} EUR")
        print(f"  ROI Kelly vol: {k['roi_on_staked_pct']:+.2f}%  |  Sharpe j: {k['sharpe_daily']:.2f}")
        print()


if __name__ == "__main__":
    main()
