#!/usr/bin/env python3
"""Réconciliation ledger portfolio vs Kelly replay (détection dérive)."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Compare portfolio_daily_bets vs kelly_replay_metrics"
    )
    p.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    p.add_argument("--mode", choices=("top5", "1d1p", "both"), default="both")
    p.add_argument("--tol-eur", type=float, default=0.02, help="Tolérance P/L (€)")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Sync settlement + recompute ledger avant compare",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Sortie JSON (sinon résumé lisible)",
    )
    p.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit code 1 si écart détecté (CI / cron)",
    )
    args = p.parse_args()

    from scripts.bets_db import open_db, sync_daily_top_proba_from_results
    from scripts.portfolio_tracking_store import (
        MODE_1D1P,
        MODE_TOP5,
        reconcile_portfolio_ledger,
        refresh_portfolio_tracking,
    )

    modes = [MODE_TOP5, MODE_1D1P] if args.mode == "both" else [args.mode]
    conn = open_db(args.db)
    reports: list[dict] = []
    try:
        sync_daily_top_proba_from_results(conn)
        if args.refresh:
            refresh_portfolio_tracking(conn)

        for mode in modes:
            rep = reconcile_portfolio_ledger(
                conn, mode, tol_eur=float(args.tol_eur)
            )
            reports.append(rep)

        if args.json:
            print(json.dumps({"reports": reports}, ensure_ascii=False, indent=2))
        else:
            all_ok = True
            for rep in reports:
                mode = rep.get("mode")
                if rep.get("skipped"):
                    print(f"[{mode}] skip — {rep.get('reason')}")
                    continue
                ok = bool(rep.get("ok"))
                all_ok = all_ok and ok
                status = "OK" if ok else "DRIFT"
                print(f"[{mode}] {status} — {rep.get('n_picks')} picks, {rep.get('n_mismatches')} ecarts")
                if not ok:
                    for m in rep.get("mismatches") or []:
                        print(
                            f"  pick {m.get('key')}: {m.get('field')} "
                            f"ledger={m.get('ledger')} sim={m.get('sim')} "
                            f"delta={m.get('delta')}"
                        )
                    for field, d in (rep.get("summary_diff") or {}).items():
                        print(
                            f"  summary {field}: ledger={d.get('ledger')} "
                            f"sim={d.get('sim')} delta={d.get('delta')}"
                        )
                else:
                    led = rep.get("summary_ledger") or {}
                    print(
                        f"  BR {led.get('bankroll_start_eur')} → "
                        f"{led.get('bankroll_final_eur')} € "
                        f"(P/L {led.get('net_profit_eur'):+.2f} €)"
                    )

            if args.fail_on_drift and not all(r.get("ok") or r.get("skipped") for r in reports):
                return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
