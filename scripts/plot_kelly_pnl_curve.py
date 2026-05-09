"""
Courbe PnL (Kelly 1/4 + plafond % BR) à partir d'un CSV backtest.

Exemple::

    python scripts/plot_kelly_pnl_curve.py data/backtest_2025_bets_ev5.csv --br-start 200 --max-stake-pct 30
"""

from __future__ import annotations

import argparse
import os
import sys

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.project_kelly_quarter_cap5 import simulate


def main() -> None:
    ap = argparse.ArgumentParser(description="Courbe PnL Kelly quart + cap % BR.")
    ap.add_argument("csv", help="CSV backtest (p_model, odd, won, date)")
    ap.add_argument("--br-start", type=float, default=200.0)
    ap.add_argument("--kelly-frac", type=float, default=0.25)
    ap.add_argument("--max-stake-pct", type=float, default=30.0)
    ap.add_argument(
        "--sequential",
        action="store_true",
        help="Mise à jour BR après chaque pari (défaut: agrégation par jour comme project_kelly)",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="",
        help="PNG sortie (défaut: data/reports/kelly_pnl_<stem>.png)",
    )
    ap.add_argument("--csv-out", default="", help="Optionnel : exporter l'historique en CSV")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print(f"Fichier introuvable: {args.csv}")
        sys.exit(1)

    df = pd.read_csv(args.csv)
    for col in ("p_model", "odd", "won", "date"):
        if col not in df.columns:
            print(f"Colonne manquante: {col}")
            sys.exit(1)

    r = simulate(
        df,
        bankroll_start=args.br_start,
        kelly_fraction=args.kelly_frac,
        max_stake_pct=args.max_stake_pct,
        by_day=not args.sequential,
        return_history=True,
    )

    hist = r.get("history") or []
    if len(hist) < 2:
        print("Historique trop court (pas de données ?).")
        sys.exit(1)

    hdf = pd.DataFrame(hist)

    stem = os.path.splitext(os.path.basename(args.csv))[0]
    out_dir = os.path.join(ROOT, "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_png = (
        args.output.strip()
        or os.path.join(out_dir, f"kelly_pnl_{stem}_cap{args.max_stake_pct:g}_br{args.br_start:g}.png")
    )

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharey=True)
    mode = "séquentiel" if args.sequential else "par jour (cap journalier)"
    fig.suptitle(
        f"PnL Kelly {args.kelly_frac:g} — cap {args.max_stake_pct:g}% BR — BR départ {args.br_start:g} € — {mode}\n"
        f"{stem} · BR finale {r['bankroll_final']:.2f} € · {r['n_bets']} paris",
        fontsize=11,
    )

    y = hdf["pnl_eur"].astype(float)

    ax0 = axes[0]
    ax0.step(
        hdf["date"], y, where="post", color="#2563eb", linewidth=1.35, marker=".", markersize=3
    )
    ax0.axhline(0.0, color="#94a3b8", linestyle="--", linewidth=0.8)
    ax0.set_xlabel("Date")
    ax0.set_ylabel("PnL (€)")
    ax0.grid(True, alpha=0.3)
    ax0.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax0.xaxis.get_major_locator()))

    ax1 = axes[1]
    ax1.step(
        hdf["n_bets_cum"],
        y,
        where="post",
        color="#059669",
        linewidth=1.35,
        marker=".",
        markersize=3,
    )
    ax1.axhline(0.0, color="#94a3b8", linestyle="--", linewidth=0.8)
    ax1.set_xlabel(
        "Paris cumulés (post : fin de jour si agrégé par jour ; sinon après chaque pari)"
    )
    ax1.set_ylabel("PnL (€)")
    ax1.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)

    if args.csv_out.strip():
        hdf.to_csv(args.csv_out, index=False)

    print(f"Figure enregistrée: {out_png}")
    print(f"Points sur la courbe: {len(hdf)}")


if __name__ == "__main__":
    main()
