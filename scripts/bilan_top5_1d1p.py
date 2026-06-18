#!/usr/bin/env python3
"""Bilan Top 5 + 1 Day 1 Pick depuis une date (BR théorique Kelly, 100 €)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PARIS = ZoneInfo("Europe/Paris")
BANKROLL_START = 100.0


def _status_flags(status: object) -> dict[str, bool]:
    st = str(status or "").strip().lower()
    void = "annul" in st
    return {
        "won": "gagn" in st,
        "lost": "perdu" in st,
        "open": "cours" in st or st in {"", "en cours"},
        "void": void,
        "settled": "gagn" in st or "perdu" in st or void,
    }


def _stake_frac(row: dict[str, Any]) -> float:
    from scripts.bets_db import _algo_kelly_stake_frac

    try:
        stake = float(row.get("theoretical_stake_frac") or 0.0)
        if stake > 0.0:
            return stake
    except (TypeError, ValueError):
        pass
    return float(
        _algo_kelly_stake_frac(
            row.get("p_model_fav"),
            row.get("odd_fav"),
            row.get("segment_brier"),
        )
    )


def _profit_frac(row: dict[str, Any]) -> float:
    flags = _status_flags(row.get("status"))
    if flags["void"] or not flags["settled"]:
        return 0.0
    try:
        stored = float(row.get("theoretical_profit") or 0.0)
        if stored != 0.0 or flags["won"] or flags["lost"]:
            return stored
    except (TypeError, ValueError):
        pass
    from scripts.bets_db import _algo_profit_for_status

    return float(
        _algo_profit_for_status(
            str(row.get("status") or ""),
            row.get("odd_fav"),
            _stake_frac(row),
        )
    )


def summarize_picks(
    picks: list[dict[str, Any]],
    *,
    bankroll_start: float = BANKROLL_START,
    label: str,
) -> dict[str, Any]:
    bankroll = float(bankroll_start)
    peak = bankroll
    max_dd = 0.0
    total_staked = 0.0
    n_won = n_lost = n_void = n_open = 0
    daily: list[dict[str, Any]] = []

    for row in picks:
        flags = _status_flags(row.get("status"))
        cal = str(row.get("calendar_date") or "")
        stake_frac = _stake_frac(row)
        profit_eur = 0.0
        icon = "⏳"
        if flags["void"]:
            n_void += 1
            icon = "⏸️"
        elif flags["won"]:
            n_won += 1
            icon = "✅"
            pf = _profit_frac(row)
            stake_eur = bankroll * stake_frac
            profit_eur = bankroll * pf
            total_staked += stake_eur
            bankroll += profit_eur
        elif flags["lost"]:
            n_lost += 1
            icon = "❌"
            pf = _profit_frac(row)
            stake_eur = bankroll * stake_frac
            profit_eur = bankroll * pf
            total_staked += stake_eur
            bankroll += profit_eur
        elif flags["open"]:
            n_open += 1

        peak = max(peak, bankroll)
        dd = ((peak - bankroll) / peak * 100.0) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

        daily.append(
            {
                "date": cal,
                "icon": icon,
                "match": row.get("match_name") or row.get("fav_player"),
                "fav": row.get("fav_player"),
                "odd": row.get("odd_fav"),
                "status": row.get("status"),
                "profit_eur": round(profit_eur, 2),
                "bankroll": round(bankroll, 2),
            }
        )

    n_decided = n_won + n_lost
    net = bankroll - bankroll_start
    return {
        "label": label,
        "n_picks": len(picks),
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "n_open": n_open,
        "hit_pct": round(n_won / n_decided * 100.0, 1) if n_decided else 0.0,
        "bankroll_final": round(bankroll, 2),
        "net_profit_eur": round(net, 2),
        "growth_pct": round(net / bankroll_start * 100.0, 1) if bankroll_start else 0.0,
        "roi_staked_pct": round(net / total_staked * 100.0, 1) if total_staked > 0 else 0.0,
        "max_drawdown_pct": round(max_dd, 1),
        "total_staked_eur": round(total_staked, 2),
        "daily": daily,
    }


def load_top5_picks(db_path: str, since: str) -> list[dict[str, Any]]:
    from scripts.bets_db import ensure_daily_top_proba_schema, open_db, sync_daily_top_proba_from_results
    from scripts.telegram_top5_notify import filter_telegram_display_picks

    conn = open_db(db_path)
    try:
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            WHERE calendar_date >= ?
              AND rank <= 5
            ORDER BY calendar_date ASC, rank ASC
            """,
            (since,),
        ).fetchall()
        picks = filter_telegram_display_picks([dict(r) for r in rows])
        return sorted(picks, key=lambda r: (str(r.get("calendar_date")), int(r.get("rank") or 99)))
    finally:
        conn.close()


def load_1d1p_picks(db_path: str, since: str) -> list[dict[str, Any]]:
    ca = os.path.join(os.path.dirname(ROOT), "CourtAlpha")
    if os.path.isdir("/opt/courtalpha"):
        ca = "/opt/courtalpha"
    if ca not in sys.path:
        sys.path.insert(0, ca)
    from api.services.one_day_one_pick import _load_ranked_rows, _select_one_pick_per_day

    rows = [r for r in _load_ranked_rows(db_path) if str(r.get("calendar_date") or "") >= since]
    return _select_one_pick_per_day(rows, exclude_date=None, ev_min_pct=15.0, ev_max_pct=100.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-05-20")
    ap.add_argument("--db", default="data/bettinghud.db")
    args = ap.parse_args()
    since = str(args.since)[:10]

    top5 = load_top5_picks(args.db, since)
    d1 = load_1d1p_picks(args.db, since)
    s5 = summarize_picks(top5, label="Top 5")
    s1 = summarize_picks(d1, label="1 Day 1 Pick")

    today = datetime.now(PARIS).date().isoformat()
    print(f"=== Bilan depuis {since} (au {today}) — BR théorique {BANKROLL_START:.0f} € ===\n")

    for s in (s1, s5):
        print(f"## {s['label']}")
        print(
            f"Picks: {s['n_picks']} | "
            f"✅ {s['n_won']} · ❌ {s['n_lost']} · ⏸️ {s['n_void']} · ⏳ {s['n_open']}"
        )
        print(
            f"Hit rate: {s['hit_pct']}% | "
            f"BR finale: {s['bankroll_final']} € ({s['growth_pct']:+.1f}%) | "
            f"ROI sur mises: {s['roi_staked_pct']:+.1f}%"
        )
        print(f"Max drawdown: {s['max_drawdown_pct']}% | Mises cumulées: {s['total_staked_eur']} €")
        print()

    print("## 1 Day 1 Pick — détail jour par jour")
    for d in s1["daily"]:
        odd = d.get("odd")
        odd_s = f" @{float(odd):.2f}" if odd else ""
        print(
            f"{d['date']} {d['icon']} {d['match']}{odd_s} → {d['status']} "
            f"({d['profit_eur']:+.2f} €, BR {d['bankroll']:.2f} €)"
        )

    print("\n## Top 5 — derniers jours (agrégé/jour)")
    by_day: dict[str, list[dict]] = {}
    for d in s5["daily"]:
        by_day.setdefault(d["date"], []).append(d)
    for day in sorted(by_day.keys())[-14:]:
        rows = by_day[day]
        wins = sum(1 for r in rows if r["icon"] == "✅")
        losses = sum(1 for r in rows if r["icon"] == "❌")
        profit = sum(float(r["profit_eur"]) for r in rows)
        print(f"{day} — {wins}G/{losses}P/{len(rows)} picks · {profit:+.2f} €")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
